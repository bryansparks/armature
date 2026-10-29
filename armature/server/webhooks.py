"""Webhooks: signature checks, delivery records, and the rules that turn a delivery into jobs.

Each webhook has its own URL under ``/hooks/`` and a source that picks the
signature check. Rules are ``triggers`` rows of kind ``webhook``. Each one
names a workflow and decides from the event and payload whether to queue it.
A shadow rule records its decision and queues nothing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets as _random
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from sqlalchemy import Connection, Engine, delete, func, insert, select, update

from armature.server import audit, rbac
from armature.server import secrets as secret_store
from armature.server.auth import safe_equal
from armature.server.db import new_id
from armature.server.db.schema import (
    WEBHOOK_GENERIC_AUTH,
    WEBHOOK_SOURCES,
    jobs,
    projects,
    triggers,
    webhook_deliveries,
    webhooks,
    workflows,
)
from armature.server.defs import detach_triggers
from armature.server.dryrun import INPUT_NAME

KIND = "webhook"
PAYLOAD_MAX = 1024 * 1024
SLACK_SKEW = 300
DEFAULT_SIGNATURE_HEADER = "X-Signature-256"
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-hub-signature", "x-hub-signature-256",
                            "x-slack-signature"})
KEPT_HEADERS = ("content-type", "user-agent", "x-github-event", "x-github-delivery", "x-slack-retry-num",
                "x-slack-retry-reason")
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                        "trailers", "transfer-encoding", "upgrade", "host", "content-length", "x-real-ip",
                        "forwarded", "cookie"})
FORWARD_TIMEOUT = 10.0
KEEP_DELIVERIES = timedelta(days=14)
PRUNE_EVERY = timedelta(hours=1)

PATH = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
PATH_MAX = 191
OPS = ("eq", "ne", "in", "not_in", "starts_with", "contains", "exists", "missing")
VALUE_OPS = ("eq", "ne", "in", "not_in", "starts_with", "contains")
TEXT_OPS = ("starts_with", "contains")
RULE_KEYS = ("order", "stop", "shadow", "events", "event_from", "conditions", "project_from", "inputs", "secrets")
MAPPING_KEYS = ("conditions", "inputs")
CONDITION_KEYS = ("path", "op", "value", "ignore_case")
TEXT_VALUE_MAX = 200
CONDITIONS_MAX = 50
INPUTS_MAX = 100
EVENTS_MAX = 50
EVENT_MAX = 64
SECRETS_MAX = 32
MAPPING_TEXT_MAX = 64 * 1024
FORWARD_URL_MAX = 2000

log = logging.getLogger(__name__)
_MISSING = object()
_pruned_at: dict[str, datetime] = {}


class WebhookError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(source: str, cfg: Mapping[str, Any], secret: str | None, headers: Mapping[str, str], raw: bytes,
           now: datetime) -> str | None:
    """Why the delivery fails its signature check, or None when it passes. Runs before the body is parsed."""
    if not secret:
        return "secret not set"
    h = _lower(headers)
    if source == "github":
        ok = safe_equal(h.get("x-hub-signature-256"), "sha256=" + _hex(secret, raw))
    elif source == "slack":
        stamp = h.get("x-slack-request-timestamp", "")
        if not stamp.isdigit() or abs(now.timestamp() - int(stamp)) > SLACK_SKEW:
            return "stale or missing timestamp"
        ok = safe_equal(h.get("x-slack-signature"), "v0=" + _hex(secret, b"v0:" + stamp.encode() + b":" + raw))
    elif cfg.get("generic_auth") == "bearer":
        scheme, _, value = h.get("authorization", "").partition(" ")
        ok = scheme.lower() == "bearer" and safe_equal(value.strip(), secret)
    else:
        header = (cfg.get("generic_signature_header") or DEFAULT_SIGNATURE_HEADER).lower()
        ok = safe_equal(h.get(header, "").strip().removeprefix("sha256=").lower(), _hex(secret, raw))
    return None if ok else "bad signature"


def parse_payload(headers: Mapping[str, str], raw: bytes) -> Any:
    """The JSON body, or a form body's fields. A form's single ``payload`` field holds JSON, as Slack and GitHub send it."""
    if _lower(headers).get("content-type", "").split(";", 1)[0].strip() == "application/x-www-form-urlencoded":
        fields = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
        return json.loads(fields["payload"]) if set(fields) == {"payload"} else fields
    return json.loads(raw)


def event_of(source: str, headers: Mapping[str, str], payload: Any) -> str | None:
    if source == "github":
        event = _lower(headers).get("x-github-event")
    elif source == "slack" and isinstance(payload, dict):
        inner = payload.get("event")
        event = inner.get("type") if payload.get("type") == "event_callback" and isinstance(inner, dict) \
            else payload.get("type")
    else:
        event = None
    return event[:EVENT_MAX] if isinstance(event, str) and event else None


def delivery_key(source: str, cfg: Mapping[str, Any], headers: Mapping[str, str], payload: Any) -> str | None:
    h = _lower(headers)
    if source == "github":
        key = h.get("x-github-delivery")
    elif source == "slack":
        key = payload.get("event_id") if isinstance(payload, dict) else None
    else:
        name = cfg.get("generic_delivery_header")
        key = h.get(name.lower()) if name else None
    return key[:191] if isinstance(key, str) and key else None


def kept_headers(cfg: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, str]:
    """The headers a delivery row keeps: never a signature, token, or cookie."""
    h = _lower(headers)
    names = set(KEPT_HEADERS)
    if cfg.get("generic_delivery_header"):
        names.add(cfg["generic_delivery_header"].lower())
    return {name: h[name][:500] for name in sorted(names - SECRET_HEADERS) if name in h}


def lookup(payload: Any, path: str) -> Any:
    """The value at a dotted path, with digits indexing lists. ``_MISSING`` when any step is absent."""
    path = path.removeprefix("$").removeprefix(".")
    value = payload
    for part in path.split(".") if path else ():
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def _check_path(value: Any, what: str) -> str:
    if not isinstance(value, str) or len(value) > PATH_MAX or not PATH.fullmatch(value):
        raise WebhookError(400, f"{what} must be a dotted path such as pull_request.head.ref.")
    return value


def _check_condition(index: int, condition: Any) -> dict[str, Any]:
    where = f"Condition {index + 1}"
    if not isinstance(condition, dict):
        raise WebhookError(400, f"{where} must be an object with path, op, and value.")
    unknown = set(condition) - set(CONDITION_KEYS)
    if unknown:
        raise WebhookError(400, f"{where} has unknown keys: {', '.join(sorted(map(str, unknown)))}.")
    path = _check_path(condition.get("path"), f"{where} path")
    op = condition.get("op")
    if op not in OPS:
        raise WebhookError(400, f"{where} op must be one of {', '.join(OPS)}.")
    out = {"path": path, "op": op}
    if op in VALUE_OPS:
        if "value" not in condition:
            raise WebhookError(400, f"{where} needs a value.")
        value = condition["value"]
        if op in ("in", "not_in") and not isinstance(value, list):
            raise WebhookError(400, f"{where} value must be a list.")
        if op in TEXT_OPS and (not isinstance(value, str) or len(value) > TEXT_VALUE_MAX):
            raise WebhookError(400, f"{where} value must be text of at most {TEXT_VALUE_MAX} characters.")
        out["value"] = value
    if "ignore_case" in condition:
        if op not in TEXT_OPS or not isinstance(condition["ignore_case"], bool):
            raise WebhookError(400, f"{where} ignore_case is true or false, and only for starts_with or contains.")
        out["ignore_case"] = condition["ignore_case"]
    return out


def _check_input(name: Any, value: Any) -> Any:
    if not isinstance(name, str) or not INPUT_NAME.fullmatch(name):
        raise WebhookError(400, f"Input name {name!r} is not valid.")
    if isinstance(value, str) and value.startswith("$") and value != "$":
        if not value.startswith("$."):
            raise WebhookError(400, f"Input {name} must be $, $.path, or a literal.")
        _check_path(value[2:], f"Input {name}")
    return value


def check_rule(config: Any) -> dict[str, Any]:
    """A rule's config with defaults filled in. Refuses unknown keys, bad ops, bad patterns, and bad input names."""
    if not isinstance(config, dict):
        raise WebhookError(400, "config must be an object.")
    unknown = set(config) - set(RULE_KEYS)
    if unknown:
        raise WebhookError(400, f"Unknown rule keys: {', '.join(sorted(map(str, unknown)))}.")
    order = config.get("order", 0)
    if not isinstance(order, int) or isinstance(order, bool):
        raise WebhookError(400, "order must be an integer.")
    out: dict[str, Any] = {"order": order}
    for flag in ("stop", "shadow"):
        if not isinstance(config.get(flag, False), bool):
            raise WebhookError(400, f"{flag} must be true or false.")
        out[flag] = config.get(flag, False)
    events = config.get("events", [])
    if not isinstance(events, list) or len(events) > EVENTS_MAX or not all(
            isinstance(e, str) and 0 < len(e) <= EVENT_MAX for e in events):
        raise WebhookError(400, f"events must be a list of event names of at most {EVENT_MAX} characters.")
    out["events"] = events
    for key in ("event_from", "project_from"):
        if config.get(key) is not None:
            out[key] = _check_path(config[key], key)
    conditions = config.get("conditions", [])
    if not isinstance(conditions, list) or len(conditions) > CONDITIONS_MAX:
        raise WebhookError(400, f"conditions must be a list of at most {CONDITIONS_MAX}.")
    out["conditions"] = [_check_condition(i, c) for i, c in enumerate(conditions)]
    inputs = config.get("inputs", {})
    if not isinstance(inputs, dict) or len(inputs) > INPUTS_MAX:
        raise WebhookError(400, f"inputs must be an object of at most {INPUTS_MAX} entries.")
    out["inputs"] = {name: _check_input(name, value) for name, value in inputs.items()}
    names = config.get("secrets", [])
    if not isinstance(names, list) or len(names) > SECRETS_MAX or not all(
            isinstance(n, str) and secret_store.NAME.fullmatch(n) for n in names):
        raise WebhookError(400, "secrets must be a list of secret names.")
    out["secrets"] = names
    return out


def parse_mapping(text: Any) -> dict[str, Any]:
    """Conditions and inputs written as YAML or JSON, as the rule form's text box holds them."""
    if not isinstance(text, str) or len(text) > MAPPING_TEXT_MAX:
        raise WebhookError(400, "The conditions and inputs text is too long.")
    try:
        data = YAML(typ="safe", pure=True).load(text) if text.strip() else {}
    except YAMLError as exc:
        raise WebhookError(400, f"The conditions and inputs text is not valid YAML or JSON: {exc}") from exc
    data = data or {}
    if not isinstance(data, dict) or set(data) - set(MAPPING_KEYS):
        raise WebhookError(400, "The conditions and inputs text may hold only conditions and inputs.")
    return data


def _passes(condition: dict[str, Any], payload: Any) -> bool:
    value = lookup(payload, condition["path"])
    op = condition["op"]
    if op == "exists":
        return value is not _MISSING
    if op == "missing":
        return value is _MISSING
    if op == "eq":
        return value is not _MISSING and value == condition["value"]
    if op == "ne":
        return value is _MISSING or value != condition["value"]
    if op == "in":
        return value is not _MISSING and value in condition["value"]
    if op == "not_in":
        return value is _MISSING or value not in condition["value"]
    if not isinstance(value, str):
        return False
    wanted = condition["value"]
    if condition.get("ignore_case"):
        value, wanted = value.casefold(), wanted.casefold()
    return value.lstrip().startswith(wanted) if op == "starts_with" else wanted in value


def _describe(condition: dict[str, Any]) -> str:
    value = f" {json.dumps(condition['value'])}" if "value" in condition else ""
    case = " (ignoring case)" if condition.get("ignore_case") else ""
    return f"{condition['path']} {condition['op']}{value}{case}"


def map_inputs(inputs: dict[str, Any], payload: Any) -> dict[str, Any]:
    out = {}
    for name, source in inputs.items():
        if isinstance(source, str) and source.startswith("$"):
            value = lookup(payload, source)
            if value is not _MISSING:
                out[name] = value
        else:
            out[name] = source
    return out


def project_for_repo(conn: Connection, team_id: str, repo: str) -> str | None:
    return conn.execute(select(projects.c.id).where(projects.c.team_id == team_id,
                                                    func.lower(projects.c.repo) == repo.lower())
                        .order_by(projects.c.created_at, projects.c.id).limit(1)).scalar()


def ordered(rules: list[Any]) -> list[Any]:
    return sorted(rules, key=lambda r: ((r.config or {}).get("order", 0), r.created_at, r.id))


def _decide(conn: Connection, team_id: str, source: str, rule: Any, event: str | None,
            payload: Any) -> dict[str, Any]:
    config = rule.config or {}
    decision = {"trigger_id": rule.id, "workflow_id": rule.workflow_id, "matched": False, "reason": "",
                "inputs": {}, "project_id": None, "shadow": bool(config.get("shadow")),
                "stop": bool(config.get("stop"))}
    if not rule.enabled:
        decision["reason"] = "rule is disabled"
        return decision
    if source == "generic" and config.get("event_from"):
        found = lookup(payload, config["event_from"])
        event = found if isinstance(found, str) else None
    if config.get("events") and event not in config["events"]:
        decision["reason"] = f"event {event or '(none)'} is not one of {', '.join(config['events'])}"
        return decision
    for index, condition in enumerate(config.get("conditions", [])):
        if not _passes(condition, payload):
            decision["reason"] = f"condition {index + 1} failed: {_describe(condition)}"
            return decision
    workflow = conn.execute(select(workflows.c.team_id, workflows.c.project_id)
                            .where(workflows.c.id == rule.workflow_id)).first()
    if workflow is None or (workflow.team_id is not None and workflow.team_id != team_id):
        decision["reason"] = "the workflow no longer exists or is not visible to this team"
        return decision
    project_id = workflow.project_id
    if config.get("project_from"):
        repo = lookup(payload, config["project_from"])
        found = project_for_repo(conn, team_id, repo) if isinstance(repo, str) and repo else None
        shown = repo if isinstance(repo, str) else "(none)"
        if found is None:
            decision["reason"] = f"no project in this team has repository {shown}"
            return decision
        if project_id is not None and found != project_id:
            decision["reason"] = f"repository {shown} is not the project this workflow belongs to"
            return decision
        project_id = found
    decision["project_id"] = project_id
    decision.update(matched=True, reason="matched", inputs=map_inputs(config.get("inputs", {}), payload))
    return decision


def evaluate(conn: Connection, team_id: str, source: str, rules: list[Any], event: str | None,
             payload: Any) -> list[dict[str, Any]]:
    """One decision per rule, in rule order. Rules after a matching stop rule are skipped, unless it is in shadow."""
    decisions = []
    stopped = False
    for rule in ordered(rules):
        if stopped:
            decisions.append({"trigger_id": rule.id, "workflow_id": rule.workflow_id, "matched": False,
                              "reason": "skipped: an earlier rule stopped", "inputs": {}, "project_id": None,
                              "shadow": bool((rule.config or {}).get("shadow")),
                              "stop": bool((rule.config or {}).get("stop"))})
            continue
        decision = _decide(conn, team_id, source, rule, event, payload)
        decisions.append(decision)
        stopped = decision["matched"] and decision["stop"] and not decision["shadow"]
    return decisions


def rules_of(conn: Connection, webhook_id: str) -> list[Any]:
    return conn.execute(select(triggers).where(triggers.c.webhook_id == webhook_id, triggers.c.kind == KIND)).all()


def started_by(conn: Connection, workflow_id: str, access: rbac.Access) -> list[dict[str, Any]]:
    """The webhook rules that queue this workflow, from webhooks in teams the user can see."""
    rows = conn.execute(
        select(webhooks.c.id, webhooks.c.name, webhooks.c.team_id, webhooks.c.enabled.label("webhook_enabled"),
               triggers.c.enabled, triggers.c.config)
        .select_from(triggers.join(webhooks, webhooks.c.id == triggers.c.webhook_id))
        .where(triggers.c.workflow_id == workflow_id, triggers.c.kind == KIND)
        .order_by(webhooks.c.name, triggers.c.created_at, triggers.c.id)).all()
    return [{"webhook_id": r.id, "webhook": r.name, "events": list((r.config or {}).get("events") or []),
             "shadow": bool((r.config or {}).get("shadow")), "enabled": bool(r.enabled and r.webhook_enabled)}
            for r in rows if rbac.sees_team(access, r.team_id)]


def enqueue(conn: Connection, *, team_id: str, delivery_id: str, decisions: list[dict[str, Any]],
            now: datetime) -> list[dict[str, Any]]:
    """Queue a job for each matching rule that is not in shadow. Returns what the delivery row records."""
    matched = []
    for decision in decisions:
        if not decision["matched"]:
            continue
        job_id = None
        if not decision["shadow"]:
            job_id = new_id()
            version = conn.execute(select(workflows.c.current_version)
                                   .where(workflows.c.id == decision["workflow_id"])).scalar()
            conn.execute(insert(jobs).values(id=job_id, team_id=team_id, project_id=decision["project_id"],
                                             workflow_id=decision["workflow_id"], workflow_version=version,
                                             inputs=decision["inputs"], status="queued", attempts=0,
                                             trigger_id=decision["trigger_id"], delivery_id=delivery_id,
                                             created_at=now))
        matched.append({"trigger_id": decision["trigger_id"], "workflow_id": decision["workflow_id"],
                        "job_id": job_id, "shadow": decision["shadow"]})
    return matched


def record_delivery(conn: Connection, webhook_id: str, *, now: datetime, status: str, reason: str | None = None,
                    headers: dict[str, str] | None = None, raw: bytes | None = None, event: str | None = None,
                    key: str | None = None, matched: list[dict[str, Any]] | None = None) -> str:
    """Insert a delivery row. The raw body is kept only when it fits under ``PAYLOAD_MAX``."""
    delivery_id = new_id()
    payload = raw.decode("utf-8", "replace") if raw is not None and len(raw) <= PAYLOAD_MAX else None
    conn.execute(insert(webhook_deliveries).values(
        id=delivery_id, webhook_id=webhook_id, received_at=now, delivery_key=key, event=event, status=status,
        reason=reason, payload=payload, headers=headers or {}, matched=matched or []))
    return delivery_id


def seen(conn: Connection, webhook_id: str, key: str | None) -> bool:
    return key is not None and conn.execute(select(webhook_deliveries.c.id).where(
        webhook_deliveries.c.webhook_id == webhook_id, webhook_deliveries.c.delivery_key == key)).first() is not None


def prune(conn: Connection, now: datetime) -> int:
    return conn.execute(delete(webhook_deliveries)
                        .where(webhook_deliveries.c.received_at < now - KEEP_DELIVERIES)).rowcount


def prune_due(engine: Engine, now: datetime) -> bool:
    last = _pruned_at.get(str(engine.url))
    return last is None or now - last >= PRUNE_EVERY


def maybe_prune(engine: Engine, now: datetime) -> None:
    """Prune at most once an hour per process and database. Runs after a response, so a failure is only logged."""
    if not prune_due(engine, now):
        return
    try:
        with engine.begin() as conn:
            prune(conn, now)
    except Exception:
        log.exception("pruning webhook deliveries failed")
        return
    _pruned_at[str(engine.url)] = now


def forward_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers
            if k.lower() not in HOP_BY_HOP and not k.lower().startswith("x-forwarded-")]


def forward(engine: Engine, delivery_id: str, url: str, headers: list[tuple[str, str]], raw: bytes) -> None:
    """Re-post the original bytes and headers, then record how that went. Its failure never changes the response."""
    try:
        with httpx.Client(timeout=FORWARD_TIMEOUT, follow_redirects=False) as client:
            response = client.post(url, content=raw, headers=forward_headers(headers))
        status = f"ok {response.status_code}" if response.is_success else f"error: HTTP {response.status_code}"
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        status = f"error: {type(exc).__name__}"
    with engine.begin() as conn:
        conn.execute(update(webhook_deliveries).where(webhook_deliveries.c.id == delivery_id)
                     .values(forward_status=status[:32]))


def check_forward_url(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > FORWARD_URL_MAX:
        raise WebhookError(400, "forward_url must be an http or https URL.")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise WebhookError(400, "forward_url must be an http or https URL without credentials.")
    return value.strip()


def _check_header(value: Any, what: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not HEADER_NAME.fullmatch(value):
        raise WebhookError(400, f"{what} must be a header name.")
    return value


def check_settings(source: str, body: dict[str, Any], current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The generic-only columns and forward URL, cleared for a source that does not use them."""
    def pick(key: str) -> Any:
        return body[key] if key in body else (current or {}).get(key)

    out = {"forward_url": check_forward_url(pick("forward_url")), "generic_auth": None,
           "generic_signature_header": None, "generic_delivery_header": None}
    if source != "generic":
        return out
    auth = pick("generic_auth") or "hmac"
    if auth not in WEBHOOK_GENERIC_AUTH:
        raise WebhookError(400, f"generic_auth must be one of {', '.join(WEBHOOK_GENERIC_AUTH)}.")
    out["generic_auth"] = auth
    if auth == "hmac":
        out["generic_signature_header"] = _check_header(pick("generic_signature_header"), "generic_signature_header")
    delivery = _check_header(pick("generic_delivery_header"), "generic_delivery_header")
    if delivery is not None and delivery.lower() in SECRET_HEADERS | {(out["generic_signature_header"] or "").lower()}:
        raise WebhookError(400, "generic_delivery_header may not name a signature or credential header.")
    out["generic_delivery_header"] = delivery
    return out


def default_secret_name(name: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    return f"WEBHOOK_{slug}"[:128] if slug else "WEBHOOK_SECRET"


def _check_secret_name(value: Any) -> str:
    if not isinstance(value, str) or not secret_store.NAME.fullmatch(value):
        raise WebhookError(400, "secret_name must be an environment variable name of at most 128 characters.")
    return value


def _check_enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise WebhookError(400, "enabled must be true or false.")
    return value


def get(conn: Connection, webhook_id: str) -> Any:
    row = conn.execute(select(webhooks).where(webhooks.c.id == webhook_id)).first()
    if row is None:
        raise WebhookError(404, "No such webhook.")
    return row


def by_token(conn: Connection, token: str) -> Any | None:
    return conn.execute(select(webhooks).where(webhooks.c.path_token == token)).first() if token else None


def create(conn: Connection, *, team_id: str, name: str, body: dict[str, Any], actor_id: str, now: datetime,
           ip: str | None) -> Any:
    source = body.get("source")
    if source not in WEBHOOK_SOURCES:
        raise WebhookError(400, f"source must be one of {', '.join(WEBHOOK_SOURCES)}.")
    secret_name = _check_secret_name(body.get("secret_name") or default_secret_name(name))
    webhook_id = new_id()
    conn.execute(insert(webhooks).values(
        id=webhook_id, team_id=team_id, name=name, source=source, path_token=_random.token_urlsafe(24),
        secret_name=secret_name, enabled=_check_enabled(body.get("enabled", True)), created_at=now, updated_at=now,
        **check_settings(source, body)))
    audit.record(conn, action="webhook.create", actor_id=actor_id, team_id=team_id, target_kind="webhook",
                 target_id=webhook_id, detail={"name": name, "source": source}, ip=ip)
    return get(conn, webhook_id)


def changes(row: Any, body: dict[str, Any], key: str) -> bool:
    return key in body and (body[key] or None) != getattr(row, key)


def secrets_change(rule: Any | None, body: dict[str, Any]) -> bool:
    """Whether a rule create or update sets or changes the secrets its runs get, which needs a step-up."""
    if rule is None:
        return bool(rule_config(body)["secrets"])
    if "config" not in body and "mapping" not in body:
        return False
    return rule_config({"config": rule.config or {}, **body})["secrets"] != (rule.config or {}).get("secrets", [])


def update_webhook(conn: Connection, row: Any, *, body: dict[str, Any], name: str | None, actor_id: str,
                   now: datetime, ip: str | None) -> Any:
    values: dict[str, Any] = check_settings(row.source, body, row._mapping)
    if name is not None:
        values["name"] = name
    if "enabled" in body:
        values["enabled"] = _check_enabled(body["enabled"])
    if "secret_name" in body:
        values["secret_name"] = _check_secret_name(body["secret_name"])
    changed = sorted(k for k, v in values.items() if v != getattr(row, k))
    if changed:
        conn.execute(update(webhooks).where(webhooks.c.id == row.id).values(**values, updated_at=now))
        audit.record(conn, action="webhook.update", actor_id=actor_id, team_id=row.team_id, target_kind="webhook",
                     target_id=row.id, detail={"name": values.get("name", row.name), "changed": changed}, ip=ip)
    return get(conn, row.id)


def remove(conn: Connection, row: Any, *, actor_id: str, ip: str | None) -> None:
    """Delete the webhook with its rules and deliveries. Jobs it queued stay."""
    rule_ids = select(triggers.c.id).where(triggers.c.webhook_id == row.id)
    detach_triggers(conn, rule_ids)
    conn.execute(delete(triggers).where(triggers.c.webhook_id == row.id))
    conn.execute(delete(webhook_deliveries).where(webhook_deliveries.c.webhook_id == row.id))
    conn.execute(delete(webhooks).where(webhooks.c.id == row.id))
    audit.record(conn, action="webhook.delete", actor_id=actor_id, team_id=row.team_id, target_kind="webhook",
                 target_id=row.id, detail={"name": row.name}, ip=ip)


def set_secret(conn: Connection, keyring: Any, row: Any, *, value: Any, actor_id: str, now: datetime,
               ip: str | None) -> str | None:
    """Store a pasted value, or generate one. Returns the generated value, the only time it is shown."""
    generated = None
    if value is None or value == "":
        generated = value = _random.token_urlsafe(32)
    elif not isinstance(value, str) or len(value) > 512:
        raise WebhookError(400, "value must be a string of at most 512 characters.")
    secret_store.set_secret(conn, keyring, team_id=row.team_id, name=row.secret_name, value=value, actor_id=actor_id)
    conn.execute(update(webhooks).where(webhooks.c.id == row.id).values(updated_at=now))
    audit.record(conn, action="webhook.secret", actor_id=actor_id, team_id=row.team_id, target_kind="webhook",
                 target_id=row.id, detail={"name": row.name, "secret_name": row.secret_name,
                                           "generated": generated is not None}, ip=ip)
    return generated


def _check_workflow(conn: Connection, team_id: str, workflow_id: Any) -> Any:
    row = conn.execute(select(workflows).where(workflows.c.id == workflow_id)).first() \
        if isinstance(workflow_id, str) else None
    if row is None or (row.team_id is not None and row.team_id != team_id):
        raise WebhookError(400, "No such workflow for this team.")
    return row


def rule_config(body: dict[str, Any]) -> dict[str, Any]:
    config = body.get("config", {})
    if not isinstance(config, dict):
        raise WebhookError(400, "config must be an object.")
    if body.get("mapping") is not None:
        config = {**config, **parse_mapping(body["mapping"])}
    return check_rule(config)


def get_rule(conn: Connection, rule_id: str) -> Any:
    row = conn.execute(select(triggers).where(triggers.c.id == rule_id, triggers.c.kind == KIND,
                                              triggers.c.webhook_id.is_not(None))).first()
    if row is None:
        raise WebhookError(404, "No such rule.")
    return row


def create_rule(conn: Connection, hook: Any, *, body: dict[str, Any], actor_id: str, now: datetime,
                ip: str | None) -> Any:
    workflow = _check_workflow(conn, hook.team_id, body.get("workflow_id"))
    config = rule_config(body)
    rule_id = new_id()
    conn.execute(insert(triggers).values(id=rule_id, team_id=hook.team_id, workflow_id=workflow.id, kind=KIND,
                                         config=config, enabled=_check_enabled(body.get("enabled", True)),
                                         created_at=now, webhook_id=hook.id))
    audit.record(conn, action="webhook.rule_create", actor_id=actor_id, team_id=hook.team_id, target_kind="trigger",
                 target_id=rule_id, detail={"webhook_id": hook.id, "workflow": workflow.name}, ip=ip)
    return get_rule(conn, rule_id)


def update_rule(conn: Connection, rule: Any, *, body: dict[str, Any], actor_id: str, ip: str | None) -> Any:
    values: dict[str, Any] = {}
    if "workflow_id" in body:
        values["workflow_id"] = _check_workflow(conn, rule.team_id, body["workflow_id"]).id
    if "config" in body or "mapping" in body:
        values["config"] = rule_config({"config": rule.config or {}, **body})
    if "enabled" in body:
        values["enabled"] = _check_enabled(body["enabled"])
    if values:
        conn.execute(update(triggers).where(triggers.c.id == rule.id).values(**values))
        audit.record(conn, action="webhook.rule_update", actor_id=actor_id, team_id=rule.team_id,
                     target_kind="trigger", target_id=rule.id,
                     detail={"webhook_id": rule.webhook_id, "changed": sorted(values)}, ip=ip)
    return get_rule(conn, rule.id)


def remove_rule(conn: Connection, rule: Any, *, actor_id: str, ip: str | None) -> None:
    detach_triggers(conn, select(triggers.c.id).where(triggers.c.id == rule.id))
    conn.execute(delete(triggers).where(triggers.c.id == rule.id))
    audit.record(conn, action="webhook.rule_delete", actor_id=actor_id, team_id=rule.team_id, target_kind="trigger",
                 target_id=rule.id, detail={"webhook_id": rule.webhook_id}, ip=ip)

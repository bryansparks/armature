"""Webhooks: signature checks, delivery records, and the rules that turn a delivery into jobs.

Each webhook has its own URL under ``/hooks/`` and a source that picks the
signature check. Rules are ``triggers`` rows of kind ``webhook``. Each one
names a workflow and decides from the event and payload whether to queue it.
A shadow rule records its decision and queues nothing. A dry rule queues jobs
whose tool calls are recorded and never sent.
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
from jinja2 import ChainableUndefined, TemplateError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from sqlalchemy import Connection, Engine, delete, func, insert, or_, select, update

from armature.runtime.templating import text_env
from armature.server import (
    audit,
    calls,
    connections,
    conversations,
    gatecards,
    rbac,
    threads,
)
from armature.server import secrets as secret_store
from armature.server.auth import safe_equal
from armature.server.db import new_id
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    WEBHOOK_GENERIC_AUTH,
    WEBHOOK_SOURCES,
    projects,
    secrets,
    triggers,
    webhook_deliveries,
    webhooks,
    workflows,
)
from armature.server.defs import WORKFLOW, body_of, detach_triggers
from armature.server.dryrun import INPUT_NAME
from armature.server.tools import marks
from armature.server.worker import cancel_key, queue_job

KIND = "webhook"
CANCELLED = "cancelled by a rule that only cancels, such as a pull request closing"
PR_HEAD = "pull_request.head.sha"
PAYLOAD_MAX = 1024 * 1024
SLACK_SKEW = 300
LINEAR_SKEW = 60
DISMISSED = "dismissed"
CLICK_NOT_REPLAYED = "gate clicks aren't replayed"
DEFAULT_SIGNATURE_HEADER = "X-Signature-256"
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-hub-signature", "x-hub-signature-256",
                            "x-slack-signature", "linear-signature"})
KEPT_HEADERS = ("content-type", "user-agent", "x-github-event", "x-github-delivery", "x-slack-retry-num",
                "x-slack-retry-reason", "x-atlassian-webhook-identifier", "x-atlassian-webhook-retry",
                "linear-delivery", "linear-event")
JIRA_GATEWAY = "api.atlassian.com"
JIRA_CLOUD_PATH = re.compile(r"^/ex/jira/([A-Za-z0-9-]+)/")
JIRA_SELF_KEYS = ("issue", "user", "comment", "worklog", "project", "version", "sprint", "board")
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
RULE_KEYS = ("order", "stop", "shadow", "dry", "events", "event_from", "conditions", "project_from", "inputs",
             "secrets", "thread", "conversation", "import_hash", "key", "concurrency", "debounce_seconds",
             "cancel_running", "sticky", "cancel_only")
IMPORT_HASH = re.compile(r"^[0-9a-f]{64}$")
CONCURRENCY = ("none", "queue", "supersede")
KEY_PATHS_MAX = 3
KEY_MAX = 191
DEBOUNCE_MAX = 3600
# Ops waits for ten quiet minutes after the last push before it reviews a pull request.
PR_QUIET_S = 600
STICKY_MAX = 10
MAPPING_KEYS = ("conditions", "inputs", "thread")
THREAD_TEMPLATE_MAX = 500
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
    elif source == "jira":
        ok = safe_equal(h.get("x-hub-signature"), "sha256=" + _hex(secret, raw))
    elif source == "linear":
        if not safe_equal(h.get("linear-signature", "").strip().lower(), _hex(secret, raw)):
            return "bad signature"
        return None if _fresh_linear(raw, now) else "stale or missing timestamp"
    elif cfg.get("generic_auth") == "bearer":
        scheme, _, value = h.get("authorization", "").partition(" ")
        ok = scheme.lower() == "bearer" and safe_equal(value.strip(), secret)
    else:
        header = (cfg.get("generic_signature_header") or DEFAULT_SIGNATURE_HEADER).lower()
        ok = safe_equal(h.get(header, "").strip().removeprefix("sha256=").lower(), _hex(secret, raw))
    return None if ok else "bad signature"


def _fresh_linear(raw: bytes, now: datetime) -> bool:
    try:
        stamp = json.loads(raw).get("webhookTimestamp")
    except (ValueError, AttributeError):
        return False
    if not isinstance(stamp, int) or isinstance(stamp, bool):
        return False
    return abs(now.timestamp() * 1000 - stamp) <= LINEAR_SKEW * 1000


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
    elif source == "jira" and isinstance(payload, dict):
        event = payload.get("webhookEvent")
    elif source == "linear":
        event = payload.get("type") if isinstance(payload, dict) else None
        event = event or _lower(headers).get("linear-event")
    else:
        event = None
    return event[:EVENT_MAX] if isinstance(event, str) and event else None


def delivery_key(source: str, cfg: Mapping[str, Any], headers: Mapping[str, str], payload: Any) -> str | None:
    h = _lower(headers)
    if source == "github":
        key = h.get("x-github-delivery")
    elif source == "slack":
        key = (payload.get("event_id") if isinstance(payload, dict) else None) or gatecards.delivery_key(payload)
    elif source == "jira":
        key = h.get("x-atlassian-webhook-identifier")
    elif source == "linear":
        key = h.get("linear-delivery")
    else:
        name = cfg.get("generic_delivery_header")
        key = h.get(name.lower()) if name else None
    return key[:191] if isinstance(key, str) and key else None


def _jira_site(payload: dict[str, Any]) -> str | None:
    """The site from the first ``self`` link: its cloud id when an app's link goes through the API gateway."""
    found = [payload[k] for k in JIRA_SELF_KEYS if k in payload] + list(payload.values())
    for value in found:
        link = value.get("self") if isinstance(value, dict) else None
        parts = urlsplit(link) if isinstance(link, str) else None
        if not parts or parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        cloud = JIRA_CLOUD_PATH.match(parts.path) if parts.hostname == JIRA_GATEWAY else None
        return f"cloud:{cloud[1]}" if cloud else f"{parts.scheme}://{parts.netloc.lower()}"
    return None


def _id_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or value == "":
        return None
    return str(value)


def sender_of(source: str, payload: Any) -> str | None:
    """Who sent a delivery, as pinning compares it. None for generic webhooks, which are never pinned."""
    if not isinstance(payload, dict):
        return None
    sender = None
    if source == "github":
        # One GitHub App installation covers many repositories, so it pins the installation.
        for kind, key in (("installation", "installation"), ("repo", "repository")):
            found = payload.get(key)
            ident = _id_text(found.get("id")) if isinstance(found, dict) else None
            if ident:
                sender = f"github:{kind}:{ident}"
                break
    elif source == "jira":
        site = _jira_site(payload)
        sender = f"jira:{site}" if site else None
    elif source == "linear":
        org = _id_text(payload.get("organizationId"))
        sender = f"linear:org:{org}" if org else None
    elif source == "slack":
        team = payload.get("team")
        ident = _id_text(payload.get("team_id")) or (_id_text(team.get("id")) if isinstance(team, dict) else None)
        sender = f"slack:team:{ident}" if ident else None
    return sender[:255] if sender else None


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


def _check_thread(value: Any) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= THREAD_TEMPLATE_MAX:
        raise WebhookError(400, f"thread must be a template of at most {THREAD_TEMPLATE_MAX} characters.")
    try:
        text_env(ChainableUndefined).from_string(value)
    except TemplateError as exc:
        raise WebhookError(400, f"thread is not a valid template: {exc}") from exc
    return value


def render_thread(template: str | None, payload: Any, source: str | None = None) -> str | None:
    """The thread a rule's template names for a payload, or None when it isn't a valid thread id.

    A reserved prefix renders only for a delivery from the source that owns it.
    """
    if not template:
        return None
    try:
        text = text_env(ChainableUndefined).from_string(template).render(payload if isinstance(payload, dict) else {})
    except (TemplateError, TypeError, ValueError):
        return None
    text = text.strip()
    if not threads.THREAD.fullmatch(text):
        return None
    owner = next((owner for prefix, owner in threads.RESERVED.items() if text.startswith(prefix)), None)
    return None if owner is not None and owner != source else text


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
    for flag in ("stop", "shadow", "dry", "conversation"):
        if not isinstance(config.get(flag, False), bool):
            raise WebhookError(400, f"{flag} must be true or false.")
        out[flag] = config.get(flag, False)
    if not out["conversation"]:
        del out["conversation"]
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
    if config.get("thread") is not None:
        out["thread"] = _check_thread(config["thread"])
    if config.get("import_hash") is not None:
        if not isinstance(config["import_hash"], str) or not IMPORT_HASH.fullmatch(config["import_hash"]):
            raise WebhookError(400, "import_hash must be a sha256 hex digest.")
        out["import_hash"] = config["import_hash"]
    out.update(_check_concurrency(config, out))
    return out


def _check_concurrency(config: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """The key, debounce, and concurrency settings. A rule without them queues exactly as before."""
    found: dict[str, Any] = {}
    if config.get("key") is not None:
        paths = config["key"]
        if not isinstance(paths, list) or not 0 < len(paths) <= KEY_PATHS_MAX:
            raise WebhookError(400, f"key must be a list of 1 to {KEY_PATHS_MAX} payload paths.")
        found["key"] = [_check_path(path, "A key path") for path in paths]
    concurrency = config.get("concurrency", "none")
    if concurrency not in CONCURRENCY:
        raise WebhookError(400, f"concurrency must be one of {', '.join(CONCURRENCY)}.")
    if concurrency != "none" and "key" not in found:
        raise WebhookError(400, "concurrency needs a key.")
    if "concurrency" in config:
        found["concurrency"] = concurrency
    quiet = PR_QUIET_S if concurrency == "supersede" and "pull_request" in out["events"] else 0
    debounce = config.get("debounce_seconds", quiet)
    if isinstance(debounce, bool) or not isinstance(debounce, int) or not 0 <= debounce <= DEBOUNCE_MAX:
        raise WebhookError(400, f"debounce_seconds must be a whole number from 0 to {DEBOUNCE_MAX}.")
    if "debounce_seconds" in config or debounce:
        found["debounce_seconds"] = debounce
    if not isinstance(config.get("cancel_running", False), bool):
        raise WebhookError(400, "cancel_running must be true or false.")
    sticky = config.get("sticky", [])
    if not isinstance(sticky, list) or len(sticky) > STICKY_MAX or not all(
            isinstance(n, str) and INPUT_NAME.fullmatch(n) for n in sticky):
        raise WebhookError(400, f"sticky must be a list of at most {STICKY_MAX} input names.")
    if (config.get("cancel_running") or sticky) and concurrency != "supersede":
        raise WebhookError(400, "cancel_running and sticky need concurrency supersede.")
    if "cancel_running" in config:
        found["cancel_running"] = config["cancel_running"]
    if not isinstance(config.get("cancel_only", False), bool):
        raise WebhookError(400, "cancel_only must be true or false.")
    if config.get("cancel_only"):
        if "key" not in found:
            raise WebhookError(400, "cancel_only needs a key.")
        if config.get("conversation"):
            raise WebhookError(400, "cancel_only can't be a conversation rule.")
        found["cancel_only"] = True
    if sticky:
        found["sticky"] = sticky
    return found


def render_key(paths: list[str] | None, payload: Any) -> str | None:
    """The concurrency key a rule's paths give a payload, joined with #. None when any is missing or not a scalar."""
    if not paths:
        return None
    parts = [lookup(payload, path) for path in paths]
    if not all(isinstance(p, (str, int)) and not isinstance(p, bool) and p != "" for p in parts):
        return None
    return "#".join(str(p) for p in parts)[:KEY_MAX]


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
                "dry": bool(config.get("dry")), "stop": bool(config.get("stop"))}
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
    message = None
    if config.get("conversation"):
        message = conversations.slack_message(payload) if source == "slack" else None
        if message is None:
            decision["reason"] = "a conversation takes only a person's message in a Slack channel"
            return decision
    workflow = conn.execute(select(workflows.c.team_id, workflows.c.project_id, workflows.c.scope, workflows.c.name,
                                   workflows.c.kind, workflows.c.current_version)
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
    try:
        calls.pin(conn, workflow.scope, body_of(conn, WORKFLOW, rule.workflow_id, workflow.current_version),
                  workflow.name)
    except ValueError as exc:
        decision["reason"] = f"a workflow it calls doesn't resolve: {exc}"
        return decision
    decision["project_id"] = project_id
    decision.update(matched=True, reason="matched", inputs=map_inputs(config.get("inputs", {}), payload),
                    thread=render_thread(config.get("thread"), payload, source))
    if message is not None:
        decision["conversation"] = message
    head = lookup(payload, PR_HEAD) if workflow.kind == "review" else None
    if isinstance(head, str) and marks.mark_of(conn, project_id, head) is not None:
        # The review's own review.marked step checks again against GitHub, so a stale payload only costs a run.
        decision["skip"] = f"head {head[:12]} already carries an Armature review mark"
    if config.get("concurrency", "none") != "none":
        decision["key"] = render_key(config.get("key"), payload)
    if config.get("cancel_only"):
        decision["key"] = render_key(config.get("key"), payload)
    for name in ("concurrency", "debounce_seconds", "cancel_running", "sticky", "cancel_only"):
        if name in config:
            decision[name] = config[name]
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
                              "dry": bool((rule.config or {}).get("dry")),
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
               webhooks.c.approval, triggers.c.enabled, triggers.c.config)
        .select_from(triggers.join(webhooks, webhooks.c.id == triggers.c.webhook_id))
        .where(triggers.c.workflow_id == workflow_id, triggers.c.kind == KIND)
        .order_by(webhooks.c.name, triggers.c.created_at, triggers.c.id)).all()
    return [{"webhook_id": r.id, "webhook": r.name, "events": list((r.config or {}).get("events") or []),
             "shadow": bool((r.config or {}).get("shadow")),
             "enabled": bool(r.enabled and r.webhook_enabled and r.approval == "approved")}
            for r in rows if rbac.sees_team(access, r.team_id)]


def enqueue(conn: Connection, *, team_id: str, delivery_id: str, decisions: list[dict[str, Any]],
            now: datetime, connection_id: str | None = None) -> list[dict[str, Any]]:
    """Queue a job for each matching rule that is not in shadow. Returns what the delivery row records.

    A conversation rule's message goes to its conversation instead, which queues a turn only when none is running.
    """
    matched = []
    for decision in decisions:
        if not decision["matched"]:
            continue
        job_id = None
        if decision.get("cancel_only") and not decision["shadow"]:
            # A dry rule records what it would cancel and cancels nothing.
            cancelled = cancel_key(conn, workflow_id=decision["workflow_id"], key=decision["key"], now=now,
                                   reason=CANCELLED) if decision.get("key") and not decision.get("dry") else 0
            matched.append({"trigger_id": decision["trigger_id"], "workflow_id": decision["workflow_id"],
                            "job_id": None, "shadow": False, "cancelled": cancelled})
            continue
        if decision.get("skip") and not decision["shadow"]:
            matched.append({"trigger_id": decision["trigger_id"], "workflow_id": decision["workflow_id"],
                            "job_id": None, "shadow": False, "skipped": decision["skip"]})
            continue
        if decision.get("conversation") and not decision["shadow"]:
            accepted = conversations.from_slack(conn, team_id=team_id, project_id=decision["project_id"],
                                                workflow_id=decision["workflow_id"],
                                                message=decision["conversation"], connection_id=connection_id,
                                                now=now, delivery_id=delivery_id)
            matched.append({"trigger_id": decision["trigger_id"], "workflow_id": decision["workflow_id"],
                            "job_id": accepted["job_id"] if accepted else None, "shadow": False,
                            "conversation_id": accepted["conversation_id"] if accepted else None,
                            "pending": bool(accepted and accepted.get("pending")),
                            "capped": bool(accepted and accepted.get("capped"))})
            continue
        if not decision["shadow"]:
            try:
                debounce = decision.get("debounce_seconds") or 0
                job_id = queue_job(conn, team_id=team_id, project_id=decision["project_id"],
                                   workflow_id=decision["workflow_id"], inputs=decision["inputs"], now=now,
                                   trigger_id=decision["trigger_id"], delivery_id=delivery_id,
                                   thread=decision.get("thread"), dry_run=decision.get("dry", False),
                                   key=decision.get("key"),
                                   not_before=now + timedelta(seconds=debounce) if debounce else None,
                                   supersede=decision.get("concurrency") == "supersede",
                                   cancel_running=bool(decision.get("cancel_running")),
                                   sticky=decision.get("sticky") or ())
            except ValueError as exc:
                # A job never queues without its pins. _decide refuses these first, so this is a rare race.
                matched.append({"trigger_id": decision["trigger_id"], "workflow_id": decision["workflow_id"],
                                "job_id": None, "shadow": False,
                                "error": f"a workflow it calls doesn't resolve: {exc}"})
                continue
        matched.append({"trigger_id": decision["trigger_id"], "workflow_id": decision["workflow_id"],
                        "job_id": job_id, "shadow": decision["shadow"]})
    return matched


def record_delivery(conn: Connection, webhook_id: str, *, now: datetime, status: str, reason: str | None = None,
                    headers: dict[str, str] | None = None, raw: bytes | None = None, event: str | None = None,
                    key: str | None = None, matched: list[dict[str, Any]] | None = None,
                    sender: str | None = None) -> str:
    """Insert a delivery row. The raw body is kept only when it fits under ``PAYLOAD_MAX``."""
    delivery_id = new_id()
    payload = raw.decode("utf-8", "replace") if raw is not None and len(raw) <= PAYLOAD_MAX else None
    conn.execute(insert(webhook_deliveries).values(
        id=delivery_id, webhook_id=webhook_id, received_at=now, delivery_key=key, event=event, status=status,
        reason=reason, payload=payload, headers=headers or {}, matched=matched or [], sender=sender))
    return delivery_id


def pinned(hook: Any, sender: str | None) -> bool:
    """Whether pinning admits the sender. A NULL list means pinning is off and the signature alone admits."""
    return hook.pinned_senders is None or sender in hook.pinned_senders


def admit(hook: Any, sender: str | None) -> str | None:
    """Why a verified delivery is held, or None when it may queue jobs."""
    if hook.approval != "approved":
        return "webhook is pending approval"
    if not pinned(hook, sender):
        return f"sender {sender or '(unknown)'} is not pinned"
    return None


def forwardable(hook: Any, sender: str | None) -> bool:
    """Whether a delivery that skips the rules may still be forwarded: never one that would have been held."""
    return hook.enabled and admit(hook, sender) is None


def dispatch(conn: Connection, hook: Any, delivery_id: str, event: str | None, payload: Any,
             now: datetime) -> tuple[str, int]:
    """Run the rules on a recorded delivery, queue what matches, and mark the row. Returns its status and job count."""
    if hook.source == "slack" and gatecards.clicked(payload) is not None:
        conn.execute(update(webhook_deliveries).where(webhook_deliveries.c.id == delivery_id)
                     .values(status="ignored", reason=CLICK_NOT_REPLAYED))
        return "ignored", 0
    decisions = evaluate(conn, hook.team_id, hook.source, rules_of(conn, hook.id), event, payload)
    if any(d.get("key") for d in decisions if d["matched"] and not d["shadow"]):
        # Deliveries for one key take turns superseding, which also keeps MySQL from deadlocking on its gap locks.
        locked(conn, hook.id)
    matched = enqueue(conn, team_id=hook.team_id, delivery_id=delivery_id, decisions=decisions, now=now,
                      connection_id=hook.connection_id)
    status = "accepted" if matched else "ignored"
    count = sum(1 for m in matched if m["job_id"])
    reason = f"{len(matched)} rule(s) matched, {count} job(s) queued" if matched else "no rule matched"
    if matched and all(m.get("capped") for m in matched):
        status, reason = "ignored", conversations.PENDING_CAP
    conn.execute(update(webhook_deliveries).where(webhook_deliveries.c.id == delivery_id)
                 .values(status=status, reason=reason, matched=matched))
    return status, count


def click(conn: Connection, hook: Any, delivery_id: str, found: gatecards.Click, now: datetime, *,
          step_up_required: bool = False) -> tuple[str, int, gatecards.Notice | None]:
    """A gate button's click, which skips the rules: decide the gate, mark the row, and say what to tell the clicker.

    Only a live click decides. One that was held, approved, or replayed later goes through ``dispatch``, which
    ignores it.
    """
    outcome = gatecards.click(conn, hook, found, now, step_up_required=step_up_required)
    status = "accepted" if outcome.decided else "ignored"
    conn.execute(update(webhook_deliveries).where(webhook_deliveries.c.id == delivery_id)
                 .values(status=status, reason=outcome.reason))
    return status, 1 if outcome.decided else 0, outcome.notice


def held_delivery(conn: Connection, hook: Any, delivery_id: str) -> Any:
    row = conn.execute(select(webhook_deliveries).where(webhook_deliveries.c.id == delivery_id,
                                                        webhook_deliveries.c.webhook_id == hook.id)).first()
    if row is None:
        raise WebhookError(404, "No such delivery.")
    if row.status != "held":
        raise WebhookError(409, "Only a held delivery can be approved, replayed, or dismissed.")
    return row


def _claim(conn: Connection, hook: Any, delivery: Any, reason: str) -> None:
    """Take a held delivery out of held, so only one of two concurrent approvals or replays goes on."""
    claimed = conn.execute(update(webhook_deliveries).where(webhook_deliveries.c.id == delivery.id,
                                                            webhook_deliveries.c.webhook_id == hook.id,
                                                            webhook_deliveries.c.status == "held")
                           .values(status="ignored", reason=reason)).rowcount
    if not claimed:
        raise WebhookError(409, "Only a held delivery can be approved, replayed, or dismissed.")


def _check_releasable(hook: Any) -> None:
    if not hook.enabled:
        raise WebhookError(409, "Enable the webhook before approving or replaying its deliveries.")


def _replay(conn: Connection, hook: Any, delivery: Any, now: datetime) -> dict[str, Any]:
    payload = None
    if delivery.payload is not None:
        try:
            payload = parse_payload(delivery.headers or {}, delivery.payload.encode())
        except (ValueError, KeyError):
            payload = None
    if payload is None:
        conn.execute(update(webhook_deliveries).where(webhook_deliveries.c.id == delivery.id)
                     .values(reason="approved without a replay: no stored payload"))
        return {"replayed": False, "status": "ignored", "jobs": 0}
    status, count = dispatch(conn, hook, delivery.id, delivery.event, payload, now)
    return {"replayed": True, "status": status, "jobs": count}


def approve(conn: Connection, hook: Any, delivery_id: str, *, actor_id: str, now: datetime, ip: str | None,
            keyring: Any) -> dict[str, Any]:
    """Approve one held delivery, pin its sender, and replay it. Other held deliveries stay held.

    Approving a pending webhook starts its pinned list with this sender. On an approved webhook with pinning on,
    the sender joins the list.
    """
    delivery = held_delivery(conn, hook, delivery_id)
    _check_releasable(hook)
    if not secret_ready(conn, keyring, hook.team_id, hook.secret_name, hook.connection_id):
        raise WebhookError(409, "Set the webhook's secret before approving it.")
    if delivery.sender is None and hook.source != "generic":
        raise WebhookError(409, "This delivery has no sender to pin.")
    _claim(conn, hook, delivery, "approving")
    hook = locked(conn, hook.id)
    senders = hook.pinned_senders
    if hook.source == "generic":
        senders = None
    elif hook.approval == "pending":
        senders = [delivery.sender]
    elif senders is not None and delivery.sender not in senders:
        senders = [*senders, delivery.sender]
    conn.execute(update(webhooks).where(webhooks.c.id == hook.id)
                 .values(approval="approved", pinned_senders=senders, updated_at=now))
    hook = get(conn, hook.id)
    result = _replay(conn, hook, delivery, now)
    audit.record(conn, action="webhook.approve", actor_id=actor_id, team_id=hook.team_id, target_kind="webhook",
                 target_id=hook.id, detail={"name": hook.name, "delivery_id": delivery.id,
                                            "sender": delivery.sender, "pinned_senders": senders,
                                            "replayed": result["replayed"]}, ip=ip)
    return result


def replay(conn: Connection, hook: Any, delivery_id: str, *, actor_id: str, now: datetime,
           ip: str | None) -> dict[str, Any]:
    """Replay a held delivery that pinning admits, as approve does without changing the pinned list."""
    delivery = held_delivery(conn, hook, delivery_id)
    _check_releasable(hook)
    if hook.approval != "approved":
        raise WebhookError(409, "Approve the webhook before replaying its deliveries.")
    if not pinned(hook, delivery.sender):
        raise WebhookError(409, "This delivery's sender is not pinned. Approve it to add the sender.")
    if delivery.payload is None:
        raise WebhookError(409, "This delivery has no stored payload to replay.")
    _claim(conn, hook, delivery, "replaying")
    result = _replay(conn, hook, delivery, now)
    audit.record(conn, action="webhook.replay", actor_id=actor_id, team_id=hook.team_id, target_kind="webhook",
                 target_id=hook.id, detail={"name": hook.name, "delivery_id": delivery.id}, ip=ip)
    return result


def dismiss(conn: Connection, hook: Any, delivery_id: str, *, actor_id: str, ip: str | None) -> None:
    """Leave a held delivery on an approved webhook unrun. A pending webhook is rejected instead."""
    delivery = held_delivery(conn, hook, delivery_id)
    if hook.approval != "approved":
        raise WebhookError(409, "Reject a pending webhook instead of dismissing its deliveries.")
    _claim(conn, hook, delivery, DISMISSED)
    audit.record(conn, action="webhook.dismiss", actor_id=actor_id, team_id=hook.team_id, target_kind="webhook",
                 target_id=hook.id, detail={"name": hook.name, "delivery_id": delivery.id,
                                            "sender": delivery.sender}, ip=ip)


def locked(conn: Connection, webhook_id: str) -> Any:
    """The webhook row, locked until commit, so two changes to its pinned senders can't lose one."""
    return conn.execute(select(webhooks).where(webhooks.c.id == webhook_id).with_for_update()).first()


def _pinnable(conn: Connection, hook: Any) -> Any:
    hook = locked(conn, hook.id)
    if hook.source == "generic":
        raise WebhookError(409, "Generic webhooks have no sender to pin.")
    if hook.approval != "approved":
        raise WebhookError(409, "Approve the webhook before changing its sender pinning.")
    return hook


def _set_pins(conn: Connection, hook: Any, senders: list[str] | None, action: str, actor_id: str, now: datetime,
              ip: str | None, **detail: Any) -> None:
    conn.execute(update(webhooks).where(webhooks.c.id == hook.id).values(pinned_senders=senders, updated_at=now))
    audit.record(conn, action=action, actor_id=actor_id, team_id=hook.team_id, target_kind="webhook",
                 target_id=hook.id, detail={"name": hook.name, "pinned_senders": senders, **detail}, ip=ip)


def admitted_senders(conn: Connection, hook: Any) -> list[str]:
    """The senders of deliveries this webhook let through, which turning pinning on would keep admitting."""
    rows = conn.execute(select(webhook_deliveries.c.sender).distinct().where(
        webhook_deliveries.c.webhook_id == hook.id, webhook_deliveries.c.sender.is_not(None),
        webhook_deliveries.c.status.in_(("accepted", "ignored")),
        or_(webhook_deliveries.c.reason.is_(None), webhook_deliveries.c.reason != DISMISSED))).scalars()
    return sorted(rows)


def pin_on(conn: Connection, hook: Any, senders: Any, *, actor_id: str, now: datetime, ip: str | None) -> None:
    """Turn pinning on with the exact list the user confirmed from ``admitted_senders``."""
    hook = _pinnable(conn, hook)
    if hook.pinned_senders is not None:
        raise WebhookError(409, "Sender pinning is already on.")
    current = admitted_senders(conn, hook)
    if not current:
        raise WebhookError(409, "No delivery has a sender to pin yet.")
    if not isinstance(senders, list) or not all(isinstance(x, str) for x in senders) or sorted(senders) != current:
        raise WebhookError(409, "The senders to pin have changed. Review the list again.")
    _set_pins(conn, hook, current, "webhook.pin_on", actor_id, now, ip)


def pin_off(conn: Connection, hook: Any, *, actor_id: str, now: datetime, ip: str | None) -> None:
    hook = _pinnable(conn, hook)
    if hook.pinned_senders is None:
        raise WebhookError(409, "Sender pinning is already off.")
    _set_pins(conn, hook, None, "webhook.pin_off", actor_id, now, ip)


def unpin(conn: Connection, hook: Any, sender: Any, *, actor_id: str, now: datetime, ip: str | None) -> None:
    hook = _pinnable(conn, hook)
    senders = hook.pinned_senders or []
    if sender not in senders:
        raise WebhookError(404, "That sender is not pinned.")
    if len(senders) == 1:
        raise WebhookError(409, "This is the last pinned sender. Turn sender pinning off instead.")
    _set_pins(conn, hook, [s for s in senders if s != sender], "webhook.unpin", actor_id, now, ip, sender=sender)


def slack_pin_problem(pins: Mapping[str, str | None], payload: Any) -> str | None:
    """Why a Slack delivery comes from another workspace or app than its connection's, or None when it doesn't.

    A connection with no verified workspace admits nothing.
    """
    if not pins.get("team_id"):
        return "connection has no verified workspace"
    found = {"team_id": (sender_of("slack", payload) or "").removeprefix("slack:team:") or None,
             "api_app_id": _id_text(payload.get("api_app_id")) if isinstance(payload, dict) else None}
    for key, wanted in pins.items():
        if found[key] != wanted:
            return f"{key} {found[key] or '(none)'} is not the connection's {wanted}"
    return None


def signed_headers(source: str, cfg: Mapping[str, Any], secret: str | None, headers: Mapping[str, str],
                   raw: bytes, now: datetime) -> list[tuple[str, str]]:
    """A replayed delivery's stored headers, signed again as its source signs, for a forward target to verify.

    Linear signs a timestamp inside the body, so a Linear replay goes out unsigned.
    """
    out = [(k, v) for k, v in headers.items() if k.lower() not in SECRET_HEADERS]
    if not secret:
        return out
    if source == "github":
        out.append(("X-Hub-Signature-256", "sha256=" + _hex(secret, raw)))
    elif source == "jira":
        out.append(("X-Hub-Signature", "sha256=" + _hex(secret, raw)))
    elif source == "slack":
        stamp = str(int(now.timestamp()))
        out += [("X-Slack-Request-Timestamp", stamp),
                ("X-Slack-Signature", "v0=" + _hex(secret, b"v0:" + stamp.encode() + b":" + raw))]
    elif source == "generic" and cfg.get("generic_auth") == "bearer":
        out.append(("Authorization", f"Bearer {secret}"))
    elif source == "generic":
        out.append((cfg.get("generic_signature_header") or DEFAULT_SIGNATURE_HEADER, "sha256=" + _hex(secret, raw)))
    return out


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


def secret_set(conn: Connection, team_id: str, name: str) -> bool:
    return conn.execute(select(secrets.c.id).where(secrets.c.name == name,
                                                   secrets.c.scope.in_([team_id, INSTANCE_SCOPE]))
                        .limit(1)).first() is not None


def linked(conn: Connection, connection_id: str | None) -> Any | None:
    return conn.execute(select(connections.connections).where(connections.connections.c.id == connection_id)
                        ).first() if connection_id else None


def secret_ready(conn: Connection, keyring: Any, team_id: str, secret_name: str | None,
                 connection_id: str | None) -> bool:
    """Whether the webhook has a secret to verify with: its connection's, else its named team secret."""
    if connection_id:
        row = linked(conn, connection_id)
        return row is not None and connections.webhook_secret(keyring, row) is not None
    return secret_name is not None and secret_set(conn, team_id, secret_name)


def _link(conn: Connection, team_id: str, source: str, connection_id: Any) -> str:
    try:
        return connections.for_webhook(conn, team_id, connection_id, source).id
    except connections.ConnError as exc:
        raise WebhookError(exc.status, exc.message) from exc


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
    connection_id = _link(conn, team_id, source, body["connection_id"]) if body.get("connection_id") else None
    secret_name = None if connection_id else _check_secret_name(body.get("secret_name") or default_secret_name(name))
    webhook_id = new_id()
    conn.execute(insert(webhooks).values(
        id=webhook_id, team_id=team_id, name=name, source=source, path_token=_random.token_urlsafe(24),
        secret_name=secret_name, connection_id=connection_id, enabled=_check_enabled(body.get("enabled", True)),
        approval="pending", created_at=now, updated_at=now, **check_settings(source, body)))
    audit.record(conn, action="webhook.create", actor_id=actor_id, team_id=team_id, target_kind="webhook",
                 target_id=webhook_id, detail={"name": name, "source": source, "connection_id": connection_id},
                 ip=ip)
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


def update_webhook(conn: Connection, keyring: Any, row: Any, *, body: dict[str, Any], name: str | None,
                   actor_id: str, now: datetime, ip: str | None) -> Any:
    values: dict[str, Any] = check_settings(row.source, body, row._mapping)
    if name is not None:
        values["name"] = name
    if "enabled" in body:
        values["enabled"] = _check_enabled(body["enabled"])
    if "secret_name" in body:
        values["secret_name"] = _check_secret_name(body["secret_name"])
    if "connection_id" in body:
        values["connection_id"] = _link(conn, row.team_id, row.source, body["connection_id"]) \
            if body["connection_id"] else None
        if values["connection_id"]:
            values["secret_name"] = None
        elif "secret_name" not in values:
            values["secret_name"] = row.secret_name or default_secret_name(values.get("name", row.name))
    changed = sorted(k for k, v in values.items() if v != getattr(row, k))
    secret_name = values.get("secret_name", row.secret_name)
    connection_id = values.get("connection_id", row.connection_id)
    if "enabled" in changed and values["enabled"] and not secret_ready(conn, keyring, row.team_id, secret_name,
                                                                       connection_id):
        raise WebhookError(409, "Set the webhook's secret before enabling it.")
    if changed:
        conn.execute(update(webhooks).where(webhooks.c.id == row.id).values(**values, updated_at=now))
        audit.record(conn, action="webhook.update", actor_id=actor_id, team_id=row.team_id, target_kind="webhook",
                     target_id=row.id, detail={"name": values.get("name", row.name), "changed": changed}, ip=ip)
    return get(conn, row.id)


def remove(conn: Connection, row: Any, *, actor_id: str, ip: str | None, action: str = "webhook.delete") -> None:
    """Delete the webhook with its rules and deliveries. Jobs it queued stay."""
    rule_ids = select(triggers.c.id).where(triggers.c.webhook_id == row.id)
    detach_triggers(conn, rule_ids)
    conn.execute(delete(triggers).where(triggers.c.webhook_id == row.id))
    conn.execute(delete(webhook_deliveries).where(webhook_deliveries.c.webhook_id == row.id))
    conn.execute(delete(webhooks).where(webhooks.c.id == row.id))
    audit.record(conn, action=action, actor_id=actor_id, team_id=row.team_id, target_kind="webhook",
                 target_id=row.id, detail={"name": row.name}, ip=ip)


def _secret_in_use(conn: Connection, row: Any) -> bool:
    """Whether another webhook in the team, or a rule outside this webhook, uses the webhook's secret name."""
    if conn.execute(select(webhooks.c.id).where(webhooks.c.team_id == row.team_id,
                                                webhooks.c.secret_name == row.secret_name,
                                                webhooks.c.id != row.id).limit(1)).first():
        return True
    configs = conn.execute(select(triggers.c.config).where(
        triggers.c.team_id == row.team_id,
        or_(triggers.c.webhook_id.is_(None), triggers.c.webhook_id != row.id))).scalars()
    return any(row.secret_name in ((config or {}).get("secrets") or []) for config in configs)


def reject(conn: Connection, row: Any, *, actor_id: str, ip: str | None) -> None:
    """Delete a pending webhook and its team secret, unless something else in the team uses that secret."""
    if row.approval != "pending":
        raise WebhookError(409, "Only a pending webhook can be rejected. Dismiss the delivery or delete the webhook.")
    shared = row.secret_name is None or _secret_in_use(conn, row)
    remove(conn, row, actor_id=actor_id, ip=ip, action="webhook.reject")
    if not shared:
        secret_store.delete_secret(conn, team_id=row.team_id, name=row.secret_name, actor_id=actor_id)


def set_secret(conn: Connection, keyring: Any, row: Any, *, value: Any, actor_id: str, now: datetime,
               ip: str | None) -> str | None:
    """Store a pasted value, or generate one. Returns the generated value, the only time it is shown."""
    if row.connection_id:
        raise WebhookError(409, "This webhook verifies with its connection's secret. Set it on the connection.")
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


def _check_conversation(conn: Connection, workflow_id: str, config: dict[str, Any]) -> None:
    if config.get("conversation") and conversations.gated(conn, workflow_id):
        raise WebhookError(400, conversations.GATED)


def create_rule(conn: Connection, hook: Any, *, body: dict[str, Any], actor_id: str, now: datetime,
                ip: str | None) -> Any:
    workflow = _check_workflow(conn, hook.team_id, body.get("workflow_id"))
    config = rule_config(body)
    _check_conversation(conn, workflow.id, config)
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
    _check_conversation(conn, values.get("workflow_id", rule.workflow_id), values.get("config", rule.config) or {})
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

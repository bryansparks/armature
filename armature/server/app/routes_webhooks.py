"""Webhook receiver under ``/hooks/`` and the team lead's API for webhooks, rules, and deliveries.

The receiver takes no session. Its URL token finds the webhook, and the
source's signature check decides whether the delivery counts.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import Connection, and_, or_, select
from sqlalchemy.exc import IntegrityError
from starlette.background import BackgroundTasks

from armature.server import rbac
from armature.server import secrets as secret_store
from armature.server import webhooks as hooks
from armature.server.app.deps import Granted, by_team, require, require_step_up, server
from armature.server.app.http import ApiError, client_ip
from armature.server.app.routes_api import _before_clause, _check_name, _cursor, _next
from armature.server.auth import AuthError, count_call, ip_bucket
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    jobs,
    secrets,
    webhook_deliveries,
    webhooks,
    workflows,
)

router = APIRouter()

HOOKS_PATH = "/hooks/"
HOOKS_BODY_LIMIT = 5 * 1024 * 1024
WEBHOOK_LIMIT = 1200
IP_LIMIT = 1200
DELIVERIES_PAGE = 50
BAD_LIMIT = 300
NOT_FOUND = {"error": "not found"}
FORWARD_ADMIN_ONLY = "Only an admin may set a forward URL."


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except hooks.WebhookError as exc:
        raise ApiError(exc.status, exc.message) from exc
    except IntegrityError as exc:
        raise ApiError(409, "That name is taken in this team.") from exc


def _by_webhook(conn: Connection, params: dict[str, Any]) -> rbac.Target:
    row = conn.execute(select(webhooks.c.team_id).where(webhooks.c.id == params["webhook_id"])).first()
    if row is None:
        raise rbac.NotFound("No such webhook.")
    return rbac.Target(team_id=row.team_id)


def _by_rule(conn: Connection, params: dict[str, Any]) -> rbac.Target:
    try:
        return rbac.Target(team_id=hooks.get_rule(conn, params["rule_id"]).team_id)
    except hooks.WebhookError as exc:
        raise rbac.NotFound(exc.message) from exc


def _public(request: Request, conn: Connection, row: Any) -> dict[str, Any]:
    cfg = server(request).cfg
    secret_set = conn.execute(select(secrets.c.id).where(secrets.c.name == row.secret_name,
                                                         secrets.c.scope.in_([row.team_id, INSTANCE_SCOPE]))
                              .limit(1)).first() is not None
    last = conn.execute(select(webhook_deliveries.c.received_at, webhook_deliveries.c.status)
                        .where(webhook_deliveries.c.webhook_id == row.id)
                        .order_by(webhook_deliveries.c.received_at.desc(), webhook_deliveries.c.id.desc())
                        .limit(1)).first()
    return {"id": row.id, "team_id": row.team_id, "name": row.name, "source": row.source,
            "url": f"{cfg.public_origin}{cfg.base_path}{HOOKS_PATH}{row.path_token}", "enabled": row.enabled,
            "secret_name": row.secret_name, "secret_set": secret_set, "generic_auth": row.generic_auth,
            "generic_signature_header": row.generic_signature_header,
            "generic_delivery_header": row.generic_delivery_header, "forward_url": row.forward_url,
            "created_at": row.created_at, "updated_at": row.updated_at,
            "last_delivery": {"received_at": last.received_at, "status": last.status} if last else None}


def _rule(conn: Connection, row: Any) -> dict[str, Any]:
    name = conn.execute(select(workflows.c.name).where(workflows.c.id == row.workflow_id)).scalar()
    return {"id": row.id, "webhook_id": row.webhook_id, "workflow_id": row.workflow_id, "workflow_name": name,
            "enabled": row.enabled, "config": row.config or {}, "created_at": row.created_at}


def _visible_workflows(conn: Connection, team_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(select(workflows.c.id, workflows.c.name, workflows.c.team_id, workflows.c.project_id)
                        .where(or_(workflows.c.team_id.is_(None), workflows.c.team_id == team_id))
                        .order_by(workflows.c.name, workflows.c.scope)).all()
    return [{"id": r.id, "name": r.name,
             "scope_kind": "project" if r.project_id else "team" if r.team_id else "instance"} for r in rows]


def _with_jobs(conn: Connection, matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    job_ids = [m["job_id"] for m in matched if m.get("job_id")]
    found = {r.id: r for r in conn.execute(select(jobs.c.id, jobs.c.status, jobs.c.run_id)
                                           .where(jobs.c.id.in_(job_ids)))} if job_ids else {}
    names = {r.id: r.name for r in conn.execute(select(workflows.c.id, workflows.c.name).where(
        workflows.c.id.in_({m["workflow_id"] for m in matched})))} if matched else {}
    out = []
    for m in matched:
        job = found.get(m.get("job_id"))
        out.append({**m, "workflow_name": names.get(m["workflow_id"]), "job_status": job.status if job else None,
                    "run_id": job.run_id if job else None})
    return out


def _delivery(conn: Connection, row: Any, *, full: bool = False) -> dict[str, Any]:
    item = {"id": row.id, "received_at": row.received_at, "event": row.event, "status": row.status,
            "reason": row.reason, "delivery_key": row.delivery_key, "matched": _with_jobs(conn, row.matched or []),
            "forward_status": row.forward_status}
    if full:
        item.update(headers=row.headers or {}, payload=row.payload)
    return item


def _webhook(conn: Connection, webhook_id: str) -> Any:
    try:
        return hooks.get(conn, webhook_id)
    except hooks.WebhookError as exc:
        raise ApiError(exc.status, exc.message) from exc


@router.get("/api/teams/{team_id}/webhooks")
def list_webhooks(request: Request, granted: Granted = Depends(require("team_lead", by_team()))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        rows = conn.execute(select(webhooks).where(webhooks.c.team_id == granted.target.team_id)
                            .order_by(webhooks.c.name, webhooks.c.id)).all()
        return {"webhooks": [_public(request, conn, r) for r in rows]}


@router.post("/api/teams/{team_id}/webhooks")
def create_webhook(request: Request, payload: dict[str, Any] = Body(...),
                   granted: Granted = Depends(require("team_lead", by_team(), step_up=True))) -> dict[str, Any]:
    srv = server(request)
    name = _check_name(str(payload.get("name") or ""), "webhook")
    if payload.get("forward_url") and not granted.access.is_admin:
        raise ApiError(403, FORWARD_ADMIN_ONLY)
    with _refusals(), srv.engine.begin() as conn:
        row = hooks.create(conn, team_id=granted.target.team_id, name=name, body=payload, actor_id=granted.user_id,
                           now=srv.now(), ip=client_ip(request))
        return {"webhook": _public(request, conn, row)}


@router.get("/api/webhooks/{webhook_id}")
def get_webhook(webhook_id: str, request: Request,
                granted: Granted = Depends(require("team_lead", _by_webhook))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        row = _webhook(conn, webhook_id)
        return {"webhook": _public(request, conn, row),
                "rules": [_rule(conn, r) for r in hooks.ordered(hooks.rules_of(conn, row.id))],
                "workflows": _visible_workflows(conn, row.team_id)}


@router.post("/api/webhooks/{webhook_id}")
def update_webhook(webhook_id: str, request: Request, payload: dict[str, Any] = Body(...),
                   granted: Granted = Depends(require("team_lead", _by_webhook))) -> dict[str, Any]:
    srv = server(request)
    name = _check_name(str(payload["name"]), "webhook") if "name" in payload else None
    with _refusals(), srv.engine.begin() as conn:
        row = _webhook(conn, webhook_id)
        forward_changed = hooks.changes(row, payload, "forward_url")
        if forward_changed and not granted.access.is_admin:
            raise ApiError(403, FORWARD_ADMIN_ONLY)
        if forward_changed or hooks.changes(row, payload, "secret_name"):
            require_step_up(request, granted.session)
        row = hooks.update_webhook(conn, row, body=payload, name=name, actor_id=granted.user_id, now=srv.now(),
                                   ip=client_ip(request))
        return {"webhook": _public(request, conn, row)}


@router.post("/api/webhooks/{webhook_id}/delete")
def delete_webhook(webhook_id: str, request: Request,
                   granted: Granted = Depends(require("team_lead", _by_webhook, step_up=True))) -> dict[str, Any]:
    with _refusals(), server(request).engine.begin() as conn:
        hooks.remove(conn, _webhook(conn, webhook_id), actor_id=granted.user_id, ip=client_ip(request))
    return {"ok": True}


@router.post("/api/webhooks/{webhook_id}/secret")
def set_webhook_secret(webhook_id: str, request: Request, payload: dict[str, Any] = Body(default={}),
                       granted: Granted = Depends(require("team_lead", _by_webhook, step_up=True))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        row = _webhook(conn, webhook_id)
        generated = hooks.set_secret(conn, srv.keyring, row, value=payload.get("value"), actor_id=granted.user_id,
                                     now=srv.now(), ip=client_ip(request))
        return {"webhook": _public(request, conn, hooks.get(conn, row.id)), "secret": generated}


@router.get("/api/webhooks/{webhook_id}/deliveries")
def list_deliveries(webhook_id: str, request: Request, before: str | None = None,
                    limit: int = Query(DELIVERIES_PAGE, ge=1, le=200),
                    granted: Granted = Depends(require("team_lead", _by_webhook))) -> dict[str, Any]:
    query = select(webhook_deliveries).where(webhook_deliveries.c.webhook_id == webhook_id)
    clause = _before_clause(_cursor(before), webhook_deliveries.c.received_at, webhook_deliveries.c.id)
    if clause is not None:
        query = query.where(clause)
    with server(request).engine.connect() as conn:
        rows = conn.execute(query.order_by(webhook_deliveries.c.received_at.desc(), webhook_deliveries.c.id.desc())
                            .limit(limit)).all()
        return {"deliveries": [_delivery(conn, r) for r in rows], "next_before": _next(rows, limit, "received_at")}


def _stored_delivery(conn: Connection, webhook_id: str, delivery_id: str) -> Any:
    row = conn.execute(select(webhook_deliveries).where(and_(webhook_deliveries.c.id == delivery_id,
                                                             webhook_deliveries.c.webhook_id == webhook_id))).first()
    if row is None:
        raise ApiError(404, "No such delivery.")
    return row


@router.get("/api/webhooks/{webhook_id}/deliveries/{delivery_id}")
def get_delivery(webhook_id: str, delivery_id: str, request: Request,
                 granted: Granted = Depends(require("team_lead", _by_webhook))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        return {"delivery": _delivery(conn, _stored_delivery(conn, webhook_id, delivery_id), full=True)}


@router.post("/api/webhooks/{webhook_id}/deliveries/{delivery_id}/test")
def test_delivery(webhook_id: str, delivery_id: str, request: Request,
                  granted: Granted = Depends(require("team_lead", _by_webhook))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        hook = _webhook(conn, webhook_id)
        row = _stored_delivery(conn, webhook_id, delivery_id)
        if row.payload is None:
            raise ApiError(409, "This delivery has no stored payload to test against.")
        try:
            payload = hooks.parse_payload(row.headers or {}, row.payload.encode())
        except (ValueError, KeyError) as exc:
            raise ApiError(409, "The stored payload is not JSON.") from exc
        decisions = hooks.evaluate(conn, hook.team_id, hook.source, hooks.rules_of(conn, hook.id), row.event, payload)
        return {"decisions": decisions}


@router.post("/api/webhooks/{webhook_id}/rules")
def create_rule(webhook_id: str, request: Request, payload: dict[str, Any] = Body(...),
                granted: Granted = Depends(require("team_lead", _by_webhook))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        if hooks.secrets_change(None, payload):
            require_step_up(request, granted.session)
        rule = hooks.create_rule(conn, _webhook(conn, webhook_id), body=payload, actor_id=granted.user_id,
                                 now=srv.now(), ip=client_ip(request))
        return {"rule": _rule(conn, rule)}


@router.post("/api/rules/{rule_id}")
def update_rule(rule_id: str, request: Request, payload: dict[str, Any] = Body(...),
                granted: Granted = Depends(require("team_lead", _by_rule))) -> dict[str, Any]:
    with _refusals(), server(request).engine.begin() as conn:
        rule = hooks.get_rule(conn, rule_id)
        if hooks.secrets_change(rule, payload):
            require_step_up(request, granted.session)
        rule = hooks.update_rule(conn, rule, body=payload, actor_id=granted.user_id, ip=client_ip(request))
        return {"rule": _rule(conn, rule)}


@router.post("/api/rules/{rule_id}/delete")
def delete_rule(rule_id: str, request: Request,
                granted: Granted = Depends(require("team_lead", _by_rule))) -> dict[str, Any]:
    with _refusals(), server(request).engine.begin() as conn:
        hooks.remove_rule(conn, hooks.get_rule(conn, rule_id), actor_id=granted.user_id, ip=client_ip(request))
    return {"ok": True}


def _throttle(srv: Any, key: str, limit: int) -> None:
    try:
        count_call(srv.engine, key=key, limit=limit, now=srv.now())
    except AuthError as exc:
        raise ApiError(exc.status, exc.message, exc.headers) from exc


def _secret(conn: Connection, keyring: Any, hook: Any) -> str | None:
    try:
        return secret_store.resolve_for_run(conn, keyring, hook.team_id, [hook.secret_name])[hook.secret_name]
    except secret_store.MissingSecrets:
        return None


def _handle(srv: Any, token: str, headers: Any, raw: bytes, ip: str) -> tuple[Response, Any]:
    """Everything after the body is read. Returns the response and the delivery to forward after it, if any."""
    _throttle(srv, f"hook-ip:{ip_bucket(ip)}", IP_LIMIT)
    with srv.engine.connect() as conn:
        hook = hooks.by_token(conn, token)
        if hook is None or not hook.enabled:
            return JSONResponse(NOT_FOUND, status_code=404), None
        secret = _secret(conn, srv.keyring, hook)
    now = srv.now()
    cfg = hook._mapping
    kept = hooks.kept_headers(cfg, headers)
    problem = hooks.verify(hook.source, cfg, secret, headers, raw, now)
    if problem is not None:
        _throttle(srv, f"hook-bad:{hook.id}", BAD_LIMIT)
        with srv.engine.begin() as conn:
            hooks.record_delivery(conn, hook.id, now=now, status="rejected", reason=problem, headers=kept)
        return JSONResponse({"error": "invalid signature"}, status_code=401), None
    _throttle(srv, f"hook:{hook.id}", WEBHOOK_LIMIT)
    try:
        payload = hooks.parse_payload(headers, raw)
    except (ValueError, KeyError):
        with srv.engine.begin() as conn:
            delivery_id = hooks.record_delivery(conn, hook.id, now=now, status="error", reason="body is not JSON",
                                                headers=kept, raw=raw)
        return JSONResponse({"error": "body is not JSON"}, status_code=400), _forwarder(hook, delivery_id)
    event = hooks.event_of(hook.source, headers, payload)
    key = hooks.delivery_key(hook.source, cfg, headers, payload)

    def ignored(reason: str) -> str:
        with srv.engine.begin() as conn:
            return hooks.record_delivery(conn, hook.id, now=now, status="ignored", reason=reason, headers=kept,
                                         raw=raw, event=event)

    if hook.source == "github" and event == "ping":
        return JSONResponse({"status": "pong"}), _forwarder(hook, ignored("ping"))
    if hook.source == "slack" and isinstance(payload, dict) and payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        return (PlainTextResponse(challenge if isinstance(challenge, str) else ""),
                _forwarder(hook, ignored("url_verification")))

    def accept() -> tuple[str, str, int] | None:
        with srv.engine.begin() as conn:
            if hooks.seen(conn, hook.id, key):
                return None
            delivery_id = hooks.record_delivery(conn, hook.id, now=now, status="ignored", headers=kept, raw=raw,
                                                event=event, key=key)
            decisions = hooks.evaluate(conn, hook.team_id, hook.source, hooks.rules_of(conn, hook.id), event, payload)
            matched = hooks.enqueue(conn, team_id=hook.team_id, delivery_id=delivery_id, decisions=decisions, now=now)
            status = "accepted" if matched else "ignored"
            count = sum(1 for m in matched if m["job_id"])
            reason = f"{len(matched)} rule(s) matched, {count} job(s) queued" if matched else "no rule matched"
            conn.execute(webhook_deliveries.update().where(webhook_deliveries.c.id == delivery_id)
                         .values(status=status, reason=reason, matched=matched))
            return delivery_id, status, count

    try:
        accepted = accept()
    except IntegrityError:
        with srv.engine.connect() as conn:
            if not hooks.seen(conn, hook.id, key):
                raise
        accepted = None
    if accepted is None:
        with srv.engine.begin() as conn:
            hooks.record_delivery(conn, hook.id, now=now, status="duplicate", reason=f"already received {key}",
                                  headers=kept, event=event)
        return JSONResponse({"status": "duplicate"}), None
    delivery_id, status, count = accepted
    return JSONResponse({"status": status, "jobs": count}), _forwarder(hook, delivery_id)


def _forwarder(hook: Any, delivery_id: str) -> tuple[str, str] | None:
    return (delivery_id, hook.forward_url) if hook.forward_url else None


@router.post(HOOKS_PATH + "{token}")
async def receive(token: str, request: Request) -> Response:
    srv = server(request)
    raw = await request.body()
    response, forward = await asyncio.to_thread(_handle, srv, token[:64], request.headers, raw, client_ip(request))
    tasks = BackgroundTasks()
    if forward is not None:
        delivery_id, forward_url = forward
        tasks.add_task(hooks.forward, srv.engine, delivery_id, forward_url, list(request.headers.items()), raw)
    if response.status_code < 400 and hooks.prune_due(srv.engine, srv.now()):
        tasks.add_task(hooks.maybe_prune, srv.engine, srv.now())
    response.background = tasks
    return response


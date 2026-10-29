"""The team lead's API for poll and schedule triggers: their state, poll history, seen keys, and checks."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy import Connection

from armature.server import pollers, rbac
from armature.server.app.deps import Granted, by_team, require, require_step_up, server
from armature.server.app.http import ApiError, client_ip
from armature.server.app.routes_webhooks import _visible_workflows
from armature.server.db.schema import TRIGGER_SEEN_OUTCOMES

router = APIRouter()

POLLS_PAGE = 50
SEEN_PAGE = 100


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except pollers.TriggerError as exc:
        raise ApiError(exc.status, exc.message) from exc


def _by_trigger(conn: Connection, params: dict[str, Any]) -> rbac.Target:
    try:
        return rbac.Target(team_id=pollers.get(conn, params["trigger_id"]).team_id)
    except pollers.TriggerError as exc:
        raise rbac.NotFound(exc.message) from exc


def _trigger(conn: Connection, trigger_id: str) -> Any:
    try:
        return pollers.get(conn, trigger_id)
    except pollers.TriggerError as exc:
        raise ApiError(exc.status, exc.message) from exc


@router.get("/api/teams/{team_id}/triggers")
def list_triggers(request: Request, granted: Granted = Depends(require("team_lead", by_team()))) -> dict[str, Any]:
    srv = server(request)
    team_id = granted.target.team_id
    with srv.engine.connect() as conn:
        return {"triggers": [pollers.public(conn, r, srv.now()) for r in pollers.listed(conn, team_id)],
                "workflows": _visible_workflows(conn, team_id),
                "connections": pollers.visible_connections(conn, team_id), "default_jql": pollers.DEFAULT_JQL}


@router.post("/api/teams/{team_id}/triggers")
def create_trigger(request: Request, payload: dict[str, Any] = Body(...),
                   granted: Granted = Depends(require("team_lead", by_team()))) -> dict[str, Any]:
    srv = server(request)
    if pollers.secrets_change(None, payload):
        require_step_up(request, granted.session)
    with _refusals(), srv.engine.begin() as conn:
        row = pollers.create(conn, team_id=granted.target.team_id, body=payload, actor_id=granted.user_id,
                             now=srv.now(), ip=client_ip(request))
        return {"trigger": pollers.public(conn, row, srv.now())}


@router.get("/api/triggers/{trigger_id}")
def get_trigger(trigger_id: str, request: Request,
                granted: Granted = Depends(require("team_lead", _by_trigger))) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.connect() as conn:
        row = _trigger(conn, trigger_id)
        return {"trigger": pollers.public(conn, row, srv.now()), "polls": pollers.polls(conn, row.id, POLLS_PAGE),
                "workflows": _visible_workflows(conn, row.team_id),
                "connections": pollers.visible_connections(conn, row.team_id)}


@router.post("/api/triggers/{trigger_id}")
def update_trigger(trigger_id: str, request: Request, payload: dict[str, Any] = Body(...),
                   granted: Granted = Depends(require("team_lead", _by_trigger))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        row = _trigger(conn, trigger_id)
        if pollers.secrets_change(row, payload):
            require_step_up(request, granted.session)
        row = pollers.update_trigger(conn, row, body=payload, actor_id=granted.user_id, now=srv.now(),
                                     ip=client_ip(request))
        return {"trigger": pollers.public(conn, row, srv.now())}


@router.post("/api/triggers/{trigger_id}/delete")
def delete_trigger(trigger_id: str, request: Request,
                   granted: Granted = Depends(require("team_lead", _by_trigger, step_up=True))) -> dict[str, Any]:
    with _refusals(), server(request).engine.begin() as conn:
        pollers.remove(conn, _trigger(conn, trigger_id), actor_id=granted.user_id, ip=client_ip(request))
    return {"ok": True}


def _call(request: Request, trigger_id: str, action: Any, **kwargs: Any) -> dict[str, Any]:
    """A check or change that calls the vendor, made outside any transaction."""
    srv = server(request)
    with srv.engine.connect() as conn:
        row = _trigger(conn, trigger_id)
    with _refusals():
        return action(srv.engine, srv.keyring, row, now=srv.now(), transport=request.app.state.vendor_transport,
                      **kwargs)


@router.post("/api/triggers/{trigger_id}/run")
def run_trigger(trigger_id: str, request: Request,
                granted: Granted = Depends(require("team_lead", _by_trigger))) -> dict[str, Any]:
    return {"poll": _call(request, trigger_id, pollers.run_now, actor_id=granted.user_id, ip=client_ip(request))}


@router.post("/api/triggers/{trigger_id}/preview")
def preview_trigger(trigger_id: str, request: Request,
                    granted: Granted = Depends(require("team_lead", _by_trigger))) -> dict[str, Any]:
    return _call(request, trigger_id, pollers.preview)


@router.post("/api/triggers/{trigger_id}/why")
def why_not(trigger_id: str, request: Request, payload: dict[str, Any] = Body(...),
            granted: Granted = Depends(require("team_lead", _by_trigger))) -> dict[str, Any]:
    return _call(request, trigger_id, pollers.why, key=payload.get("key"))


@router.post("/api/triggers/{trigger_id}/seed")
def seed_trigger(trigger_id: str, request: Request,
                 granted: Granted = Depends(require("team_lead", _by_trigger))) -> dict[str, Any]:
    return _call(request, trigger_id, pollers.seed, actor_id=granted.user_id, ip=client_ip(request))


@router.post("/api/triggers/{trigger_id}/requeue")
def requeue_key(trigger_id: str, request: Request, payload: dict[str, Any] = Body(...),
                granted: Granted = Depends(require("team_lead", _by_trigger, step_up=True))) -> dict[str, Any]:
    return _call(request, trigger_id, pollers.requeue, key=payload.get("key"), actor_id=granted.user_id,
                 ip=client_ip(request))


@router.post("/api/triggers/{trigger_id}/reset")
def reset_seen(trigger_id: str, request: Request,
               granted: Granted = Depends(require("team_lead", _by_trigger, step_up=True))) -> dict[str, Any]:
    with _refusals(), server(request).engine.begin() as conn:
        return {"forgot": pollers.reset(conn, _trigger(conn, trigger_id), actor_id=granted.user_id,
                                        ip=client_ip(request))}


@router.get("/api/triggers/{trigger_id}/polls")
def list_polls(trigger_id: str, request: Request, limit: int = Query(POLLS_PAGE, ge=1, le=pollers.POLLS_PER_TRIGGER),
               granted: Granted = Depends(require("team_lead", _by_trigger))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        return {"polls": pollers.polls(conn, _trigger(conn, trigger_id).id, limit)}


@router.get("/api/triggers/{trigger_id}/seen")
def list_seen(trigger_id: str, request: Request, outcome: str | None = Query(None, max_length=16),
              q: str | None = Query(None, max_length=191), limit: int = Query(SEEN_PAGE, ge=1, le=500),
              granted: Granted = Depends(require("team_lead", _by_trigger))) -> dict[str, Any]:
    if outcome and outcome not in TRIGGER_SEEN_OUTCOMES:
        raise ApiError(400, f"outcome must be one of {', '.join(TRIGGER_SEEN_OUTCOMES)}.")
    with server(request).engine.connect() as conn:
        return {"seen": pollers.seen_keys(conn, _trigger(conn, trigger_id).id, outcome=outcome, q=q, limit=limit)}

"""Run actions offered on a run's page, and the operations they recorded."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from armature.server import actions, rbac
from armature.server.app.deps import (
    Granted,
    by_run,
    grant,
    require,
    require_step_up,
    server,
    signed_in,
)
from armature.server.app.http import ApiError, client_ip
from armature.server.auth import Session
from armature.server.db.schema import operations, runs
from armature.server.ingest import load_summary

router = APIRouter()

OPERATIONS_LIMIT = 50
OPERATIONS_LIMIT_MAX = 200


async def _summary(request: Request, run_id: str) -> dict[str, Any]:
    live = request.app.state.live.summary(run_id)

    def read() -> tuple[dict[str, Any] | None, bool]:
        with server(request).engine.connect() as conn:
            return (None if live is not None else load_summary(conn, run_id)), actions.host_run(conn, run_id)

    stored, host = await asyncio.to_thread(read)
    summary = live if live is not None else stored
    if summary is None:
        raise ApiError(404, "No such run.")
    return {**summary, "host_run": host}


@router.get("/api/runs/{run_id}/actions")
async def list_actions(run_id: str, request: Request,
                       granted: Granted = Depends(require("viewer", by_run()))) -> dict[str, Any]:
    summary = await _summary(request, run_id)
    return {"actions": actions.offered(server(request).cfg.actions, summary, granted.role)}


@router.post("/api/runs/{run_id}/actions/{action_id}")
async def run_action(run_id: str, action_id: str, request: Request,
                     session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    action = actions.find(srv.cfg.actions, action_id)
    if action is None:
        raise ApiError(404, "No such action.")

    def authorize() -> Granted:
        with srv.engine.connect() as conn:
            try:
                target = rbac.run_target(conn, run_id)
            except rbac.NotFound as exc:
                raise ApiError(404, str(exc)) from exc
            return grant(srv, session, target, "admin" if action.admin_only else action.min_role, conn)

    granted = await asyncio.to_thread(authorize)
    if action.admin_only:
        require_step_up(request, session)
    summary = await _summary(request, run_id)
    reason = actions.unavailable(action, summary, granted.role)
    if reason:
        raise ApiError(409, reason)
    ip = client_ip(request)

    def start_and_run() -> dict[str, Any]:
        op_id = actions.begin(srv.engine, action, run_id, session.user_id, srv.now())
        with srv.engine.connect() as conn:
            payload = actions.run_payload(conn, summary, session.user.username)
        return actions.execute(srv.engine, srv.keyring, action, payload, op_id, team_id=summary["team_id"],
                               user_id=session.user_id, now=srv.now, ip=ip)

    try:
        row = await asyncio.to_thread(start_and_run)
    except actions.ActionError as exc:
        raise ApiError(exc.status, exc.message) from exc
    return {"operation": actions.shown(row, admin=granted.role == "admin")}


@router.get("/api/operations")
def list_operations(request: Request, run: str | None = Query(None, max_length=36),
                    limit: int = Query(OPERATIONS_LIMIT, ge=1, le=OPERATIONS_LIMIT_MAX),
                    session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.connect() as conn:
        query = select(operations).join(runs, runs.c.id == operations.c.run_id)
        if run is not None:
            try:
                target = rbac.run_target(conn, run)
            except rbac.NotFound as exc:
                raise ApiError(404, str(exc)) from exc
            grant(srv, session, target, "viewer", conn)
            query = query.where(operations.c.run_id == run)
        else:
            access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
            query = query.where(rbac.scope_clause(rbac.visible_scope(access), runs.c.team_id, runs.c.project_id))
        rows = conn.execute(query.order_by(operations.c.started_at.desc(), operations.c.id.desc())
                            .limit(limit)).all()
    return {"operations": [actions.shown(row, admin=bool(session.user.is_admin)) for row in rows]}


@router.get("/api/operations/{operation_id}")
def get_operation(operation_id: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.connect() as conn:
        row = conn.execute(select(operations).where(operations.c.id == operation_id)).first()
        if row is None:
            raise ApiError(404, "No such operation.")
        try:
            target = rbac.run_target(conn, row.run_id)
        except rbac.NotFound as exc:
            raise ApiError(404, "No such operation.") from exc
        granted = grant(srv, session, target, "viewer", conn)
    # The log is the host command's own output, so only an admin reads it.
    admin = granted.role == "admin"
    return {"operation": actions.shown(row, with_log=admin, admin=admin)}

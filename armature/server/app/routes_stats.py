"""The Overview and Trends aggregates, and the display settings every page reads."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from armature.server import display, rbac, stats
from armature.server.app.deps import server, signed_in
from armature.server.app.http import ApiError
from armature.server.auth import Session

router = APIRouter()

TRENDS_DAYS_MAX = 90


def _read(request: Request, session: Session, team: str | None, compute) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.connect() as conn:
        access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
        if team:
            try:
                rbac.team_target(conn, team)
            except rbac.NotFound as exc:
                raise ApiError(404, "Not found.") from exc
            if not rbac.sees_team(access, team):
                raise ApiError(404, "Not found.")
        return compute(conn, rbac.visible_scope(access), team or None, srv.now(), display.load(conn))


@router.get("/api/display")
def get_display(request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        scope = rbac.visible_scope(rbac.load_access(conn, session.user_id, bool(session.user.is_admin)))
        names = stats.workflow_names(conn, scope)
        shown = dict(display.load(conn))
        # Labels are instance-wide, so a member only learns about workflows their own teams can see.
        if not session.user.is_admin:
            visible = set(names)
            shown["workflow_labels"] = {name: label for name, label in (shown.get("workflow_labels") or {}).items()
                                        if name in visible}
        return {"display": shown, "workflows": names}


@router.get("/api/overview")
def get_overview(request: Request, team: str | None = Query(None, max_length=36),
                 session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _read(request, session, team, stats.overview)


@router.get("/api/trends")
def get_trends(request: Request, team: str | None = Query(None, max_length=36),
               days: int = Query(30, ge=1, le=TRENDS_DAYS_MAX),
               session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _read(request, session, team,
                 lambda conn, scope, team_id, now, settings: stats.trends(conn, scope, team_id, now, days, settings))

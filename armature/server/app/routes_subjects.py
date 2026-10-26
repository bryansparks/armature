"""Pull requests and issues: every run about one subject, as a list and as one page."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from armature.server import rbac, subjects
from armature.server.app.deps import server, signed_in
from armature.server.app.http import ApiError
from armature.server.auth import Session

router = APIRouter()

LIMIT_DEFAULT = 50
LIMIT_MAX = 200
SUBJECT_MAX = 191


@router.get("/api/subjects")
def list_subjects(request: Request, team: str | None = Query(None, max_length=36),
                  q: str = Query("", max_length=191), before: str | None = Query(None, max_length=400),
                  limit: int = Query(LIMIT_DEFAULT, ge=1, le=LIMIT_MAX),
                  session: Session = Depends(signed_in)) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
        if team:
            try:
                rbac.team_target(conn, team)
            except rbac.NotFound as exc:
                raise ApiError(404, "Not found.") from exc
            if not rbac.sees_team(access, team):
                raise ApiError(404, "Not found.")
        try:
            return subjects.list_subjects(conn, rbac.visible_scope(access), team or None, q, before, limit)
        except ValueError as exc:
            raise ApiError(400, "before is not a cursor from this list.") from exc


@router.get("/api/subjects/{subject:path}")
def get_subject(subject: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    if not subject or len(subject) > SUBJECT_MAX:
        raise ApiError(404, "Not found.")
    with server(request).engine.connect() as conn:
        access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
        detail = subjects.subject_detail(conn, rbac.visible_scope(access), subject)
    if detail is None:
        raise ApiError(404, "Not found.")
    return {"subject": detail}

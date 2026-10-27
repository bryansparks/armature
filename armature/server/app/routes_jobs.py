"""Jobs the webhook rules queued, and how the worker left each one."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from armature.server import rbac
from armature.server.app.deps import server, signed_in
from armature.server.auth import Session
from armature.server.db.schema import jobs, workflows

router = APIRouter()


@router.get("/api/jobs")
def list_jobs(request: Request, team_id: str | None = Query(None, max_length=36),
              status: str | None = Query(None, max_length=16), limit: int = Query(100, ge=1, le=500),
              session: Session = Depends(signed_in)) -> dict[str, Any]:
    query = (select(jobs.c.id, workflows.c.name.label("workflow"), jobs.c.status, jobs.c.created_at,
                    jobs.c.claimed_at, jobs.c.finished_at, jobs.c.run_id, jobs.c.error, jobs.c.trigger_id,
                    jobs.c.delivery_id)
             .select_from(jobs.outerjoin(workflows, workflows.c.id == jobs.c.workflow_id))
             .order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(limit))
    if team_id is not None:
        query = query.where(jobs.c.team_id == team_id)
    if status is not None:
        query = query.where(jobs.c.status == status)
    with server(request).engine.connect() as conn:
        access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
        rows = conn.execute(query.where(rbac.scope_clause(rbac.visible_scope(access), jobs.c.team_id,
                                                          jobs.c.project_id))).all()
    return {"jobs": [dict(r._mapping) for r in rows]}

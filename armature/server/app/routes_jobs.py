"""Jobs the webhook rules queued, how the worker left each one, and canceling them."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from armature.server import dispatch, environments, rbac
from armature.server.app.deps import Granted, ScopeFrom, by_run, require, server, signed_in
from armature.server.app.http import ApiError, client_ip
from armature.server.auth import Session
from armature.server.db.schema import jobs, workflows

router = APIRouter()


def by_job(param: str = "job_id") -> ScopeFrom:
    return lambda conn, params: dispatch.job_target(conn, params[param])


def _cancel(request: Request, granted: Granted, where: Any, missing: str) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.begin() as conn:
        job = conn.execute(select(jobs).where(where).order_by(jobs.c.created_at.desc(), jobs.c.id)).first()
        if job is None:
            raise ApiError(404, missing)
        try:
            return {"job": dispatch.cancel(conn, job=job, actor_id=granted.user_id, now=srv.now(),
                                           ip=client_ip(request))}
        except dispatch.RunnerError as exc:
            raise ApiError(exc.status, exc.message) from exc


@router.get("/api/jobs")
def list_jobs(request: Request, team_id: str | None = Query(None, max_length=36),
              status: str | None = Query(None, max_length=16), limit: int = Query(100, ge=1, le=500),
              session: Session = Depends(signed_in)) -> dict[str, Any]:
    query = (select(jobs.c.id, workflows.c.name.label("workflow"), jobs.c.status, jobs.c.created_at,
                    jobs.c.claimed_at, jobs.c.finished_at, jobs.c.run_id, jobs.c.error, jobs.c.trigger_id,
                    jobs.c.delivery_id, jobs.c.team_id, jobs.c.project_id, jobs.c.requires, jobs.c.concurrency_key,
                    jobs.c.not_before, jobs.c.superseded_by)
             .select_from(jobs.outerjoin(workflows, workflows.c.id == jobs.c.workflow_id))
             .order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(limit))
    if team_id is not None:
        query = query.where(jobs.c.team_id == team_id)
    # A superseded job never ran, so the list leaves it out unless asked.
    query = query.where(jobs.c.status == status) if status is not None else query.where(jobs.c.status != "superseded")
    srv = server(request)
    with srv.engine.connect() as conn:
        access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
        rows = conn.execute(query.where(rbac.scope_clause(rbac.visible_scope(access), jobs.c.team_id,
                                                          jobs.c.project_id))).all()
        warned = environments.warnings(conn, rows, srv.now(), request.app.state.environments.reasons)
    hidden = ("team_id", "project_id", "requires")
    return {"jobs": [{**{k: v for k, v in r._mapping.items() if k not in hidden}, "warning": warned.get(r.id)}
                     for r in rows]}


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request,
               granted: Granted = Depends(require("contributor", by_job()))) -> dict[str, Any]:
    return _cancel(request, granted, jobs.c.id == job_id, "No such job.")


@router.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request,
               granted: Granted = Depends(require("contributor", by_run()))) -> dict[str, Any]:
    return _cancel(request, granted, jobs.c.run_id == run_id, "Only a run the server started can be canceled here.")

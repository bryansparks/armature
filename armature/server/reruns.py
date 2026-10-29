"""Rerunning a finished run: a new job for its workflow with the inputs, trigger, thread, key, and dry mode it had.

The new job runs the workflow's current version, as any newly queued job does,
and the run it opens links back to the one it reruns.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select

from armature.server import audit, worker
from armature.server.db.schema import jobs, runs, workflows

FINISHED = ("succeeded", "failed", "canceled", "lost")
ACTIVE = ("queued", *worker.HELD)


class RerunError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _original(conn: Connection, run_id: str) -> Any:
    """The job that opened the run, as against the ones that resumed it from a gate."""
    return conn.execute(select(jobs).where(jobs.c.run_id == run_id, jobs.c.resume_of_gate.is_(None))
                        .order_by(jobs.c.created_at, jobs.c.id).limit(1)).first()


def check(conn: Connection, run_id: str) -> Any:
    """The job to rerun the run from. Raises RerunError saying why the run can't be rerun now."""
    run = conn.execute(select(runs.c.status).where(runs.c.id == run_id)).first()
    if run is None:
        raise RerunError(404, "No such run.")
    if run.status not in FINISHED:
        raise RerunError(409, "This run hasn't finished. Rerun it once it ends.")
    job = _original(conn, run_id)
    if job is None:
        raise RerunError(409, "Only a run the server queued can be rerun here.")
    if isinstance(job.inputs, dict) and "conversation_id" in job.inputs:
        raise RerunError(409, "A conversation turn reruns from its conversation.")
    workflow = conn.execute(select(workflows.c.current_version).where(workflows.c.id == job.workflow_id)).first()
    if workflow is None or not workflow.current_version:
        raise RerunError(409, "This run's workflow no longer exists.")
    if conn.execute(select(jobs.c.id).where(jobs.c.run_id == run_id, jobs.c.status.in_(ACTIVE))).first():
        raise RerunError(409, "This run's job is still held by a runner.")
    if conn.execute(select(jobs.c.id).where(jobs.c.rerun_of == run_id, jobs.c.status.in_(ACTIVE))).first():
        raise RerunError(409, "This run already has a rerun queued or running.")
    if job.concurrency_key is not None and conn.execute(select(jobs.c.id).where(
            jobs.c.workflow_id == job.workflow_id, jobs.c.concurrency_key == job.concurrency_key,
            jobs.c.status.in_(ACTIVE))).first():
        raise RerunError(409, "A job for the same work is already queued or running.")
    return job


def allowed(conn: Connection, run_id: str) -> bool:
    try:
        check(conn, run_id)
    except RerunError:
        return False
    return True


def latest(conn: Connection, run_id: str) -> dict[str, Any] | None:
    """The newest job that reruns the run, so its page can link to it."""
    row = conn.execute(select(jobs.c.id, jobs.c.status, jobs.c.run_id).where(jobs.c.rerun_of == run_id)
                       .order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(1)).first()
    return dict(row._mapping) if row else None


def rerun(conn: Connection, run_id: str, *, actor_id: str, now: datetime, ip: str | None) -> dict[str, Any]:
    """Queue a job that reruns the run. Returns the new job."""
    # Two reruns of one run take turns here, so the second sees the first's job.
    conn.execute(select(runs.c.id).where(runs.c.id == run_id).with_for_update()).first()
    job = check(conn, run_id)
    try:
        job_id = worker.queue_job(conn, team_id=job.team_id, project_id=job.project_id, workflow_id=job.workflow_id,
                                  inputs=job.inputs, now=now, trigger_id=job.trigger_id, delivery_id=job.delivery_id,
                                  thread=job.thread, dry_run=bool(job.dry_run), key=job.concurrency_key,
                                  rerun_of=run_id)
    except ValueError as exc:
        raise RerunError(409, f"A workflow it calls doesn't resolve: {exc}") from exc
    audit.record(conn, action="run.rerun", actor_id=actor_id, team_id=job.team_id, target_kind="run",
                 target_id=run_id, detail={"job_id": job_id, "from_job": job.id, "dry_run": bool(job.dry_run)},
                 ip=ip)
    return {"id": job_id, "status": "queued", "run_id": None}

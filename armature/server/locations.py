"""Backfill where recorded runs executed, from their jobs and stored step outputs.

A run its job or steps can't place reads as unknown, so a second pass finds nothing to do.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Engine, Text, cast, select, update

from armature.events import location
from armature.server.db.schema import (
    environment_hosts,
    environment_types,
    environments,
    jobs,
    run_steps,
    runners,
    runs,
)
from armature.server.worker import EMBEDDED_CLAIM

BATCH = 500


def _from_job(job: Any) -> dict[str, Any] | None:
    if job.provider_id is not None or job.kind is not None:
        mac = job.kind == "ec2-mac" or job.host_instance is not None
        return location.make(location.AWS_MAC if mac else location.AWS_LINUX,
                             instance=job.host_instance or job.provider_id, instance_type=job.instance_type,
                             environment_type=job.type_name)
    found = location.parse(job.platform, trusted=True)
    if found is None and job.claimed_by and EMBEDDED_CLAIM.fullmatch(job.claimed_by):
        # The embedded worker ran it on the server's host, whose chip then is not on record.
        found = location.make(location.LOCAL)
    return found


def _from_jobs(conn: Connection, ids: list[str]) -> dict[str, dict[str, Any] | None]:
    """Each run's newest job, placed by its newest environment, then its runner's platform."""
    rows = conn.execute(
        select(jobs.c.run_id, jobs.c.claimed_by, runners.c.platform, environments.c.provider_id,
               environments.c.instance_type, environment_types.c.kind, environment_types.c.name.label("type_name"),
               environment_hosts.c.instance_id.label("host_instance"))
        .select_from(jobs.outerjoin(runners, runners.c.id == jobs.c.runner_id)
                     .outerjoin(environments, environments.c.job_id == jobs.c.id)
                     .outerjoin(environment_types, environment_types.c.id == environments.c.type_id)
                     .outerjoin(environment_hosts, environment_hosts.c.id == environments.c.host_id))
        .where(jobs.c.run_id.in_(ids))
        .order_by(jobs.c.created_at.desc(), jobs.c.id, environments.c.requested_at.desc())).all()
    found: dict[str, dict[str, Any] | None] = {}
    for row in rows:
        if row.run_id not in found:
            found[row.run_id] = _from_job(row)
    return found


def _from_steps(conn: Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    outputs = conn.execute(select(run_steps.c.run_id, run_steps.c.output).where(
        run_steps.c.run_id.in_(ids), cast(run_steps.c.output, Text).like('%"placement"%')))
    for run_id, output in outputs:
        placed = location.from_output(output)
        if location.better(found.get(run_id), placed):
            found[run_id] = placed
    return found


def backfill(engine: Engine, *, batch: int = BATCH) -> dict[str, int]:
    """Set the location of every run without one. Returns how many runs got each kind."""
    counts: dict[str, int] = {}
    last = ""
    while True:
        with engine.begin() as conn:
            ids = conn.execute(select(runs.c.id).where(runs.c.location.is_(None), runs.c.id > last)
                               .order_by(runs.c.id).limit(batch)).scalars().all()
            if not ids:
                return counts
            by_job = _from_jobs(conn, ids)
            unplaced = [run_id for run_id in ids if by_job.get(run_id) is None]
            by_steps = _from_steps(conn, unplaced) if unplaced else {}
            for run_id in ids:
                found = by_job.get(run_id) or by_steps.get(run_id) or location.make(location.UNKNOWN)
                conn.execute(update(runs).where(runs.c.id == run_id, runs.c.location.is_(None))
                             .values(location=found))
                counts[found["kind"]] = counts.get(found["kind"], 0) + 1
        last = ids[-1]
        if len(ids) < batch:
            return counts

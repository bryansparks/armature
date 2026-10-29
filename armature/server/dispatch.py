"""The runner side of the job queue: runners say hello, claim jobs, heartbeat, and report how each job ended.

A runner claims only queued jobs whose requirements its declared capabilities
cover and its key's allowlist allows, and with a team key only that team's
jobs. The claim is the embedded worker's compare-and-set, and the bundle is
the worker's too, so a job runs the same wherever it lands. Heartbeats extend
the lease. A job whose runner stops heartbeating is reaped by the server like
any other and is never queued again.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, func, insert, select, update

from armature.events import location
from armature.runner.process import BUNDLE_VERSION
from armature.server import audit, gates, rbac, worker
from armature.server.crypto import Keyring
from armature.server.db import new_id
from armature.server.db.schema import (
    environment_hosts,
    environment_types,
    environments,
    jobs,
    runner_keys,
    runners,
    runs,
    teams,
)
from armature.spec.models import CAPABILITY

log = logging.getLogger(__name__)

ONLINE_WITHIN = timedelta(minutes=2)
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:@-]{0,190}$")
MAX_CAPABILITIES = 64
MAX_LABELS = 32
LABEL_MAX = 191
MAX_SLOTS = 64
ERROR_MAX = 10_000
REPORTED = ("succeeded", "failed", "canceled", "suspended")


class RunnerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def claimed_by(runner_id: str) -> str:
    return f"runner:{runner_id}"


def capabilities(value: Any, what: str = "capabilities") -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CAPABILITIES:
        raise RunnerError(400, f"{what} is a list of at most {MAX_CAPABILITIES} capabilities.")
    for item in value:
        if not isinstance(item, str) or not CAPABILITY.fullmatch(item):
            raise RunnerError(400, f"{item!r} is not a capability. Use names like llm, git, or platform:ios.")
    return sorted(set(value))


def _text(value: Any, what: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RunnerError(400, f"{what} must be a string.")
    return "".join(ch for ch in value.strip() if ch.isprintable())[:limit] or None


def _labels(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_LABELS:
        raise RunnerError(400, f"labels is an object of at most {MAX_LABELS} names and values.")
    labels = {_text(k, "A label name", 64): _text(v, "A label value", LABEL_MAX) or "" for k, v in value.items()}
    return {k: v for k, v in labels.items() if k}


def _slots(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SLOTS:
        raise RunnerError(400, f"slots is a whole number from 1 to {MAX_SLOTS}.")
    return value


def hello(conn: Connection, *, key: Any, body: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Record a runner and what it can do, by key and name, so a restart keeps its row."""
    name = _text(body.get("name"), "name", 191)
    if name is None or not NAME.fullmatch(name):
        raise RunnerError(400, "name is 1 to 191 letters, digits, spaces, or . _ : @ -")
    values = {"capabilities": capabilities(body.get("capabilities")), "labels": _labels(body.get("labels")),
              "version": _text(body.get("version"), "version", 64), "host": _text(body.get("host"), "host", 191),
              "slots": _slots(body.get("slots")), "platform": location.parse(body.get("platform"), trusted=True),
              "state": "online", "last_seen_at": now}
    runner_id = conn.execute(select(runners.c.id).where(runners.c.runner_key_id == key.id,
                                                        runners.c.name == name)).scalar()
    if runner_id is None:
        runner_id = new_id()
        conn.execute(insert(runners).values(id=runner_id, runner_key_id=key.id, name=name, created_at=now, **values))
    else:
        conn.execute(update(runners).where(runners.c.id == runner_id).values(**values))
    conn.execute(update(environments).where(environments.c.runner_key_id == key.id,
                                            environments.c.state.in_(("requested", "booting")))
                 .values(state="ready", ready_at=now))
    allowed = values["capabilities"] if key.capabilities is None else sorted(
        set(values["capabilities"]) & set(key.capabilities))
    return {"runner_id": runner_id, "capabilities": allowed}


def _runner(conn: Connection, key: Any, runner_id: Any) -> Any:
    row = None if not isinstance(runner_id, str) else conn.execute(
        select(runners).where(runners.c.id == runner_id, runners.c.runner_key_id == key.id)).first()
    if row is None:
        raise RunnerError(404, "No such runner for this key. Say hello first.")
    return row


def _seen(conn: Connection, runner_id: str, now: datetime) -> None:
    conn.execute(update(runners).where(runners.c.id == runner_id).values(last_seen_at=now, state="online"))


def _fail(engine: Engine, job_id: str, error: str, now: datetime) -> None:
    with engine.begin() as conn:
        if conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(worker.HELD))
                        .values(status="failed", error=error, finished_at=now)).rowcount == 1:
            gates.fail_waiting(conn, job_id=job_id, error=error, now=now)


def run_location(conn: Connection, key: Any, runner: Any) -> dict[str, Any]:
    """Where a claimed job runs: the key's environment when it has one, else what the runner reported."""
    env = conn.execute(select(environments.c.provider_id, environments.c.instance_type,
                              environment_types.c.kind, environment_types.c.name,
                              environment_hosts.c.instance_id.label("host_instance"))
                       .select_from(environments
                                    .outerjoin(environment_types, environment_types.c.id == environments.c.type_id)
                                    .outerjoin(environment_hosts, environment_hosts.c.id == environments.c.host_id))
                       .where(environments.c.runner_key_id == key.id)
                       .order_by(environments.c.requested_at.desc()).limit(1)).first()
    if env is None:
        return location.parse(runner.platform, trusted=True) or location.make(location.UNKNOWN)
    mac = env.kind == "ec2-mac" or env.host_instance is not None
    return location.make(location.AWS_MAC if mac else location.AWS_LINUX,
                         instance=env.host_instance or env.provider_id, instance_type=env.instance_type,
                         environment_type=env.name)


def _bundle_version(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= BUNDLE_VERSION:
        raise RunnerError(400, f"bundle_version is a whole number from 1 to {BUNDLE_VERSION}, the newest this "
                               "server sends.")
    return value


def claim(engine: Engine, keyring: Keyring, *, key: Any, runner_id: Any, now: datetime, lease: timedelta,
          events: str, bundle_version: Any = None) -> worker.Bundle | None:
    """Claim the oldest job this runner may run and return its bundle. None when there is none.

    A job that can't be prepared fails, as in the embedded worker, and the next one is tried. An ephemeral key
    claims only the job its environment was launched for. A runner that reads call bundles has
    ``worker.CALLS_CAPABILITY``. One that sends no ``bundle_version`` predates them, so it gets no job that calls
    another workflow, not even one queued before jobs required the capability. One older than
    ``worker.GATES_BUNDLE_VERSION`` gets no job that may pause at a gate or resumes from one.
    """
    version = _bundle_version(bundle_version)
    reads_calls = version >= worker.CALLS_BUNDLE_VERSION
    pauses = version >= worker.GATES_BUNDLE_VERSION
    extra = [worker.CALLS_CAPABILITY] if reads_calls else []
    if version >= worker.ROLES_BUNDLE_VERSION:
        extra.append(worker.ROLES_CAPABILITY)
    bound = key.id if key.kind == "ephemeral" else None
    with engine.begin() as conn:
        runner = _runner(conn, key, runner_id)
        _seen(conn, runner.id, now)
        found = worker.claimable(conn, capabilities=[*(runner.capabilities or []), *extra],
                                 allowlist=None if key.capabilities is None else [*key.capabilities, *extra],
                                 team_id=key.team_id, bound_key=bound, now=now)
        if not reads_calls:
            found = [job_id for job_id in found if not worker.calls_workflows(conn, job_id)]
        if not pauses:
            found = [job_id for job_id in found if not worker.pauses(conn, job_id)]
    for job_id in found:
        with engine.begin() as conn:
            # A bound key's environment must still be coming up, and goes running with the claim or not at all.
            won = not bound or conn.execute(update(environments).where(
                environments.c.runner_key_id == key.id, environments.c.job_id == job_id,
                environments.c.state.in_(("requested", "booting", "ready"))).values(state="running")).rowcount == 1
            won = won and conn.execute(update(jobs).where(
                jobs.c.id == job_id, jobs.c.status == ("provisioning" if bound else "queued"),
                *([] if bound else worker.claim_guard(conn, job_id, now))).values(
                status="claimed", claimed_by=claimed_by(runner.id), runner_id=runner.id, claimed_at=now,
                heartbeat_at=now, lease_expires_at=now + lease, attempts=jobs.c.attempts + 1)).rowcount == 1
            if not won:
                conn.rollback()
        if not won:
            continue
        try:
            with engine.begin() as conn:
                job = conn.execute(select(jobs).where(jobs.c.id == job_id)).one()
                bundle = worker.build_bundle(conn, keyring, job, now=now, lease=lease, events=events,
                                             runner_key_id=key.id, instance_secrets=key.team_id is None,
                                             location=run_location(conn, key, runner))
                audit.record(conn, action="job.claim", actor_id=None, team_id=job.team_id, target_kind="job",
                             target_id=job_id, detail={"runner_id": runner.id, "runner_key_id": key.id,
                                                       "run_id": bundle.run_id, "secrets": sorted(bundle.secrets)})
            return bundle
        except worker.JobError as exc:
            _fail(engine, job_id, str(exc), now)
        except Exception:
            log.exception("preparing job %s for runner %s failed", job_id, runner.id)
            _fail(engine, job_id, "The server could not prepare this job.", now)
    return None


def _held_by_key(conn: Connection, key: Any, job_id: str) -> Any:
    row = conn.execute(select(jobs.c.id, jobs.c.status, jobs.c.run_id, jobs.c.runner_id, jobs.c.cancel_requested_at,
                              runs.c.status.label("run_status"), runs.c.error.label("run_error"))
                       .select_from(jobs.join(runners, runners.c.id == jobs.c.runner_id)
                                    .outerjoin(runs, runs.c.id == jobs.c.run_id))
                       .where(jobs.c.id == job_id, runners.c.runner_key_id == key.id)).first()
    if row is None:
        raise RunnerError(404, "No such job for this runner key.")
    return row


def heartbeat(engine: Engine, *, key: Any, job_id: str, now: datetime, lease: timedelta) -> dict[str, Any]:
    """Extend the lease of a job the key's runner holds. Says whether a user asked to cancel it."""
    with engine.begin() as conn:
        row = _held_by_key(conn, key, job_id)
        if row.status in worker.HELD:
            conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(worker.HELD))
                         .values(heartbeat_at=now, lease_expires_at=now + lease))
        _seen(conn, row.runner_id, now)
    return {"status": row.status, "cancel_requested": row.cancel_requested_at is not None, "run_id": row.run_id,
            "run_status": row.run_status}


def run_of(engine: Engine, *, key: Any, job_id: str) -> str | None:
    with engine.connect() as conn:
        return _held_by_key(conn, key, job_id).run_id


def finish(engine: Engine, keyring: Keyring, *, key: Any, job_id: str, body: dict[str, Any], now: datetime,
           live_status: str | None) -> str:
    """Settle a job from its run, or from the runner's report when the run never ended. Returns the job's status.

    The run's own record wins, so a runner can't report a failed run as succeeded. A run that paused at a gate is
    reported as suspended with the gate request its process left, and the job waits on the gate.
    """
    reported = body.get("status")
    if reported not in REPORTED:
        raise RunnerError(400, f"status must be one of {', '.join(REPORTED)}.")
    error = _text(body.get("error"), "error", ERROR_MAX)
    with engine.begin() as conn:
        row = _held_by_key(conn, key, job_id)
        _seen(conn, row.runner_id, now)
        if row.status not in worker.HELD:
            return row.status
    if reported == "suspended":
        return gates.settle_paused(engine, keyring, job_id=job_id, request=body.get("gate"), now=now)
    with engine.begin() as conn:
        row = _held_by_key(conn, key, job_id)
        if row.status not in worker.HELD:
            return row.status
        cancel_requested = row.cancel_requested_at is not None
        run_status = live_status or row.run_status
        if run_status == worker.RUN_WAITING:
            gates.abandon(conn, job_id=job_id, error="The run paused at a gate, and the runner didn't report it.",
                          now=now)
            return "failed"
        if run_status is not None and run_status not in worker.RUN_OPEN:
            status = worker.outcome(run_status, cancel_requested)
            error = None if status == "succeeded" else row.run_error or error
        else:
            status = "canceled" if cancel_requested and reported == "canceled" else "failed"
            error = error or "The run ended without reporting how."
        conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(worker.HELD))
                     .values(status=status, error=error, finished_at=now))
    return status


def cancel(conn: Connection, *, job: Any, actor_id: str, now: datetime, ip: str | None) -> dict[str, Any]:
    """Cancel a queued job now, or ask the runner holding one to stop it at its next heartbeat."""
    status = job.status
    for _ in range(2):
        if status in ("queued", "provisioning", "suspended"):
            # A provisioning job has no runner yet, so it ends now and the reaper tears its environment down.
            # A suspended job, or a queued one resuming from a gate, takes its waiting run with it.
            if conn.execute(update(jobs).where(jobs.c.id == job.id, jobs.c.status == status).values(
                    status="canceled", cancel_requested_at=now, finished_at=now)).rowcount == 1:
                if job.run_id is not None and (status == "suspended" or job.resume_of_gate is not None):
                    gates.cancel_run(conn, run_id=job.run_id, actor_id=actor_id, now=now)
                break
            status = conn.execute(select(jobs.c.status).where(jobs.c.id == job.id)).scalar()
        elif status in worker.HELD:
            conn.execute(update(jobs).where(jobs.c.id == job.id, jobs.c.cancel_requested_at.is_(None))
                         .values(cancel_requested_at=now))
            break
        else:
            raise RunnerError(409, "This job has already finished.")
    audit.record(conn, action="job.cancel", actor_id=actor_id, team_id=job.team_id, target_kind="job",
                 target_id=job.id, detail={"status": status, "run_id": job.run_id}, ip=ip)
    row = conn.execute(select(jobs.c.id, jobs.c.status, jobs.c.cancel_requested_at).where(jobs.c.id == job.id)).one()
    return dict(row._mapping)


def job_target(conn: Connection, job_id: str) -> rbac.Target:
    row = conn.execute(select(jobs.c.team_id, jobs.c.project_id).where(jobs.c.id == job_id)).first()
    if row is None:
        raise rbac.NotFound("No such job.")
    return rbac.Target(team_id=row.team_id, project_id=row.project_id)


def list_runners(conn: Connection, now: datetime) -> list[dict[str, Any]]:
    held = (select(jobs.c.runner_id, func.count().label("running")).where(jobs.c.status.in_(worker.HELD))
            .group_by(jobs.c.runner_id).subquery())
    rows = conn.execute(
        select(runners, runner_keys.c.name.label("key_name"), runner_keys.c.revoked_at, teams.c.name.label("team"),
               func.coalesce(held.c.running, 0).label("running"))
        .select_from(runners.join(runner_keys, runner_keys.c.id == runners.c.runner_key_id)
                     .outerjoin(teams, teams.c.id == runner_keys.c.team_id)
                     .outerjoin(held, held.c.runner_id == runners.c.id))
        .order_by(runners.c.last_seen_at.desc(), runners.c.name)).all()
    return [{"id": r.id, "name": r.name, "key": r.key_name, "team": r.team, "capabilities": r.capabilities or [],
             "labels": r.labels or {}, "version": r.version, "host": r.host, "slots": r.slots,
             "running": r.running, "last_seen_at": r.last_seen_at,
             "same_user": isinstance(r.platform, dict) and r.platform.get("same_user") is True,
             "online": r.revoked_at is None and now - r.last_seen_at < ONLINE_WITHIN} for r in rows]

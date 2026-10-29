"""The job worker: claims queued jobs and runs each one as a workflow run the server watches live.

A claim is one compare-and-set UPDATE, so servers sharing a database never
start the same job twice. A claim holds a lease that the worker extends while
the run's process lives. A job whose lease runs out is settled from its run
if the run ended, else marked lost. It is never queued again, so no run is
paid for twice.
"""
from __future__ import annotations

import logging
import os
import re
import secrets as _random
import shutil
import socket
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, func, select, update

from armature.packs import PACKS_PATH_ENV
from armature.server import defs, dryrun, runners
from armature.server import secrets as secret_store
from armature.server.config import ServerConfig
from armature.server.crypto import Keyring
from armature.server.db import utcnow
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    jobs,
    runs,
    settings,
    triggers,
    workflows,
)
from armature.server.ingest import load_summary

log = logging.getLogger(__name__)

INTERVAL_S = 2.0
LEASE = timedelta(minutes=2)
WORK_DIR = "~/.armature/work"
SLOTS_KEY = "worker.slots"
ENABLED_KEY = "worker.enabled"
DEFAULT_SLOTS = 2
ENV_PREFIX = "env."
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TRIGGER_KIND = "webhook"
HELD = ("claimed", "running")
# A lost run can still send events and end, as dryrun.ended treats it.
RUN_OPEN = ("queued", "running", "lost")
STOP_TIMEOUT_S = 30.0


class JobError(Exception):
    pass


def outcome(run_status: str) -> str:
    return "succeeded" if run_status == "succeeded" else "failed"


def team_env(conn: Connection, team_id: str) -> dict[str, str]:
    """The ``env.NAME`` settings as NAME, instance first so a team's own setting wins."""
    rows = conn.execute(select(settings.c.scope, settings.c.key, settings.c.value).where(
        settings.c.scope.in_([team_id, INSTANCE_SCOPE]), settings.c.key.startswith(ENV_PREFIX, autoescape=True))).all()
    env = {}
    for row in sorted(rows, key=lambda r: r.scope != INSTANCE_SCOPE):
        name = row.key[len(ENV_PREFIX):]
        if ENV_NAME.fullmatch(name) and isinstance(row.value, str):
            env[name] = row.value
    return env


def _instance_setting(conn: Connection, key: str) -> Any:
    return conn.execute(select(settings.c.value).where(settings.c.scope == INSTANCE_SCOPE,
                                                       settings.c.key == key)).scalar()


class Worker:
    """Polls every ``interval`` seconds while started. Tests leave it stopped and call ``tick`` themselves."""

    def __init__(self, engine: Engine, keyring: Keyring, cfg: ServerConfig, hub: Any, *,
                 clock: Callable[[], datetime] = utcnow, interval: float = INTERVAL_S, lease: timedelta = LEASE,
                 work_dir: str | Path = WORK_DIR, worker_id: str | None = None):
        self.engine = engine
        self.keyring = keyring
        self.cfg = cfg
        self.hub = hub
        self.interval = interval
        self.lease = lease
        self.work_dir = Path(work_dir).expanduser()
        self.id = worker_id or f"{socket.gethostname()[:150]}:{os.getpid()}:{_random.token_hex(4)}"
        self._clock = clock
        self._procs: dict[str, subprocess.Popen] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Settle what an earlier server process left behind, then start polling."""
        self.reap(self._clock())
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="armature-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        # Runs start in their own session, so nothing else stops them, and a later reap would mark them lost.
        for proc in list(self._procs.values()):
            dryrun._stop(proc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("job worker tick failed")
            self._stop.wait(self.interval)

    def tick(self) -> None:
        now = self._clock()
        self.heartbeat(now)
        self.reap(now)
        self.finish(now)
        with self.engine.connect() as conn:
            enabled = _instance_setting(conn, ENABLED_KEY)
            slots = _instance_setting(conn, SLOTS_KEY)
        if enabled is False:
            return
        if not isinstance(slots, int) or isinstance(slots, bool) or slots < 0:
            slots = DEFAULT_SLOTS
        while self.held() < slots and not self._stop.is_set():
            job_id = self.claim(self._clock())
            if job_id is None:
                return
            self.launch(job_id)

    def held(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(jobs).where(
                jobs.c.claimed_by == self.id, jobs.c.status.in_(HELD))).scalar_one()

    def claim(self, now: datetime) -> str | None:
        """Claim the oldest queued job. None when nothing is queued or another worker took it first."""
        with self.engine.connect() as conn:
            job_id = conn.execute(select(jobs.c.id).where(jobs.c.status == "queued")
                                  .order_by(jobs.c.created_at, jobs.c.id).limit(1)).scalar()
        return job_id if job_id is not None and self.try_claim(job_id, now) else None

    def try_claim(self, job_id: str, now: datetime) -> bool:
        with self.engine.begin() as conn:
            return conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status == "queued").values(
                status="claimed", claimed_by=self.id, claimed_at=now, heartbeat_at=now,
                lease_expires_at=now + self.lease, attempts=jobs.c.attempts + 1)).rowcount == 1

    def heartbeat(self, now: datetime) -> None:
        for job_id in [j for j, proc in self._procs.items() if proc.poll() is not None]:
            del self._procs[job_id]
        if not self._procs:
            return
        with self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id.in_(list(self._procs)), jobs.c.claimed_by == self.id,
                                            jobs.c.status == "running")
                         .values(heartbeat_at=now, lease_expires_at=now + self.lease))

    def reap(self, now: datetime) -> int:
        """Settle every held job whose lease ran out, from its run when it ended, else as lost."""
        with self.engine.begin() as conn:
            rows = conn.execute(select(jobs.c.id, jobs.c.status, runs.c.status.label("run_status"),
                                       runs.c.error.label("run_error"))
                                .select_from(jobs.outerjoin(runs, runs.c.id == jobs.c.run_id))
                                .where(jobs.c.status.in_(HELD), jobs.c.lease_expires_at < now)).all()
            for row in rows:
                if row.run_status is not None and row.run_status not in RUN_OPEN:
                    values = {"status": outcome(row.run_status), "error": row.run_error}
                else:
                    values = {"status": "lost", "error": "lease expired"}
                conn.execute(update(jobs).where(jobs.c.id == row.id, jobs.c.status == row.status,
                                                jobs.c.lease_expires_at < now).values(finished_at=now, **values))
        return len(rows)

    def finish(self, now: datetime) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(select(jobs.c.id, runs.c.status, runs.c.error).join(runs, runs.c.id == jobs.c.run_id)
                                .where(jobs.c.status == "running", runs.c.status.not_in(RUN_OPEN))).all()
            for row in rows:
                conn.execute(update(jobs).where(jobs.c.id == row.id, jobs.c.status == "running").values(
                    status=outcome(row.status), error=row.error, finished_at=now))

    def _fail(self, job_id: str, error: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(HELD))
                         .values(status="failed", error=error, finished_at=self._clock()))

    def launch(self, job_id: str) -> None:
        """Start a job this worker claimed. Any problem before the process starts fails the job instead."""
        workdir = self.work_dir / job_id
        binary = dryrun.armature_binary()
        try:
            with self.engine.begin() as conn:
                job = conn.execute(select(jobs).where(jobs.c.id == job_id, jobs.c.claimed_by == self.id,
                                                      jobs.c.status == "claimed")).first()
                if job is None:
                    return
                launch, args, env = self._prepare(conn, job, binary, workdir)
        except JobError as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            self._fail(job_id, str(exc))
            return
        except Exception:
            log.exception("preparing job %s failed", job_id)
            shutil.rmtree(workdir, ignore_errors=True)
            self._fail(job_id, "The worker could not start this job.")
            return
        try:
            self._procs[job_id] = dryrun.spawn(binary, launch, args, env, self.hub, label="run")
        except OSError as exc:
            self._fail(job_id, f"The run could not start: {exc.strerror or exc}.")
            return
        loop = getattr(self.hub, "_loop", None)
        if loop is not None and self.hub.running:
            try:
                loop.call_soon_threadsafe(self.hub.run_started, launch.summary)
            except RuntimeError:
                log.warning("live view missed the start of job %s: the event loop is closed", job_id)

    def _prepare(self, conn: Connection, job: Any, binary: str | None,
                 workdir: Path) -> tuple[dryrun.Launch, list[str], dict[str, str]]:
        if binary is None:
            raise JobError("armature executable not found")
        workflow = conn.execute(select(workflows.c.id, workflows.c.name)
                                .where(workflows.c.id == job.workflow_id)).first()
        if workflow is None:
            raise JobError("The workflow no longer exists.")
        body = None if job.workflow_version is None else defs.body_of(conn, defs.WORKFLOW, workflow.id,
                                                                      job.workflow_version)
        if body is None:
            raise JobError(f"Workflow {workflow.name} has no version {job.workflow_version}.")
        trigger = None if job.trigger_id is None else conn.execute(
            select(triggers.c.config, triggers.c.webhook_id).where(triggers.c.id == job.trigger_id)).first()
        names = ((trigger.config or {}) if trigger is not None else {}).get("secrets") or []
        try:
            secret_values = secret_store.resolve_for_run(conn, self.keyring, job.team_id, list(names))
        except secret_store.MissingSecrets as exc:
            raise JobError(f"missing secret {', '.join(exc.args[0])}") from exc
        try:
            args = dryrun.input_args(job.inputs)
        except dryrun.DryRunError as exc:
            raise JobError(exc.message) from exc
        workdir.mkdir(parents=True, exist_ok=True)
        dryrun.write_workflow(workdir, body)
        trigger_ref = {key: value for key, value in (("webhook_id", trigger.webhook_id if trigger else None),
                                                     ("delivery_id", job.delivery_id),
                                                     ("trigger_id", job.trigger_id)) if value is not None}
        now = self._clock()
        run_row, token = runners.open_run(conn, self.keyring, team_id=job.team_id, project_id=job.project_id,
                                          workflow_id=workflow.id, workflow_name=workflow.name, now=now,
                                          workflow_version=job.workflow_version, triggered_by=None, actor=None,
                                          trigger_kind=TRIGGER_KIND, trigger_ref=trigger_ref)
        conn.execute(update(jobs).where(jobs.c.id == job.id).values(
            run_id=run_row["id"], status="running", heartbeat_at=now, lease_expires_at=now + self.lease))
        launch = dryrun.Launch(run_row["id"], token, workdir, load_summary(conn, run_row["id"]))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME") or str(Path.home()),
               PACKS_PATH_ENV: dryrun.packs_path(self.cfg.packs_dir), **team_env(conn, job.team_id), **secret_values,
               "ARMATURE_EVENTS": dryrun.events_endpoint(self.hub, self.cfg), "ARMATURE_EVENTS_TOKEN": token}
        return launch, args, env

"""Test runs of a stored workflow in stub mode, which replays recorded step outputs instead of running anything.

The server registers the run itself, writes the workflow and its stub outputs
to a temporary directory, and starts ``armature run`` there with a clean
environment. The run streams events like any other, so its page shows it
live. A watcher ends the run if the process exits without saying so, and
removes the directory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.events.location import this_machine
from armature.packs import PACKS_PATH_ENV
from armature.runner.process import LOG_FILE, popen, write_bundle
from armature.runner.process import armature_binary as armature_binary
from armature.runner.process import stop as _stop
from armature.runner.process import tail as _tail
from armature.server import audit, calls, defs, rbac, runners
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    run_steps,
    runs,
    settings,
    workflows,
)
from armature.server.ingest import load_summary
from armature.spec.loader import STORED_WORKFLOW_HEADER

log = logging.getLogger(__name__)

MODES = ("stub", "dry")
MISSING = ("fail", "empty")
TRIGGER_KIND = "test"
MAX_CONCURRENT = 2
TIMEOUT_S = 600
END_GRACE_S = 1.0
END_WAIT_S = 10.0
LOCK_KEY = "_lock.dry_run"
OUTPUTS_MAX = 256 * 1024
INPUT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ACTIVE = ("queued", "running")


class DryRunError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Launch:
    run_id: str
    token: str
    workdir: Path
    summary: dict[str, Any]
    # The run's engine key, which spawn hands over on a pipe.
    engine_key: str | None = None


def _team(conn: Connection, access: rbac.Access, row: Any, team_id: Any) -> str:
    if row.team_id is not None:
        return row.team_id
    if not isinstance(team_id, str) or not team_id:
        raise DryRunError(400, "Pick the team this test run belongs to.")
    try:
        rbac.team_target(conn, team_id)
    except rbac.NotFound as exc:
        raise DryRunError(404, "Not found.") from exc
    return team_id


def _recorded(conn: Connection, access: rbac.Access, workflow_name: str, run_id: Any) -> dict[str, Any]:
    if run_id is None:
        return {}
    if not isinstance(run_id, str):
        raise DryRunError(400, "from_run is a run id.")
    found = conn.execute(select(runs.c.team_id, runs.c.project_id,
                                func.coalesce(workflows.c.name, runs.c.workflow_name).label("workflow"))
                         .select_from(runs.outerjoin(workflows, workflows.c.id == runs.c.workflow_id))
                         .where(runs.c.id == run_id)).first()
    if found is None or rbac.role_for(access, rbac.Target(found.team_id, found.project_id)) is None:
        raise DryRunError(404, "No such run.")
    if found.workflow != workflow_name:
        raise DryRunError(400, f"That run is of {found.workflow}, not {workflow_name}.")
    rows = conn.execute(select(run_steps.c.step_id, run_steps.c.output).where(
        run_steps.c.run_id == run_id, run_steps.c.parent_step.is_(None), run_steps.c.output.is_not(None))).all()
    return {r.step_id: r.output for r in rows}


def _outputs(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and k for k in value):
        raise DryRunError(400, "outputs maps a step id to its output.")
    if len(json.dumps(value, default=str)) > OUTPUTS_MAX:
        raise DryRunError(400, "outputs is larger than 256 KB.")
    return value


def input_args(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise DryRunError(400, "inputs is a JSON object.")
    args = []
    for name, item in value.items():
        if not isinstance(name, str) or not INPUT_NAME.fullmatch(name):
            raise DryRunError(400, f"Input name {name!r} is not valid.")
        args += ["--input", f"{name}={item if isinstance(item, str) else json.dumps(item)}"]
    return args


def ensure_lock_row(engine: Engine) -> None:
    try:
        with engine.begin() as conn:
            if conn.execute(select(settings.c.key).where(settings.c.scope == INSTANCE_SCOPE,
                                                         settings.c.key == LOCK_KEY)).first() is None:
                conn.execute(insert(settings).values(team_id=None, scope=INSTANCE_SCOPE, key=LOCK_KEY, value=None))
    except IntegrityError:
        pass


# The first statement of the transaction, so the counts after it see every committed test run on all three databases.
def _lock(conn: Connection, now: Any) -> None:
    conn.execute(update(settings).where(settings.c.scope == INSTANCE_SCOPE, settings.c.key == LOCK_KEY)
                 .values(updated_at=now))


def _active(conn: Connection, *where: Any) -> int:
    return conn.execute(select(func.count()).select_from(runs).where(
        runs.c.trigger_kind == TRIGGER_KIND, runs.c.status.in_(ACTIVE), *where)).scalar_one()


def check_mode(mode: Any) -> None:
    if mode == "dry":
        raise DryRunError(400, "needs a runner; coming with the job worker")
    if mode != "stub":
        raise DryRunError(400, f"mode must be one of {', '.join(MODES)}.")


def prepare(conn: Connection, keyring: Any, *, access: rbac.Access, user_id: str, username: str, workflow_id: str,
            payload: dict[str, Any], now: Any, ip: str | None, max_active: int) -> tuple[Launch, list[str], bool]:
    """Check everything, write the temporary directory, and register the run. Returns it, the args, and missing."""
    _lock(conn, now)
    check_mode(payload.get("mode"))
    missing = payload.get("missing") or "fail"
    if missing not in MISSING:
        raise DryRunError(400, f"missing must be one of {', '.join(MISSING)}.")
    try:
        row = defs.readable_row(conn, defs.WORKFLOW, workflow_id, access)
    except defs.DefError as exc:
        raise DryRunError(exc.status, exc.message) from exc
    team_id = _team(conn, access, row, payload.get("team_id"))
    if not rbac.at_least(rbac.role_for(access, rbac.Target(team_id, row.project_id)), "contributor"):
        raise DryRunError(403, "Not allowed.")
    if _active(conn, runs.c.triggered_by == user_id) >= MAX_CONCURRENT:
        raise DryRunError(429, f"You already have {MAX_CONCURRENT} test runs going. Wait for one to finish.")
    if _active(conn) >= max_active:
        raise DryRunError(429, "This server is running as many test runs as it allows. Try again shortly.")
    stubs = {**_recorded(conn, access, row.name, payload.get("from_run")), **_outputs(payload.get("outputs"))}
    args = input_args(payload.get("inputs"))
    body = defs.body_of(conn, defs.WORKFLOW, row.id, row.current_version)
    if body is None:
        raise DryRunError(400, "This workflow has no saved version.")
    try:
        call_files = calls.bundle(conn, row.scope, body, row.name, None, stored_body)
    except ValueError as exc:
        raise DryRunError(400, f"A called workflow doesn't resolve: {exc}") from exc
    workdir = Path(tempfile.mkdtemp(prefix="armature-test-"))
    try:
        write_bundle(workdir, stored_body(body), call_files)
        (workdir / "stubs.json").write_text(json.dumps(stubs, default=str), encoding="utf-8")
        trigger_ref = {"dry_run": True, "mode": "stub", "from_run": payload.get("from_run")}
        run_row, token = runners.open_run(conn, keyring, team_id=team_id, project_id=row.project_id,
                                          workflow_id=row.id, workflow_name=row.name, now=now,
                                          workflow_version=row.current_version, triggered_by=user_id,
                                          actor=username, trigger_kind=TRIGGER_KIND, trigger_ref=trigger_ref,
                                          location=this_machine())
        audit.record(conn, action="run.test", actor_id=user_id, team_id=team_id, target_kind="run",
                     target_id=run_row["id"], detail={"workflow": row.name, "workflow_id": row.id, "mode": "stub",
                                                      "from_run": payload.get("from_run"),
                                                      "stubbed_steps": sorted(stubs)}, ip=ip)
        summary = load_summary(conn, run_row["id"])
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return Launch(run_row["id"], token, workdir, summary), args, missing == "empty"


def stored_body(body: str) -> str:
    """The body with the stored-workflow header first, so ``armature run`` parses it as one."""
    header = "" if body.split("\n", 1)[0].rstrip("\r") == STORED_WORKFLOW_HEADER else STORED_WORKFLOW_HEADER + "\n"
    return header + body


def events_endpoint(hub: Any, cfg: Any) -> str:
    if hub.unix_endpoint:
        return f"unix:{hub.unix_endpoint}"
    return f"{cfg.public_origin}{cfg.base_path}/api/events"


def packs_path(packs_dir: str) -> str:
    return os.pathsep.join(p for p in (os.environ.get(PACKS_PATH_ENV, ""), str(Path(packs_dir).expanduser())) if p)


def child_env(launch: Launch, endpoint: str, packs_dir: str, missing_empty: bool) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(launch.workdir),
           "ARMATURE_STUB_OUTPUTS": str(launch.workdir / "stubs.json"), "ARMATURE_EVENTS": endpoint,
           "ARMATURE_EVENTS_TOKEN": launch.token, PACKS_PATH_ENV: packs_path(packs_dir)}
    if missing_empty:
        env["ARMATURE_STUB_DEFAULT"] = "empty"
    return env


def _end_line(token: str, error: str, status: str = "failed") -> bytes:
    return json.dumps({"v": 1, "token": token, "kind": "run_end",
                       "payload": {"status": status, "error": error}}).encode()


def fail_run(hub: Any, launch: Launch, error: str, loop: asyncio.AbstractEventLoop | None,
             status: str = "failed") -> None:
    """End the run as failed unless it already ended. Through ingest when it runs, so the page updates live."""
    if loop is not None and hub.running:
        asyncio.run_coroutine_threadsafe(hub.ingest(_end_line(launch.token, error, status)), loop).result(30)
        return
    with hub.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == launch.run_id, runs.c.status.in_(ACTIVE))
                     .values(status=status, finished_at=hub.clock(), error=error))
        runners.revoke_tokens(conn, [launch.run_id])


async def _summary(hub: Any, run_id: str) -> dict[str, Any] | None:
    return hub.summary(run_id)


def ended(hub: Any, run_id: str, loop: asyncio.AbstractEventLoop | None) -> bool:
    if loop is not None and hub.running:
        summary = asyncio.run_coroutine_threadsafe(_summary(hub, run_id), loop).result(10)
        if summary is not None and summary["status"] not in (*ACTIVE, "lost"):
            return True
    with hub.engine.connect() as conn:
        status = conn.execute(select(runs.c.status).where(runs.c.id == run_id)).scalar()
    return status not in (*ACTIVE, "lost")


def watch(proc: subprocess.Popen, launch: Launch, hub: Any, loop: asyncio.AbstractEventLoop | None,
          timeout: float | None = None, label: str = "test run", paused: Callable[[], bool] | None = None) -> None:
    """Wait for the process, end the run if it never sent run_end, and remove the directory.

    ``paused`` settles a run that stopped at a gate and says whether it did, before the directory goes.
    """
    try:
        try:
            proc.wait(timeout)
            error = f"The {label} exited with status {proc.returncode} without finishing."
        except subprocess.TimeoutExpired:
            _stop(proc)
            error = f"The {label} took longer than {timeout:g} seconds and was stopped."
        else:
            if paused is not None and paused():
                return
        tail = _tail(launch.workdir / LOG_FILE)
        if tail:
            error += "\n" + tail
        # A run_end the process sent may still sit in the socket, so give it time to land first.
        deadline = time.monotonic() + (END_WAIT_S if proc.returncode == 0 else END_GRACE_S)
        while not ended(hub, launch.run_id, loop) and time.monotonic() < deadline:
            time.sleep(0.2)
        if not ended(hub, launch.run_id, loop):
            fail_run(hub, launch, error, loop)
    except Exception:
        log.exception("watching %s %s failed", label, launch.run_id)
    finally:
        shutil.rmtree(launch.workdir, ignore_errors=True)


def spawn(binary: str, launch: Launch, args: list[str], env: dict[str, str], hub: Any, *,
          timeout: float | None = None, label: str = "test run", memory_mb: int | None = None,
          paused: Callable[[], bool] | None = None) -> subprocess.Popen:
    """Start ``armature run`` on the workdir's workflow.yaml and a thread that watches it. Fails the run on OSError."""
    loop = getattr(hub, "_loop", None)
    try:
        proc = popen(binary, launch.workdir, args, env, engine_key=launch.engine_key, memory_mb=memory_mb)
    except OSError as exc:
        fail_run(hub, launch, f"The {label} could not start: {exc.strerror or exc}.", None)
        shutil.rmtree(launch.workdir, ignore_errors=True)
        raise
    threading.Thread(target=watch, args=(proc, launch, hub, loop, timeout, label, paused),
                     name=f"armature-{label.replace(' ', '-')}-{launch.run_id}", daemon=True).start()
    return proc


def start(binary: str, launch: Launch, args: list[str], env: dict[str, str], hub: Any, *,
          memory_mb: int | None = None) -> None:
    try:
        spawn(binary, launch, args, env, hub, timeout=TIMEOUT_S, memory_mb=memory_mb)
    except OSError as exc:
        raise DryRunError(503, "The test run could not start.") from exc

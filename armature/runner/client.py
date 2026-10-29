"""The runner loop: say hello, claim jobs, run each as ``armature run``, heartbeat, and report how it ended.

Each job runs from a private work directory that is removed when the job
ends. Its secrets reach the run only through the run's environment. When the
run's process exits without ending its run, the runner ends it, as the
server's own worker does, and a canceled job's run is ended as canceled
before its process is stopped, so the run never reads as failed.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from armature.events.location import this_machine
from armature.nodes.gate import GATE_DIR_ENV, GATE_FILE, read_gate
from armature.nodes.harness import (
    AGENT_DIR_ENV,
    AGENT_USER_ENV,
    HOST_ONLY_ENV,
    ROLES_ENV,
    SAME_USER_ENV,
    SESSIONS_ENV,
    SKILLS_ENV,
)
from armature.packs import PACKS_PATH_ENV
from armature.packs.tool import URL_ENV as TOOLS_URL_ENV
from armature.runner.process import (
    AGENT_PREFIX,
    BUNDLE_VERSION,
    LOG_FILE,
    ROLES_DIR,
    SESSIONS_DIR,
    SKILLS_DIR,
    check_bundle_version,
    popen,
    share,
    stop,
    stop_sessions,
    tail,
    unshare,
    write_bundle,
)

log = logging.getLogger(__name__)

POLL_S = 5.0
HEARTBEAT_S = 30.0
WAIT_STEP_S = 1.0
END_WAIT_S = 10.0
END_GRACE_S = 1.0
END_POLL_S = 0.5
HTTP_TIMEOUT_S = 30.0
OPEN = ("queued", "running", "lost")
JOB_PREFIX = "job-"
LOCK_FILE = ".lock"
HELD = ("provisioning", "claimed", "running")


class RunnerLocked(Exception):
    """Another runner with this name already uses this work directory."""


class ServerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class Client:
    """The runner API over HTTP, with the runner key as the bearer token."""

    def __init__(self, server: str, key: str, *, transport: httpx.BaseTransport | None = None):
        self.server = server.rstrip("/")
        self._key = key
        self._http = httpx.Client(timeout=HTTP_TIMEOUT_S, transport=transport, trust_env=False)

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """POST to the server. None for 204. Raises ServerError for an error status or no answer."""
        try:
            response = self._http.post(f"{self.server}{path}", json=body or {},
                                       headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as exc:
            raise ServerError(0, str(exc) or exc.__class__.__name__) from exc
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            try:
                message = response.json().get("error") or response.reason_phrase
            except ValueError:
                message = response.reason_phrase
            raise ServerError(response.status_code, message)
        return response.json()

    def end_run(self, events_url: str, token: str, status: str, error: str) -> None:
        """Post the run's run_end with its own token, as the run would have."""
        line = json.dumps({"v": 1, "token": token, "kind": "run_end", "payload": {"status": status, "error": error}})
        try:
            self._http.post(events_url, content=line.encode() + b"\n",
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"})
        except httpx.HTTPError as exc:
            log.warning("could not end run: %s", exc)

    def close(self) -> None:
        self._http.close()


def enroll(server: str, token: str, name: str, *, transport: httpx.BaseTransport | None = None) -> str:
    """Trade a one-time enrollment token for this environment's runner key."""
    with httpx.Client(timeout=HTTP_TIMEOUT_S, transport=transport, trust_env=False) as http:
        try:
            response = http.post(f"{server.rstrip('/')}/api/runner/enroll", json={"token": token, "name": name})
        except httpx.HTTPError as exc:
            raise ServerError(0, str(exc) or exc.__class__.__name__) from exc
    if response.status_code >= 400:
        try:
            message = response.json().get("error") or response.reason_phrase
        except ValueError:
            message = response.reason_phrase
        raise ServerError(response.status_code, message)
    return response.json()["key"]


class Runner:
    def __init__(self, client: Client, *, name: str, capabilities: list[str], slots: int, work_dir: Path,
                 binary: str, labels: dict[str, str] | None = None, version: str | None = None,
                 host: str | None = None, poll: float = POLL_S, heartbeat: float = HEARTBEAT_S,
                 clock: Callable[[], float] = time.monotonic, job_user: str | None = None,
                 agent_user: str | None = None, same_user: bool = False, memory_mb: int = 0):
        self.client = client
        self.name = name
        self.capabilities = capabilities
        self.slots = slots
        self.work_dir = work_dir
        self.binary = binary
        self.labels = labels or {}
        self.version = version
        self.host = host
        self.poll = poll
        self.heartbeat = heartbeat
        self._clock = clock
        self.job_user = job_user
        self.agent_user = agent_user
        self.same_user = same_user
        self.memory_mb = memory_mb
        self.id: str | None = None
        self.allowed: list[str] = []
        self._jobs: dict[str, threading.Thread] = {}
        self._stopping = threading.Event()

    def hello(self) -> None:
        answer = self.client.post("/api/runner/hello", {
            "name": self.name, "capabilities": self.capabilities, "labels": self.labels, "slots": self.slots,
            "version": self.version, "host": self.host, "platform": this_machine(self.same_user)}) or {}
        self.id = answer["runner_id"]
        self.allowed = answer.get("capabilities") or []

    @property
    def home(self) -> Path:
        """This runner's own directory, so runners sharing a work directory never touch each other's jobs."""
        return self.work_dir / self.name

    def _lock(self) -> Any:
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = open(self.home / LOCK_FILE, "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RunnerLocked(f"A runner named {self.name} is already running from {self.home}. "
                               "Stop it, or start this one with another --name.") from None
        return lock

    def run(self, stop_event: threading.Event) -> None:
        """Claim and run jobs until ``stop_event`` is set, then stop the runs still going and report them.

        Raises RunnerLocked when a runner with this name already runs from this work directory.
        """
        lock = self._lock()
        # A runner that died mid-job left its bundles here, secrets included.
        for leftover in self.home.glob(f"{JOB_PREFIX}*"):
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover, ignore_errors=True)
        try:
            while not stop_event.is_set():
                try:
                    if self.id is None:
                        self.hello()
                        log.info("runner %s ready with %s", self.name, ", ".join(self.allowed) or "no capabilities")
                    self._fill()
                except ServerError as exc:
                    if exc.status == 404:
                        self.id = None
                    log.warning("server: %s", exc.message)
                stop_event.wait(self.poll)
        finally:
            self._stopping.set()
            for thread in list(self._jobs.values()):
                thread.join()
            lock.close()

    def _fill(self) -> None:
        for job_id in [j for j, t in self._jobs.items() if not t.is_alive()]:
            del self._jobs[job_id]
        while len(self._jobs) < self.slots and not self._stopping.is_set():
            answer = self.client.post("/api/runner/claim", {"runner_id": self.id, "bundle_version": BUNDLE_VERSION})
            if answer is None:
                return
            bundle = answer["job"]
            thread = threading.Thread(target=self.run_job, args=(bundle,), name=f"armature-job-{bundle['job_id']}",
                                      daemon=True)
            self._jobs[bundle["job_id"]] = thread
            thread.start()

    def _beat(self, job_id: str) -> dict[str, Any] | None:
        try:
            return self.client.post(f"/api/runner/jobs/{job_id}/heartbeat")
        except ServerError as exc:
            log.warning("heartbeat for job %s failed: %s", job_id, exc.message)
            return None

    def _finish(self, job_id: str, status: str, error: str | None, paused: dict[str, Any] | None = None) -> None:
        body = {"status": status, "error": error, **(paused or {})}
        try:
            self.client.post(f"/api/runner/jobs/{job_id}/finish", body)
        except ServerError as exc:
            log.warning("reporting job %s failed: %s", job_id, exc.message)

    def env(self, bundle: dict[str, Any]) -> dict[str, str]:
        # The host's PATH, HOME, and packs path go last, so no setting or secret replaces them.
        env = {**bundle["settings"], **bundle["secrets"], "ARMATURE_EVENTS": bundle["events_url"],
               "ARMATURE_EVENTS_TOKEN": bundle["run_token"], "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": os.environ.get("HOME") or str(Path.home())}
        if bundle.get("thread"):
            env["ARMATURE_THREAD"] = bundle["thread"]
        for name in (PACKS_PATH_ENV, *HOST_ONLY_ENV):
            env.pop(name, None)
        if os.environ.get(PACKS_PATH_ENV):
            env[PACKS_PATH_ENV] = os.environ[PACKS_PATH_ENV]
        env[TOOLS_URL_ENV] = self.client.server
        return env

    def run_job(self, bundle: dict[str, Any]) -> None:
        job_id = bundle["job_id"]
        workdir = self.home / f"{JOB_PREFIX}{job_id}"
        run_dir = agent_dir = gate_dir = None
        try:
            try:
                check_bundle_version(bundle.get("bundle_version"))
                write_bundle(workdir, bundle["workflow"]["body"], bundle.get("calls") or {}, bundle.get("skills"),
                             bundle.get("checkpoint"), bundle.get("roles"))
                env = self.env(bundle)
                if self.job_user:
                    run_dir = share(workdir, job_id, self.job_user)
                    env["HOME"] = str(run_dir)
                if bundle.get("checkpoint") is not None:
                    gate_dir = run_dir or workdir
                    env[GATE_DIR_ENV] = str(gate_dir)
                if bundle.get("skills"):
                    env[SKILLS_ENV] = str((run_dir or workdir) / SKILLS_DIR)
                if bundle.get("roles"):
                    env[ROLES_ENV] = str((run_dir or workdir) / ROLES_DIR)
                if self.agent_user:
                    agent_dir = Path(tempfile.gettempdir()) / f"{AGENT_PREFIX}{job_id}"
                    env.update({AGENT_USER_ENV: self.agent_user, AGENT_DIR_ENV: str(agent_dir)})
                elif self.same_user:
                    env.update({SAME_USER_ENV: "1", SESSIONS_ENV: str(workdir / SESSIONS_DIR)})
                proc = popen(self.binary, workdir, list(bundle["args"]), env, job_user=self.job_user,
                             engine_key=bundle.get("engine_key"),
                             run_dir=run_dir, memory_mb=self.memory_mb or None)
            except (OSError, ValueError, KeyError) as exc:
                error = f"The run could not start: {getattr(exc, 'strerror', None) or exc}."
                self.client.end_run(bundle["events_url"], bundle["run_token"], "failed", error)
                self._finish(job_id, "failed", error)
                return
            status, error = self._watch(bundle, proc, workdir, gate_dir)
            self._finish(job_id, status, error, {"gate": read_gate(gate_dir)} if status == "suspended" else None)
        except Exception:
            log.exception("job %s failed in the runner", job_id)
            self._finish(job_id, "failed", "The runner failed while running this job.")
        finally:
            if self.same_user:
                stop_sessions(workdir / SESSIONS_DIR)
            shutil.rmtree(workdir, ignore_errors=True)
            if run_dir is not None:
                unshare(run_dir, self.job_user, self.agent_user, agent_dir)

    def _watch(self, bundle: dict[str, Any], proc: subprocess.Popen, workdir: Path,
               gate_dir: Path | None = None) -> tuple[str, str | None]:
        """Heartbeat until the process exits or the job is canceled. Returns the status and error to report.

        A run that paused at a gate left its request in ``gate_dir``, and reports as suspended.
        """
        job_id = bundle["job_id"]
        next_beat = self._clock() + self.heartbeat
        while True:
            try:
                proc.wait(WAIT_STEP_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if self._stopping.is_set():
                return self._stop_run(bundle, proc, "failed", "The runner stopped before the run finished.")
            if self._clock() >= next_beat:
                next_beat = self._clock() + self.heartbeat
                beat = self._beat(job_id)
                if beat is not None and beat["cancel_requested"]:
                    return self._stop_run(bundle, proc, "canceled", "Canceled.")
                if beat is not None and beat["status"] not in HELD:
                    return self._stop_run(bundle, proc, "failed", "The server gave this job up.")
        if gate_dir is not None and (gate_dir / GATE_FILE).is_file() and self._paused(job_id):
            return "suspended", None
        error = f"The run exited with status {proc.returncode} without finishing."
        log_tail = tail(workdir / LOG_FILE)
        if log_tail:
            error += "\n" + log_tail
        # A run_end the process sent may still be on its way, so give it time to land first.
        deadline = self._clock() + (END_WAIT_S if proc.returncode == 0 else END_GRACE_S)
        while True:
            beat = self._beat(job_id)
            run_status = beat["run_status"] if beat is not None else None
            if run_status is not None and run_status not in OPEN:
                return ("succeeded" if run_status == "succeeded" else "failed"), None
            if self._clock() >= deadline:
                break
            time.sleep(END_POLL_S)
        self.client.end_run(bundle["events_url"], bundle["run_token"], "failed", error)
        return "failed", error

    def _paused(self, job_id: str) -> bool:
        """Whether the server heard the run's run_suspend. A gate request file alone could come from any step."""
        deadline = self._clock() + END_WAIT_S
        while True:
            beat = self._beat(job_id)
            run_status = beat["run_status"] if beat is not None else None
            if run_status == "waiting":
                return True
            if (run_status is not None and run_status not in OPEN) or self._clock() >= deadline:
                return False
            time.sleep(END_POLL_S)

    def _stop_run(self, bundle: dict[str, Any], proc: subprocess.Popen, status: str,
                  error: str) -> tuple[str, str]:
        self.client.end_run(bundle["events_url"], bundle["run_token"], status, error)
        stop(proc)
        return status, error

"""Registers a top-level run with an Armature server to get its event endpoint."""
from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from armature.events.emitter import (
    Emitter,
    is_loopback_host,
    plain_http_to_remote,
    warn_once,
)
from armature.events.summary import subject_problem, trigger_ref_problem

REGISTER_TIMEOUT_S = 3.0
RETRY_AFTER_FAILURE_S = 300.0

log = logging.getLogger(__name__)
_skip_until: dict[str, float] = {}


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _string(inputs: dict[str, Any], name: str) -> str | None:
    value = inputs.get(name)
    return value if isinstance(value, str) and value else None


def _trigger_ref() -> dict[str, Any]:
    raw = os.environ.get("ARMATURE_TRIGGER_REF")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    problem = trigger_ref_problem(value)
    if problem:
        warn_once("trigger_ref", "Ignoring ARMATURE_TRIGGER_REF: %s", problem)
        return {}
    return value


def _subject() -> str | None:
    value = os.environ.get("ARMATURE_SUBJECT")
    if not value:
        return None
    problem = subject_problem(value)
    if problem:
        warn_once("subject", "Ignoring ARMATURE_SUBJECT: %s", problem)
        return None
    return value


def registration_body(workflow: str, inputs: dict[str, Any] | None) -> dict[str, Any]:
    inputs = inputs or {}
    return {
        "workflow": workflow,
        "project": os.environ.get("ARMATURE_PROJECT") or _string(inputs, "project") or _string(inputs, "repo"),
        "actor": os.environ.get("ARMATURE_ACTOR") or os.environ.get("USER"),
        "trigger_kind": os.environ.get("ARMATURE_TRIGGER_KIND") or "cli",
        "trigger_ref": _trigger_ref(),
        "subject": _subject(),
        "rerun_of": os.environ.get("ARMATURE_RERUN_OF"),
    }


def _post_registration(server: str, runner_key: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        server.rstrip("/") + "/api/runner/runs",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {runner_key}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read())


def _with_deadline(fn, timeout: float) -> Any:
    """Run `fn` in a thread so DNS and every socket step share one deadline."""
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, name="armature-register", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"no reply within {timeout:g}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _endpoint(server: str, events: dict[str, Any]) -> str:
    unix_path = events.get("unix")
    if unix_path and is_loopback_host(urlsplit(server).hostname) and _is_socket(unix_path):
        return f"unix:{unix_path}"
    http_url = events.get("http")
    if not http_url:
        raise ValueError("no usable event endpoint in the reply")
    if plain_http_to_remote(http_url):
        raise ValueError(f"refusing plain http to a non-loopback host: {http_url}")
    return http_url


def register_run(
    server: str, runner_key: str, body: dict[str, Any], timeout: float | None = None,
) -> tuple[str, Emitter] | None:
    """POST the run to `server`. Returns (run_id, emitter), or None after one warning.

    After a failure, registration with that server is skipped for five minutes.
    """
    timeout = REGISTER_TIMEOUT_S if timeout is None else timeout
    if time.monotonic() < _skip_until.get(server, 0.0):
        return None
    try:
        if plain_http_to_remote(server):
            raise ValueError("refusing plain http to a non-loopback host")
        reply = _with_deadline(lambda: _post_registration(server, runner_key, body, timeout), timeout)
        endpoint = _endpoint(server, reply.get("events") or {})
        return str(reply["run_id"]), Emitter(endpoint, str(reply["token"]))
    except Exception as exc:
        _skip_until[server] = time.monotonic() + RETRY_AFTER_FAILURE_S
        log.warning(
            "Run not registered with %s, running without events for %d minutes: %s",
            server, RETRY_AFTER_FAILURE_S // 60, exc,
        )
        return None

"""Start a runner from a command line: check its options, get its key, and run it until Ctrl-C or SIGTERM.

``armature runner`` uses these, and so can a remote provider's own command.
"""
from __future__ import annotations

import grp
import logging
import os
import pwd
import shutil
import signal
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

DEFAULT_KEY_ENV = "ARMATURE_RUNNER_KEY"
DEFAULT_KEY_FILE = Path("/etc/armature/runner.env")


def fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def check_server(server: str) -> None:
    from armature.events.emitter import plain_http_to_remote

    if not server.startswith(("https://", "http://")):
        fail("--server is an http:// or https:// address.")
    if plain_http_to_remote(server):
        fail("--server must use https unless it is on this host, so the key and secrets never travel in clear.")


def check_name(name: str | None) -> None:
    if name is not None and (not name.strip() or "/" in name or name.strip() in (".", "..")):
        fail("--name can't be empty, contain '/', or be '.' or '..'.")


def parse_labels(values: list[str]) -> dict[str, str]:
    labels = {}
    for value in values:
        name, sep, text = value.partition("=")
        if not sep or not name:
            fail(f"--label {value!r} is not NAME=VALUE.")
        labels[name] = text
    return labels


def _stored_key(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        fail(f"Can't read {path}: {exc.strerror}.")
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == DEFAULT_KEY_ENV and value.strip():
            return value.strip()
    return None


def _store_key(path: Path, key: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        os.fchmod(fd, 0o600)
        out.write(f"{DEFAULT_KEY_ENV}={key}\n")


def enrolled_key(server: str, token_env: str, key_file: Path, name: str) -> str:
    """The key this environment enrolled for: from ``key_file`` after a restart, else by trading the token once."""
    from armature.runner.client import ServerError, enroll

    stored = _stored_key(key_file)
    if stored:
        return stored
    token = os.environ.get(token_env, "").strip()
    if not token:
        fail(f"Set {token_env} to this environment's enrollment token.")
    try:
        key = enroll(server, token, name)
    except ServerError as exc:
        fail(f"Enrollment failed: {exc.message}")
    try:
        _store_key(key_file, key)
    except OSError as exc:
        fail(f"Enrolled, but can't keep the key in {key_file}: {exc.strerror}.")
    return key


def runner_key(server: str, key_env: str, enroll_token_env: str | None, key_file: Path, name: str) -> str:
    """The runner key from ``key_env``, else from enrolling with the token in ``enroll_token_env``."""
    key = os.environ.get(key_env, "").strip()
    if not key and enroll_token_env:
        key = enrolled_key(server, enroll_token_env, key_file.expanduser(), name)
    if not key:
        fail(f"Set {key_env} to a runner key. Create one on the server's Runner keys page.")
    return key


def check_job_user(job_user: str) -> None:
    try:
        pwd.getpwnam(job_user)
        grp.getgrnam(job_user)
    except KeyError:
        fail(f"--job-user {job_user!r} needs a user and a group of that name on this host.")
    if job_user == pwd.getpwuid(os.getuid()).pw_name:
        fail("--job-user must be another user than the one running the runner.")
    if shutil.which("sudo") is None:
        fail("--job-user needs sudo on this host.")


def serve(make: Callable[[Any, str, str], tuple[Any, Callable[[], None] | None]], *, server: str,
          key_env: str = DEFAULT_KEY_ENV, enroll_token_env: str | None = None, key_file: Path = DEFAULT_KEY_FILE,
          name: str | None = None, how: str = "") -> None:
    """Get the runner key, build the runner with ``make``, and run it until Ctrl-C or SIGTERM.

    ``make(client, name, host)`` returns the Runner and a function to call once it stops, or None. ``how`` ends
    the line that says the runner started, such as ``with 4 slot(s)``. Refuses plain http to anything but this host.
    """
    from armature.runner.client import Client, RunnerLocked

    check_server(server)
    host = socket.gethostname()
    key = runner_key(server, key_env, enroll_token_env, key_file, name or host)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = Client(server, key)
    job_runner, close = make(client, name or host, host)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    typer.echo(f"Runner {job_runner.name} polling {server}{f' {how}' if how else ''}. Ctrl-C stops it.")
    try:
        job_runner.run(stop)
    except RunnerLocked as exc:
        fail(str(exc))
    finally:
        client.close()
        if close is not None:
            close()

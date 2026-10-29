"""Run a configured host command: no shell, a minimal environment, a hard timeout, output read from files.

Output goes to files rather than pipes, so a command that leaves a background
child behind (a queue drain started with nohup) cannot hold the call open.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, select

from armature.events.redact import redact_text
from armature.server import secrets as secret_store
from armature.server.crypto import Keyring
from armature.server.db.schema import INSTANCE_SCOPE, secrets

ENV_NAMES = ("PATH", "HOME", "LANG")
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
MIN_SECRET = 4
KILL_GRACE_S = 3.0
TRUNCATED_NOTE = "[truncated]\n"


@dataclass(frozen=True)
class Result:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None


def minimal_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {name: os.environ[name] for name in ENV_NAMES if name in os.environ}
    env.setdefault("PATH", DEFAULT_PATH)
    env.setdefault("HOME", str(Path.home()))
    env.update(extra or {})
    return env


def working_dir(cwd: str | None) -> Path:
    return Path(cwd).expanduser() if cwd else Path.home()


def _read(path: Path, limit: int) -> str:
    """The last ``limit`` bytes of a file, where a command's newest and most telling lines are."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            data = handle.read(limit)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return TRUNCATED_NOTE + text if size > limit else text


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(KILL_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def run(argv: list[str], *, cwd: Path, env: Mapping[str, str], timeout: float, scratch: Path,
        read_limit: int) -> Result:
    """Run ``argv`` with stdout and stderr in ``scratch``, killing its process group on timeout."""
    out_path, err_path = scratch / "stdout", scratch / "stderr"
    try:
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    start_new_session=True, close_fds=True)
    except OSError as exc:
        return Result(None, "", "", error=f"could not start the command: {exc.strerror or exc}")
    timed_out = False
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
    return Result(proc.returncode, _read(out_path, read_limit), _read(err_path, read_limit), timed_out=timed_out)


def last_json(stdout: str) -> dict[str, Any] | None:
    """The command's last non-empty stdout line, when it is a JSON object."""
    for line in reversed(stdout.splitlines()):
        if line.strip():
            try:
                value = json.loads(line)
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def cap(text: str, limit: int, *, keep_tail: bool = False) -> str:
    """At most ``limit`` UTF-8 bytes of ``text``."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    if keep_tail:
        return data[len(data) - limit:].decode("utf-8", errors="ignore")
    return data[:limit].decode("utf-8", errors="ignore")


def instance_secret_values(conn: Connection, keyring: Keyring) -> tuple[str, ...]:
    """Every instance secret's value, longest first. Raises if any will not open."""
    names = conn.execute(select(secrets.c.name).where(secrets.c.scope == INSTANCE_SCOPE)).scalars().all()
    values = (secret_store.resolve_instance(conn, keyring, name) for name in names)
    return tuple(sorted({v for v in values if v and len(v) >= MIN_SECRET}, key=len, reverse=True))


def scrub(text: str, secret_values: tuple[str, ...]) -> str:
    return redact_text(text, secret_values)

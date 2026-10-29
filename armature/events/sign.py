"""Signing what only a run's engine may send: a pause at a gate, and tool grant requests.

The server derives a per-run engine key (``runners.engine_key``). The host
hands it to the engine as ``<run id>:<key>`` over an inherited pipe that
``ARMATURE_ENGINE_FD`` names, never in the environment, since a same-user
process can read another's original environment block. The engine reads and
closes the pipe before it starts any step.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

ENGINE_FD_ENV = "ARMATURE_ENGINE_FD"
KEY_MAX = 4096


def canonical(value: Any) -> bytes:
    """The one JSON encoding that signatures and digests cover: sorted keys, no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def sign_engine(key: str, payload: dict[str, Any]) -> str:
    """HMAC-SHA256 of the payload as canonical JSON. Put a ``kind`` in it, so one signature never serves another."""
    return hmac.new(key.encode(), canonical(payload), hashlib.sha256).hexdigest()


def engine_pipe(key: str) -> int:
    """A pipe's read end holding ``key`` and then end of file, for a host to pass to the run it starts."""
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, key.encode())
    finally:
        os.close(write_end)
    return read_end


def read_engine_key() -> tuple[str, str] | None:
    """The run id and key from the pipe ``ARMATURE_ENGINE_FD`` names, which is closed here. None when unset.

    Standard input may be the pipe, as when sudo closes every other descriptor. It is left on /dev/null.
    """
    raw = os.environ.pop(ENGINE_FD_ENV, "")
    if not raw.isdigit():
        # Under sudo, standard input may still be a key pipe whose descriptor name was lost, so no step gets it.
        if os.environ.get("SUDO_USER"):
            _close(0)
        return None
    fd, data = int(raw), b""
    try:
        while len(data) <= KEY_MAX and (chunk := os.read(fd, KEY_MAX)):
            data += chunk
    except OSError:
        return None
    finally:
        _close(fd)
        if fd != 0 and os.environ.get("SUDO_USER"):
            _close(0)
    run_id, _, key = data.decode(errors="replace").strip().partition(":")
    return (run_id, key) if run_id and key and len(data) <= KEY_MAX else None


def _close(fd: int) -> None:
    if fd == 0:
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        return
    try:
        os.close(fd)
    except OSError:
        pass

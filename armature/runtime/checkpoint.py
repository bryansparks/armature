"""Checkpoint persistence for stage results.

When enabled, completed stage results are written to `checkpoint.json` in the
session directory after each stage. On re-run, completed stages are loaded from
the file and their execution skipped, allowing workflows to resume from the last
successful point.

File format: `{"stage_id": <result>, ...}` (JSON object).

Writes are atomic: we write to `checkpoint.json.tmp` then rename, so a crash
mid-write leaves the checkpoint file intact.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any


class SessionDirInUse(RuntimeError):
    """The session directory is locked by an active run."""


class SessionLock:
    """Fail-closed advisory lock guarding a session directory.

    Two concurrent runs against one session directory would both execute
    un-checkpointed stages and clobber each other's checkpoint writes
    (last rename wins) — silently duplicating external effects. Instead,
    the second run fails loudly with `SessionDirInUse`.

    Uses `fcntl.flock` so the OS releases the lock if the process dies:
    there are no stale-lock files to clean up. Because flock is scoped to
    the open file description (not the process), two Harness instances in
    the same process are also correctly rejected.
    """

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def acquire(self) -> None:
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise SessionDirInUse(
                f"Session directory is in use by an active run: {self._path.parent}. "
                f"Concurrent resumes would duplicate un-checkpointed stage effects — "
                f"wait for the active run to finish or use a different session_dir."
            ) from None

    def release(self) -> None:
        if self._fh is not None:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "SessionLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class CheckpointStore:
    def __init__(self, path: Path):
        self._path = path
        self._tmp = path.with_suffix(".json.tmp")

    def load(self) -> dict[str, Any]:
        """Return the persisted stage results, or {} if the file doesn't exist."""
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def write(self, stage_id: str, result: Any, existing: dict[str, Any]) -> None:
        """Atomically persist `result` under `stage_id`, merging with `existing`."""
        updated = {**existing, stage_id: result}
        self._tmp.write_text(json.dumps(updated, default=str))
        self._tmp.rename(self._path)

    def clear(self) -> None:
        """Remove the checkpoint file (force-restart)."""
        self._path.unlink(missing_ok=True)

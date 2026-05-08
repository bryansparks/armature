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

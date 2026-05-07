from __future__ import annotations
import json
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS memories (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_name TEXT NOT NULL,
        stage_id      TEXT NOT NULL,
        capture_key   TEXT NOT NULL,
        value         TEXT NOT NULL,
        timestamp     TEXT NOT NULL
    )
"""


class MemoryStore:
    """Persist and retrieve cross-run stage outputs for a workflow."""

    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            await db.commit()

    async def record(
        self,
        workflow_name: str,
        stage_id: str,
        capture_key: str,
        value: Any,
        max_entries: int = 5,
    ) -> None:
        serialized = json.dumps(value, default=str)
        timestamp = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO memories (workflow_name, stage_id, capture_key, value, timestamp) "
                "VALUES (?,?,?,?,?)",
                (workflow_name, stage_id, capture_key, serialized, timestamp),
            )
            # Keep only the newest max_entries; delete everything beyond that offset.
            await db.execute(
                """DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories
                    WHERE workflow_name=? AND stage_id=? AND capture_key=?
                    ORDER BY timestamp DESC
                    LIMIT -1 OFFSET ?
                )""",
                (workflow_name, stage_id, capture_key, max_entries),
            )
            await db.commit()

    async def load(self, workflow_name: str) -> dict[str, dict[str, list]]:
        """Return memories as {stage_id: {capture_key: [newest, ..., oldest]}}."""
        if not self._path.exists():
            return {}
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT stage_id, capture_key, value FROM memories "
                "WHERE workflow_name=? ORDER BY timestamp DESC",
                (workflow_name,),
            )
            rows = await cursor.fetchall()

        result: dict[str, dict[str, list]] = {}
        for row in rows:
            stage = row["stage_id"]
            key = row["capture_key"]
            val = json.loads(row["value"])
            result.setdefault(stage, {}).setdefault(key, []).append(val)
        return result

    async def clear(self, workflow_name: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM memories WHERE workflow_name=?", (workflow_name,)
            )
            await db.commit()

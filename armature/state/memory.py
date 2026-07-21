from __future__ import annotations
import json
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS memories (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_name TEXT NOT NULL,
        stage_id      TEXT NOT NULL,
        capture_key   TEXT NOT NULL,
        value         TEXT NOT NULL,
        quality       REAL NOT NULL DEFAULT 0.5,
        timestamp     TEXT NOT NULL
    )
"""


class MemoryStore:
    """Persist and retrieve cross-run stage outputs for a workflow."""

    def __init__(self, db_path: Path | str, staleness_threshold_days: float = 30.0):
        self._path = Path(db_path)
        self._staleness_days = staleness_threshold_days

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            # Migrate existing tables that lack the quality column
            try:
                await db.execute("ALTER TABLE memories ADD COLUMN quality REAL NOT NULL DEFAULT 0.5")
            except Exception:
                pass
            await db.commit()

    async def record(
        self,
        workflow_name: str,
        stage_id: str,
        capture_key: str,
        value: Any,
        max_entries: int = 5,
        quality: float = 0.5,
    ) -> None:
        serialized = json.dumps(value, default=str)
        timestamp = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO memories (workflow_name, stage_id, capture_key, value, quality, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                (workflow_name, stage_id, capture_key, serialized, quality, timestamp),
            )
            # Keep best max_entries (highest quality, then newest); evict lowest-quality oldest first.
            await db.execute(
                """DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories
                    WHERE workflow_name=? AND stage_id=? AND capture_key=?
                    ORDER BY quality DESC, timestamp DESC
                    LIMIT -1 OFFSET ?
                )""",
                (workflow_name, stage_id, capture_key, max_entries),
            )
            await db.commit()

    async def load(
        self, workflow_name: str
    ) -> Tuple[dict[str, dict[str, list]], set[tuple[str, str]]]:
        """Return (memories, stale_keys).

        memories  — {stage_id: {capture_key: [newest, ..., oldest]}}
        stale_keys — {(stage_id, capture_key), ...} for entries older than staleness_threshold_days
        """
        if not self._path.exists():
            return {}, set()
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT stage_id, capture_key, value, timestamp FROM memories "
                "WHERE workflow_name=? ORDER BY timestamp DESC",
                (workflow_name,),
            )
            rows = await cursor.fetchall()

        result: dict[str, dict[str, list]] = {}
        stale_keys: set[tuple[str, str]] = set()
        for row in rows:
            stage = row["stage_id"]
            key = row["capture_key"]
            val = json.loads(row["value"])
            result.setdefault(stage, {}).setdefault(key, []).append(val)
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_days = (now - ts).total_seconds() / 86400
                if age_days > self._staleness_days:
                    stale_keys.add((stage, key))
            except (ValueError, TypeError):
                pass
        return result, stale_keys

    async def clear(self, workflow_name: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM memories WHERE workflow_name=?", (workflow_name,)
            )
            await db.commit()

    async def search_conversation(
        self, workflow_name: str, query: str,
        stage_id: str | None = None, top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Keyword scan over L0 raw captures (`memories.value`), newest first.

        `value` is stored JSON-serialized; the raw serialized string is returned
        so the agent can read it. Optional `stage_id` filter restricts the scan.
        """
        if not self._path.exists():
            return []
        pattern = f"%{query.lower()}%"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            if stage_id:
                cursor = await db.execute(
                    "SELECT stage_id, capture_key, value, timestamp FROM memories "
                    "WHERE workflow_name=? AND stage_id=? AND LOWER(value) LIKE ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (workflow_name, stage_id, pattern, top_k),
                )
            else:
                cursor = await db.execute(
                    "SELECT stage_id, capture_key, value, timestamp FROM memories "
                    "WHERE workflow_name=? AND LOWER(value) LIKE ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (workflow_name, pattern, top_k),
                )
            rows = await cursor.fetchall()
        return [
            {
                "stage_id": r["stage_id"],
                "capture_key": r["capture_key"],
                "value": r["value"],
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

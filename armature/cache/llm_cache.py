from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS llm_cache (
        cache_key   TEXT PRIMARY KEY,
        response_json TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
"""


class LLMCache:
    """Content-addressed SQLite cache for LLM responses.

    Cache key = SHA-256(model + messages + extra_kwargs) so byte-identical
    requests serve from cache without calling the model.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(_CREATE_SQL)
            await db.commit()

    def _make_key(self, model: str, messages: list[dict], extra_kwargs: dict) -> str:
        payload = json.dumps(
            {"model": model, "messages": messages, **extra_kwargs},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    async def get(self, key: str) -> str | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT response_json FROM llm_cache WHERE cache_key = ?", (key,)
            )
            row = await cursor.fetchone()
        return row[0] if row else None

    async def put(self, key: str, response_json: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO llm_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
                (key, response_json, now),
            )
            await db.commit()

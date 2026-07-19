"""L3 team-profile store for the memory pyramid.

Single markdown row per workflow (≤ char_budget). Shares the knowledge DB file.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import aiosqlite

_CREATE_PROFILE_SQL = """
CREATE TABLE IF NOT EXISTS team_profile (
    workflow_name TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    char_budget   INTEGER NOT NULL DEFAULT 2000,
    updated_at    TEXT NOT NULL
)
"""


class ProfileStore:
    def __init__(self, path: Path):
        self._path = path

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_PROFILE_SQL)
            await db.commit()

    async def upsert_profile(
        self, workflow_name: str, content: str, char_budget: int,
    ) -> dict:
        if len(content) > char_budget:
            return {"error": "content exceeds char_budget", "len": len(content),
                    "char_budget": char_budget}
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO team_profile (workflow_name, content, char_budget, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(workflow_name) DO UPDATE SET "
                "content=excluded.content, char_budget=excluded.char_budget, "
                "updated_at=excluded.updated_at",
                (workflow_name, content, char_budget, now),
            )
            await db.commit()
        return {"updated_at": now}

    async def get_profile(self, workflow_name: str) -> str | None:
        if not self._path.exists():
            return None
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT content FROM team_profile WHERE workflow_name=?",
                (workflow_name,),
            )
            row = await cur.fetchone()
            return row[0] if row is not None else None
"""L2 topic-track store for the memory pyramid.

Shares the knowledge DB file with `KnowledgeStore`. Tracks are ≤20 markdown
summaries per workflow, each citing L1 record ids via `evidence_links`.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import aiosqlite

_CREATE_TRACKS_SQL = """
CREATE TABLE IF NOT EXISTS topic_tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name   TEXT NOT NULL,
    track_id        TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    narrative       TEXT,
    evidence_links  TEXT NOT NULL DEFAULT '[]',
    char_budget     INTEGER NOT NULL DEFAULT 2000,
    updated_at      TEXT NOT NULL,
    UNIQUE(workflow_name, track_id)
)
"""


class TrackStore:
    def __init__(self, path: Path):
        self._path = path

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_TRACKS_SQL)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracks_wf ON topic_tracks(workflow_name)"
            )
            await db.commit()

    async def upsert_track(
        self,
        workflow_name: str,
        track_id: str,
        title: str,
        summary: str,
        narrative: str | None,
        evidence_links: list[int],
        char_budget: int,
        track_budget: int,
    ) -> dict:
        if len(summary) > char_budget:
            return {"error": "summary exceeds char_budget", "len": len(summary),
                    "char_budget": char_budget, "track_id": track_id}
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            # budget: count *other* tracks (updates to existing track_id are exempt)
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM topic_tracks "
                "WHERE workflow_name=? AND track_id != ?",
                (workflow_name, track_id),
            )
            existing = (await cur.fetchone())["n"]
            cur = await db.execute(
                "SELECT id FROM topic_tracks WHERE workflow_name=? AND track_id=?",
                (workflow_name, track_id),
            )
            row = await cur.fetchone()
            is_new = row is None
            if is_new and existing >= track_budget:
                return {"error": f"track_budget exceeded ({existing}/{track_budget})",
                        "track_id": track_id}
            # validate evidence_links: must exist AND be live (superseded_by IS NULL)
            valid: list[int] = []
            dropped: list[int] = []
            if evidence_links:
                placeholders = ",".join("?" * len(evidence_links))
                cur = await db.execute(
                    f"SELECT id FROM knowledge WHERE id IN ({placeholders}) "
                    f"AND superseded_by IS NULL",
                    evidence_links,
                )
                live = {r["id"] for r in await cur.fetchall()}
                for eid in evidence_links:
                    if eid in live:
                        valid.append(eid)
                    else:
                        dropped.append(eid)
            links_json = json.dumps(valid)
            if is_new:
                await db.execute(
                    "INSERT INTO topic_tracks (workflow_name, track_id, title, summary, "
                    "narrative, evidence_links, char_budget, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (workflow_name, track_id, title, summary, narrative,
                     links_json, char_budget, now),
                )
            else:
                await db.execute(
                    "UPDATE topic_tracks SET title=?, summary=?, narrative=?, "
                    "evidence_links=?, char_budget=?, updated_at=? "
                    "WHERE workflow_name=? AND track_id=?",
                    (title, summary, narrative, links_json, char_budget, now,
                     workflow_name, track_id),
                )
            await db.commit()
        return {"track_id": track_id, "dropped_evidence": dropped, "updated_at": now}

    async def get_track(self, workflow_name: str, track_id: str) -> dict | None:
        if not self._path.exists():
            return None
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT track_id, title, summary, narrative, evidence_links, updated_at "
                "FROM topic_tracks WHERE workflow_name=? AND track_id=?",
                (workflow_name, track_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            try:
                links = json.loads(row["evidence_links"])
            except (ValueError, TypeError):
                links = []
            return {
                "track_id": row["track_id"],
                "title": row["title"],
                "summary": row["summary"],
                "narrative": row["narrative"],
                "evidence_links": links,
                "updated_at": row["updated_at"],
            }

    async def list_tracks(self, workflow_name: str) -> list[dict]:
        if not self._path.exists():
            return []
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT track_id, title, summary, evidence_links "
                "FROM topic_tracks WHERE workflow_name=? ORDER BY updated_at DESC LIMIT 20",
                (workflow_name,),
            )
            rows = await cur.fetchall()
            out = []
            for r in rows:
                try:
                    links = json.loads(r["evidence_links"])
                except (ValueError, TypeError):
                    links = []
                out.append({
                    "track_id": r["track_id"], "title": r["title"],
                    "summary": r["summary"], "evidence_links": links,
                })
            return out

    async def last_updated_at(self, workflow_name: str) -> str | None:
        if not self._path.exists():
            return None
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT MAX(updated_at) AS m FROM topic_tracks WHERE workflow_name=?",
                (workflow_name,),
            )
            row = await cur.fetchone()
            return row[0] if row is not None else None

    async def count(self, workflow_name: str) -> int:
        if not self._path.exists():
            return 0
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM topic_tracks WHERE workflow_name=?",
                (workflow_name,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row is not None else 0
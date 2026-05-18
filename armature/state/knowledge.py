"""KnowledgeStore — persists LLM-extracted facts across workflow runs."""
from __future__ import annotations
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field


class KnowledgeRecord(BaseModel):
    workflow_name: str
    entity: str
    fact: str
    confidence: float
    source_run_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS knowledge (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_name TEXT NOT NULL,
        entity        TEXT NOT NULL,
        fact          TEXT NOT NULL,
        confidence    REAL NOT NULL,
        source_run_id TEXT NOT NULL,
        timestamp     TEXT NOT NULL
    )
"""

_CREATE_FTS_SQL = """
    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
        fact,
        content=knowledge,
        content_rowid=id
    )
"""


class KnowledgeStore:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            await db.execute(_CREATE_FTS_SQL)
            await db.commit()

    async def record(self, record: KnowledgeRecord) -> None:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "INSERT INTO knowledge (workflow_name, entity, fact, confidence, source_run_id, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                (record.workflow_name, record.entity, record.fact,
                 record.confidence, record.source_run_id, record.timestamp),
            )
            await db.execute(
                "INSERT INTO knowledge_fts(rowid, fact) VALUES (?, ?)",
                (cur.lastrowid, record.fact),
            )
            await db.commit()

    async def load(self, workflow_name: str) -> list[KnowledgeRecord]:
        if not self._path.exists():
            return []
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM knowledge WHERE workflow_name=? ORDER BY timestamp DESC",
                (workflow_name,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_record(r) for r in rows]

    async def search(self, workflow_name: str, query: str, top_k: int = 5) -> list[KnowledgeRecord]:
        """BM25 full-text search over stored facts, ranked by relevance."""
        if not self._path.exists():
            return []
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            try:
                cursor = await db.execute(
                    """SELECT k.* FROM knowledge_fts f
                       JOIN knowledge k ON k.id = f.rowid
                       WHERE f.fact MATCH ?
                         AND k.workflow_name = ?
                       ORDER BY f.rank
                       LIMIT ?""",
                    (query, workflow_name, top_k),
                )
            except Exception:
                # FTS5 query syntax error — fall back to substring match
                pattern = f"%{query.lower()}%"
                cursor = await db.execute(
                    "SELECT * FROM knowledge WHERE workflow_name=? AND LOWER(fact) LIKE ? "
                    "ORDER BY confidence DESC, timestamp DESC LIMIT ?",
                    (workflow_name, pattern, top_k),
                )
            rows = await cursor.fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(r: "aiosqlite.Row") -> KnowledgeRecord:
        return KnowledgeRecord(
            workflow_name=r["workflow_name"],
            entity=r["entity"],
            fact=r["fact"],
            confidence=r["confidence"],
            source_run_id=r["source_run_id"],
            timestamp=r["timestamp"],
        )

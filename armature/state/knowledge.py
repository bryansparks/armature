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


class KnowledgeStore:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            await db.commit()

    async def record(self, record: KnowledgeRecord) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO knowledge (workflow_name, entity, fact, confidence, source_run_id, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                (record.workflow_name, record.entity, record.fact,
                 record.confidence, record.source_run_id, record.timestamp),
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
        """Keyword search over stored facts (case-insensitive substring match)."""
        if not self._path.exists():
            return []
        pattern = f"%{query.lower()}%"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
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

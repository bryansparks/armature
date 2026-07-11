"""KnowledgeStore — persists LLM-extracted facts across workflow runs."""
from __future__ import annotations
import aiosqlite
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from armature.state.embedder import LocalEmbedder


class MemoryType(str, Enum):
    FACT = "fact"
    EVENT = "event"
    INSTRUCTION = "instruction"
    PREFERENCE = "preference"


class KnowledgeRecord(BaseModel):
    workflow_name: str
    entity: str
    fact: str
    confidence: float
    source_run_id: str
    source_stage_id: str | None = None
    source_capture_key: str | None = None
    source_msg_id: str | None = None
    type: MemoryType = MemoryType.FACT
    provenance: list[dict] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str | None = None
    superseded_by: int | None = None
    id: int | None = None


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS knowledge (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_name      TEXT NOT NULL,
        entity             TEXT NOT NULL,
        fact               TEXT NOT NULL,
        confidence         REAL NOT NULL,
        source_run_id      TEXT NOT NULL,
        source_stage_id   TEXT,
        source_capture_key TEXT,
        source_msg_id      TEXT,
        type               TEXT NOT NULL DEFAULT 'fact',
        provenance         TEXT,
        timestamp          TEXT NOT NULL,
        updated_at         TEXT,
        superseded_by      INTEGER,
        embedding          BLOB
    )
"""

_CREATE_FTS_SQL = """
    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
        fact,
        content=knowledge,
        content_rowid=id
    )
"""

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

_CREATE_PROFILE_SQL = """
    CREATE TABLE IF NOT EXISTS team_profile (
        workflow_name TEXT PRIMARY KEY,
        content       TEXT NOT NULL,
        char_budget   INTEGER NOT NULL DEFAULT 2000,
        updated_at    TEXT NOT NULL
    )
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_knowledge_wf_type    ON knowledge(workflow_name, type)    WHERE superseded_by IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_wf_entity  ON knowledge(workflow_name, entity) WHERE superseded_by IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_superseded ON knowledge(superseded_by)         WHERE superseded_by IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_tracks_wf ON topic_tracks(workflow_name)",
]


class KnowledgeStore:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(_CREATE_SQL)
            await db.execute(_CREATE_FTS_SQL)
            cur = await db.execute("PRAGMA user_version")
            version = (await cur.fetchone())[0]
            if version < 1:
                # Add new columns idempotently (legacy DBs pre-date them).
                for col, ddl in [
                    ("type", "TEXT NOT NULL DEFAULT 'fact'"),
                    ("source_stage_id", "TEXT"),
                    ("source_capture_key", "TEXT"),
                    ("source_msg_id", "TEXT"),
                    ("provenance", "TEXT"),
                    ("updated_at", "TEXT"),
                    ("superseded_by", "INTEGER"),
                ]:
                    try:
                        await db.execute(f"ALTER TABLE knowledge ADD COLUMN {col} {ddl}")
                    except Exception:
                        pass  # column already exists
                await db.execute("UPDATE knowledge SET type='fact' WHERE type IS NULL OR type=''")
                await db.execute(
                    "UPDATE knowledge SET provenance = json_array(json_object('run_id', source_run_id)) "
                    "WHERE provenance IS NULL"
                )
                # FTS5 external-content pointers are invalidated by ALTER; rebuild.
                await db.execute("DROP TABLE IF EXISTS knowledge_fts")
                await db.execute(_CREATE_FTS_SQL)
                await db.execute("INSERT INTO knowledge_fts(rowid, fact) SELECT id, fact FROM knowledge")
                # L2/L3 tables (populated in Phase 3; created here so migration is one-shot).
                await db.execute(_CREATE_TRACKS_SQL)
                await db.execute(_CREATE_PROFILE_SQL)
                for idx in _INDEXES:
                    await db.execute(idx)
                await db.execute("PRAGMA user_version = 1")
            await db.commit()

    async def record(
        self,
        record: KnowledgeRecord,
        embedder: "LocalEmbedder | None" = None,
    ) -> int:
        emb_bytes: bytes | None = None
        if embedder is not None:
            try:
                from armature.state.embedder import vector_to_bytes
                vec = embedder.embed(record.fact)
                emb_bytes = vector_to_bytes(vec)
            except Exception:
                pass  # embedding failure is non-fatal

        prov_json = json.dumps(record.provenance) if record.provenance else None
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "INSERT INTO knowledge "
                "(workflow_name, entity, fact, confidence, source_run_id, source_stage_id, "
                " source_capture_key, source_msg_id, type, provenance, timestamp, embedding) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (record.workflow_name, record.entity, record.fact, record.confidence,
                 record.source_run_id, record.source_stage_id, record.source_capture_key,
                 record.source_msg_id, record.type.value, prov_json, record.timestamp, emb_bytes),
            )
            row_id = cur.lastrowid
            await db.execute(
                "INSERT INTO knowledge_fts(rowid, fact) VALUES (?, ?)",
                (row_id, record.fact),
            )
            await db.commit()
        return row_id

    async def load(self, workflow_name: str) -> list[KnowledgeRecord]:
        if not self._path.exists():
            return []
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM knowledge WHERE workflow_name=? AND superseded_by IS NULL ORDER BY timestamp DESC",
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
                         AND k.superseded_by IS NULL
                       ORDER BY f.rank
                       LIMIT ?""",
                    (query, workflow_name, top_k),
                )
            except Exception:
                # FTS5 query syntax error — fall back to substring match
                pattern = f"%{query.lower()}%"
                cursor = await db.execute(
                    "SELECT * FROM knowledge WHERE workflow_name=? AND superseded_by IS NULL AND LOWER(fact) LIKE ? "
                    "ORDER BY confidence DESC, timestamp DESC LIMIT ?",
                    (workflow_name, pattern, top_k),
                )
            rows = await cursor.fetchall()
        return [self._row_to_record(r) for r in rows]

    async def semantic_search(
        self,
        workflow_name: str,
        query: str,
        embedder: "LocalEmbedder",
        top_k: int = 5,
    ) -> list[KnowledgeRecord]:
        """Find facts by cosine similarity to *query* using local embeddings.

        Only records that were stored with an embedder (non-NULL embedding blob)
        are considered; records without embeddings are silently skipped.
        """
        if not self._path.exists():
            return []

        from armature.state.embedder import LocalEmbedder, bytes_to_vector

        query_vec = embedder.embed(query)

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM knowledge WHERE workflow_name=? AND embedding IS NOT NULL "
                "AND superseded_by IS NULL ORDER BY confidence DESC, timestamp DESC LIMIT 500",
                (workflow_name,),
            )
            rows = await cursor.fetchall()

        scored: list[tuple[float, KnowledgeRecord]] = []
        for row in rows:
            emb_bytes = row["embedding"]
            if emb_bytes is None:
                continue
            vec = bytes_to_vector(emb_bytes)
            sim = LocalEmbedder.cosine_similarity(query_vec, vec)
            scored.append((sim, self._row_to_record(row)))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [rec for _, rec in scored[:top_k]]

    @staticmethod
    def _row_to_record(r: "aiosqlite.Row") -> KnowledgeRecord:
        prov_raw = r["provenance"] if "provenance" in r.keys() else None
        try:
            provenance = json.loads(prov_raw) if prov_raw else []
        except (ValueError, TypeError):
            provenance = []
        type_raw = r["type"] if "type" in r.keys() else "fact"
        try:
            mtype = MemoryType(type_raw)
        except ValueError:
            mtype = MemoryType.FACT
        return KnowledgeRecord(
            id=r["id"],
            workflow_name=r["workflow_name"],
            entity=r["entity"],
            fact=r["fact"],
            confidence=r["confidence"],
            source_run_id=r["source_run_id"],
            source_stage_id=r["source_stage_id"] if "source_stage_id" in r.keys() else None,
            source_capture_key=r["source_capture_key"] if "source_capture_key" in r.keys() else None,
            source_msg_id=r["source_msg_id"] if "source_msg_id" in r.keys() else None,
            type=mtype,
            provenance=provenance,
            timestamp=r["timestamp"],
            updated_at=r["updated_at"] if "updated_at" in r.keys() else None,
            superseded_by=r["superseded_by"] if "superseded_by" in r.keys() else None,
        )
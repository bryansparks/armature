"""Migration: legacy knowledge DB upgrades to schema v1 idempotently."""
import aiosqlite
import pytest


_LEGACY_SCHEMA = """
CREATE TABLE knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name TEXT NOT NULL,
    entity TEXT NOT NULL,
    fact TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    embedding BLOB
);
CREATE VIRTUAL TABLE knowledge_fts USING fts5(fact, content=knowledge, content_rowid=id);
"""


async def _make_legacy_db(path):
    async with aiosqlite.connect(path) as db:
        await db.executescript(_LEGACY_SCHEMA)
        await db.execute(
            "INSERT INTO knowledge (workflow_name, entity, fact, confidence, source_run_id, timestamp) "
            "VALUES ('wf','user','prefers concise answers',0.9,'r1','2026-01-01T00:00:00+00:00')"
        )
        await db.execute(
            "INSERT INTO knowledge (workflow_name, entity, fact, confidence, source_run_id, timestamp) "
            "VALUES ('wf','domain','uses REST APIs',0.8,'r1','2026-01-01T00:00:00+00:00')"
        )
        await db.execute("INSERT INTO knowledge_fts(rowid, fact) SELECT id, fact FROM knowledge")
        await db.execute("PRAGMA user_version = 0")
        await db.commit()


async def test_migration_adds_columns_and_backfills(tmp_path):
    """Legacy DB (user_version=0) upgrades: new columns exist, type='fact', provenance set."""
    from armature.state.knowledge import KnowledgeStore
    db_path = tmp_path / "k.db"
    await _make_legacy_db(db_path)

    store = KnowledgeStore(db_path)
    await store.init()

    async with aiosqlite.connect(db_path) as db:
        cols = {row[1] for row in await (await db.execute("PRAGMA table_info(knowledge)")).fetchall()}
        assert "type" in cols
        assert "source_stage_id" in cols
        assert "source_capture_key" in cols
        assert "provenance" in cols
        assert "superseded_by" in cols
        assert "updated_at" in cols

        rows = await (await db.execute("SELECT type, provenance, superseded_by FROM knowledge WHERE entity='user'")).fetchone()
        assert rows[0] == "fact"
        assert rows[1] is not None  # provenance backfilled from source_run_id
        assert rows[2] is None      # not superseded

        version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
        assert version == 1


async def test_migration_is_idempotent(tmp_path):
    """Running init() twice does not error, duplicate FTS rows, or change user_version."""
    from armature.state.knowledge import KnowledgeStore
    db_path = tmp_path / "k.db"
    await _make_legacy_db(db_path)

    store = KnowledgeStore(db_path)
    await store.init()
    await store.init()  # second run must be a no-op

    async with aiosqlite.connect(db_path) as db:
        assert (await (await db.execute("PRAGMA user_version")).fetchone())[0] == 1
        k_count = (await (await db.execute("SELECT count(*) FROM knowledge")).fetchone())[0]
        fts_count = (await (await db.execute("SELECT count(*) FROM knowledge_fts")).fetchone())[0]
        assert k_count == fts_count  # no duplicate FTS rows


async def test_post_migration_search_works(tmp_path):
    """After migration, search() still finds legacy facts."""
    from armature.state.knowledge import KnowledgeStore
    db_path = tmp_path / "k.db"
    await _make_legacy_db(db_path)

    store = KnowledgeStore(db_path)
    await store.init()
    results = await store.search("wf", "REST")
    assert len(results) == 1
    assert "REST" in results[0].fact
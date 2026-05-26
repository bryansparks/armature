import json
import aiosqlite
import pytest
from datetime import datetime, timezone, timedelta
from armature.state.memory import MemoryStore


async def test_record_and_load(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "stage1", "summary", "first run output")
    memories, _ = await store.load("wf")
    assert memories["stage1"]["summary"] == ["first run output"]


async def test_load_empty_returns_empty(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    memories, stale = await store.load("wf")
    assert memories == {} and not stale


async def test_load_missing_db_returns_empty(tmp_path):
    store = MemoryStore(tmp_path / "nonexistent.db")
    memories, stale = await store.load("wf")
    assert memories == {} and not stale


async def test_multiple_entries_newest_first(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "s1", "key", "first")
    await store.record("wf", "s1", "key", "second")
    await store.record("wf", "s1", "key", "third")
    memories, _ = await store.load("wf")
    assert memories["s1"]["key"] == ["third", "second", "first"]


async def test_max_entries_evicts_oldest(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    for i in range(6):
        await store.record("wf", "s1", "key", f"run-{i}", max_entries=5)
    memories, _ = await store.load("wf")
    entries = memories["s1"]["key"]
    assert len(entries) == 5
    assert "run-0" not in entries   # oldest evicted
    assert "run-5" in entries       # newest kept


async def test_separate_workflows_isolated(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf-a", "s1", "key", "alpha")
    await store.record("wf-b", "s1", "key", "beta")
    a, _ = await store.load("wf-a")
    b, _ = await store.load("wf-b")
    assert a["s1"]["key"] == ["alpha"]
    assert b["s1"]["key"] == ["beta"]


async def test_clear_removes_workflow_entries(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "s1", "key", "value")
    await store.clear("wf")
    memories, stale = await store.load("wf")
    assert memories == {} and not stale


async def test_dict_value_round_trips(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    payload = {"decision": "proceed", "score": 0.9}
    await store.record("wf", "s1", "result", payload)
    memories, _ = await store.load("wf")
    assert memories["s1"]["result"][0] == payload


async def test_memory_record_accepts_quality_param(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "s1", "key", "value", quality=0.9)
    memories, _ = await store.load("wf")
    assert memories["s1"]["key"] == ["value"]


async def test_memory_evicts_low_quality_first(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "s1", "key", "low-quality-old", max_entries=2, quality=0.2)
    await store.record("wf", "s1", "key", "high-quality", max_entries=2, quality=0.9)
    await store.record("wf", "s1", "key", "new-entry", max_entries=2, quality=0.5)
    memories, _ = await store.load("wf")
    entries = memories["s1"]["key"]
    assert len(entries) == 2
    assert "low-quality-old" not in entries
    assert "high-quality" in entries
    assert "new-entry" in entries


# ── Phase A: Memory Staleness (RED) ─────────────────────────────────────────

async def test_load_returns_tuple_of_memories_and_stale_keys(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "s1", "key", "value")
    result = await store.load("wf")
    assert isinstance(result, tuple), "load() must return (memories, stale_keys)"
    memories, stale_keys = result
    assert isinstance(memories, dict)
    assert isinstance(stale_keys, set)


async def test_fresh_memory_not_in_stale_keys(tmp_path):
    store = MemoryStore(tmp_path / "mem.db", staleness_threshold_days=30.0)
    await store.init()
    await store.record("wf", "s1", "summary", "fresh value")
    memories, stale_keys = await store.load("wf")
    assert ("s1", "summary") not in stale_keys
    assert memories["s1"]["summary"] == ["fresh value"]


async def test_old_memory_appears_in_stale_keys(tmp_path):
    db_path = tmp_path / "mem.db"
    store = MemoryStore(db_path, staleness_threshold_days=30.0)
    await store.init()
    # Insert a row with a timestamp 40 days in the past
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO memories (workflow_name, stage_id, capture_key, value, quality, timestamp) "
            "VALUES (?,?,?,?,?,?)",
            ("wf", "s1", "old_key", json.dumps("stale value"), 0.5, old_ts),
        )
        await db.commit()
    memories, stale_keys = await store.load("wf")
    assert ("s1", "old_key") in stale_keys
    assert memories["s1"]["old_key"] == ["stale value"]


async def test_stale_threshold_is_configurable(tmp_path):
    db_path = tmp_path / "mem.db"
    store_tight = MemoryStore(db_path, staleness_threshold_days=5.0)
    await store_tight.init()
    # Insert a 10-day-old entry
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO memories (workflow_name, stage_id, capture_key, value, quality, timestamp) "
            "VALUES (?,?,?,?,?,?)",
            ("wf", "s1", "key", json.dumps("data"), 0.5, old_ts),
        )
        await db.commit()
    _, stale_tight = await store_tight.load("wf")
    assert ("s1", "key") in stale_tight

    # Same DB with a lenient threshold: same entry should NOT be stale
    store_lenient = MemoryStore(db_path, staleness_threshold_days=30.0)
    _, stale_lenient = await store_lenient.load("wf")
    assert ("s1", "key") not in stale_lenient


async def test_default_staleness_threshold_is_30_days(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    assert store._staleness_days == 30.0

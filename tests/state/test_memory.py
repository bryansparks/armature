import pytest
from armature.state.memory import MemoryStore


async def test_record_and_load(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "stage1", "summary", "first run output")
    memories = await store.load("wf")
    assert memories["stage1"]["summary"] == ["first run output"]


async def test_load_empty_returns_empty(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    assert await store.load("wf") == {}


async def test_load_missing_db_returns_empty(tmp_path):
    store = MemoryStore(tmp_path / "nonexistent.db")
    assert await store.load("wf") == {}


async def test_multiple_entries_newest_first(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "s1", "key", "first")
    await store.record("wf", "s1", "key", "second")
    await store.record("wf", "s1", "key", "third")
    memories = await store.load("wf")
    assert memories["s1"]["key"] == ["third", "second", "first"]


async def test_max_entries_evicts_oldest(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    for i in range(6):
        await store.record("wf", "s1", "key", f"run-{i}", max_entries=5)
    memories = await store.load("wf")
    entries = memories["s1"]["key"]
    assert len(entries) == 5
    assert "run-0" not in entries   # oldest evicted
    assert "run-5" in entries       # newest kept


async def test_separate_workflows_isolated(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf-a", "s1", "key", "alpha")
    await store.record("wf-b", "s1", "key", "beta")
    a = await store.load("wf-a")
    b = await store.load("wf-b")
    assert a["s1"]["key"] == ["alpha"]
    assert b["s1"]["key"] == ["beta"]


async def test_clear_removes_workflow_entries(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "s1", "key", "value")
    await store.clear("wf")
    assert await store.load("wf") == {}


async def test_dict_value_round_trips(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    payload = {"decision": "proceed", "score": 0.9}
    await store.record("wf", "s1", "result", payload)
    memories = await store.load("wf")
    assert memories["s1"]["result"][0] == payload


async def test_memory_record_accepts_quality_param(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "s1", "key", "value", quality=0.9)
    memories = await store.load("wf")
    assert memories["s1"]["key"] == ["value"]


async def test_memory_evicts_low_quality_first(tmp_path):
    store = MemoryStore(tmp_path / "mem.db")
    await store.init()
    await store.record("wf", "s1", "key", "low-quality-old", max_entries=2, quality=0.2)
    await store.record("wf", "s1", "key", "high-quality", max_entries=2, quality=0.9)
    await store.record("wf", "s1", "key", "new-entry", max_entries=2, quality=0.5)
    entries = (await store.load("wf"))["s1"]["key"]
    assert len(entries) == 2
    assert "low-quality-old" not in entries
    assert "high-quality" in entries
    assert "new-entry" in entries

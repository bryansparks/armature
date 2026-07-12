from __future__ import annotations
import pytest
from armature.registry.registry import ToolRegistry
from armature.registry.memory_tools import reciprocal_rank_fusion


def _make_record(rid, fact, entity="e", confidence=0.5, type_="fact", superseded_by=None):
    from armature.state.knowledge import KnowledgeRecord, MemoryType
    return KnowledgeRecord(
        id=rid, workflow_name="wf", entity=entity, fact=fact,
        confidence=confidence, source_run_id="r1", type=MemoryType(type_),
        superseded_by=superseded_by,
    )


def test_rrf_overlap_ranks_first():
    bm = [_make_record(1, "alpha"), _make_record(2, "beta")]
    sm = [_make_record(2, "beta"), _make_record(3, "gamma")]
    fused = reciprocal_rank_fusion(bm, sm, k=60, top_k=3)
    ids = [rid for rid, _ in fused]
    assert ids[0] == 2          # appears in both → highest RRF score
    assert len(ids) == 3        # union, no dupes
    assert len(set(ids)) == 3


def test_rrf_empty_when_no_results():
    fused = reciprocal_rank_fusion([], [], top_k=5)
    assert fused == []


async def test_search_records_dispatches_and_fuses(tmp_path):
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType
    from armature.registry.memory_tools import register_memory_tools

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="dogs", fact="dogs are loyal animals",
        confidence=0.9, source_run_id="r1", type=MemoryType.FACT))

    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=None, knowledge_store=store, trace_store=None,
        embedder=None, workflow_name="wf", run_id="r1",
    )
    result = await reg.dispatch("memory.search_records", {"query": "dogs", "top_k": 5})
    assert result["records"]
    rec = result["records"][0]
    assert rec["entity"] == "dogs"
    assert rec["id"] is not None
    assert rec["type"] == "fact"


async def test_search_records_excludes_superseded(tmp_path):
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType
    from armature.registry.memory_tools import register_memory_tools

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    old_id = await store.record(KnowledgeRecord(
        workflow_name="wf", entity="dogs", fact="dogs are loyal animals",
        confidence=0.5, source_run_id="r1", type=MemoryType.FACT))
    new_id = await store.record(KnowledgeRecord(
        workflow_name="wf", entity="dogs", fact="dogs are loyal animals",
        confidence=0.95, source_run_id="r2", type=MemoryType.FACT))
    await store.set_superseded(old_id, new_id)

    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=None, knowledge_store=store, trace_store=None,
        embedder=None, workflow_name="wf", run_id="r1",
    )
    result = await reg.dispatch("memory.search_records", {"query": "dogs"})
    ids = [r["id"] for r in result["records"]]
    assert old_id not in ids
    assert new_id in ids


async def test_get_records_by_id(tmp_path):
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType
    from armature.registry.memory_tools import register_memory_tools

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    rid = await store.record(KnowledgeRecord(
        workflow_name="wf", entity="cats", fact="cats are independent",
        confidence=0.8, source_run_id="r1", type=MemoryType.FACT))

    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=None, knowledge_store=store, trace_store=None,
        embedder=None, workflow_name="wf", run_id="r1",
    )
    result = await reg.dispatch("memory.get_records", {"ids": [rid, 9999]})
    ids = [r["id"] for r in result["records"]]
    assert rid in ids
    assert 9999 not in ids  # missing id dropped, no crash


async def test_search_records_closure_isolation(tmp_path):
    """Two registries over two stores return different data."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType
    from armature.registry.memory_tools import register_memory_tools

    s1 = KnowledgeStore(tmp_path / "k1.db"); await s1.init()
    s2 = KnowledgeStore(tmp_path / "k2.db"); await s2.init()
    await s1.record(KnowledgeRecord(
        workflow_name="wf", entity="alpha", fact="alpha fact",
        confidence=0.9, source_run_id="r1", type=MemoryType.FACT))
    await s2.record(KnowledgeRecord(
        workflow_name="wf", entity="beta", fact="beta fact",
        confidence=0.9, source_run_id="r1", type=MemoryType.FACT))

    r1 = ToolRegistry(); r2 = ToolRegistry()
    register_memory_tools(r1, memory_store=None, knowledge_store=s1, trace_store=None, embedder=None, workflow_name="wf", run_id="r1")
    register_memory_tools(r2, memory_store=None, knowledge_store=s2, trace_store=None, embedder=None, workflow_name="wf", run_id="r1")

    o1 = await r1.dispatch("memory.search_records", {"query": "alpha"})
    o2 = await r2.dispatch("memory.search_records", {"query": "beta"})
    assert {r["entity"] for r in o1["records"]} == {"alpha"}
    assert {r["entity"] for r in o2["records"]} == {"beta"}


async def test_search_records_no_store_returns_empty():
    from armature.registry.memory_tools import register_memory_tools
    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=None, knowledge_store=None, trace_store=None,
        embedder=None, workflow_name="wf", run_id="r1",
    )
    result = await reg.dispatch("memory.search_records", {"query": "x"})
    assert result["records"] == []
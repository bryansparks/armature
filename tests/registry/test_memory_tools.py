from __future__ import annotations
import pytest
from armature.registry.registry import ToolRegistry
from armature.registry.memory_tools import reciprocal_rank_fusion
from armature.permissions.permissions import PermissionLevel, Reversibility


async def _fresh_registry():
    return ToolRegistry()


def _descriptor(registry: ToolRegistry, name: str):
    # Note: registry.descriptors() returns only {name,description,parameters}
    # (no handler). Use registry.get() to access the ToolDescriptor.handler so
    # tests can invoke it directly.
    desc = registry.get(name)
    if desc is None:
        return None
    return {
        "name": desc.name,
        "description": desc.description,
        "parameters": desc.parameters,
        "handler": desc.handler,
    }


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


async def test_search_conversation_scans_l0(tmp_path):
    from armature.state.memory import MemoryStore
    from armature.registry.memory_tools import register_memory_tools

    mem = MemoryStore(tmp_path / "mem.db"); await mem.init()
    await mem.record("wf", "researcher", "summary", "the auth pattern is oauth2")
    await mem.record("wf", "writer", "draft", "final report on unrelated topic")

    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=mem, knowledge_store=None, trace_store=None,
        embedder=None, workflow_name="wf", run_id="r1",
    )
    result = await reg.dispatch("memory.search_conversation", {"query": "oauth"})
    caps = result["captures"]
    assert len(caps) == 1
    assert caps[0]["stage_id"] == "researcher"
    assert "oauth" in caps[0]["value"]


async def test_search_conversation_stage_filter(tmp_path):
    from armature.state.memory import MemoryStore
    from armature.registry.memory_tools import register_memory_tools

    mem = MemoryStore(tmp_path / "mem.db"); await mem.init()
    await mem.record("wf", "researcher", "summary", "oauth2 auth pattern")
    await mem.record("wf", "writer", "draft", "oauth2 mention in draft")

    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=mem, knowledge_store=None, trace_store=None,
        embedder=None, workflow_name="wf", run_id="r1",
    )
    result = await reg.dispatch(
        "memory.search_conversation", {"query": "oauth", "stage_id": "writer"})
    assert len(result["captures"]) == 1
    assert result["captures"][0]["stage_id"] == "writer"


async def test_read_track_returns_null_when_no_store():
    from armature.registry.memory_tools import register_memory_tools
    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=None, knowledge_store=None, trace_store=None,
        embedder=None, workflow_name="wf", run_id="r1",
    )
    result = await reg.dispatch("memory.read_track", {})
    assert result == {"tracks": []}


async def test_read_profile_returns_null_when_no_store():
    from armature.registry.memory_tools import register_memory_tools
    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=None, knowledge_store=None, trace_store=None,
        embedder=None, workflow_name="wf", run_id="r1",
    )
    result = await reg.dispatch("memory.read_profile", {})
    assert result == {"content": None}


async def test_get_run_trace_uses_trace_store(tmp_path):
    from armature.state.traces import TraceStore, TraceRecord
    from armature.registry.memory_tools import register_memory_tools

    ts = TraceStore(tmp_path / "tr.db"); await ts.init()
    await ts.record(TraceRecord(
        run_id="r9", workflow_name="wf", stage_id="s1", role_type="worker",
        model="m", input_tokens=1, output_tokens=1, latency_ms=10, success=True,
        output_valid=True, outputs={"content": "hello"}, tools_declared=[],
        tools_called=[], spec_version="v", inputs_hash="h", policy_version="p",
    ))

    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=None, knowledge_store=None, trace_store=ts,
        embedder=None, workflow_name="wf", run_id="r9",
    )
    result = await reg.dispatch("memory.get_run_trace", {})
    assert result["traces"]
    assert result["traces"][0]["stage_id"] == "s1"
    assert result["traces"][0]["outputs"]["content"] == "hello"


async def test_get_run_trace_stage_filter(tmp_path):
    from armature.state.traces import TraceStore, TraceRecord
    from armature.registry.memory_tools import register_memory_tools

    ts = TraceStore(tmp_path / "tr.db"); await ts.init()
    for sid in ("s1", "s2"):
        await ts.record(TraceRecord(
            run_id="r9", workflow_name="wf", stage_id=sid, role_type="worker",
            model="m", input_tokens=1, output_tokens=1, latency_ms=10, success=True,
            output_valid=True, outputs={"content": sid}, tools_declared=[],
            tools_called=[], spec_version="v", inputs_hash="h", policy_version="p",
        ))

    reg = ToolRegistry()
    register_memory_tools(
        reg, memory_store=None, knowledge_store=None, trace_store=ts,
        embedder=None, workflow_name="wf", run_id="r9",
    )
    result = await reg.dispatch("memory.get_run_trace", {"stage_id": "s2"})
    assert len(result["traces"]) == 1
    assert result["traces"][0]["stage_id"] == "s2"


# ── Phase 3: read-tool wiring + write-tool registration ──

async def test_read_track_returns_track_when_id(tmp_path):
    from armature.state.tracks import TrackStore
    from armature.registry.memory_tools import register_memory_tools
    ts = TrackStore(tmp_path / "k.db")
    await ts.init()
    await ts.upsert_track("wf", "auth", "Auth", "s", None, [], 2000, 20)
    reg = await _fresh_registry()
    register_memory_tools(reg, memory_store=None, knowledge_store=None,
                          trace_store=None, embedder=None,
                          workflow_name="wf", run_id="r1", track_store=ts)
    desc = _descriptor(reg, "memory.read_track")
    out = await desc["handler"]({"track_id": "auth"})
    assert out["track"]["track_id"] == "auth"


async def test_read_track_lists_when_no_id(tmp_path):
    from armature.state.tracks import TrackStore
    from armature.registry.memory_tools import register_memory_tools
    ts = TrackStore(tmp_path / "k.db")
    await ts.init()
    await ts.upsert_track("wf", "a", "A", "s", None, [], 2000, 20)
    await ts.upsert_track("wf", "b", "B", "s", None, [], 2000, 20)
    reg = await _fresh_registry()
    register_memory_tools(reg, memory_store=None, knowledge_store=None,
                          trace_store=None, embedder=None,
                          workflow_name="wf", run_id="r1", track_store=ts)
    out = await _descriptor(reg, "memory.read_track")["handler"]({})
    assert {t["track_id"] for t in out["tracks"]} == {"a", "b"}


async def test_read_track_empty_when_store_none():
    from armature.registry.memory_tools import register_memory_tools
    reg = await _fresh_registry()
    register_memory_tools(reg, memory_store=None, knowledge_store=None,
                          trace_store=None, embedder=None,
                          workflow_name="wf", run_id="r1")
    out = await _descriptor(reg, "memory.read_track")["handler"]({})
    assert out == {"tracks": []}


async def test_read_profile_returns_content(tmp_path):
    from armature.state.profile import ProfileStore
    from armature.registry.memory_tools import register_memory_tools
    ps = ProfileStore(tmp_path / "k.db")
    await ps.init()
    await ps.upsert_profile("wf", "body", 2000)
    reg = await _fresh_registry()
    register_memory_tools(reg, memory_store=None, knowledge_store=None,
                          trace_store=None, embedder=None,
                          workflow_name="wf", run_id="r1", profile_store=ps)
    out = await _descriptor(reg, "memory.read_profile")["handler"]({})
    assert out["content"] == "body"


async def test_read_profile_null_when_store_none():
    from armature.registry.memory_tools import register_memory_tools
    reg = await _fresh_registry()
    register_memory_tools(reg, memory_store=None, knowledge_store=None,
                          trace_store=None, embedder=None,
                          workflow_name="wf", run_id="r1")
    out = await _descriptor(reg, "memory.read_profile")["handler"]({})
    assert out == {"content": None}


async def test_write_track_dispatch(tmp_path):
    from armature.state.tracks import TrackStore
    from armature.registry.memory_tools import register_memory_tools
    ts = TrackStore(tmp_path / "k.db")
    await ts.init()
    reg = await _fresh_registry()
    register_memory_tools(reg, memory_store=None, knowledge_store=None,
                          trace_store=None, embedder=None,
                          workflow_name="wf", run_id="r1", track_store=ts,
                          profile_store=None, curator_stage="curator")
    desc = _descriptor(reg, "memory.write_track")
    out = await desc["handler"]({
        "track_id": "t", "title": "T", "summary": "s", "evidence_links": [],
    })
    assert out["track_id"] == "t"
    # write_profile is NOT registered here because profile_store=None; that
    # gating is covered by test_write_profile_dispatch and
    # test_write_tools_not_registered_without_curator_stage.


async def test_write_profile_dispatch(tmp_path):
    from armature.state.profile import ProfileStore
    from armature.registry.memory_tools import register_memory_tools
    ps = ProfileStore(tmp_path / "k.db")
    await ps.init()
    reg = await _fresh_registry()
    register_memory_tools(reg, memory_store=None, knowledge_store=None,
                          trace_store=None, embedder=None,
                          workflow_name="wf", run_id="r1", profile_store=ps,
                          track_store=None, curator_stage="curator")
    out = await _descriptor(reg, "memory.write_profile")["handler"]({"content": "body"})
    assert "updated_at" in out


async def test_write_tools_not_registered_without_curator_stage(tmp_path):
    from armature.state.tracks import TrackStore
    from armature.state.profile import ProfileStore
    from armature.registry.memory_tools import register_memory_tools
    ts = TrackStore(tmp_path / "k.db"); await ts.init()
    ps = ProfileStore(tmp_path / "k.db"); await ps.init()
    reg = await _fresh_registry()
    register_memory_tools(reg, memory_store=None, knowledge_store=None,
                          trace_store=None, embedder=None,
                          workflow_name="wf", run_id="r1",
                          track_store=ts, profile_store=ps, curator_stage=None)
    assert _descriptor(reg, "memory.write_track") is None
    assert _descriptor(reg, "memory.write_profile") is None
    # read tools still present
    assert _descriptor(reg, "memory.read_track") is not None


async def test_write_tool_permissions_workspace_partial(tmp_path):
    from armature.state.tracks import TrackStore
    from armature.registry.memory_tools import register_memory_tools
    ts = TrackStore(tmp_path / "k.db"); await ts.init()
    reg = await _fresh_registry()
    register_memory_tools(reg, memory_store=None, knowledge_store=None,
                          trace_store=None, embedder=None,
                          workflow_name="wf", run_id="r1", track_store=ts,
                          curator_stage="curator")
    # PermissionLevel/Reversibility are stored on the descriptor; descriptors() only
    # returns name/description/parameters, so verify by inspecting the registry's
    # internal tool objects directly.
    tools = [t for t in reg._tools.values() if t.name == "memory.write_track"]
    assert tools and tools[0].permission == PermissionLevel.WORKSPACE
    assert tools[0].reversibility == Reversibility.PARTIAL
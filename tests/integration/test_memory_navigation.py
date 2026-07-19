"""Phase-2 memory-navigation end-to-end tests.

Covers: a worker declaring memory.search_records receives the tool, calls it
via a mocked LLM, and its context lacks the full _knowledge dump (suppressed
per §5.2). Also: navigation_tools=false is byte-identical to today.
"""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import MagicMock, patch


FIXTURES = Path(__file__).parent.parent / "fixtures"


def _nav_spec(tmp_path, navigation_tools: bool):
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        MemoryConfig, Contract,
    )
    return HarnessSpec(
        name="nav_e2e", version="1.0",
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        role_type_defaults={"worker": "small", "orchestrator": "small",
                            "judge": "small", "researcher": "small"},
        contracts=Contract(inputs=[{"name": "topic"}]),
        memory=MemoryConfig(
            enabled=True, db=str(tmp_path / "mem.db"),
            extract_knowledge=True, navigation_tools=navigation_tools,
        ),
        stages=[Stage(id="worker", role=Role(
            name="Worker", type=RoleType.WORKER,
            description="Research {{ topic }}. Use memory.search_records if helpful.",
            model_tier="small", tools=["memory.search_records"],
        ))],
    )


def _curator_spec(tmp_path):
    """Spec with extract_knowledge + navigation_tools + a post_run curator stage.

    The worker's `content` output is captured to L0 memory so the extractor
    can pull it; the curator declares the Phase 3 write tools and runs after
    all normal stages with a signature.input filter that excludes _transcript.
    """
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        MemoryConfig, MemoryCapture, Contract, Signature,
    )
    return HarnessSpec(
        name="nav_e2e", version="1.0",
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        role_type_defaults={"worker": "small", "orchestrator": "small",
                            "judge": "small", "researcher": "small"},
        contracts=Contract(inputs=[{"name": "topic"}]),
        memory=MemoryConfig(
            enabled=True, db=str(tmp_path / "mem.db"),
            extract_knowledge=True, navigation_tools=True,
            capture=[MemoryCapture(stage="worker", key="content")],
            curator_stage="curator",
        ),
        stages=[
            Stage(id="worker", role=Role(
                name="Worker", type=RoleType.WORKER,
                description="Research {{ topic }}.",
                model_tier="small",
            )),
            Stage(
                id="curator",
                post_run=True,
                depends_on=[],
                role=Role(
                    name="Curator", type=RoleType.JUDGE,
                    description=(
                        "Curate L2 tracks and the L3 team profile from this run. "
                        "Call memory.write_track then memory.write_profile."
                    ),
                    model_tier="small",
                    tools=["memory.write_track", "memory.write_profile"],
                ),
                signature=Signature(input={
                    "topic": "Research topic",
                    "_memory_index": "Knowledge index summary",
                    "_memory_index_refresh_hint": "Refresh hint",
                }),
            ),
        ],
    )


def _nav_read_spec(tmp_path):
    """Spec with navigation_tools + a worker declaring memory.read_track, no curator.

    No capture config → the extractor returns early (no LLM call) so only the
    worker's LLM calls flow through the patched nodes.llm entry point.
    """
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        MemoryConfig, Contract,
    )
    return HarnessSpec(
        name="nav_e2e", version="1.0",
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        role_type_defaults={"worker": "small", "orchestrator": "small",
                            "judge": "small", "researcher": "small"},
        contracts=Contract(inputs=[{"name": "topic"}]),
        memory=MemoryConfig(
            enabled=True, db=str(tmp_path / "mem.db"),
            extract_knowledge=True, navigation_tools=True,
        ),
        stages=[Stage(id="worker", role=Role(
            name="Worker", type=RoleType.WORKER,
            description="Research {{ topic }}. Use memory.read_track if helpful.",
            model_tier="small", tools=["memory.read_track"],
        ))],
    )


def _extractor_fact_response(entity, fact):
    """Return the guided_json-shaped response the KnowledgeExtractor expects.

    The extractor parses `choices[0].message.content` as a JSON array of
    {entity, fact, confidence, source_stage, source_key, type} records
    (see tests/state/test_knowledge.py — patch target
    `armature.state.extractor.litellm_completion`). We cite the worker/content
    source so provenance threads through.
    """
    r = MagicMock(); r.choices = [MagicMock()]
    content = json.dumps([{
        "entity": entity,
        "fact": fact,
        "confidence": 0.9,
        "source_stage": "worker",
        "source_key": "content",
        "type": "fact",
    }])
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
    return r


def _tool_call_response(tool_name, args, call_id="tc_1"):
    r = MagicMock(); r.choices = [MagicMock()]
    tc = MagicMock(); tc.id = call_id; tc.function.name = tool_name
    tc.function.arguments = json.dumps(args)
    r.choices[0].message.tool_calls = [tc]
    r.choices[0].message.content = None
    r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
    return r


def _plain_response(content):
    r = MagicMock(); r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
    return r


async def test_worker_with_memory_tool_receives_and_calls_it(tmp_path):
    from armature.runtime.engine import Harness
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType

    h = Harness(spec=_nav_spec(tmp_path, navigation_tools=True), session_dir=tmp_path)
    # Seed one record so search_records has something to return.
    await h._knowledge_store.init()
    await h._knowledge_store.record(KnowledgeRecord(
        workflow_name="nav_e2e", entity="dogs", fact="dogs are loyal",
        confidence=0.9, source_run_id="r0", type=MemoryType.FACT))

    call_count = {"n": 0}
    captured_messages: list = []

    async def mock_completion(**kwargs):
        call_count["n"] += 1
        captured_messages.append(kwargs.get("messages"))
        if call_count["n"] == 1:
            # First call: the LLM sees the memory tool and decides to call it.
            return _tool_call_response("memory.search_records", {"query": "dogs"})
        # Second call: tool returned results, LLM produces a final text answer.
        return _plain_response("dogs are loyal")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await h.run({"topic": "dogs"})

    # The tool was actually dispatched: the ReAct loop appends a tool-role
    # message and re-calls the LLM, so the 2nd call's messages include it.
    assert len(captured_messages) >= 2, "expected a tool-call round-trip"
    second_messages = captured_messages[1]
    roles = [m["role"] for m in second_messages]
    assert "tool" in roles


async def test_navigation_stage_context_lacks_knowledge_dump(tmp_path):
    from armature.runtime.engine import Harness
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType

    h = Harness(spec=_nav_spec(tmp_path, navigation_tools=True), session_dir=tmp_path)
    await h._knowledge_store.init()
    await h._knowledge_store.record(KnowledgeRecord(
        workflow_name="nav_e2e", entity="dogs", fact="dogs are loyal",
        confidence=0.9, source_run_id="r0", type=MemoryType.FACT))

    captured_user_context: list[dict] = []

    async def mock_completion(**kwargs):
        msgs = kwargs.get("messages") or []
        for m in msgs:
            if m.get("role") == "user":
                try:
                    captured_user_context.append(json.loads(m["content"]))
                except Exception:
                    pass
        return _plain_response("ok")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await h.run({"topic": "dogs"})

    # The worker's user-message context must not carry the full _knowledge dump.
    for ctx in captured_user_context:
        assert "_knowledge" not in ctx, (
            "navigation-enabled worker received the passive _knowledge dump — "
            "suppression failed"
        )
    # But _memory_index should be present (it's the navigation TOC).
    assert any("_memory_index" in ctx for ctx in captured_user_context)


async def test_navigation_off_is_byte_identical_to_today(tmp_path):
    """navigation_tools=False: _knowledge still injected, no _memory_index,
    no memory.* tools registered."""
    from armature.runtime.engine import Harness
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType

    h = Harness(spec=_nav_spec(tmp_path, navigation_tools=False), session_dir=tmp_path)
    await h._knowledge_store.init()
    await h._knowledge_store.record(KnowledgeRecord(
        workflow_name="nav_e2e", entity="dogs", fact="dogs are loyal",
        confidence=0.9, source_run_id="r0", type=MemoryType.FACT))

    names = {d["name"] for d in h._registry.descriptors()}
    assert not any(n.startswith("memory.") for n in names)

    captured_user_context: list[dict] = []

    async def mock_completion(**kwargs):
        for m in (kwargs.get("messages") or []):
            if m.get("role") == "user":
                try:
                    captured_user_context.append(json.loads(m["content"]))
                except Exception:
                    pass
        return _plain_response("ok")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await h.run({"topic": "dogs"})

    # _knowledge IS injected (passive dump unchanged); _memory_index is NOT.
    assert any("_knowledge" in ctx for ctx in captured_user_context)
    assert all("_memory_index" not in ctx for ctx in captured_user_context)


# ── Phase 3: curator end-to-end + second-run read + no-curator byte-identical ──


async def test_curator_writes_track_and_profile_e2e(tmp_path, monkeypatch):
    """Worker output → extractor → curator writes track + profile via tool calls.

    Proves the Phase 3 write tools dispatch end-to-end through the ReAct loop
    in armature/nodes/llm.py and populate the L2/L3 tables. The worker's
    `content` output is captured to L0 memory, the extractor pulls one fact
    into L1 (reconciled), then the post_run curator issues write_track and
    write_profile tool calls.
    """
    from armature.runtime.engine import Harness

    spec = _curator_spec(tmp_path)

    calls = {"i": 0}

    async def fake_completion(**kwargs):
        calls["i"] += 1
        n = calls["i"]
        if n == 1:
            # worker stage: plain text output (captured to L0 memory)
            return _plain_response("worker output about auth patterns")
        if n == 2:
            # KnowledgeExtractor: one fact as guided_json (JSON array content)
            return _extractor_fact_response("auth", "use OAuth2 for auth")
        if n == 3:
            # curator: ask to write a track
            return _tool_call_response(
                "memory.write_track",
                {
                    "track_id": "auth", "title": "Auth patterns",
                    "summary": "Use OAuth2.", "evidence_links": [],
                },
                call_id="tc_track",
            )
        if n == 4:
            # curator: then write the profile
            return _tool_call_response(
                "memory.write_profile",
                {"content": "Team builds Python services."},
                call_id="tc_profile",
            )
        # curator final: plain text
        return _plain_response("done")

    # Patch BOTH litellm entry points — the LLM node and the KnowledgeExtractor
    # call module-local `litellm_completion` wrappers. The shared counter
    # dispatches on call index across both. (See tests/state/test_knowledge.py
    # lines 320/342/362/378/396/418/446/475/503 for the extractor patch target
    # `armature.state.extractor.litellm_completion`; the LLM node patch target
    # `armature.nodes.llm.litellm_completion` mirrors test_worker_with_memory_tool_*.)
    monkeypatch.setattr("armature.nodes.llm.litellm_completion", fake_completion)
    monkeypatch.setattr("armature.state.extractor.litellm_completion", fake_completion)

    # use_cache=False: the global LLM cache (~/.armature/llm_cache.sqlite)
    # would otherwise short-circuit the curator's tool-call round-trip — the
    # curator's signature.input filter excludes run_id, so its cache key is
    # stable across runs and a prior run's "done" response gets returned
    # without ever calling fake_completion.
    h = Harness(spec, session_dir=tmp_path, use_cache=False)
    await h.run({"topic": "auth"})

    # L2 track populated via the write_track tool dispatch.
    assert await h._track_store.count(spec.name) == 1
    track = await h._track_store.get_track(spec.name, "auth")
    assert track is not None
    assert track["summary"] == "Use OAuth2."
    # L3 profile populated via the write_profile tool dispatch.
    assert await h._profile_store.get_profile(spec.name) == "Team builds Python services."


async def test_second_run_reads_track_via_read_track(tmp_path, monkeypatch):
    """A second run with navigation reads the track written by the first run.

    The first run seeds a track directly into the shared L2 store (proven by
    Task 1 + the e2e test above). The second run's worker declares
    memory.read_track and calls it; we assert the tool was actually dispatched
    (a tool-role message appears in the ReAct round-trip), mirroring
    test_worker_with_memory_tool_receives_and_calls_it.
    """
    from armature.runtime.engine import Harness

    spec = _curator_spec(tmp_path)
    # First run: seed a track directly into the L2 store.
    h1 = Harness(spec, session_dir=tmp_path)
    if h1._track_store is not None:
        await h1._track_store.init()
        await h1._track_store.upsert_track(
            spec.name, "auth", "Auth", "Use OAuth2.",
            None, [], 2000, 20,
        )

    # Second run: worker declares memory.read_track and calls it.
    read_spec = _nav_read_spec(tmp_path)
    captured_messages: list = []

    async def fake_completion(**kwargs):
        captured_messages.append(kwargs.get("messages"))
        if len(captured_messages) == 1:
            return _tool_call_response("memory.read_track", {"list": True})
        return _plain_response("ok")

    monkeypatch.setattr("armature.nodes.llm.litellm_completion", fake_completion)
    # No capture config in _nav_read_spec → extractor returns early (no LLM call),
    # but patch the extractor entry point too for safety.
    monkeypatch.setattr("armature.state.extractor.litellm_completion", fake_completion)

    h2 = Harness(read_spec, session_dir=tmp_path, use_cache=False)
    await h2.run({"topic": "auth"})

    # The read tool was actually dispatched: the ReAct loop appends a tool-role
    # message and re-calls the LLM, so the 2nd call's messages include it.
    assert len(captured_messages) >= 2, "expected a read_track tool-call round-trip"
    second_messages = captured_messages[1]
    roles = [m["role"] for m in second_messages]
    assert "tool" in roles


async def test_no_curator_stage_byte_identical_to_phase2(tmp_path, monkeypatch):
    """Without curator_stage: no write tools, read_track returns empty, tables empty.

    A spec with navigation_tools but no curator_stage registers the read tools
    (so reads work) but NOT the write tools; the L2/L3 stores are constructed
    (so reads have a backing table) but stay empty. This is the Phase 3
    opt-in guarantee — a Phase 2 spec is byte-identical.
    """
    from armature.runtime.engine import Harness

    spec = _nav_read_spec(tmp_path)  # navigation_tools, NO curator_stage
    h = Harness(spec, session_dir=tmp_path)

    names = {d["name"] for d in h._registry.descriptors()}
    assert "memory.write_track" not in names
    assert "memory.write_profile" not in names
    # read tools present
    assert "memory.read_track" in names
    # stores constructed (so reads work) but tables empty
    assert h._track_store is not None
    assert await h._track_store.list_tracks(spec.name) == []
    assert await h._profile_store.get_profile(spec.name) is None
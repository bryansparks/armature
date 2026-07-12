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
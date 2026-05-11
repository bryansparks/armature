"""Tests for prompt bootstrapping from high-quality traces.

BootstrapStore retrieves few-shot examples from TraceStore.
PromptAssembler injects them as a ## Examples section.
LLMNode wires bootstrap examples into the system prompt when configured.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from armature.spec.models import (
    Stage, Role, RoleType, HarnessSpec, ModelTiers, ModelTierConfig,
    Signature,
)
from armature.runtime.prompt import PromptAssembler
from armature.state.traces import TraceRecord, TraceStore


# ── BootstrapStore ────────────────────────────────────────────────────────────

async def test_bootstrap_store_returns_examples_for_stage(tmp_path):
    """examples_for_stage returns (inputs, outputs) pairs from high-quality traces."""
    from armature.state.bootstrap import BootstrapStore

    store = BootstrapStore(TraceStore(tmp_path / "traces.db"))
    await store._traces.init()

    await store._traces.record(TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="judge",
        role_type="judge", model="m", latency_ms=100,
        success=True, output_valid=True, quorum_score=0.95,
        inputs={"brief": "some research"}, outputs={"decision": "approve"},
    ))

    examples = await store.examples_for_stage(
        workflow_name="wf", stage_id="judge", min_score=0.85, max_examples=5
    )
    assert len(examples) == 1
    assert examples[0]["inputs"] == {"brief": "some research"}
    assert examples[0]["outputs"] == {"decision": "approve"}


async def test_bootstrap_store_filters_below_threshold(tmp_path):
    """Traces with quorum_score below min_score are excluded."""
    from armature.state.bootstrap import BootstrapStore

    store = BootstrapStore(TraceStore(tmp_path / "traces.db"))
    await store._traces.init()

    await store._traces.record(TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s",
        role_type="worker", model="m", latency_ms=100,
        success=True, output_valid=True, quorum_score=0.60,
        inputs={"x": "1"}, outputs={"y": "2"},
    ))

    examples = await store.examples_for_stage("wf", "s", min_score=0.85)
    assert examples == []


async def test_bootstrap_store_filters_by_stage_id(tmp_path):
    """Only traces for the requested stage_id are returned."""
    from armature.state.bootstrap import BootstrapStore

    store = BootstrapStore(TraceStore(tmp_path / "traces.db"))
    await store._traces.init()

    await store._traces.record(TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="research",
        role_type="worker", model="m", latency_ms=100,
        success=True, output_valid=True, quorum_score=0.9,
        inputs={"q": "?"}, outputs={"brief": "..."},
    ))
    await store._traces.record(TraceRecord(
        run_id="r2", workflow_name="wf", stage_id="judge",
        role_type="judge", model="m", latency_ms=100,
        success=True, output_valid=True, quorum_score=0.9,
        inputs={"brief": "..."}, outputs={"decision": "approve"},
    ))

    examples = await store.examples_for_stage("wf", "research")
    assert len(examples) == 1
    assert "q" in examples[0]["inputs"]


async def test_bootstrap_store_respects_max_examples(tmp_path):
    """max_examples caps the number of returned examples."""
    from armature.state.bootstrap import BootstrapStore

    store = BootstrapStore(TraceStore(tmp_path / "traces.db"))
    await store._traces.init()

    for i in range(10):
        await store._traces.record(TraceRecord(
            run_id=f"r{i}", workflow_name="wf", stage_id="s",
            role_type="worker", model="m", latency_ms=100,
            success=True, output_valid=True, quorum_score=0.9,
            inputs={"i": str(i)}, outputs={"o": str(i)},
        ))

    examples = await store.examples_for_stage("wf", "s", max_examples=3)
    assert len(examples) == 3


async def test_bootstrap_store_empty_returns_empty(tmp_path):
    """No traces → no examples, no error."""
    from armature.state.bootstrap import BootstrapStore

    store = BootstrapStore(TraceStore(tmp_path / "traces.db"))
    await store._traces.init()

    examples = await store.examples_for_stage("wf", "s")
    assert examples == []


# ── PromptAssembler — few-shot injection ─────────────────────────────────────

def _make_role(role_type=RoleType.JUDGE):
    return Role(name="r", type=role_type, description="test role")


def test_examples_section_absent_without_examples():
    """No examples → no ## Examples section in prompt."""
    assembler = PromptAssembler()
    role = _make_role()
    prompt = assembler.build(role=role, tools=[], context={}, examples=[])
    assert "## Examples" not in prompt


def test_examples_section_present_with_examples():
    """Providing examples injects an ## Examples section."""
    assembler = PromptAssembler()
    role = _make_role()
    examples = [
        {"inputs": {"brief": "AI research"}, "outputs": {"decision": "approve"}},
    ]
    prompt = assembler.build(role=role, tools=[], context={}, examples=examples)
    assert "## Examples" in prompt


def test_examples_section_contains_input_key():
    """Example input keys appear in the injected section."""
    assembler = PromptAssembler()
    role = _make_role()
    examples = [{"inputs": {"brief": "climate change"}, "outputs": {"decision": "approve"}}]
    prompt = assembler.build(role=role, tools=[], context={}, examples=examples)
    assert "climate change" in prompt or "brief" in prompt


def test_examples_section_contains_output_key():
    """Example output keys appear in the injected section."""
    assembler = PromptAssembler()
    role = _make_role()
    examples = [{"inputs": {"x": "1"}, "outputs": {"decision": "reject"}}]
    prompt = assembler.build(role=role, tools=[], context={}, examples=examples)
    assert "reject" in prompt or "decision" in prompt


def test_multiple_examples_all_present():
    """All provided examples appear in the prompt."""
    assembler = PromptAssembler()
    role = _make_role()
    examples = [
        {"inputs": {"q": "alpha"}, "outputs": {"a": "x"}},
        {"inputs": {"q": "beta"}, "outputs": {"a": "y"}},
        {"inputs": {"q": "gamma"}, "outputs": {"a": "z"}},
    ]
    prompt = assembler.build(role=role, tools=[], context={}, examples=examples)
    assert "alpha" in prompt
    assert "beta" in prompt
    assert "gamma" in prompt


def test_examples_appear_before_context_section():
    """## Examples appears before ## Current Context for prompt flow."""
    assembler = PromptAssembler()
    role = _make_role()
    examples = [{"inputs": {"x": "1"}, "outputs": {"y": "2"}}]
    prompt = assembler.build(role=role, tools=[], context={"z": "val"}, examples=examples)
    examples_pos = prompt.find("## Examples")
    context_pos = prompt.find("## Current Context")
    assert examples_pos != -1
    assert context_pos != -1
    assert examples_pos < context_pos


def test_default_examples_parameter_is_empty_list():
    """Calling build() without examples= doesn't raise — defaults to no examples."""
    assembler = PromptAssembler()
    role = _make_role()
    prompt = assembler.build(role=role, tools=[], context={})
    assert "## Examples" not in prompt


# ── LLMNode wires bootstrap examples ─────────────────────────────────────────

async def test_llmnode_passes_examples_to_assembler():
    """When bootstrap_store is set on LLMNode, examples are fetched and injected."""
    from armature.nodes.llm import LLMNode

    stage = Stage(
        id="judge",
        role=Role(name="r", type=RoleType.JUDGE, description="evaluate"),
    )
    tiers = ModelTiers(small=ModelTierConfig(provider="anthropic", model="claude-haiku-4-5-20251001"))
    node = LLMNode(stage=stage, tiers=tiers, workflow_name="wf")

    # Inject a fake bootstrap store that returns one example
    fake_store = MagicMock()
    fake_store.examples_for_stage = AsyncMock(return_value=[
        {"inputs": {"brief": "test"}, "outputs": {"decision": "approve"}}
    ])
    node._bootstrap_store = fake_store

    captured_prompt = {}

    async def mock_completion(**kwargs):
        captured_prompt["system"] = kwargs.get("messages", [{}])[0].get("content", "")
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "ok"
        resp.choices[0].message.tool_calls = None
        resp.usage = MagicMock()
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5
        return resp

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    assert "## Examples" in captured_prompt["system"]
    assert "approve" in captured_prompt["system"]


async def test_llmnode_without_bootstrap_store_works_normally():
    """LLMNode without _bootstrap_store set works the same as before."""
    from armature.nodes.llm import LLMNode

    stage = Stage(
        id="worker",
        role=Role(name="r", type=RoleType.WORKER, description="do work"),
    )
    tiers = ModelTiers(small=ModelTierConfig(provider="anthropic", model="claude-haiku-4-5-20251001"))
    node = LLMNode(stage=stage, tiers=tiers)

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "result"
        resp.choices[0].message.tool_calls = None
        resp.usage = MagicMock()
        resp.usage.prompt_tokens = 5
        resp.usage.completion_tokens = 3
        return resp

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert result["content"] == "result"

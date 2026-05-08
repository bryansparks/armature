"""Tests for Contract.max_llm_calls and Contract.timeout_hours enforcement."""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, Role, RoleType,
    ToolCallConfig, Contract,
)
from armature.runtime.engine import Harness
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


# ── Helpers ──────────────────────────────────────────────────────────────────

def _llm_response(content: str = "done"):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    r.usage.prompt_tokens = 5
    r.usage.completion_tokens = 5
    return r


def _make_llm_stage(sid: str, deps: list[str] | None = None) -> Stage:
    return Stage(
        id=sid,
        role=Role(name="r", type=RoleType.WORKER, description="test", model_tier="small"),
        depends_on=deps or [],
    )


def _make_tool_stage(sid: str, deps: list[str] | None = None) -> Stage:
    return Stage(id=sid, tool_call=ToolCallConfig(name="t"), depends_on=deps or [])


def _make_harness(stages, contracts, tmp_path) -> Harness:
    spec = HarnessSpec(
        name="wf",
        stages=stages,
        contracts=contracts,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=tmp_path)


# ── max_llm_calls ─────────────────────────────────────────────────────────────

def test_contract_max_llm_calls_defaults_100():
    c = Contract()
    assert c.max_llm_calls == 100


async def test_max_llm_calls_not_exceeded_runs_normally(tmp_path):
    response = _llm_response()

    async def mock_completion(**kwargs):
        return response

    spec = HarnessSpec(
        name="wf",
        stages=[_make_llm_stage("s")],
        contracts=Contract(max_llm_calls=5),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await harness.run({})
    assert "s" in result


async def test_max_llm_calls_exceeded_raises(tmp_path):
    response = _llm_response()

    async def mock_completion(**kwargs):
        return response

    spec = HarnessSpec(
        name="wf",
        stages=[
            _make_llm_stage("s1"),
            _make_llm_stage("s2", deps=["s1"]),
            _make_llm_stage("s3", deps=["s2"]),
        ],
        contracts=Contract(max_llm_calls=2),  # only 2 allowed, 3 stages
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        with pytest.raises(RuntimeError, match="max_llm_calls"):
            await harness.run({})


async def test_max_llm_calls_zero_means_unlimited(tmp_path):
    """max_llm_calls=0 is treated as unlimited (no check)."""
    response = _llm_response()

    async def mock_completion(**kwargs):
        return response

    spec = HarnessSpec(
        name="wf",
        stages=[_make_llm_stage("s1"), _make_llm_stage("s2", deps=["s1"])],
        contracts=Contract(max_llm_calls=0),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await harness.run({})
    assert "s1" in result and "s2" in result


async def test_max_llm_calls_counter_resets_per_run(tmp_path):
    """Each call to run() uses a fresh counter (counter is on harness instance)."""
    response = _llm_response()
    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        return response

    spec = HarnessSpec(
        name="wf",
        stages=[_make_llm_stage("s")],
        contracts=Contract(max_llm_calls=1),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await harness.run({})

    # On a new harness instance, counter starts at 0
    harness2 = Harness(spec=spec, session_dir=tmp_path)
    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await harness2.run({})

    assert call_count == 2


async def test_tool_call_stages_dont_count_against_llm_limit(tmp_path):
    """tool_call and adapter stages don't consume LLM call budget."""
    async def t(args): return {"ok": True}

    spec = HarnessSpec(
        name="wf",
        stages=[_make_tool_stage("s1"), _make_tool_stage("s2", deps=["s1"])],
        contracts=Contract(max_llm_calls=0),  # 0 LLM calls allowed
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=t, parameters={},
    ))

    result = await harness.run({})
    assert result["s1"]["ok"] is True
    assert result["s2"]["ok"] is True


# ── timeout_hours ─────────────────────────────────────────────────────────────

def test_contract_timeout_hours_defaults_8():
    c = Contract()
    assert c.timeout_hours == 8.0


async def test_timeout_hours_exceeded_raises(tmp_path):
    async def slow_tool(args):
        await asyncio.sleep(10)
        return {}

    spec = HarnessSpec(
        name="wf",
        stages=[_make_tool_stage("s")],
        contracts=Contract(timeout_hours=0.00001),  # ~0.036 seconds
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=slow_tool, parameters={},
    ))

    with pytest.raises(TimeoutError, match="timeout_hours"):
        await harness.run({})


async def test_timeout_hours_not_exceeded_runs_normally(tmp_path):
    async def fast_tool(args):
        return {"done": True}

    spec = HarnessSpec(
        name="wf",
        stages=[_make_tool_stage("s")],
        contracts=Contract(timeout_hours=8.0),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=fast_tool, parameters={},
    ))

    result = await harness.run({})
    assert result["s"]["done"] is True

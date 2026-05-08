"""Tests for stage-level timeout_s and fail_as_value."""
import asyncio
import pytest
from pathlib import Path
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig,
    Role, RoleType, OnFailConfig, LoopConfig,
)
from armature.runtime.engine import Harness
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_harness(stages: list[Stage], tmp_path: Path) -> Harness:
    spec = HarnessSpec(
        name="test_wf",
        stages=stages,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=tmp_path)


def _register(harness: Harness, name: str, fn) -> None:
    harness._registry.register(ToolDescriptor(
        name=name, description=name, permission=PermissionLevel.READ_ONLY,
        handler=fn, parameters={},
    ))


# ── Model field tests ─────────────────────────────────────────────────────────

def test_stage_timeout_s_defaults_none():
    s = Stage(id="s", depends_on=[])
    assert s.timeout_s is None


def test_stage_fail_as_value_defaults_false():
    s = Stage(id="s", depends_on=[])
    assert s.fail_as_value is False


def test_stage_parses_timeout_and_fail_as_value():
    s = Stage.model_validate({
        "id": "s",
        "timeout_s": 30.0,
        "fail_as_value": True,
        "depends_on": [],
    })
    assert s.timeout_s == 30.0
    assert s.fail_as_value is True


# ── timeout_s ────────────────────────────────────────────────────────────────

async def test_timeout_raises_when_exceeded(tmp_path):
    async def slow_tool(args):
        await asyncio.sleep(10)
        return {"done": True}

    harness = _make_harness([
        Stage(id="s", timeout_s=0.05, tool_call=ToolCallConfig(name="slow"), depends_on=[]),
    ], tmp_path)
    _register(harness, "slow", slow_tool)

    with pytest.raises(TimeoutError):
        await harness.run({})


async def test_timeout_not_raised_when_within_limit(tmp_path):
    async def fast_tool(args):
        return {"done": True}

    harness = _make_harness([
        Stage(id="s", timeout_s=5.0, tool_call=ToolCallConfig(name="fast"), depends_on=[]),
    ], tmp_path)
    _register(harness, "fast", fast_tool)

    result = await harness.run({})
    assert result["s"]["done"] is True


async def test_timeout_without_timeout_s_runs_normally(tmp_path):
    async def normal_tool(args):
        return {"value": 42}

    harness = _make_harness([
        Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[]),
    ], tmp_path)
    _register(harness, "t", normal_tool)

    result = await harness.run({})
    assert result["s"]["value"] == 42


async def test_timeout_with_fail_as_value_returns_failed_dict(tmp_path):
    async def slow_tool(args):
        await asyncio.sleep(10)
        return {}

    harness = _make_harness([
        Stage(id="s", timeout_s=0.05, fail_as_value=True,
              tool_call=ToolCallConfig(name="slow"), depends_on=[]),
    ], tmp_path)
    _register(harness, "slow", slow_tool)

    result = await harness.run({})
    assert result["s"]["_failed"] is True
    assert result["s"]["_failed_type"] == "TimeoutError"
    assert "timed out" in result["s"]["_failed_reason"]


# ── fail_as_value ─────────────────────────────────────────────────────────────

async def test_fail_as_value_returns_structured_failure(tmp_path):
    async def bad_tool(args):
        raise ValueError("something went wrong")

    harness = _make_harness([
        Stage(id="s", fail_as_value=True,
              tool_call=ToolCallConfig(name="bad"), depends_on=[]),
    ], tmp_path)
    _register(harness, "bad", bad_tool)

    result = await harness.run({})
    assert result["s"]["_failed"] is True
    assert result["s"]["_failed_type"] == "ValueError"
    assert "something went wrong" in result["s"]["_failed_reason"]


async def test_fail_as_value_false_propagates_exception(tmp_path):
    async def bad_tool(args):
        raise RuntimeError("hard failure")

    harness = _make_harness([
        Stage(id="s", fail_as_value=False,
              tool_call=ToolCallConfig(name="bad"), depends_on=[]),
    ], tmp_path)
    _register(harness, "bad", bad_tool)

    with pytest.raises(RuntimeError, match="hard failure"):
        await harness.run({})


async def test_fail_as_value_result_flows_to_downstream_context(tmp_path):
    async def bad_tool(args):
        raise KeyError("missing")

    async def reporter(args):
        return {"reported": True}

    harness = _make_harness([
        Stage(id="scan", fail_as_value=True,
              tool_call=ToolCallConfig(name="bad"), depends_on=[]),
        Stage(id="report", tool_call=ToolCallConfig(name="reporter"),
              depends_on=["scan"]),
    ], tmp_path)
    _register(harness, "bad", bad_tool)
    _register(harness, "reporter", reporter)

    result = await harness.run({})
    assert result["scan"]["_failed"] is True
    assert result["report"]["reported"] is True


async def test_fail_as_value_and_skip_if_downstream_skips_on_failure(tmp_path):
    async def bad_tool(args):
        raise RuntimeError("boom")

    async def expensive_tool(args):
        return {"expensive": True}

    harness = _make_harness([
        Stage(id="scan", fail_as_value=True,
              tool_call=ToolCallConfig(name="bad"), depends_on=[]),
        Stage(id="analyze", skip_if="{{ scan._failed }}",
              tool_call=ToolCallConfig(name="expensive"), depends_on=["scan"]),
    ], tmp_path)
    _register(harness, "bad", bad_tool)
    _register(harness, "expensive", expensive_tool)

    result = await harness.run({})
    assert result["scan"]["_failed"] is True
    assert result["analyze"] == {"_skipped": True}


async def test_fail_as_value_exhausts_retries_first(tmp_path):
    call_count = 0

    async def flaky_tool(args):
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"attempt {call_count}")

    harness = _make_harness([
        Stage(
            id="s",
            fail_as_value=True,
            tool_call=ToolCallConfig(name="flaky"),
            on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=2)),
            depends_on=[],
        ),
    ], tmp_path)
    _register(harness, "flaky", flaky_tool)

    result = await harness.run({})
    # 1 initial + 2 retries = 3 total attempts
    assert call_count == 3
    assert result["s"]["_failed"] is True
    assert "attempt 3" in result["s"]["_failed_reason"]


async def test_fail_as_value_success_returns_normally(tmp_path):
    async def good_tool(args):
        return {"ok": True}

    harness = _make_harness([
        Stage(id="s", fail_as_value=True,
              tool_call=ToolCallConfig(name="good"), depends_on=[]),
    ], tmp_path)
    _register(harness, "good", good_tool)

    result = await harness.run({})
    # fail_as_value has no effect on success
    assert result["s"]["ok"] is True
    assert "_failed" not in result["s"]

"""Tests for stage output truncation (context window management)."""
import pytest
from pathlib import Path
from armature.runtime.truncation import truncate_result
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig, Contract,
)
from armature.runtime.engine import Harness
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


# ── Unit: truncate_result ────────────────────────────────────────────────────

def test_string_within_limit_unchanged():
    assert truncate_result("hello", 10) == "hello"


def test_string_over_limit_truncated():
    result = truncate_result("hello world", 5)
    assert result.startswith("hello")
    assert "truncated" in result
    assert len(result) > 5  # marker appended


def test_dict_string_value_over_limit_truncated():
    result = truncate_result({"key": "a" * 100}, 10)
    assert len(result["key"]) > 10  # marker appended
    assert result["key"].startswith("a" * 10)
    assert result["_truncated"] is True


def test_dict_string_value_within_limit_unchanged():
    result = truncate_result({"key": "short"}, 100)
    assert result["key"] == "short"
    assert "_truncated" not in result


def test_dict_non_string_values_never_truncated():
    result = truncate_result({"count": 999, "flag": True, "val": 3.14}, 1)
    assert result["count"] == 999
    assert result["flag"] is True
    assert result["val"] == 3.14
    # no _truncated key because no strings were truncated
    assert "_truncated" not in result


def test_dict_nested_strings_truncated():
    result = truncate_result({"outer": {"inner": "x" * 50}}, 10)
    assert "truncated" in result["outer"]["inner"]
    assert result["_truncated"] is True


def test_list_within_limit_unchanged():
    data = [1, 2, 3]
    assert truncate_result(data, 1000) == [1, 2, 3]


def test_list_over_limit_returns_truncated_string():
    big_list = list(range(1000))
    result = truncate_result(big_list, 10)
    assert isinstance(result, str)
    assert "truncated" in result


def test_none_passes_through():
    assert truncate_result(None, 10) is None


def test_int_passes_through():
    assert truncate_result(42, 1) == 42


def test_multiple_string_values_all_over_limit():
    result = truncate_result({"a": "x" * 20, "b": "y" * 20}, 5)
    assert "truncated" in result["a"]
    assert "truncated" in result["b"]
    assert result["_truncated"] is True


# ── Engine: _effective_output_limit ─────────────────────────────────────────

def _make_harness(stages, contracts=None, tmp_path=None) -> Harness:
    spec = HarnessSpec(
        name="wf",
        stages=stages,
        contracts=contracts or Contract(),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=tmp_path)


def test_effective_limit_uses_stage_level(tmp_path):
    stage = Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[], output_max_chars=500)
    harness = _make_harness([stage], Contract(output_max_chars=1000), tmp_path)
    assert harness._effective_output_limit(stage) == 500


def test_effective_limit_falls_back_to_contract(tmp_path):
    stage = Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])
    harness = _make_harness([stage], Contract(output_max_chars=2000), tmp_path)
    assert harness._effective_output_limit(stage) == 2000


def test_effective_limit_none_when_neither_set(tmp_path):
    stage = Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])
    harness = _make_harness([stage], Contract(), tmp_path)
    assert harness._effective_output_limit(stage) is None


# ── Integration: truncation applied to stage output ─────────────────────────

def _register(harness, name, fn):
    harness._registry.register(ToolDescriptor(
        name=name, description=name, permission=PermissionLevel.READ_ONLY,
        handler=fn, parameters={},
    ))


async def test_stage_output_truncated_at_stage_level(tmp_path):
    async def big_tool(args):
        return {"content": "x" * 10_000}

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="big"), depends_on=[], output_max_chars=100)],
        tmp_path=tmp_path,
    )
    _register(harness, "big", big_tool)

    result = await harness.run({})
    assert len(result["s"]["content"]) < 200
    assert "truncated" in result["s"]["content"]
    assert result["s"]["_truncated"] is True


async def test_stage_output_truncated_at_contract_level(tmp_path):
    async def big_tool(args):
        return {"content": "y" * 5_000}

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="big"), depends_on=[])],
        contracts=Contract(output_max_chars=50),
        tmp_path=tmp_path,
    )
    _register(harness, "big", big_tool)

    result = await harness.run({})
    assert "truncated" in result["s"]["content"]


async def test_stage_output_within_limit_unchanged(tmp_path):
    async def small_tool(args):
        return {"content": "hello"}

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="small"), depends_on=[], output_max_chars=100)],
        tmp_path=tmp_path,
    )
    _register(harness, "small", small_tool)

    result = await harness.run({})
    assert result["s"]["content"] == "hello"
    assert "_truncated" not in result["s"]


async def test_stage_level_overrides_contract_level(tmp_path):
    async def tool(args):
        return {"content": "z" * 200}

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[], output_max_chars=50)],
        contracts=Contract(output_max_chars=500),  # stage limit is tighter
        tmp_path=tmp_path,
    )
    _register(harness, "t", tool)

    result = await harness.run({})
    # content should be truncated at 50, not 500
    raw_content = result["s"]["content"]
    assert len(raw_content) < 100  # well under 500


async def test_fail_as_value_dict_not_truncated(tmp_path):
    async def bad_tool(args):
        raise RuntimeError("boom")

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="bad"), depends_on=[],
               fail_as_value=True, output_max_chars=5)],
        tmp_path=tmp_path,
    )
    _register(harness, "bad", bad_tool)

    result = await harness.run({})
    # _failed dict should not be mangled by truncation
    assert result["s"]["_failed"] is True
    assert result["s"]["_failed_type"] == "RuntimeError"


async def test_skipped_dict_not_truncated(tmp_path):
    async def noop(args):
        return {}

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[],
               skip_if="true", output_max_chars=5)],
        tmp_path=tmp_path,
    )
    _register(harness, "t", noop)

    result = await harness.run({})
    assert result["s"] == {"_skipped": True}


async def test_no_limit_set_passes_result_unchanged(tmp_path):
    async def tool(args):
        return {"content": "a" * 50_000}

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        tmp_path=tmp_path,
    )
    _register(harness, "t", tool)

    result = await harness.run({})
    assert len(result["s"]["content"]) == 50_000
    assert "_truncated" not in result["s"]

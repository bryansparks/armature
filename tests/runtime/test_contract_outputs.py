"""Tests for Contract.outputs validation (post-run enforcement)."""
import pytest
from pathlib import Path
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig, Contract,
)
from armature.runtime.engine import Harness
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


def _make_harness(stages, contracts, tmp_path, *, validate=True) -> Harness:
    spec = HarnessSpec(
        name="wf",
        stages=stages,
        contracts=contracts,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    h = Harness(spec=spec, session_dir=tmp_path, validate=validate)
    for s in stages:
        if s.tool_call:
            async def noop(args): return {"ok": True}
            h._registry.register(ToolDescriptor(
                name=s.tool_call.name, description="noop",
                permission=PermissionLevel.READ_ONLY, handler=noop, parameters={},
            ))
    return h


# ── Unit: _validate_outputs ──────────────────────────────────────────────────

def test_validate_outputs_passes_when_required_present(tmp_path):
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="s"), depends_on=[])],
        Contract(outputs=[{"stage": "s", "key": "value", "required": True}]),
        tmp_path,
    )
    harness._validate_outputs({"s": {"value": "hello"}})


def test_validate_outputs_raises_for_missing_key(tmp_path):
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="s"), depends_on=[])],
        Contract(outputs=[{"stage": "s", "key": "result", "required": True}]),
        tmp_path,
    )
    with pytest.raises(ValueError, match="result"):
        harness._validate_outputs({"s": {}})


def test_validate_outputs_raises_for_none_value(tmp_path):
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="s"), depends_on=[])],
        Contract(outputs=[{"stage": "s", "key": "result", "required": True}]),
        tmp_path,
    )
    with pytest.raises(ValueError, match="result"):
        harness._validate_outputs({"s": {"result": None}})


def test_validate_outputs_skips_optional(tmp_path):
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="s"), depends_on=[])],
        Contract(outputs=[{"stage": "s", "key": "result", "required": False}]),
        tmp_path,
    )
    harness._validate_outputs({"s": {}})  # no exception


def test_validate_outputs_skips_entry_without_stage(tmp_path):
    # validate=False: runtime is lenient, but spec validator catches missing "stage"
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="s"), depends_on=[])],
        Contract(outputs=[{"key": "result", "required": True}]),  # no "stage"
        tmp_path, validate=False,
    )
    harness._validate_outputs({"s": {}})  # skips silently at runtime


def test_validate_outputs_skips_entry_without_key(tmp_path):
    # validate=False: runtime is lenient, but spec validator catches missing "key"
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="s"), depends_on=[])],
        Contract(outputs=[{"stage": "s", "required": True}]),  # no "key"
        tmp_path, validate=False,
    )
    harness._validate_outputs({"s": {}})  # skips silently at runtime


def test_validate_outputs_empty_list_always_passes(tmp_path):
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="s"), depends_on=[])],
        Contract(outputs=[]),
        tmp_path,
    )
    harness._validate_outputs({"s": {}})


def test_validate_outputs_missing_stage_in_results_raises(tmp_path):
    # validate=False: testing runtime raises when referenced stage has no results
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="s"), depends_on=[])],
        Contract(outputs=[{"stage": "missing_stage", "key": "x", "required": True}]),
        tmp_path, validate=False,
    )
    with pytest.raises(ValueError, match="missing_stage"):
        harness._validate_outputs({"s": {"x": "val"}})


# ── Integration: fires after run() ───────────────────────────────────────────

async def test_run_raises_when_required_output_missing(tmp_path):
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        contracts=Contract(outputs=[{"stage": "s", "key": "report", "required": True}]),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def t(args): return {"other_key": "data"}
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=t, parameters={},
    ))

    with pytest.raises(ValueError, match="report"):
        await harness.run({})


async def test_run_passes_when_required_output_present(tmp_path):
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        contracts=Contract(outputs=[{"stage": "s", "key": "report", "required": True}]),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def t(args): return {"report": "all clear"}
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=t, parameters={},
    ))

    result = await harness.run({})
    assert result["s"]["report"] == "all clear"


async def test_run_optional_output_missing_does_not_raise(tmp_path):
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        contracts=Contract(outputs=[{"stage": "s", "key": "optional_key", "required": False}]),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def t(args): return {"other": True}
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=t, parameters={},
    ))

    result = await harness.run({})
    assert result["s"]["other"] is True

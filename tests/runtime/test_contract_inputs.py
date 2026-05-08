"""Tests for Contract.inputs validation at harness.run() boundary."""
import pytest
from pathlib import Path
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig, Contract,
)
from armature.runtime.engine import Harness
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


def _make_harness(stages: list[Stage], contracts: Contract, tmp_path: Path, *, validate: bool = True) -> Harness:
    spec = HarnessSpec(
        name="test_wf",
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


def _stage(name: str = "s") -> Stage:
    return Stage(id=name, tool_call=ToolCallConfig(name=name), depends_on=[])


# ── Unit tests for _validate_inputs ─────────────────────────────────────────

def test_validate_inputs_passes_when_required_present(tmp_path):
    harness = _make_harness([_stage()], Contract(inputs=[
        {"name": "repo_path", "required": True}
    ]), tmp_path)
    harness._validate_inputs({"repo_path": "/tmp/repo"})  # no exception


def test_validate_inputs_raises_for_missing_required(tmp_path):
    harness = _make_harness([_stage()], Contract(inputs=[
        {"name": "repo_path", "required": True}
    ]), tmp_path)
    with pytest.raises(ValueError, match="repo_path"):
        harness._validate_inputs({})


def test_validate_inputs_raises_for_none_value(tmp_path):
    harness = _make_harness([_stage()], Contract(inputs=[
        {"name": "api_key", "required": True}
    ]), tmp_path)
    with pytest.raises(ValueError, match="api_key"):
        harness._validate_inputs({"api_key": None})


def test_validate_inputs_skips_optional_absent(tmp_path):
    harness = _make_harness([_stage()], Contract(inputs=[
        {"name": "debug", "required": False}
    ]), tmp_path)
    harness._validate_inputs({})  # no exception


def test_validate_inputs_skips_optional_with_no_required_key(tmp_path):
    harness = _make_harness([_stage()], Contract(inputs=[
        {"name": "x"}  # no "required" key — defaults to optional
    ]), tmp_path)
    harness._validate_inputs({})  # no exception


def test_validate_inputs_empty_list_always_passes(tmp_path):
    harness = _make_harness([_stage()], Contract(inputs=[]), tmp_path)
    harness._validate_inputs({})


def test_validate_inputs_skips_entries_without_name(tmp_path):
    # validate=False: this test checks runtime tolerance for malformed specs,
    # but the validator (correctly) flags missing "name" as an error.
    harness = _make_harness([_stage()], Contract(inputs=[
        {"required": True}  # no "name" — skip silently at runtime
    ]), tmp_path, validate=False)
    harness._validate_inputs({})


def test_validate_inputs_reports_first_missing_key(tmp_path):
    harness = _make_harness([_stage()], Contract(inputs=[
        {"name": "a", "required": True},
        {"name": "b", "required": True},
    ]), tmp_path)
    with pytest.raises(ValueError, match="'a'"):
        harness._validate_inputs({})


# ── Integration: validation fires before any stage ────────────────────────

async def test_run_raises_before_any_stage_on_missing_input(tmp_path):
    call_count = 0

    async def side_effect_tool(args):
        nonlocal call_count
        call_count += 1
        return {"ok": True}

    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        contracts=Contract(inputs=[{"name": "repo_path", "required": True}]),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=side_effect_tool, parameters={},
    ))

    with pytest.raises(ValueError, match="repo_path"):
        await harness.run({})
    assert call_count == 0


async def test_run_succeeds_when_all_required_inputs_present(tmp_path):
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        contracts=Contract(inputs=[{"name": "repo_path", "required": True}]),
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def t(args): return {"done": True}
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=t, parameters={},
    ))

    result = await harness.run({"repo_path": "/tmp/repo"})
    assert result["s"]["done"] is True


async def test_run_no_contracts_inputs_still_works(tmp_path):
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def t(args): return {"done": True}
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=t, parameters={},
    ))

    result = await harness.run({})
    assert result["s"]["done"] is True

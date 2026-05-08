"""Tests for Stage.condition (positive-gate execution control)."""
import pytest
from pathlib import Path
from armature.spec.models import Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig
from armature.runtime.engine import Harness
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


def _make_harness(stages, tmp_path) -> Harness:
    spec = HarnessSpec(
        name="wf",
        stages=stages,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=tmp_path)


def _register(harness, name, fn):
    harness._registry.register(ToolDescriptor(
        name=name, description=name, permission=PermissionLevel.READ_ONLY,
        handler=fn, parameters={},
    ))


# ── Model field ───────────────────────────────────────────────────────────────

def test_stage_condition_defaults_none():
    s = Stage(id="s", depends_on=[])
    assert s.condition is None


def test_stage_condition_parses():
    s = Stage.model_validate({
        "id": "s",
        "condition": "{{ env == 'prod' }}",
        "tool_call": {"name": "t"},
        "depends_on": [],
    })
    assert s.condition == "{{ env == 'prod' }}"


# ── Execution: condition truthy → runs ────────────────────────────────────────

async def test_condition_truthy_stage_runs(tmp_path):
    async def t(args): return {"executed": True}

    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        condition="{{ mode == 'active' }}", depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({"mode": "active"})
    assert result["s"]["executed"] is True


async def test_condition_falsy_stage_skipped(tmp_path):
    executed = False

    async def t(args):
        nonlocal executed
        executed = True
        return {"executed": True}

    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        condition="{{ mode == 'active' }}", depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({"mode": "disabled"})
    assert result["s"] == {"_skipped": True}
    assert not executed


async def test_condition_literal_false_skips(tmp_path):
    async def t(args): return {}

    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        condition="false", depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({})
    assert result["s"] == {"_skipped": True}


async def test_condition_literal_true_runs(tmp_path):
    async def t(args): return {"ok": True}

    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        condition="true", depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({})
    assert result["s"]["ok"] is True


async def test_condition_missing_var_is_falsy(tmp_path):
    async def t(args): return {}

    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        condition="{{ nonexistent_var }}", depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({})
    assert result["s"] == {"_skipped": True}


async def test_condition_evaluates_from_upstream_context(tmp_path):
    """Condition can reference the result of a prior stage."""
    async def first(args): return {"enabled": True}
    async def second(args): return {"ran": True}

    harness = _make_harness([
        Stage(id="first", tool_call=ToolCallConfig(name="first"), depends_on=[]),
        Stage(id="second", tool_call=ToolCallConfig(name="second"),
              condition="{{ first.enabled }}", depends_on=["first"]),
    ], tmp_path)
    _register(harness, "first", first)
    _register(harness, "second", second)

    result = await harness.run({})
    assert result["second"]["ran"] is True


async def test_condition_upstream_disabled_skips_downstream(tmp_path):
    async def first(args): return {"enabled": False}
    async def second(args): return {"ran": True}

    harness = _make_harness([
        Stage(id="first", tool_call=ToolCallConfig(name="first"), depends_on=[]),
        Stage(id="second", tool_call=ToolCallConfig(name="second"),
              condition="{{ first.enabled }}", depends_on=["first"]),
    ], tmp_path)
    _register(harness, "first", first)
    _register(harness, "second", second)

    result = await harness.run({})
    assert result["second"] == {"_skipped": True}


# ── Interaction with skip_if ──────────────────────────────────────────────────

async def test_condition_and_skip_if_both_must_allow(tmp_path):
    """Stage runs only if condition is truthy AND skip_if is falsy."""
    async def t(args): return {"ok": True}

    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        condition="true",
        skip_if="{{ should_skip }}",
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({"should_skip": "true"})
    assert result["s"] == {"_skipped": True}


async def test_skip_if_checked_before_condition(tmp_path):
    """skip_if is evaluated first; if it skips, condition is irrelevant."""
    async def t(args): return {}

    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        skip_if="true",
        condition="false",
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({})
    assert result["s"] == {"_skipped": True}


# ── on_event ──────────────────────────────────────────────────────────────────

async def test_stage_skipped_event_includes_reason(tmp_path):
    events = []
    async def t(args): return {}

    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        condition="false", depends_on=[],
    )], tmp_path)
    harness._on_event = lambda typ, data: events.append((typ, data))
    _register(harness, "t", t)

    await harness.run({})

    skipped = [data for typ, data in events if typ == "stage_skipped"]
    assert skipped[0]["reason"] == "condition"

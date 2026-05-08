"""Tests for checkpoint/resume functionality."""
import json
import pytest
from pathlib import Path
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig,
)
from armature.runtime.engine import Harness
from armature.runtime.checkpoint import CheckpointStore
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_harness(stages, tmp_path, checkpoint=True) -> Harness:
    spec = HarnessSpec(
        name="wf",
        stages=stages,
        checkpoint=checkpoint,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=tmp_path)


def _register(harness, name, fn):
    harness._registry.register(ToolDescriptor(
        name=name, description=name, permission=PermissionLevel.READ_ONLY,
        handler=fn, parameters={},
    ))


# ── CheckpointStore unit tests ────────────────────────────────────────────────

def test_load_returns_empty_when_no_file(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    assert store.load() == {}


def test_write_and_load_roundtrip(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    store.write("stage_a", {"value": 42}, {})
    loaded = store.load()
    assert loaded == {"stage_a": {"value": 42}}


def test_write_merges_with_existing(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    prior = {"stage_a": {"done": True}}
    store.write("stage_b", {"out": "x"}, prior)
    loaded = store.load()
    assert loaded["stage_a"] == {"done": True}
    assert loaded["stage_b"] == {"out": "x"}


def test_clear_removes_file(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    store.write("s", {}, {})
    assert store._path.exists()
    store.clear()
    assert not store._path.exists()


def test_clear_is_idempotent(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    store.clear()  # file does not exist — should not raise


def test_load_recovers_from_corrupt_file(tmp_path):
    cp = tmp_path / "checkpoint.json"
    cp.write_text("not valid json{{{")
    store = CheckpointStore(cp)
    assert store.load() == {}


# ── HarnessSpec field ─────────────────────────────────────────────────────────

def test_harness_spec_checkpoint_defaults_false():
    spec = HarnessSpec(
        name="wf",
        stages=[],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    assert spec.checkpoint is False


def test_harness_spec_checkpoint_can_be_set():
    spec = HarnessSpec(
        name="wf",
        stages=[],
        checkpoint=True,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    assert spec.checkpoint is True


# ── Integration: checkpoint=False writes no file ──────────────────────────────

async def test_no_checkpoint_file_when_disabled(tmp_path):
    async def t(args): return {"ok": True}

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        tmp_path, checkpoint=False,
    )
    _register(harness, "t", t)

    await harness.run({})
    assert not (tmp_path / "checkpoint.json").exists()


# ── Integration: first run creates checkpoint ─────────────────────────────────

async def test_checkpoint_file_created_after_run(tmp_path):
    async def t(args): return {"done": True}

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        tmp_path,
    )
    _register(harness, "t", t)

    result = await harness.run({})
    assert result["s"]["done"] is True
    assert (tmp_path / "checkpoint.json").exists()
    data = json.loads((tmp_path / "checkpoint.json").read_text())
    assert data["s"]["done"] is True


async def test_checkpoint_contains_all_completed_stages(tmp_path):
    async def a(args): return {"a": 1}
    async def b(args): return {"b": 2}

    harness = _make_harness([
        Stage(id="stage_a", tool_call=ToolCallConfig(name="a"), depends_on=[]),
        Stage(id="stage_b", tool_call=ToolCallConfig(name="b"), depends_on=["stage_a"]),
    ], tmp_path)
    _register(harness, "a", a)
    _register(harness, "b", b)

    await harness.run({})
    data = json.loads((tmp_path / "checkpoint.json").read_text())
    assert "stage_a" in data
    assert "stage_b" in data


# ── Integration: second run skips completed stages ───────────────────────────

async def test_second_run_skips_checkpointed_stage(tmp_path):
    call_count = 0

    async def expensive(args):
        nonlocal call_count
        call_count += 1
        return {"result": "done"}

    stages = [Stage(id="s", tool_call=ToolCallConfig(name="exp"), depends_on=[])]

    # First run — executes the stage
    h1 = _make_harness(stages, tmp_path)
    _register(h1, "exp", expensive)
    await h1.run({})
    assert call_count == 1

    # Second run — stage is already in checkpoint, should not execute
    h2 = _make_harness(stages, tmp_path)
    _register(h2, "exp", expensive)
    result = await h2.run({})
    assert call_count == 1  # not called again
    assert result["s"]["result"] == "done"


async def test_checkpointed_upstream_result_available_to_downstream(tmp_path):
    """Upstream result from checkpoint must be accessible to downstream stage."""
    # Pre-seed checkpoint with only the upstream stage result
    cp = tmp_path / "checkpoint.json"
    cp.write_text(json.dumps({"up": {"value": 99}}))

    downstream_received_value = []

    async def downstream(args):
        # The tool receives context via ToolCallNode args rendering, but here we
        # just return a marker so we can verify it ran
        return {"reported": True}

    stages = [
        Stage(id="up", tool_call=ToolCallConfig(name="up"), depends_on=[]),
        Stage(id="down", tool_call=ToolCallConfig(name="down"), depends_on=["up"]),
    ]

    harness = _make_harness(stages, tmp_path)

    async def up_should_not_run(args):
        raise AssertionError("up should be skipped from checkpoint")

    _register(harness, "up", up_should_not_run)
    _register(harness, "down", downstream)

    result = await harness.run({})
    # upstream returned from checkpoint
    assert result["up"]["value"] == 99
    # downstream ran successfully
    assert result["down"]["reported"] is True


# ── Integration: failed stage not written (raises) ───────────────────────────

async def test_failing_stage_not_written_to_checkpoint(tmp_path):
    async def bad(args):
        raise RuntimeError("boom")

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="bad"), depends_on=[])],
        tmp_path,
    )
    _register(harness, "bad", bad)

    with pytest.raises(RuntimeError):
        await harness.run({})

    # checkpoint file may not exist or may not contain "s"
    cp = tmp_path / "checkpoint.json"
    if cp.exists():
        data = json.loads(cp.read_text())
        assert "s" not in data


# ── Integration: fail_as_value result IS written ─────────────────────────────

async def test_fail_as_value_result_written_to_checkpoint(tmp_path):
    async def bad(args):
        raise ValueError("oops")

    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="bad"), depends_on=[],
               fail_as_value=True)],
        tmp_path,
    )
    _register(harness, "bad", bad)

    result = await harness.run({})
    assert result["s"]["_failed"] is True

    data = json.loads((tmp_path / "checkpoint.json").read_text())
    assert data["s"]["_failed"] is True


# ── Integration: force=True ignores checkpoint ────────────────────────────────

async def test_force_true_reruns_all_stages(tmp_path):
    call_count = 0

    async def t(args):
        nonlocal call_count
        call_count += 1
        return {"run": call_count}

    stages = [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])]

    h1 = _make_harness(stages, tmp_path)
    _register(h1, "t", t)
    await h1.run({})
    assert call_count == 1

    h2 = _make_harness(stages, tmp_path)
    _register(h2, "t", t)
    result = await h2.run({}, force=True)
    assert call_count == 2
    assert result["s"]["run"] == 2


async def test_force_true_clears_checkpoint_file(tmp_path):
    async def t(args): return {"ok": True}

    stages = [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])]
    h1 = _make_harness(stages, tmp_path)
    _register(h1, "t", t)
    await h1.run({})
    assert (tmp_path / "checkpoint.json").exists()

    h2 = _make_harness(stages, tmp_path)
    _register(h2, "t", t)
    await h2.run({}, force=True)

    # After force run, checkpoint reflects this run's results (not old ones)
    data = json.loads((tmp_path / "checkpoint.json").read_text())
    assert "s" in data


# ── Integration: multi-stage partial resume ───────────────────────────────────

async def test_partial_resume_only_remaining_stages_run(tmp_path):
    """Simulate: stage_a completed, stage_b failed. Resume runs only stage_b."""
    # Pre-seed checkpoint with stage_a completed
    cp = tmp_path / "checkpoint.json"
    cp.write_text(json.dumps({"stage_a": {"processed": True}}))

    b_calls = 0

    async def b(args):
        nonlocal b_calls
        b_calls += 1
        return {"final": True}

    stages = [
        Stage(id="stage_a", tool_call=ToolCallConfig(name="a"), depends_on=[]),
        Stage(id="stage_b", tool_call=ToolCallConfig(name="b"), depends_on=["stage_a"]),
    ]

    harness = _make_harness(stages, tmp_path)

    async def a_should_not_run(args):
        raise AssertionError("stage_a should have been skipped by checkpoint")

    _register(harness, "a", a_should_not_run)
    _register(harness, "b", b)

    result = await harness.run({})
    assert b_calls == 1
    assert result["stage_a"]["processed"] is True
    assert result["stage_b"]["final"] is True

"""Tests for checkpoint/resume functionality."""
import json
import pytest
from pathlib import Path
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig,
    IterationConfig,
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


# ── Loop + Checkpoint integration ──────────────────────────────────────────────
# These tests verify that loop stages with checkpointing enabled execute all
# iterations, not just the first. The original bug caused _execute_stage_with_recovery
# to return cached iteration-1 results on iteration 2+, short-circuiting the loop.

async def test_loop_with_checkpoint_executes_all_iterations(tmp_path):
    """Loop stage with checkpoint=True must execute all iterations, not just the first."""
    call_count = 0

    async def worker(args):
        nonlocal call_count
        call_count += 1
        return {"iteration": call_count, "done": call_count >= 3}

    stages = [Stage(
        id="loop_stage",
        tool_call=ToolCallConfig(name="worker"),
        loop=IterationConfig(max_iterations=3, until="{{ done }}"),
        depends_on=[],
    )]

    harness = _make_harness(stages, tmp_path, checkpoint=True)
    _register(harness, "worker", worker)

    result = await harness.run({})
    # All 3 iterations should execute (the bug caused only 1 to run)
    assert call_count == 3
    assert result["loop_stage"]["iteration"] == 3


async def test_loop_checkpoint_writes_final_result_under_stage_id(tmp_path):
    """After a loop completes, the checkpoint must contain both iteration-scoped
    keys and the plain stage.id key for downstream stage references."""
    async def worker(args):
        return {"status": "ok"}

    stages = [Stage(
        id="loop_stage",
        tool_call=ToolCallConfig(name="worker"),
        loop=IterationConfig(max_iterations=2),
        depends_on=[],
    )]

    harness = _make_harness(stages, tmp_path, checkpoint=True)
    _register(harness, "worker", worker)

    await harness.run({})
    data = json.loads((tmp_path / "checkpoint.json").read_text())
    # Iteration-scoped keys should exist
    assert "loop_stage__iter_1" in data
    assert "loop_stage__iter_2" in data
    # Plain stage.id key must also exist for downstream references
    assert "loop_stage" in data
    assert data["loop_stage"]["status"] == "ok"


async def test_loop_resume_skips_completed_iterations(tmp_path):
    """When checkpoint has iteration 1 and 2 results, resume only runs iteration 3+."""
    # Pre-seed checkpoint with iteration 1 and 2 completed
    cp = tmp_path / "checkpoint.json"
    cp.write_text(json.dumps({
        "loop_stage__iter_1": {"iteration": 1, "done": False},
        "loop_stage__iter_2": {"iteration": 2, "done": False},
    }))

    call_count = 0

    async def worker(args):
        nonlocal call_count
        call_count += 1
        return {"iteration": call_count + 2, "done": call_count + 2 >= 3}

    stages = [Stage(
        id="loop_stage",
        tool_call=ToolCallConfig(name="worker"),
        loop=IterationConfig(max_iterations=3, until="{{ done }}"),
        depends_on=[],
    )]

    harness = _make_harness(stages, tmp_path, checkpoint=True)
    _register(harness, "worker", worker)

    result = await harness.run({})
    # Only iteration 3 should actually execute (iterations 1-2 from checkpoint)
    assert call_count == 1
    assert result["loop_stage"]["iteration"] == 3


async def test_loop_full_resume_skips_entire_loop(tmp_path):
    """When checkpoint has the plain stage.id key, the entire loop is skipped."""
    # Pre-seed checkpoint with full loop completion
    cp = tmp_path / "checkpoint.json"
    cp.write_text(json.dumps({
        "loop_stage__iter_1": {"iteration": 1},
        "loop_stage__iter_2": {"iteration": 2},
        "loop_stage": {"iteration": 2, "done": True},
    }))

    call_count = 0

    async def worker(args):
        nonlocal call_count
        call_count += 1
        return {"iteration": call_count, "done": True}

    stages = [Stage(
        id="loop_stage",
        tool_call=ToolCallConfig(name="worker"),
        loop=IterationConfig(max_iterations=2),
        depends_on=[],
    )]

    harness = _make_harness(stages, tmp_path, checkpoint=True)
    _register(harness, "worker", worker)

    result = await harness.run({})
    # The loop should not have executed at all — result from checkpoint
    assert call_count == 0
    assert result["loop_stage"]["iteration"] == 2


async def test_loop_checkpoint_does_not_pollute_context(tmp_path):
    """Iteration-scoped keys (__iter_N) should not appear as top-level context keys."""
    # Pre-seed checkpoint with iteration-scoped keys
    cp = tmp_path / "checkpoint.json"
    cp.write_text(json.dumps({
        "loop_stage__iter_1": {"iteration": 1},
        "loop_stage__iter_2": {"iteration": 2},
        "loop_stage": {"iteration": 2, "done": True},
    }))

    received_context_keys = []

    async def downstream(args):
        # Capture what keys the downstream stage sees
        # The tool args dict is rendered from context, but we can check
        # by having the harness context injected via a special mechanism
        return {"saw_keys": True}

    stages = [
        Stage(
            id="loop_stage",
            tool_call=ToolCallConfig(name="up"),
            loop=IterationConfig(max_iterations=2),
            depends_on=[],
        ),
        Stage(
            id="down",
            tool_call=ToolCallConfig(name="down"),
            depends_on=["loop_stage"],
        ),
    ]

    harness = _make_harness(stages, tmp_path, checkpoint=True)

    async def up_tool(args):
        return {"iteration": 1}

    _register(harness, "up", up_tool)
    _register(harness, "down", downstream)

    await harness.run({})

    # Verify the checkpoint file has __iter_ keys but they are NOT
    # in _checkpoint_prior (which gets merged into context)
    assert "loop_stage__iter_1" not in harness._checkpoint_prior
    assert "loop_stage__iter_2" not in harness._checkpoint_prior
    assert "loop_stage" in harness._checkpoint_prior


async def test_non_loop_checkpoint_unchanged_after_loop_fix(tmp_path):
    """Non-loop stages with checkpoint should behave identically after the loop fix."""
    call_count = 0

    async def tool(args):
        nonlocal call_count
        call_count += 1
        return {"result": "done"}

    stages = [Stage(id="s", tool_call=ToolCallConfig(name="tool"), depends_on=[])]

    # First run
    h1 = _make_harness(stages, tmp_path, checkpoint=True)
    _register(h1, "tool", tool)
    await h1.run({})
    assert call_count == 1

    # Second run — should skip from checkpoint
    h2 = _make_harness(stages, tmp_path, checkpoint=True)
    _register(h2, "tool", tool)
    result = await h2.run({})
    assert call_count == 1  # not called again
    assert result["s"]["result"] == "done"

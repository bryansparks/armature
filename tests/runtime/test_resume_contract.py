"""Resume-contract conformance tests.

Modeled on the checkpoint/resume conformance literature (Resume Means
Resume, arXiv:2608.03836): pin the effect-delivery contract for checkpointed
runs with effect-instrumented tools, so any future change to these semantics
is deliberate and visible.

Contract being pinned (see docs/CHECKPOINT-AND-RESUME.md → Effect-delivery
contract):

- Completed stages are exactly-once: never re-executed on resume
  (prefix continuation).
- A stage that was in-flight at the crash point is at-least-once: its
  external effects may be applied again on resume.
- Fan-out is checkpointed as a unit: a mid-fan-out interruption re-runs
  ALL items — there is no per-item preservation.
- Concurrent runs on one session directory are rejected (fail-closed
  session lock) instead of silently duplicating un-checkpointed effects.

The effect ledger is a plain file in the session directory — it stands in
for any external effect (a written artifact, an API call, a sent email).
"""
import asyncio
import json
import pytest
from pathlib import Path

from armature.spec.models import Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig
from armature.runtime.engine import Harness
from armature.runtime.checkpoint import SessionDirInUse
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_harness(stages, session_dir, checkpoint=True) -> Harness:
    spec = HarnessSpec(
        name="wf",
        stages=stages,
        checkpoint=checkpoint,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=session_dir)


def _register(harness, name, fn):
    harness._registry.register(ToolDescriptor(
        name=name, description=name, permission=PermissionLevel.READ_ONLY,
        handler=fn, parameters={},
    ))


class Ledger:
    """A durable external-effect stand-in: an append-only file."""

    def __init__(self, path: Path):
        self.path = path

    def append(self, line: str) -> None:
        with open(self.path, "a") as fh:
            fh.write(line + "\n")

    def lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text().splitlines()

    def count(self, needle: str) -> int:
        return sum(1 for line in self.lines() if needle in line)


# ── Prefix continuation: completed stages are exactly-once ────────────────────

async def test_completed_stage_effect_never_repeats_on_resume(tmp_path):
    """A stage that completed and was checkpointed must not re-run — its
    external effect appears exactly once, no matter how many resumes."""
    ledger = Ledger(tmp_path / "effects.log")

    async def apply_effect(args):
        ledger.append("effect:stage_a")
        return {"done": True}

    stages = [
        Stage(id="stage_a", tool_call=ToolCallConfig(name="apply"), depends_on=[]),
        Stage(id="stage_b", tool_call=ToolCallConfig(name="b"), depends_on=["stage_a"]),
    ]

    h1 = _make_harness(stages, tmp_path)
    _register(h1, "apply", apply_effect)

    async def b(args):
        return {"ok": True}
    _register(h1, "b", b)
    await h1.run({})
    assert ledger.count("effect:stage_a") == 1

    # Resume: stage_a must come from checkpoint — the tool must not run.
    h2 = _make_harness(stages, tmp_path)

    async def apply_should_not_run(args):
        raise AssertionError("checkpointed stage_a re-executed on resume")
    _register(h2, "apply", apply_should_not_run)
    _register(h2, "b", b)
    result = await h2.run({})

    assert result["stage_a"]["done"] is True
    assert ledger.count("effect:stage_a") == 1  # exactly-once for completed stages


# ── Mid-stage crash: in-flight stages are at-least-once ──────────────────────

async def test_mid_stage_crash_effect_repeats_on_resume(tmp_path):
    """A stage that applied its external effect and then crashed is NOT in
    the checkpoint, so resume re-executes it — the effect is applied again.
    This pins at-least-once for un-checkpointed, effect-bearing stages."""
    ledger = Ledger(tmp_path / "effects.log")

    async def flaky(args):
        ledger.append("effect:side_effect")
        if ledger.count("effect:side_effect") == 1:
            raise RuntimeError("crash after effect was applied")
        return {"ok": True}

    stages = [Stage(id="s", tool_call=ToolCallConfig(name="flaky"), depends_on=[])]

    # Run 1: effect applied, then the stage crashes.
    h1 = _make_harness(stages, tmp_path)
    _register(h1, "flaky", flaky)
    with pytest.raises(RuntimeError):
        await h1.run({})
    assert ledger.count("effect:side_effect") == 1
    data = json.loads((tmp_path / "checkpoint.json").read_text()) if (tmp_path / "checkpoint.json").exists() else {}
    assert "s" not in data  # crash before checkpoint — nothing recorded

    # Run 2 (resume): the stage re-runs, applying the effect a second time.
    h2 = _make_harness(stages, tmp_path)
    _register(h2, "flaky", flaky)
    result = await h2.run({})

    assert result["s"]["ok"] is True
    assert ledger.count("effect:side_effect") == 2  # at-least-once, pinned


# ── Fan-out granularity: unit checkpointing, no per-item preservation ────────

async def test_fanout_interruption_reruns_all_items(tmp_path):
    """A fan-out stage is checkpointed as a unit after ALL items complete.
    If the run is interrupted mid-fan-out, resume re-runs every item —
    even items that had already completed. There is no per-item
    checkpointing (documented; per-item preservation is a deferred item)."""
    ledger = Ledger(tmp_path / "effects.log")
    items = ["a", "b"]

    async def plan(args):
        return {"things": items}

    async def work(args):
        # The partition item is Jinja-rendered into the tool args.
        thing = args["thing"]
        ledger.append(f"effect:item:{thing}")
        await asyncio.sleep(2.0)  # still in flight when run 1 is cancelled
        return {"done": thing}

    async def work_guard(args):
        raise AssertionError("plan stage re-executed on resume")

    stages = [
        Stage(id="plan", tool_call=ToolCallConfig(name="plan"), depends_on=[]),
        Stage(
            id="fan", tool_call=ToolCallConfig(name="work", args={"thing": "{{ thing }}"}),
            depends_on=["plan"],
            fan_out=2, fan_in="list",
            partition_source="{{ plan.things }}", partition_key="thing",
        ),
    ]

    # Run 1: interrupted mid-fan-out — items appended their effects, then
    # the run is cancelled before the fan-out stage completes/checkpoints.
    h1 = _make_harness(stages, tmp_path)
    _register(h1, "plan", plan)
    _register(h1, "work", work)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(h1.run({}), timeout=0.5)
    assert ledger.count("effect:item:") == 2  # both items acted before cancel

    data = json.loads((tmp_path / "checkpoint.json").read_text())
    assert "plan" in data      # completed before the fan-out — checkpointed
    assert "fan" not in data   # in-flight at cancellation — not checkpointed

    # Run 2 (resume): plan comes from checkpoint; the fan-out re-runs ALL items.
    h2 = _make_harness(stages, tmp_path)
    _register(h2, "plan", work_guard)
    _register(h2, "work", work)
    result = await h2.run({})

    assert result["fan"][0]["done"] in items
    # Every item ran twice (once per run) — no per-item preservation.
    assert ledger.count("effect:item:a") == 2
    assert ledger.count("effect:item:b") == 2


# ── Consume-once: concurrent runs on one session dir are rejected ────────────

async def test_concurrent_run_same_session_dir_rejected(tmp_path):
    """A second run against a session directory whose run is still active
    must fail loudly, not silently duplicate un-checkpointed effects."""
    started = asyncio.Event()

    async def slow(args):
        started.set()
        await asyncio.sleep(0.3)
        return {"ok": True}

    stages = [Stage(id="s", tool_call=ToolCallConfig(name="slow"), depends_on=[])]

    h1 = _make_harness(stages, tmp_path)
    _register(h1, "slow", slow)
    task = asyncio.ensure_future(h1.run({}))
    await started.wait()  # h1 is now mid-run and holds the session lock

    h2 = _make_harness(stages, tmp_path)
    _register(h2, "slow", slow)
    with pytest.raises(SessionDirInUse):
        await h2.run({})

    # The active run is unaffected, and the lock is released on completion.
    result = await task
    assert result["s"]["ok"] is True

    h3 = _make_harness(stages, tmp_path)
    _register(h3, "slow", slow)
    result3 = await h3.run({})  # stage now checkpointed — no re-execution
    assert result3["s"]["ok"] is True


async def test_lock_released_after_failed_run(tmp_path):
    """A crashed run must not leave the session directory permanently
    locked — the next run can start."""
    async def bad(args):
        raise RuntimeError("boom")

    stages = [Stage(id="s", tool_call=ToolCallConfig(name="bad"), depends_on=[])]

    h1 = _make_harness(stages, tmp_path)
    _register(h1, "bad", bad)
    with pytest.raises(RuntimeError):
        await h1.run({})

    async def good(args):
        return {"ok": True}
    h2 = _make_harness(stages, tmp_path)
    _register(h2, "bad", good)
    result = await h2.run({})  # must not raise SessionDirInUse
    assert result["s"]["ok"] is True


async def test_no_lock_when_checkpoint_disabled(tmp_path):
    """Non-checkpointed runs never take the session lock — two concurrent
    runs on one session dir are allowed (nothing to protect)."""
    async def slow(args):
        await asyncio.sleep(0.1)
        return {"ok": True}

    stages = [Stage(id="s", tool_call=ToolCallConfig(name="slow"), depends_on=[])]

    h1 = _make_harness(stages, tmp_path, checkpoint=False)
    _register(h1, "slow", slow)
    h2 = _make_harness(stages, tmp_path, checkpoint=False)
    _register(h2, "slow", slow)

    # Both run concurrently without SessionDirInUse.
    await asyncio.gather(h1.run({}), h2.run({}))

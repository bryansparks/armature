"""Tests for on_event callback emissions."""
import asyncio
import pytest
from pathlib import Path
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig,
    LoopConfig, OnFailConfig,
)
from armature.runtime.engine import Harness
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_harness(stages, tmp_path, on_event=None) -> Harness:
    spec = HarnessSpec(
        name="wf",
        stages=stages,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=tmp_path, on_event=on_event)


def _register(harness, name, fn):
    harness._registry.register(ToolDescriptor(
        name=name, description=name, permission=PermissionLevel.READ_ONLY,
        handler=fn, parameters={},
    ))


def _capture_events():
    events: list[tuple[str, dict]] = []
    def handler(event_type, data):
        events.append((event_type, data))
    return events, handler


def _events_of_type(events, event_type):
    return [data for typ, data in events if typ == event_type]


# ── stage_start / stage_complete baseline ────────────────────────────────────

async def test_stage_start_and_complete_emitted(tmp_path):
    async def t(args): return {"ok": True}

    events, on_event = _capture_events()
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        tmp_path, on_event=on_event,
    )
    _register(harness, "t", t)
    await harness.run({})

    types = [typ for typ, _ in events]
    assert "stage_start" in types
    assert "stage_complete" in types


async def test_stage_start_includes_kind_tool_call(tmp_path):
    async def t(args): return {}

    events, on_event = _capture_events()
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        tmp_path, on_event=on_event,
    )
    _register(harness, "t", t)
    await harness.run({})

    starts = _events_of_type(events, "stage_start")
    assert starts[0]["kind"] == "tool_call"
    assert starts[0]["stage"] == "s"


async def test_stage_complete_includes_elapsed(tmp_path):
    async def t(args): return {}

    events, on_event = _capture_events()
    harness = _make_harness(
        [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        tmp_path, on_event=on_event,
    )
    _register(harness, "t", t)
    await harness.run({})

    completes = _events_of_type(events, "stage_complete")
    assert "elapsed_s" in completes[0]
    assert isinstance(completes[0]["elapsed_s"], float)


# ── retry_attempt ─────────────────────────────────────────────────────────────

async def test_retry_attempt_emitted_on_failure_retry(tmp_path):
    call_count = 0

    async def flaky(args):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError("not ready")
        return {"ok": True}

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="flaky"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=3)),
        depends_on=[],
    )], tmp_path, on_event=on_event)
    _register(harness, "flaky", flaky)
    await harness.run({})

    retries = _events_of_type(events, "retry_attempt")
    assert len(retries) == 2  # 2 retries before succeeding on attempt 3
    assert retries[0]["attempt"] == 1
    assert retries[1]["attempt"] == 2
    assert retries[0]["stage"] == "s"
    assert "not ready" in retries[0]["reason"]


async def test_retry_attempt_emitted_on_until_condition_unmet(tmp_path):
    call_count = 0

    async def poll(args):
        nonlocal call_count
        call_count += 1
        return {"ready": call_count >= 3}

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="poll"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=5, until="{{ ready }}")),
        depends_on=[],
    )], tmp_path, on_event=on_event)
    _register(harness, "poll", poll)
    await harness.run({})

    retries = _events_of_type(events, "retry_attempt")
    assert len(retries) == 2  # retried twice before condition met
    assert "until condition" in retries[0]["reason"]


async def test_retry_attempt_includes_max(tmp_path):
    async def always_fails(args):
        raise RuntimeError("boom")

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=2)),
        depends_on=[],
        fail_as_value=True,
    )], tmp_path, on_event=on_event)
    _register(harness, "t", always_fails)
    await harness.run({})

    retries = _events_of_type(events, "retry_attempt")
    assert all(r["max"] == 2 for r in retries)


# ── stage_failed ──────────────────────────────────────────────────────────────

async def test_stage_failed_emitted_on_fail_as_value(tmp_path):
    async def bad(args):
        raise ValueError("oops")

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="bad"),
        fail_as_value=True,
        depends_on=[],
    )], tmp_path, on_event=on_event)
    _register(harness, "bad", bad)
    await harness.run({})

    failures = _events_of_type(events, "stage_failed")
    assert len(failures) == 1
    assert failures[0]["stage"] == "s"
    assert failures[0]["type"] == "ValueError"
    assert "oops" in failures[0]["reason"]


async def test_stage_failed_not_emitted_on_success(tmp_path):
    async def good(args): return {"ok": True}

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="good"),
        fail_as_value=True,
        depends_on=[],
    )], tmp_path, on_event=on_event)
    _register(harness, "good", good)
    await harness.run({})

    assert _events_of_type(events, "stage_failed") == []


async def test_stage_failed_emitted_on_timeout_with_fail_as_value(tmp_path):
    async def slow(args):
        await asyncio.sleep(10)
        return {}

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="slow"),
        timeout_s=0.05,
        fail_as_value=True,
        depends_on=[],
    )], tmp_path, on_event=on_event)
    _register(harness, "slow", slow)
    await harness.run({})

    failures = _events_of_type(events, "stage_failed")
    assert failures[0]["type"] == "TimeoutError"


# ── run_summary ───────────────────────────────────────────────────────────────

async def test_run_summary_emitted_at_end(tmp_path):
    async def t(args): return {"ok": True}

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"), depends_on=[],
    )], tmp_path, on_event=on_event)
    _register(harness, "t", t)
    await harness.run({})

    summaries = _events_of_type(events, "run_summary")
    assert len(summaries) == 1
    s = summaries[0]
    assert s["workflow"] == "wf"
    assert s["stages_total"] == 1
    assert s["stages_ran"] == 1
    assert s["stages_skipped"] == 0
    assert s["stages_resumed"] == 0
    assert s["stages_failed"] == 0
    assert isinstance(s["elapsed_s"], float)


async def test_run_summary_counts_skipped(tmp_path):
    async def t(args): return {}

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"), skip_if="true", depends_on=[],
    )], tmp_path, on_event=on_event)
    _register(harness, "t", t)
    await harness.run({})

    s = _events_of_type(events, "run_summary")[0]
    assert s["stages_skipped"] == 1
    assert s["stages_ran"] == 0


async def test_run_summary_counts_failed(tmp_path):
    async def bad(args): raise RuntimeError("x")

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="bad"), fail_as_value=True, depends_on=[],
    )], tmp_path, on_event=on_event)
    _register(harness, "bad", bad)
    await harness.run({})

    s = _events_of_type(events, "run_summary")[0]
    assert s["stages_failed"] == 1


async def test_run_summary_counts_resumed_from_checkpoint(tmp_path):
    import json
    cp = tmp_path / "checkpoint.json"
    cp.write_text(json.dumps({"s": {"done": True}}))

    events, on_event = _capture_events()
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        checkpoint=True,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path, on_event=on_event)
    async def t(args): raise AssertionError("should not run")
    _register(harness, "t", t)
    await harness.run({})

    s = _events_of_type(events, "run_summary")[0]
    assert s["stages_resumed"] == 1
    assert s["stages_ran"] == 0


async def test_run_summary_is_last_event(tmp_path):
    """run_summary should fire after all stage events."""
    async def t(args): return {}

    events, on_event = _capture_events()
    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"), depends_on=[],
    )], tmp_path, on_event=on_event)
    _register(harness, "t", t)
    await harness.run({})

    types = [typ for typ, _ in events]
    assert types[-1] == "run_summary"


async def test_no_events_when_no_callback(tmp_path):
    """Without on_event, no errors raised and workflow runs normally."""
    async def t(args): return {"ok": True}

    harness = _make_harness([Stage(
        id="s", tool_call=ToolCallConfig(name="t"), depends_on=[],
    )], tmp_path, on_event=None)
    _register(harness, "t", t)
    result = await harness.run({})
    assert result["s"]["ok"] is True

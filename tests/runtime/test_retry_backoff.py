"""Tests for exponential backoff in on_fail.loop retries."""
import asyncio
import time
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


# ── Model field defaults ──────────────────────────────────────────────────────

def test_loop_config_backoff_defaults_none():
    cfg = LoopConfig(stage="s")
    assert cfg.backoff_s is None


def test_loop_config_backoff_max_defaults_60():
    cfg = LoopConfig(stage="s")
    assert cfg.backoff_max_s == 60.0


def test_loop_config_parses_backoff():
    cfg = LoopConfig.model_validate({
        "stage": "s",
        "backoff_s": 1.0,
        "backoff_max_s": 10.0,
    })
    assert cfg.backoff_s == 1.0
    assert cfg.backoff_max_s == 10.0


# ── Backoff not set: no sleep between retries ─────────────────────────────────

async def test_no_backoff_retries_immediately(tmp_path):
    call_count = 0

    async def flaky(args):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError("not yet")
        return {"ok": True}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="flaky"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=2)),
        depends_on=[],
    )], tmp_path)
    _register(harness, "flaky", flaky)

    t0 = time.monotonic()
    result = await harness.run({})
    elapsed = time.monotonic() - t0

    assert result["s"]["ok"] is True
    assert call_count == 3
    assert elapsed < 0.5  # no sleep → fast


# ── Backoff applied between retries ──────────────────────────────────────────

async def test_backoff_delays_between_retries(tmp_path):
    """With backoff_s=0.05 and 2 retries, total sleep should be ~0.05 + 0.10 = 0.15s."""
    call_count = 0

    async def always_fails(args):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("boom")

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="fail"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=2, backoff_s=0.05)),
        depends_on=[],
    )], tmp_path)
    _register(harness, "fail", always_fails)

    t0 = time.monotonic()
    with pytest.raises(RuntimeError):
        await harness.run({})
    elapsed = time.monotonic() - t0

    assert call_count == 3  # 1 initial + 2 retries
    # Should have slept ~0.05 + 0.10 = 0.15s between retries
    assert elapsed >= 0.12, f"Expected backoff delays, elapsed={elapsed:.3f}s"


async def test_backoff_doubles_each_attempt(tmp_path):
    """Verify exponential doubling: attempt 0→1 waits backoff_s, 1→2 waits 2*backoff_s."""
    sleep_times: list[float] = []
    original_sleep = asyncio.sleep

    async def recording_sleep(delay):
        sleep_times.append(delay)
        await original_sleep(0)  # don't actually wait in tests

    call_count = 0

    async def always_fails(args):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("x")

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="fail"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=3, backoff_s=1.0)),
        depends_on=[],
    )], tmp_path)
    _register(harness, "fail", always_fails)

    import unittest.mock as mock
    with mock.patch("asyncio.sleep", side_effect=recording_sleep):
        with pytest.raises(RuntimeError):
            await harness.run({})

    assert len(sleep_times) == 3  # 3 sleeps for 3 retries (not after the last attempt)
    assert sleep_times[0] == pytest.approx(1.0)    # 1.0 * 2^0
    assert sleep_times[1] == pytest.approx(2.0)    # 1.0 * 2^1
    assert sleep_times[2] == pytest.approx(4.0)    # 1.0 * 2^2


async def test_backoff_capped_at_backoff_max_s(tmp_path):
    """backoff_max_s caps the delay regardless of exponential growth."""
    sleep_times: list[float] = []
    original_sleep = asyncio.sleep

    async def recording_sleep(delay):
        sleep_times.append(delay)
        await original_sleep(0)

    async def always_fails(args):
        raise RuntimeError("x")

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="fail"),
        on_fail=OnFailConfig(loop=LoopConfig(
            stage="s", max=4, backoff_s=1.0, backoff_max_s=3.0
        )),
        depends_on=[],
    )], tmp_path)
    _register(harness, "fail", always_fails)

    import unittest.mock as mock
    with mock.patch("asyncio.sleep", side_effect=recording_sleep):
        with pytest.raises(RuntimeError):
            await harness.run({})

    # delays: 1.0, 2.0, min(4.0, 3.0)=3.0, min(8.0, 3.0)=3.0
    assert sleep_times[0] == pytest.approx(1.0)
    assert sleep_times[1] == pytest.approx(2.0)
    assert sleep_times[2] == pytest.approx(3.0)
    assert sleep_times[3] == pytest.approx(3.0)


async def test_backoff_no_sleep_after_last_retry(tmp_path):
    """No sleep after the final (max) retry attempt — just raise immediately."""
    sleep_count = 0
    original_sleep = asyncio.sleep

    async def recording_sleep(delay):
        nonlocal sleep_count
        sleep_count += 1
        await original_sleep(0)

    async def always_fails(args):
        raise RuntimeError("x")

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="fail"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=2, backoff_s=1.0)),
        depends_on=[],
    )], tmp_path)
    _register(harness, "fail", always_fails)

    import unittest.mock as mock
    with mock.patch("asyncio.sleep", side_effect=recording_sleep):
        with pytest.raises(RuntimeError):
            await harness.run({})

    # max=2: attempts 0, 1, 2. Sleeps after attempts 0 and 1 only.
    assert sleep_count == 2


async def test_backoff_succeeds_on_eventual_success(tmp_path):
    """Verify retry with backoff still returns result when stage eventually succeeds."""
    call_count = 0

    async def flaky(args):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError("not ready")
        return {"result": "done"}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="flaky"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=3, backoff_s=0.01)),
        depends_on=[],
    )], tmp_path)
    _register(harness, "flaky", flaky)

    result = await harness.run({})
    assert result["s"]["result"] == "done"
    assert call_count == 3

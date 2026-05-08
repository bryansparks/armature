"""Tests for LoopConfig.until polling condition."""
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


# ── _eval_until unit tests ───────────────────────────────────────────────────

def test_eval_until_truthy_stops():
    assert Harness._eval_until("{{ status == 'done' }}", {"status": "done"}, {}) is True


def test_eval_until_falsy_continues():
    assert Harness._eval_until("{{ status == 'done' }}", {"status": "pending"}, {}) is False


def test_eval_until_uses_context_keys():
    assert Harness._eval_until("{{ ready }}", {"ready": True}, {}) is True


def test_eval_until_merges_result_dict_keys():
    # result dict keys take precedence over context keys with same name
    assert Harness._eval_until("{{ val }}", {"val": True}, {"val": False}) is True


def test_eval_until_non_dict_result_as_result_key():
    assert Harness._eval_until("{{ _result == 42 }}", 42, {}) is True


def test_eval_until_false_string_is_falsy():
    assert Harness._eval_until("{{ done }}", {"done": False}, {}) is False


def test_eval_until_numeric_one_is_truthy():
    assert Harness._eval_until("{{ count }}", {"count": 1}, {}) is True


def test_eval_until_zero_is_falsy():
    assert Harness._eval_until("{{ count }}", {"count": 0}, {}) is False


# ── Integration: until stops loop on condition ────────────────────────────────

async def test_until_stops_loop_when_condition_met(tmp_path):
    """Stage keeps running until status == 'done'."""
    call_count = 0

    async def poll(args):
        nonlocal call_count
        call_count += 1
        status = "done" if call_count >= 3 else "pending"
        return {"status": status, "attempt": call_count}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="poll"),
        on_fail=OnFailConfig(loop=LoopConfig(
            stage="s", max=5, until="{{ status == 'done' }}"
        )),
        depends_on=[],
    )], tmp_path)
    _register(harness, "poll", poll)

    result = await harness.run({})
    assert result["s"]["status"] == "done"
    assert call_count == 3  # stopped at the 3rd attempt


async def test_until_with_no_exception_retries_on_falsy_condition(tmp_path):
    """Retries even on success if until condition is not satisfied."""
    calls = []

    async def stage(args):
        calls.append(len(calls) + 1)
        return {"ready": len(calls) >= 2}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=3, until="{{ ready }}")),
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", stage)

    result = await harness.run({})
    assert result["s"]["ready"] is True
    assert len(calls) == 2


async def test_until_first_success_meets_condition(tmp_path):
    """If condition met on first try, no retry needed."""
    call_count = 0

    async def immediate(args):
        nonlocal call_count
        call_count += 1
        return {"done": True}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", max=5, until="{{ done }}")),
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", immediate)

    result = await harness.run({})
    assert result["s"]["done"] is True
    assert call_count == 1


async def test_until_max_exhausted_returns_last_result(tmp_path):
    """When max is reached without satisfying until, return the last result."""
    call_count = 0

    async def never_ready(args):
        nonlocal call_count
        call_count += 1
        return {"status": "pending", "attempt": call_count}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        on_fail=OnFailConfig(loop=LoopConfig(
            stage="s", max=2, until="{{ status == 'done' }}"
        )),
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", never_ready)

    result = await harness.run({})
    # Until never satisfied — returns last result, does NOT raise
    assert result["s"]["status"] == "pending"
    assert result["s"]["attempt"] == 3  # 1 initial + 2 retries
    assert call_count == 3


async def test_until_retry_injects_last_result_context(tmp_path):
    """_last_result is injected into context on each retry when until is set."""
    received_contexts = []

    async def stage(args):
        received_contexts.append(dict(args))
        return {"val": len(received_contexts)}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        on_fail=OnFailConfig(loop=LoopConfig(
            stage="s", max=2, until="{{ val >= 2 }}"
        )),
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", stage)

    result = await harness.run({})
    assert result["s"]["val"] == 2
    # Second call should have seen _retry_attempt and _last_result in its args (if rendered)
    assert len(received_contexts) == 2


async def test_until_combined_with_exception_retry(tmp_path):
    """until works alongside exception retries: exceptions retry, successful results checked."""
    call_count = 0

    async def mixed(args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient error")
        return {"status": "done" if call_count >= 3 else "pending"}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        on_fail=OnFailConfig(loop=LoopConfig(
            stage="s", max=5, until="{{ status == 'done' }}"
        )),
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", mixed)

    result = await harness.run({})
    assert result["s"]["status"] == "done"
    assert call_count == 3  # 1 exception, 1 pending, 1 done


async def test_until_with_fail_as_value_returns_last_on_condition_unmet(tmp_path):
    """fail_as_value + until: condition never met returns last result (not failure dict)."""
    call_count = 0

    async def always_pending(args):
        nonlocal call_count
        call_count += 1
        return {"ready": False}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        fail_as_value=True,
        on_fail=OnFailConfig(loop=LoopConfig(
            stage="s", max=1, until="{{ ready }}"
        )),
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", always_pending)

    result = await harness.run({})
    # No exception was raised, so fail_as_value doesn't apply — last result returned
    assert result["s"]["ready"] is False
    assert "_failed" not in result["s"]

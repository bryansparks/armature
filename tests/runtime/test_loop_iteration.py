"""Tests for IterationConfig loop feature (deliberate iteration, not retry-on-failure)."""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig,
    IterationConfig,
)
from armature.runtime.engine import (
    Harness,
    _resolve_dot_path,
    _set_nested_key,
    _merge_carry_forward,
)
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


# ── Unit tests for helper functions ──────────────────────────────────────────

def test_resolve_dot_path_basic():
    result = _resolve_dot_path({"a": {"b": 1}}, "a.b")
    assert result == 1


def test_resolve_dot_path_missing_returns_none():
    result = _resolve_dot_path({"a": {"b": 1}}, "a.c")
    assert result is None


def test_resolve_dot_path_deeply_nested():
    data = {"x": {"y": {"z": "deep"}}}
    result = _resolve_dot_path(data, "x.y.z")
    assert result == "deep"


def test_set_nested_key_creates_intermediaries():
    data = {}
    _set_nested_key(data, "a.b.c", "v")
    assert data == {"a": {"b": {"c": "v"}}}


def test_merge_carry_forward_nested():
    """Deep merge must not clobber existing nested keys not present in carry."""
    target = {"outer": {"existing_key": "keep_me", "other": 1}}
    carry = {"outer": {"new_key": "added"}}
    _merge_carry_forward(target, carry)
    assert target["outer"]["existing_key"] == "keep_me"
    assert target["outer"]["new_key"] == "added"
    assert target["outer"]["other"] == 1


# ── Integration tests ─────────────────────────────────────────────────────────

async def test_loop_basic_iteration_count(tmp_path):
    """Stage with max_iterations=3 and no until runs exactly 3 times."""
    call_count = 0

    async def counter(args):
        nonlocal call_count
        call_count += 1
        return {"count": call_count}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="counter"),
        loop=IterationConfig(max_iterations=3),
        depends_on=[],
    )], tmp_path)
    _register(harness, "counter", counter)

    result = await harness.run({})
    assert call_count == 3
    assert result["s"]["count"] == 3


async def test_loop_stops_when_until_true(tmp_path):
    """Stage stops on iteration 2 when until condition is satisfied."""
    call_count = 0

    async def worker(args):
        nonlocal call_count
        call_count += 1
        return {"done": call_count >= 2, "iteration": call_count}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="worker"),
        loop=IterationConfig(max_iterations=5, until="{{ done }}"),
        depends_on=[],
    )], tmp_path)
    _register(harness, "worker", worker)

    result = await harness.run({})
    assert call_count == 2
    assert result["s"]["done"] is True
    assert result["s"]["iteration"] == 2


async def test_loop_iteration_var_always_defined(tmp_path):
    """_iteration.num is 1 on first call — the variable is never undefined.

    We verify by rendering _iteration.num into a tool arg via Jinja2.
    """
    received_nums = []

    async def capture(args):
        received_nums.append(args.get("iter_num"))
        return {"val": 1}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="capture",
            args={"iter_num": "{{ _iteration.num }}"},
        ),
        loop=IterationConfig(max_iterations=1),
        depends_on=[],
    )], tmp_path)
    _register(harness, "capture", capture)

    await harness.run({})
    assert len(received_nums) == 1
    assert received_nums[0] == 1


async def test_loop_iteration_var_fields(tmp_path):
    """is_first True on iter 1, is_last True on iter 3 of max_iterations=3.

    We verify by rendering each field into a tool arg via Jinja2.
    """
    received = []

    async def capture(args):
        received.append({
            "num": args.get("iter_num"),
            "is_first": args.get("is_first"),
            "is_last": args.get("is_last"),
        })
        return {"val": len(received)}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="capture",
            args={
                "iter_num": "{{ _iteration.num }}",
                "is_first": "{{ _iteration.is_first }}",
                "is_last": "{{ _iteration.is_last }}",
            },
        ),
        loop=IterationConfig(max_iterations=3),
        depends_on=[],
    )], tmp_path)
    _register(harness, "capture", capture)

    await harness.run({})
    assert len(received) == 3

    # Jinja2 NativeEnvironment renders Python booleans as True/False
    assert received[0]["num"] == 1
    assert received[0]["is_first"] is True
    assert received[0]["is_last"] is False

    assert received[1]["num"] == 2
    assert received[1]["is_first"] is False
    assert received[1]["is_last"] is False

    assert received[2]["num"] == 3
    assert received[2]["is_first"] is False
    assert received[2]["is_last"] is True


async def test_loop_carry_forward_selective(tmp_path):
    """Only specified dot-paths appear in _iteration.carry_forward on iteration 2+.

    We verify by rendering carry_forward contents into tool args via Jinja2.
    """
    received = []

    async def worker(args):
        received.append({
            "carry_keep": args.get("carry_keep"),
            "carry_nested_x": args.get("carry_nested_x"),
            "carry_drop": args.get("carry_drop"),
            "iter_num": args.get("iter_num"),
        })
        return {"keep_this": "value_a", "drop_this": "value_b", "nested": {"x": 42}}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="worker",
            args={
                "iter_num": "{{ _iteration.num }}",
                "carry_keep": "{{ _iteration.carry_forward.keep_this }}",
                "carry_nested_x": "{{ _iteration.carry_forward.nested.x }}",
                "carry_drop": "{{ _iteration.carry_forward.drop_this }}",
            },
        ),
        loop=IterationConfig(
            max_iterations=2,
            carry_forward=["keep_this", "nested.x"],
        ),
        depends_on=[],
    )], tmp_path)
    _register(harness, "worker", worker)

    await harness.run({})
    assert len(received) == 2

    # First iteration: carry_forward is empty, so Jinja renders as empty/None
    # NativeEnvironment with ChainableUndefined renders missing as ''
    iter1 = received[0]
    assert iter1["iter_num"] == 1
    # carry fields should be empty/falsy on iteration 1
    assert not iter1["carry_keep"]
    assert not iter1["carry_nested_x"]

    # Second iteration: specified carry paths should be populated
    iter2 = received[1]
    assert iter2["iter_num"] == 2
    assert iter2["carry_keep"] == "value_a"
    assert iter2["carry_nested_x"] == 42
    # "drop_this" was NOT listed in carry_forward, so should be empty
    assert not iter2["carry_drop"]


async def test_loop_carry_forward_none_carries_everything(tmp_path):
    """When carry_forward is None, entire previous result is in _iteration.carry_forward."""
    received = []

    async def worker(args):
        received.append({
            "carry_alpha": args.get("carry_alpha"),
            "carry_beta": args.get("carry_beta"),
            "iter_num": args.get("iter_num"),
        })
        return {"alpha": 1, "beta": "two"}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="worker",
            args={
                "iter_num": "{{ _iteration.num }}",
                "carry_alpha": "{{ _iteration.carry_forward.alpha }}",
                "carry_beta": "{{ _iteration.carry_forward.beta }}",
            },
        ),
        loop=IterationConfig(max_iterations=2, carry_forward=None),
        depends_on=[],
    )], tmp_path)
    _register(harness, "worker", worker)

    await harness.run({})

    # On iteration 2, all previous result keys should be in carry_forward
    assert received[1]["carry_alpha"] == 1
    assert received[1]["carry_beta"] == "two"


async def test_loop_carry_forward_top_level_merge(tmp_path):
    """Carry-forward values are accessible at top-level context, not just _iteration.carry_forward.

    The engine merges carry_forward into the top-level context so stage
    descriptions (Jinja2) can reference prior-iteration fields directly
    (e.g. {{ score }}) without the _iteration.carry_forward prefix.
    We verify this by rendering top-level key references into tool args.
    """
    received = []

    async def worker(args):
        received.append({
            "top_score": args.get("top_score"),
            "top_label": args.get("top_label"),
            "iter_num": args.get("iter_num"),
        })
        return {"score": 99, "label": "excellent"}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="worker",
            args={
                "iter_num": "{{ _iteration.num }}",
                # Reference top-level keys directly (not via _iteration.carry_forward)
                "top_score": "{{ score }}",
                "top_label": "{{ label }}",
            },
        ),
        loop=IterationConfig(max_iterations=2, carry_forward=None),
        depends_on=[],
    )], tmp_path)
    _register(harness, "worker", worker)

    await harness.run({})

    # On iteration 2, top-level merged carry keys must be resolved by Jinja2
    assert received[1]["top_score"] == 99
    assert received[1]["top_label"] == "excellent"


async def test_loop_max_iterations_exhausted_returns_last_result(tmp_path):
    """When until is never satisfied and max_iterations is reached, return last result without raising."""
    call_count = 0

    async def never_done(args):
        nonlocal call_count
        call_count += 1
        return {"status": "pending", "attempt": call_count}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="never_done"),
        loop=IterationConfig(max_iterations=3, until="{{ status == 'done' }}"),
        depends_on=[],
    )], tmp_path)
    _register(harness, "never_done", never_done)

    result = await harness.run({})
    assert call_count == 3
    assert result["s"]["status"] == "pending"
    assert result["s"]["attempt"] == 3


async def test_loop_first_iteration_meets_until(tmp_path):
    """If until is true on iteration 1, stage runs exactly once."""
    call_count = 0

    async def immediate(args):
        nonlocal call_count
        call_count += 1
        return {"ready": True}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="immediate"),
        loop=IterationConfig(max_iterations=10, until="{{ ready }}"),
        depends_on=[],
    )], tmp_path)
    _register(harness, "immediate", immediate)

    result = await harness.run({})
    assert call_count == 1
    assert result["s"]["ready"] is True


async def test_loop_iteration_var_custom_name(tmp_path):
    """iteration_var='_round' makes the variable available as _round, not _iteration.

    We verify by rendering _round.num into a tool arg and confirming _iteration is absent.
    """
    received = []

    async def worker(args):
        received.append({
            "round_num": args.get("round_num"),
            "iter_num": args.get("iter_num"),
        })
        return {"val": 1}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="worker",
            args={
                # Custom variable name
                "round_num": "{{ _round.num }}",
                # Default variable name should NOT be set (should render as empty string)
                "iter_num": "{{ _iteration.num }}",
            },
        ),
        loop=IterationConfig(max_iterations=2, iteration_var="_round"),
        depends_on=[],
    )], tmp_path)
    _register(harness, "worker", worker)

    await harness.run({})

    # _round.num should resolve correctly
    assert received[0]["round_num"] == 1
    assert received[1]["round_num"] == 2
    # _iteration.num should be empty/None since we used _round instead
    assert not received[0]["iter_num"]


async def test_loop_event_emissions(tmp_path):
    """loop_iteration events are emitted with correct fields."""
    events = []
    call_count = 0

    async def worker(args):
        nonlocal call_count
        call_count += 1
        return {"done": call_count >= 3}

    harness = _make_harness(
        [Stage(
            id="s",
            tool_call=ToolCallConfig(name="worker"),
            loop=IterationConfig(max_iterations=3, until="{{ done }}"),
            depends_on=[],
        )],
        tmp_path,
        on_event=lambda t, d: events.append((t, d)),
    )
    _register(harness, "worker", worker)

    await harness.run({})

    loop_events = [(t, d) for t, d in events if t == "loop_iteration"]
    assert len(loop_events) == 3

    for i, (event_type, data) in enumerate(loop_events, start=1):
        assert event_type == "loop_iteration"
        assert data["stage"] == "s"
        assert data["iteration"] == i
        assert data["max"] == 3
        # until_met is True only on the last iteration (done == True on call 3)
        assert data["until_met"] == (i == 3)


async def test_loop_backoff_pacing(tmp_path):
    """Backoff delays are applied between iterations (not after the last)."""
    call_count = 0

    async def worker(args):
        nonlocal call_count
        call_count += 1
        return {"val": call_count}

    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        harness = _make_harness([Stage(
            id="s",
            tool_call=ToolCallConfig(name="worker"),
            loop=IterationConfig(max_iterations=3, backoff_s=1.0, backoff_max_s=60.0),
            depends_on=[],
        )], tmp_path)
        _register(harness, "worker", worker)

        await harness.run({})

    assert call_count == 3
    # Two sleeps for 3 iterations (between iter 1->2 and 2->3, NOT after iter 3)
    assert len(sleep_calls) == 2
    # Delays should double: 1.0 * 2^0 = 1.0, 1.0 * 2^1 = 2.0
    assert sleep_calls[0] == pytest.approx(1.0)
    assert sleep_calls[1] == pytest.approx(2.0)


async def test_loop_dot_path_resolution_in_carry_forward(tmp_path):
    """Selective carry_forward correctly extracts a nested path from previous result."""
    received = []

    async def worker(args):
        received.append({
            "iter_num": args.get("iter_num"),
            "carry_summary": args.get("carry_summary"),
            "carry_details": args.get("carry_details"),
            "carry_unrelated": args.get("carry_unrelated"),
        })
        return {
            "report": {
                "summary": "good stuff",
                "details": {"score": 95},
            },
            "unrelated": "ignored",
        }

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="worker",
            args={
                "iter_num": "{{ _iteration.num }}",
                # dot-path "report.summary" -> carry_forward.report.summary
                "carry_summary": "{{ _iteration.carry_forward.report.summary }}",
                # "report.details" not listed -> should be empty
                "carry_details": "{{ _iteration.carry_forward.report.details }}",
                # "unrelated" not listed -> should be empty
                "carry_unrelated": "{{ _iteration.carry_forward.unrelated }}",
            },
        ),
        loop=IterationConfig(
            max_iterations=2,
            carry_forward=["report.summary"],
        ),
        depends_on=[],
    )], tmp_path)
    _register(harness, "worker", worker)

    await harness.run({})

    iter2 = received[1]
    assert iter2["iter_num"] == 2
    assert iter2["carry_summary"] == "good stuff"
    # "report.details" was not listed in carry_forward — should be absent/falsy
    assert not iter2["carry_details"]
    # "unrelated" was not listed — should be absent/falsy
    assert not iter2["carry_unrelated"]


async def test_loop_carry_forward_empty_on_first_iteration(tmp_path):
    """_iteration.carry_forward is {} on first iteration regardless of carry_forward setting.

    We verify by rendering a carry_forward key that only exists in the previous result.
    On iteration 1 it must be empty/falsy; on iteration 2 it must be populated.
    """
    received = []

    async def worker(args):
        received.append({
            "iter_num": args.get("iter_num"),
            "carry_data": args.get("carry_data"),
        })
        return {"data": "something"}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="worker",
            args={
                "iter_num": "{{ _iteration.num }}",
                "carry_data": "{{ _iteration.carry_forward.data }}",
            },
        ),
        loop=IterationConfig(max_iterations=2, carry_forward=None),
        depends_on=[],
    )], tmp_path)
    _register(harness, "worker", worker)

    await harness.run({})

    # Iteration 1: carry_forward is empty — data key should not exist yet
    assert received[0]["iter_num"] == 1
    assert not received[0]["carry_data"]

    # Iteration 2: carry_forward includes everything from the previous result
    assert received[1]["iter_num"] == 2
    assert received[1]["carry_data"] == "something"

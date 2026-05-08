import pytest
from armature.runtime.loop import LoopExecutor


async def test_loop_stops_at_max():
    call_count = 0

    async def handler(ctx):
        nonlocal call_count
        call_count += 1
        return {"value": call_count}

    executor = LoopExecutor(handler=handler, until=lambda r: False, max_iterations=3)
    result = await executor.run({})
    assert call_count == 3


async def test_loop_stops_when_condition_met():
    call_count = 0

    async def handler(ctx):
        nonlocal call_count
        call_count += 1
        return {"value": call_count}

    executor = LoopExecutor(handler=handler, until=lambda r: r.get("value", 0) >= 2, max_iterations=10)
    result = await executor.run({})
    assert call_count == 2
    assert result["value"] == 2


async def test_loop_returns_last_result():
    async def handler(ctx):
        return {"final": "output"}

    executor = LoopExecutor(handler=handler, until=lambda r: True, max_iterations=5)
    result = await executor.run({})
    assert result["final"] == "output"


async def test_loop_runs_once_when_until_immediately_true():
    calls = []

    async def handler(ctx):
        calls.append(1)
        return {"done": True}

    executor = LoopExecutor(handler=handler, until=lambda r: r.get("done"), max_iterations=5)
    await executor.run({})
    assert len(calls) == 1


async def test_loop_passes_context_updates_to_next_iteration():
    """Handler receives merged context from previous iteration's result."""
    seen = []

    async def handler(ctx):
        seen.append(ctx.get("count", 0))
        return {"count": ctx.get("count", 0) + 1}

    executor = LoopExecutor(handler=handler, until=lambda r: r.get("count", 0) >= 3, max_iterations=10)
    await executor.run({})
    assert seen == [0, 1, 2]


async def test_loop_max_one_iteration():
    calls = []

    async def handler(ctx):
        calls.append(True)
        return {}

    executor = LoopExecutor(handler=handler, until=lambda r: False, max_iterations=1)
    await executor.run({})
    assert len(calls) == 1


async def test_loop_with_initial_context():
    """Initial context values are available in the first handler call."""
    received = []

    async def handler(ctx):
        received.append(ctx.get("seed"))
        return {}

    executor = LoopExecutor(handler=handler, until=lambda r: True, max_iterations=3)
    await executor.run({"seed": "initial"})
    assert received[0] == "initial"


async def test_loop_non_dict_result_does_not_update_context():
    """When handler returns a non-dict, context is not updated but result is returned."""
    async def handler(ctx):
        return "plain string"

    executor = LoopExecutor(handler=handler, until=lambda r: True, max_iterations=3)
    result = await executor.run({})
    assert result == "plain string"

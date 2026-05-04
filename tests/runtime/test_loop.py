import pytest
from armature.runtime.loop import LoopExecutor

async def test_loop_stops_at_max():
    call_count = 0

    async def handler(ctx):
        nonlocal call_count
        call_count += 1
        return {"value": call_count}

    def never_done(result): return False

    executor = LoopExecutor(handler=handler, until=never_done, max_iterations=3)
    result = await executor.run({})
    assert call_count == 3

async def test_loop_stops_when_condition_met():
    call_count = 0

    async def handler(ctx):
        nonlocal call_count
        call_count += 1
        return {"value": call_count}

    def done_at_two(result): return result.get("value", 0) >= 2

    executor = LoopExecutor(handler=handler, until=done_at_two, max_iterations=10)
    result = await executor.run({})
    assert call_count == 2
    assert result["value"] == 2

async def test_loop_returns_last_result():
    async def handler(ctx):
        return {"final": "output"}

    executor = LoopExecutor(handler=handler, until=lambda r: True, max_iterations=5)
    result = await executor.run({})
    assert result["final"] == "output"

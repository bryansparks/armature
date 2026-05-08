import asyncio
import pytest
from armature.runtime.dag import topological_order, DAGExecutor

def test_topological_order_linear():
    stages = {"a": [], "b": ["a"], "c": ["b"]}
    order = topological_order(stages)
    assert order.index("a") < order.index("b") < order.index("c")

def test_topological_order_parallel():
    stages = {"a": [], "b": [], "c": ["a", "b"]}
    order = topological_order(stages)
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("c")

def test_topological_order_cycle_raises():
    stages = {"a": ["b"], "b": ["a"]}
    with pytest.raises(ValueError, match="cycle"):
        topological_order(stages)


# ── DAGExecutor: correctness ──────────────────────────────────────────────────

async def test_dag_executor_respects_dependency_order():
    """A → B → C: B must run after A, C after B."""
    execution_order = []

    async def make_handler(name):
        async def handler(ctx):
            execution_order.append(name)
            return {"done": True}
        return handler

    handlers = {
        "a": await make_handler("a"),
        "b": await make_handler("b"),
        "c": await make_handler("c"),
    }
    deps = {"a": [], "b": ["a"], "c": ["b"]}

    executor = DAGExecutor(handlers, deps)
    results = await executor.run({})

    assert execution_order.index("a") < execution_order.index("b")
    assert execution_order.index("b") < execution_order.index("c")
    assert "a" in results and "b" in results and "c" in results


async def test_dag_executor_passes_results_to_downstream():
    async def stage_a(ctx):
        return {"value": 42}

    async def stage_b(ctx):
        return {"doubled": ctx["a"]["value"] * 2}

    executor = DAGExecutor(
        {"a": stage_a, "b": stage_b},
        {"a": [], "b": ["a"]},
    )
    results = await executor.run({})
    assert results["b"]["doubled"] == 84


async def test_dag_executor_single_stage():
    async def handler(ctx):
        return {"ok": True}

    executor = DAGExecutor({"s": handler}, {"s": []})
    results = await executor.run({})
    assert results["s"]["ok"] is True


async def test_dag_executor_passes_initial_ctx():
    async def handler(ctx):
        return {"saw": ctx["input_val"]}

    executor = DAGExecutor({"s": handler}, {"s": []})
    results = await executor.run({"input_val": "hello"})
    assert results["s"]["saw"] == "hello"


async def test_dag_executor_propagates_exception():
    async def bad(ctx):
        raise ValueError("stage failed")

    executor = DAGExecutor({"s": bad}, {"s": []})
    with pytest.raises(ValueError, match="stage failed"):
        await executor.run({})


# ── DAGExecutor: parallelism ──────────────────────────────────────────────────

async def test_independent_stages_run_concurrently():
    """Stages A and B with no deps should overlap in wall time."""
    import time

    barrier = asyncio.Event()
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    overlap_detected = False

    async def stage_a(ctx):
        nonlocal overlap_detected
        a_started.set()
        await asyncio.sleep(0.02)
        if b_started.is_set():
            overlap_detected = True
        return {"a": True}

    async def stage_b(ctx):
        b_started.set()
        await asyncio.sleep(0.02)
        return {"b": True}

    executor = DAGExecutor(
        {"a": stage_a, "b": stage_b},
        {"a": [], "b": []},
    )
    results = await executor.run({})
    assert results["a"]["a"] is True
    assert results["b"]["b"] is True
    assert overlap_detected, "Stages A and B should have run concurrently"


async def test_parallel_stages_faster_than_sequential():
    """Two independent 50ms stages should complete in ~50ms, not ~100ms."""
    import time

    async def slow(ctx):
        await asyncio.sleep(0.05)
        return {"done": True}

    t0 = time.monotonic()
    executor = DAGExecutor(
        {"a": slow, "b": slow},
        {"a": [], "b": []},
    )
    await executor.run({})
    elapsed = time.monotonic() - t0

    assert elapsed < 0.09, f"Expected ~50ms for parallel run, got {elapsed:.3f}s"


async def test_diamond_dag_parallelism():
    """A → (B, C) → D: B and C should overlap, D waits for both."""
    b_started = asyncio.Event()
    c_started = asyncio.Event()
    overlap_at_b = False
    overlap_at_c = False

    async def stage_a(ctx):
        return {"a": 1}

    async def stage_b(ctx):
        nonlocal overlap_at_b
        b_started.set()
        if c_started.is_set():
            overlap_at_b = True
        await asyncio.sleep(0.02)
        return {"b": 1}

    async def stage_c(ctx):
        nonlocal overlap_at_c
        c_started.set()
        if b_started.is_set():
            overlap_at_c = True
        await asyncio.sleep(0.02)
        return {"c": 1}

    async def stage_d(ctx):
        return {"d": ctx["b"]["b"] + ctx["c"]["c"]}

    executor = DAGExecutor(
        {"a": stage_a, "b": stage_b, "c": stage_c, "d": stage_d},
        {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]},
    )
    results = await executor.run({})
    assert results["d"]["d"] == 2
    assert overlap_at_b or overlap_at_c, "B and C should have overlapped"


async def test_fan_out_style_parallelism():
    """Many independent stages all run in a single wave."""
    import time

    completed: list[str] = []

    async def make_slow(name):
        async def handler(ctx):
            await asyncio.sleep(0.03)
            completed.append(name)
            return {"name": name}
        return handler

    names = [f"s{i}" for i in range(5)]
    handlers = {n: await make_slow(n) for n in names}
    deps = {n: [] for n in names}

    t0 = time.monotonic()
    executor = DAGExecutor(handlers, deps)
    results = await executor.run({})
    elapsed = time.monotonic() - t0

    assert len(completed) == 5
    assert elapsed < 0.08, f"5 parallel 30ms stages should finish in ~30ms, got {elapsed:.3f}s"

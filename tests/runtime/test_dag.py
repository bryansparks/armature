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

async def test_dag_executor_runs_in_order():
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

    assert execution_order == ["a", "b", "c"]
    assert "a" in results and "b" in results and "c" in results

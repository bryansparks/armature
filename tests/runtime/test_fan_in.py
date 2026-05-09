"""Tests for fan_in strategies in the engine's partition_source fan-out."""
import pytest
from pathlib import Path
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig,
)
from armature.runtime.engine import Harness
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


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


# ── fan_in="list" (default) ───────────────────────────────────────────────────

async def test_fan_in_list_returns_list_of_results(tmp_path):
    async def process(args):
        return {"item": args.get("it"), "processed": True}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="proc", args={"it": "{{ it }}"}),
        partition_source="{{ items }}",
        partition_key="it",
        fan_out=3,
        fan_in="list",
        depends_on=[],
    )], tmp_path)
    _register(harness, "proc", process)

    result = await harness.run({"items": ["a", "b", "c"]})
    assert isinstance(result["s"], list)
    assert len(result["s"]) == 3
    items_seen = {r["item"] for r in result["s"]}
    assert items_seen == {"a", "b", "c"}


async def test_fan_in_default_is_list(tmp_path):
    """Stage without fan_in set defaults to 'list'."""
    async def t(args): return {"val": 1}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        partition_source="{{ items }}",
        partition_key="item",
        fan_out=2,
        depends_on=[],
        # fan_in not set — defaults to "list"
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({"items": [1, 2]})
    assert isinstance(result["s"], list)
    assert len(result["s"]) == 2


# ── fan_in="merge" ────────────────────────────────────────────────────────────

async def test_fan_in_merge_merges_all_result_dicts(tmp_path):
    async def process(args):
        item = args.get("item")
        return {f"result_{item}": f"value_{item}"}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="proc", args={"item": "{{ item }}"}),
        partition_source="{{ items }}",
        partition_key="item",
        fan_out=3,
        fan_in="merge",
        depends_on=[],
    )], tmp_path)
    _register(harness, "proc", process)

    result = await harness.run({"items": ["x", "y", "z"]})
    assert isinstance(result["s"], dict)
    assert result["s"]["result_x"] == "value_x"
    assert result["s"]["result_y"] == "value_y"
    assert result["s"]["result_z"] == "value_z"


async def test_fan_in_merge_empty_list_returns_empty_dict(tmp_path):
    async def t(args): return {}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        partition_source="{{ items }}",
        partition_key="item",
        fan_out=5,
        fan_in="merge",
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({"items": []})
    assert result["s"] == {}


async def test_fan_in_merge_later_results_overwrite_earlier(tmp_path):
    """With merge, later items' keys overwrite earlier items' conflicting keys."""
    call_order = []

    async def process(args):
        item = args.get("item")
        call_order.append(item)
        return {"shared_key": f"from_{item}"}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="proc", args={"item": "{{ item }}"}),
        partition_source="{{ items }}",
        partition_key="item",
        fan_out=1,  # serial to make order deterministic
        fan_in="merge",
        depends_on=[],
    )], tmp_path)
    _register(harness, "proc", process)

    result = await harness.run({"items": ["first", "last"]})
    # Result is a merged dict — "last" should overwrite "first" for shared_key
    assert result["s"]["shared_key"] in ("from_first", "from_last")


# ── fan_in="first" ────────────────────────────────────────────────────────────

async def test_fan_in_first_returns_first_result(tmp_path):
    async def process(args):
        return {"item_result": args.get("item"), "data": "ok"}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="proc", args={"item": "{{ item }}"}),
        partition_source="{{ items }}",
        partition_key="item",
        fan_out=1,  # serial, so first=items[0]
        fan_in="first",
        depends_on=[],
    )], tmp_path)
    _register(harness, "proc", process)

    result = await harness.run({"items": ["alpha", "beta", "gamma"]})
    assert isinstance(result["s"], dict)
    assert result["s"]["item_result"] == "alpha"


async def test_fan_in_first_empty_list_returns_empty_dict(tmp_path):
    async def t(args): return {}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        partition_source="{{ items }}",
        partition_key="item",
        fan_out=5,
        fan_in="first",
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    result = await harness.run({"items": []})
    assert result["s"] == {}


# ── fan_in result flows downstream ───────────────────────────────────────────

async def test_fan_in_merge_result_accessible_downstream(tmp_path):
    async def gather(args):
        return {"count_a": 3, "count_b": 5}

    async def aggregate(args): return {"total": 42}

    harness = _make_harness([
        Stage(
            id="gather",
            tool_call=ToolCallConfig(name="gather"),
            partition_source="{{ items }}",
            partition_key="item",
            fan_out=2,
            fan_in="merge",
            depends_on=[],
        ),
        Stage(id="agg", tool_call=ToolCallConfig(name="agg"), depends_on=["gather"]),
    ], tmp_path)
    _register(harness, "gather", gather)
    _register(harness, "agg", aggregate)

    result = await harness.run({"items": ["a", "b"]})
    assert isinstance(result["gather"], dict)  # merged, not list
    assert result["agg"]["total"] == 42


# ── Error handling ────────────────────────────────────────────────────────────

async def test_partition_source_non_list_raises_value_error(tmp_path):
    """If partition_source resolves to non-list, ValueError is raised."""
    async def t(args): return {"ok": True}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        partition_source="{{ not_a_list }}",  # will resolve to a string
        partition_key="item",
        fan_out=2,
        depends_on=[],
    )], tmp_path)
    _register(harness, "t", t)

    with pytest.raises(ValueError, match="expected list"):
        await harness.run({"not_a_list": "string-value"})


async def test_per_item_exception_caught_as_fan_out_error(tmp_path):
    """A failing item returns _fan_out_error key rather than aborting all items."""
    call_count = 0

    async def fail_on_second(args):
        nonlocal call_count
        call_count += 1
        if args.get("item") == "bad":
            raise RuntimeError("item failed")
        return {"item": args.get("item"), "ok": True}

    harness = _make_harness([Stage(
        id="s",
        tool_call=ToolCallConfig(name="proc", args={"item": "{{ item }}"}),
        partition_source="{{ items }}",
        partition_key="item",
        fan_out=2,
        fan_in="list",
        depends_on=[],
    )], tmp_path)
    _register(harness, "proc", fail_on_second)

    result = await harness.run({"items": ["good", "bad"]})
    results = result["s"]
    assert len(results) == 2
    good = next(r for r in results if r.get("ok"))
    bad = next(r for r in results if "_fan_out_error" in r)
    assert good["item"] == "good"
    assert "item failed" in bad["_fan_out_error"]

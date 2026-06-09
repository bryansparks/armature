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


async def test_fan_out_fail_as_value_when_partition_source_undefined(tmp_path):
    """A fan-out stage with fail_as_value=True returns _failed dict when
    partition_source resolves to undefined (e.g., upstream guided_json parse error).

    Regression: _execute_fan_out_stage raised ValueError outside the try/except
    block that honours fail_as_value, so the flag was silently ignored.
    """
    async def produce_parse_error(args):
        return {"_parse_error": True, "raw": "```json\n{}\n```"}

    harness = _make_harness([
        Stage(
            id="upstream",
            tool_call=ToolCallConfig(name="produce_error"),
            depends_on=[],
        ),
        Stage(
            id="fanout",
            tool_call=ToolCallConfig(name="proc", args={"x": "{{ it }}"}),
            partition_source="{{ upstream.items }}",
            partition_key="it",
            fan_out=3,
            fan_in="list",
            fail_as_value=True,
            depends_on=["upstream"],
        ),
    ], tmp_path)
    _register(harness, "produce_error", produce_parse_error)

    async def proc(args): return {"ok": True}
    _register(harness, "proc", proc)

    result = await harness.run({})
    assert result["fanout"].get("_failed") is True
    assert "expected list" in result["fanout"]["_failed_reason"]


# ── Phase E: fan_in="consensus" (RED) ────────────────────────────────────────

async def test_consensus_fan_in_calls_judge(tmp_path):
    """fan_in='consensus' should invoke the judge LLM to synthesize parallel results."""
    from unittest.mock import AsyncMock, patch
    from armature.nodes.subagent import _fan_in

    results = [{"answer": "42"}, {"answer": "43"}, {"answer": "42"}]

    mock_judge = AsyncMock(return_value={"answer": "42", "consensus": True})
    with patch("armature.nodes.subagent._consensus_judge", mock_judge):
        result = _fan_in(results, "consensus")

    # consensus strategy should mark that the judge needs to be called
    assert result.get("_needs_consensus") is True


async def test_consensus_judge_returns_dict(tmp_path):
    """_consensus_judge must return a dict synthesizing all parallel outputs."""
    from unittest.mock import AsyncMock, patch
    from armature.nodes.subagent import _consensus_judge
    from armature.spec.models import Stage, ModelTiers, ModelTierConfig

    stage = Stage(
        id="s",
        subagent_spec="dummy.yaml",
        fan_out=2,
        fan_in="consensus",
        depends_on=[],
    )
    results = [{"summary": "A says X"}, {"summary": "B says Y"}]

    mock_response_msg = AsyncMock()
    mock_response_msg.content = '{"consensus": "X is correct"}'
    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock(message=mock_response_msg)]

    with patch("armature.nodes.subagent.litellm_completion", AsyncMock(return_value=mock_response)):
        result = await _consensus_judge(results, stage)

    assert isinstance(result, dict)


async def test_fan_in_consensus_invalid_value_raises(tmp_path):
    """Unknown fan_in strategy should still return the list-wrapped fallback."""
    from armature.nodes.subagent import _fan_in
    result = _fan_in([{"a": 1}], "unknown_strategy")
    assert result == {"results": [{"a": 1}]}


# ── partition_source error hint ───────────────────────────────────────────────

async def test_partition_source_null_error_hints_at_upstream_stage(tmp_path):
    """When partition_source resolves to None because an upstream stage returned null
    for the referenced key, the error message names the upstream stage and key."""
    async def noop(args):
        return {}

    harness = _make_harness([
        Stage(
            id="planner",
            tool_call=ToolCallConfig(name="noop"),
            depends_on=[],
        ),
        Stage(
            id="runner",
            tool_call=ToolCallConfig(name="noop"),
            fan_out=5,
            fan_in="list",
            partition_source="{{ planner.queries }}",
            partition_key="q",
            depends_on=["planner"],
        ),
    ], tmp_path)
    _register(harness, "noop", noop)

    with pytest.raises(ValueError) as exc_info:
        # planner returns {} so planner.queries is None/Undefined → error
        await harness.run({})

    msg = str(exc_info.value)
    assert "planner" in msg
    assert "queries" in msg


async def test_partition_source_null_error_suggests_output_valid(tmp_path):
    """The null partition_source error message mentions checking output_valid."""
    async def noop(args):
        return {}

    harness = _make_harness([
        Stage(
            id="src",
            tool_call=ToolCallConfig(name="noop"),
            depends_on=[],
        ),
        Stage(
            id="fan",
            tool_call=ToolCallConfig(name="noop"),
            fan_out=3,
            fan_in="list",
            partition_source="{{ src.items }}",
            partition_key="item",
            depends_on=["src"],
        ),
    ], tmp_path)
    _register(harness, "noop", noop)

    with pytest.raises(ValueError) as exc_info:
        await harness.run({})

    assert "output_valid" in str(exc_info.value).lower() or "guided_json" in str(exc_info.value).lower()

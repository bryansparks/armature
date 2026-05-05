import asyncio
import pytest
from pathlib import Path
from armature.nodes.subagent import SubagentNode
from armature.spec.models import Stage

FIXTURES = Path(__file__).parent.parent / "fixtures"


def make_subagent_stage() -> Stage:
    return Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
    )


async def test_subagent_runs_child_workflow(tmp_path):
    stage = make_subagent_stage()
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "hello"})
    assert "respond" in result
    assert result["respond"]["exit_code"] == 0
    assert "child says" in result["respond"]["stdout"]


async def test_subagent_passes_context_as_vars(tmp_path):
    stage = make_subagent_stage()
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "world"})
    assert "world" in result["respond"]["stdout"]


def test_subagent_raises_if_spec_missing(tmp_path):
    stage = Stage(id="bad", subagent_spec="/nonexistent/spec.yaml")
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        asyncio.run(node.execute({}))


def make_fanout_stage(n: int, fan_in: str = "list", partition_key: str | None = None) -> Stage:
    return Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
        fan_out=n,
        fan_in=fan_in,
        partition_key=partition_key,
    )


async def test_fan_out_list_returns_n_results(tmp_path):
    stage = make_fanout_stage(3, fan_in="list")
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "hello"})
    assert "results" in result
    assert len(result["results"]) == 3
    for r in result["results"]:
        assert "respond" in r
        assert "child says" in r["respond"]["stdout"]


async def test_fan_out_children_run_in_parallel(tmp_path):
    stage = make_fanout_stage(3, fan_in="list")
    node = SubagentNode(stage=stage, session_dir=tmp_path)

    import time
    t0 = time.monotonic()
    result = await node.execute({"greeting": "timing"})
    elapsed = time.monotonic() - t0

    assert len(result["results"]) == 3
    assert elapsed < 5.0


async def test_fan_out_merge_combines_dicts(tmp_path):
    stage = make_fanout_stage(2, fan_in="merge")
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "merge-test"})
    assert "respond" in result


async def test_fan_out_first_returns_single_result(tmp_path):
    stage = make_fanout_stage(3, fan_in="first")
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "first"})
    assert "respond" in result
    assert "results" not in result


async def test_fan_out_partition_key_splits_list(tmp_path):
    stage = Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
        fan_out=2,
        fan_in="list",
        partition_key="items",
    )
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "hello", "items": ["a", "b", "c", "d"]})
    assert "results" in result
    assert len(result["results"]) == 2
    for child_result in result["results"]:
        assert isinstance(child_result, dict)


async def test_fan_out_partition_key_missing_gives_full_context(tmp_path):
    stage = Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
        fan_out=2,
        fan_in="list",
        partition_key="nonexistent_key",
    )
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "fallback"})
    assert len(result["results"]) == 2


async def test_fan_out_one_wraps_in_fan_in(tmp_path):
    stage_fanout = make_fanout_stage(1, fan_in="list")
    stage_single = Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
    )
    node_fanout = SubagentNode(stage=stage_fanout, session_dir=tmp_path / "fanout")
    node_single = SubagentNode(stage=stage_single, session_dir=tmp_path / "single")

    result_fanout = await node_fanout.execute({"greeting": "one"})
    result_single = await node_single.execute({"greeting": "one"})

    # fan_out=1 with fan_in="list" wraps in {"results": [...]}
    assert "results" in result_fanout
    assert len(result_fanout["results"]) == 1
    # no fan_out returns raw result dict
    assert "respond" in result_single

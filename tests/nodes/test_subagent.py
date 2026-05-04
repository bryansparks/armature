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
    import asyncio
    with pytest.raises(FileNotFoundError):
        asyncio.run(node.execute({}))

import pytest
from armature.nodes.script import ScriptNode
from armature.spec.models import Adapter

async def test_script_node_runs_command():
    adapter = Adapter(name="echo_test", type="script", cmd="echo hello")
    node = ScriptNode(adapter=adapter)
    result = await node.execute({})
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]

async def test_script_node_captures_failure():
    adapter = Adapter(name="fail_test", type="script", cmd="exit 1")
    node = ScriptNode(adapter=adapter)
    result = await node.execute({})
    assert result["exit_code"] == 1

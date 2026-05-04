import pytest
from unittest.mock import patch
from armature.nodes.gate import HumanGateNode
from armature.spec.models import Stage, Role, RoleType

async def test_gate_approved_on_yes():
    stage = Stage(
        id="gate1",
        gate="human",
        present="Decision: approve?",
        role=Role(name="r", type=RoleType.WORKER, description="d"),
    )
    node = HumanGateNode(stage=stage)
    with patch("builtins.input", return_value="yes"):
        result = await node.execute({})
    assert result["approved"] is True

async def test_gate_rejected_on_no():
    stage = Stage(
        id="gate1",
        gate="human",
        present="Decision: approve?",
        role=Role(name="r", type=RoleType.WORKER, description="d"),
    )
    node = HumanGateNode(stage=stage)
    with patch("builtins.input", side_effect=["no", ""]):
        result = await node.execute({})
    assert result["approved"] is False
    assert "feedback" in result

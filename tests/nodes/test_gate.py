import pytest
from unittest.mock import patch
from armature.nodes.gate import HumanGateNode
from armature.spec.models import Stage


def _gate_stage(present: str | None = "Decision: approve?") -> Stage:
    return Stage(id="gate1", gate="human", present=present)


async def test_gate_approved_on_yes():
    node = HumanGateNode(stage=_gate_stage())
    with patch("builtins.input", return_value="yes"):
        result = await node.execute({})
    assert result["approved"] is True
    assert result["feedback"] is None


async def test_gate_approved_on_y():
    node = HumanGateNode(stage=_gate_stage())
    with patch("builtins.input", return_value="y"):
        result = await node.execute({})
    assert result["approved"] is True


async def test_gate_approved_on_approve():
    node = HumanGateNode(stage=_gate_stage())
    with patch("builtins.input", return_value="approve"):
        result = await node.execute({})
    assert result["approved"] is True


async def test_gate_rejected_on_no():
    node = HumanGateNode(stage=_gate_stage())
    with patch("builtins.input", side_effect=["no", ""]):
        result = await node.execute({})
    assert result["approved"] is False
    assert "feedback" in result


async def test_gate_rejected_with_feedback():
    node = HumanGateNode(stage=_gate_stage())
    with patch("builtins.input", side_effect=["no", "needs more work"]):
        result = await node.execute({})
    assert result["approved"] is False
    assert result["feedback"] == "needs more work"


async def test_gate_rejected_empty_feedback_falls_back_to_response():
    """When feedback input is blank, the original non-yes response is used."""
    node = HumanGateNode(stage=_gate_stage())
    with patch("builtins.input", side_effect=["reject", ""]):
        result = await node.execute({})
    assert result["approved"] is False
    assert result["feedback"] == "reject"


async def test_gate_default_message_when_present_not_set():
    """Stage without present uses the default 'Review required.' message."""
    stage = Stage(id="g", gate="human", present=None)
    node = HumanGateNode(stage=stage)
    with patch("builtins.input", return_value="yes"):
        result = await node.execute({})
    assert result["approved"] is True


async def test_gate_present_renders_jinja2_template():
    """present field is Jinja2 rendered against context."""
    stage = Stage(id="g", gate="human", present="Review for {{ env }}: approve?")
    node = HumanGateNode(stage=stage)
    with patch("builtins.input", return_value="yes"), \
         patch("builtins.print") as mock_print:
        result = await node.execute({"env": "production"})
    printed = " ".join(str(c) for call in mock_print.call_args_list for c in call[0])
    assert "production" in printed
    assert result["approved"] is True

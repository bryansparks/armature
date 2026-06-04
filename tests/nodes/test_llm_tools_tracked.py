"""Tests that LLMNode tracks tools_declared and tools_called for SLR metric.

arXiv:2605.30621v1: Skill-Load Rate requires knowing which tools were declared
vs actually invoked during each trajectory.
"""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from armature.nodes.llm import LLMNode
from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig, OutputMode
from armature.registry.registry import ToolRegistry, ToolDescriptor
from armature.permissions.permissions import PermissionLevel


def _make_tiers():
    return ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))


def _make_stage_with_tools(tool_names: list[str]):
    role = Role(
        name="worker",
        type=RoleType.WORKER,
        description="Do the task.",
        tools=tool_names,
    )
    return Stage(id="s1", role=role, depends_on=[], output_mode=OutputMode.TEXT)


def _make_registry(*tool_names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in tool_names:
        registry.register(ToolDescriptor(
            name=name,
            description=f"{name} tool",
            permission=PermissionLevel.READ_ONLY,
            handler=lambda args: {"ok": True},
            parameters={"q": {"type": "string"}},
        ))
    return registry


def _fake_text_response(content: str = "done"):
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    mock_resp.usage = None
    return mock_resp


async def test_tools_declared_returned_when_role_has_tools():
    """_tools_declared in result equals the tool names listed in role.tools."""
    registry = _make_registry("search")
    stage = _make_stage_with_tools(["search"])
    node = LLMNode(stage=stage, tiers=_make_tiers(), registry=registry)

    async def fake_completion(**kwargs):
        return _fake_text_response("result")

    with patch("armature.nodes.llm.litellm_completion", side_effect=fake_completion):
        result = await node.execute({})

    assert result.get("_tools_declared") == ["search"]


async def test_tools_called_empty_when_no_tool_calls_made():
    """_tools_called is empty when the LLM does not invoke any tools."""
    registry = _make_registry("search")
    stage = _make_stage_with_tools(["search"])
    node = LLMNode(stage=stage, tiers=_make_tiers(), registry=registry)

    async def fake_completion(**kwargs):
        return _fake_text_response("no tools used")

    with patch("armature.nodes.llm.litellm_completion", side_effect=fake_completion):
        result = await node.execute({})

    assert result.get("_tools_called") == []


async def test_tools_called_populated_when_llm_invokes_tool():
    """_tools_called contains the tool name when the LLM makes a tool call."""
    async def my_search(args):
        return {"results": ["hit"]}

    registry = ToolRegistry()
    registry.register(ToolDescriptor(
        name="search",
        description="search tool",
        permission=PermissionLevel.READ_ONLY,
        handler=my_search,
        parameters={"q": {"type": "string"}},
    ))

    stage = _make_stage_with_tools(["search"])
    node = LLMNode(stage=stage, tiers=_make_tiers(), registry=registry)

    def _tool_call_response():
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "search"
        tc.function.arguments = json.dumps({"q": "test"})
        msg1 = MagicMock()
        msg1.content = ""
        msg1.tool_calls = [tc]
        resp1 = MagicMock()
        resp1.choices = [MagicMock()]
        resp1.choices[0].message = msg1
        resp1.usage = None
        return resp1

    def _final_response():
        msg2 = MagicMock()
        msg2.content = "found it"
        msg2.tool_calls = None
        resp2 = MagicMock()
        resp2.choices = [MagicMock()]
        resp2.choices[0].message = msg2
        resp2.usage = None
        return resp2

    responses = [_tool_call_response(), _final_response()]
    call_idx = [0]

    async def fake_completion(**kwargs):
        resp = responses[call_idx[0]]
        call_idx[0] += 1
        return resp

    with patch("armature.nodes.llm.litellm_completion", side_effect=fake_completion):
        result = await node.execute({})

    assert "search" in result.get("_tools_called", [])

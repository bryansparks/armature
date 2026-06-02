"""Tests for LLMNode mission_context injection into the system prompt."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from armature.nodes.llm import LLMNode
from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig


def _make_stage():
    role = Role(name="worker", type=RoleType.WORKER, description="Do the task.")
    return Stage(id="s1", role=role, depends_on=[])


def _make_tiers():
    return ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))


async def test_mission_context_appears_first_in_system_prompt():
    """When mission_context is set, it is the first section of the system prompt."""
    stage = _make_stage()
    tiers = _make_tiers()
    mission = "[Workflow Mission]\nDeliver a Q3 report."

    captured_system = {}

    async def fake_completion(**kwargs):
        msgs = kwargs.get("messages", [])
        captured_system["content"] = msgs[0]["content"] if msgs else ""
        mock_choice = MagicMock()
        mock_choice.message.content = '{"result": "ok"}'
        mock_choice.message.tool_calls = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        return mock_resp

    node = LLMNode(
        stage=stage,
        tiers=tiers,
        mission_context=mission,
    )

    with patch("litellm.acompletion", side_effect=fake_completion):
        await node.execute({})

    system = captured_system.get("content", "")
    assert system.startswith(mission), (
        f"Expected system prompt to start with mission block.\nGot: {system[:200]}"
    )


async def test_empty_mission_context_leaves_prompt_unchanged():
    """When mission_context is empty, the system prompt is identical to the no-mission case."""
    stage = _make_stage()
    tiers = _make_tiers()

    captured = {}

    async def fake_completion(**kwargs):
        msgs = kwargs.get("messages", [])
        captured["content"] = msgs[0]["content"] if msgs else ""
        mock_choice = MagicMock()
        mock_choice.message.content = '{"result": "ok"}'
        mock_choice.message.tool_calls = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        return mock_resp

    node_with = LLMNode(stage=stage, tiers=tiers, mission_context="")
    node_without = LLMNode(stage=stage, tiers=tiers)

    with patch("litellm.acompletion", side_effect=fake_completion):
        await node_with.execute({})
    prompt_with = captured["content"]

    with patch("litellm.acompletion", side_effect=fake_completion):
        await node_without.execute({})
    prompt_without = captured["content"]

    assert prompt_with == prompt_without

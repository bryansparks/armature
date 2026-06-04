"""Tests for LLMNode token streaming via on_token callback."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from armature.nodes.llm import LLMNode
from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig, OutputMode


def _make_tiers():
    return ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))


def _make_stage(output_mode=OutputMode.TEXT):
    role = Role(name="worker", type=RoleType.WORKER, description="Answer.")
    return Stage(id="s1", role=role, depends_on=[], output_mode=output_mode, response_stage=True)


def _make_chunk(content: str):
    chunk = MagicMock()
    chunk.choices[0].delta.content = content
    chunk.usage = None
    return chunk


async def _async_chunks(contents: list[str]):
    for c in contents:
        yield _make_chunk(c)


async def test_on_token_called_for_each_chunk_when_text_mode():
    """When on_token is set and output_mode is TEXT, each chunk content is passed to callback."""
    stage = _make_stage(OutputMode.TEXT)
    tiers = _make_tiers()
    received: list[str] = []

    async def on_token(chunk: str):
        received.append(chunk)

    node = LLMNode(stage=stage, tiers=tiers, on_token=on_token)

    # acompletion with stream=True returns a coroutine that resolves to an async generator
    async def fake_streaming_completion(**kwargs):
        assert kwargs.get("stream") is True
        return _async_chunks(["Hello", ", world", "!"])

    with patch("armature.nodes.llm.litellm_completion", side_effect=fake_streaming_completion):
        result = await node.execute({})

    assert received == ["Hello", ", world", "!"]
    assert result["content"] == "Hello, world!"


async def test_streaming_skipped_when_json_mode():
    """When output_mode is JSON, the non-streaming path runs even if on_token is set."""
    stage = _make_stage(OutputMode.JSON)
    tiers = _make_tiers()
    received: list[str] = []

    async def on_token(chunk: str):
        received.append(chunk)

    node = LLMNode(stage=stage, tiers=tiers, on_token=on_token)

    async def fake_completion(**kwargs):
        assert kwargs.get("stream") is None or not kwargs.get("stream")
        mock_choice = MagicMock()
        mock_choice.message.content = '{"answer": "42"}'
        mock_choice.message.tool_calls = None
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = None
        return mock_resp

    with patch("armature.nodes.llm.litellm_completion", side_effect=fake_completion):
        result = await node.execute({})

    assert received == [], "on_token should not be called in JSON mode"
    assert result.get("answer") == "42"

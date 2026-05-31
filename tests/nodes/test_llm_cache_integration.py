"""Integration tests for LLMNode + LLMCache — verify cache hit/miss behavior."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

from armature.nodes.llm import LLMNode
from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig, OutputMode


def make_stage() -> Stage:
    return Stage(
        id="test",
        role=Role(name="r", type=RoleType.WORKER, description="test", model_tier="small"),
        output_mode=OutputMode.JSON,
    )


def make_tiers() -> ModelTiers:
    return ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))


def make_litellm_response(content: str):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None
    response.usage = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 5
    return response


async def test_cache_hit_skips_litellm():
    """When the cache returns a response, litellm should not be called."""
    cache = MagicMock()
    cache._make_key = MagicMock(return_value="hit_key")
    cache.get = AsyncMock(return_value='{"answer": "cached"}')
    cache.put = AsyncMock()

    node = LLMNode(stage=make_stage(), tiers=make_tiers(), cache=cache)

    with patch("armature.nodes.llm.litellm_completion") as mock_litellm:
        result = await node.execute({"question": "what?"})

    mock_litellm.assert_not_called()
    assert result == {"answer": "cached"}


async def test_cache_miss_calls_litellm_and_stores():
    """On a cache miss, litellm should be called and the result should be stored."""
    cache = MagicMock()
    cache._make_key = MagicMock(return_value="miss_key")
    cache.get = AsyncMock(return_value=None)
    cache.put = AsyncMock()

    node = LLMNode(stage=make_stage(), tiers=make_tiers(), cache=cache)

    with patch("armature.nodes.llm.litellm_completion", return_value=make_litellm_response('{"score": 1}')):
        result = await node.execute({"question": "what?"})

    cache.put.assert_called_once()
    stored_key, stored_json = cache.put.call_args[0]
    assert stored_key == "miss_key"
    stored = json.loads(stored_json)
    assert stored.get("score") == 1

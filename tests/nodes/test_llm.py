import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from armature.nodes.llm import LLMNode
from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig, OutputMode

def make_stage(role_type: RoleType = RoleType.WORKER) -> Stage:
    return Stage(
        id="test",
        role=Role(name="r", type=role_type, description="test role", model_tier="small"),
    )

def make_tiers() -> ModelTiers:
    return ModelTiers(
        small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"),
        frontier=ModelTierConfig(provider="anthropic", model="claude-opus-4-7"),
    )

async def test_worker_routes_to_small_model():
    stage = make_stage(RoleType.WORKER)
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)
    model_str = node._resolve_model()
    assert "qwen" in model_str or "ollama" in model_str.lower()

async def test_judge_routes_to_frontier_model():
    stage = make_stage(RoleType.JUDGE)
    stage.role.model_tier = "frontier"
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)
    model_str = node._resolve_model()
    assert "claude" in model_str or "anthropic" in model_str.lower()

def test_llm_node_requires_role():
    stage = Stage(id="no-role", role=None)
    with pytest.raises(ValueError, match="role"):
        LLMNode(stage=stage, tiers=ModelTiers())


def make_litellm_response(content: str, input_tokens: int = 10, output_tokens: int = 5):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.usage = MagicMock()
    response.usage.prompt_tokens = input_tokens
    response.usage.completion_tokens = output_tokens
    return response


async def test_guided_json_passes_response_format():
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    stage.output_schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)

    captured_kwargs = {}

    async def mock_completion(**kwargs):
        captured_kwargs.update(kwargs)
        return make_litellm_response('{"score": 0.9}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert "response_format" in captured_kwargs
    assert result["score"] == pytest.approx(0.9)


async def test_tier_escalation_on_parse_failure():
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    tiers = ModelTiers(
        small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"),
        medium=ModelTierConfig(provider="ollama", model="qwen2.5:14b"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_litellm_response("not valid json")
        return make_litellm_response('{"ok": true}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert call_count == 2  # first call failed, escalated to medium
    assert result.get("ok") is True
    assert "_parse_error" not in result


async def test_no_escalation_if_no_higher_tier():
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    # Only small tier configured — no escalation target
    tiers = ModelTiers(small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"))
    node = LLMNode(stage=stage, tiers=tiers)

    async def mock_completion(**kwargs):
        return make_litellm_response("not valid json")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert result.get("_parse_error") is True  # gracefully returns parse error

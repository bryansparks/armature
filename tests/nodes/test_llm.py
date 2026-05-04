import pytest
from unittest.mock import AsyncMock, patch
from armature.nodes.llm import LLMNode
from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig

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

"""Tests for adapter-related spec models."""
import pytest
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType, ToolCallConfig, ModelTiers, ModelTierConfig,
    SkillDef, SkillAdapterRef, AdapterFactoryConfig,
)


def _small_tiers() -> ModelTiers:
    return ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))


def test_skilldef_requires_source_or_adapter():
    with pytest.raises(ValueError):
        SkillDef(id="s", description="d")


def test_skilldef_with_content_passes():
    s = SkillDef(id="s", description="d", content="do x")
    assert s.content == "do x"


def test_skilldef_with_adapter_only_passes():
    s = SkillDef(id="s", description="d", adapter=SkillAdapterRef(name="a"))
    assert s.adapter is not None
    assert s.adapter.name == "a"


def test_skilldef_with_adapter_and_content_passes():
    s = SkillDef(id="s", description="d", content="do x", adapter=SkillAdapterRef(name="a"))
    assert s.content is not None
    assert s.adapter is not None


def test_model_tier_default_adapter_support_is_none():
    cfg = ModelTierConfig(provider="openai", model="gpt-4o-mini")
    assert cfg.adapter_support == "none"


def test_model_tier_adapter_support_values():
    cfg = ModelTierConfig(provider="vllm", model="qwen/qwen2.5-7b", adapter_support="dynamic")
    assert cfg.adapter_support == "dynamic"


def test_adapter_factory_defaults():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        adapter_factory=AdapterFactoryConfig(),
    )
    assert spec.adapter_factory.backend == "mock"
    assert spec.adapter_factory.rank == 16


def test_adapter_factory_loads_from_yaml_fields():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        adapter_factory=AdapterFactoryConfig(
            backend="modal",
            base_model="qwen/qwen2.5-7b",
            skills={
                "tdd_workflow": {"backend": "local", "rank": 8},
            },
        ),
    )
    override = spec.adapter_factory.skills["tdd_workflow"]
    assert override.backend == "local"
    assert override.rank == 8


def test_role_can_use_adapter_skill():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(
            id="s",
            role=Role(
                name="r",
                type=RoleType.WORKER,
                description="d",
                skills=["tdd_workflow"],
            ),
            depends_on=[],
        )],
        model_tiers=_small_tiers(),
        skill_library={
            "tdd_workflow": SkillDef(
                id="tdd_workflow",
                description="TDD",
                adapter=SkillAdapterRef(name="tdd-workflow"),
            ),
        },
    )
    assert spec.skill_library["tdd_workflow"].adapter is not None

"""Runtime adapter resolution tests for LLMNode."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry
from armature.cache.llm_cache import LLMCache
from armature.nodes.llm import LLMNode
from armature.spec.models import (
    ModelTierConfig,
    ModelTiers,
    Role,
    RoleType,
    SkillAdapterRef,
    SkillDef,
    Stage,
)


def make_stage(role_type: RoleType = RoleType.WORKER) -> Stage:
    return Stage(
        id="test",
        role=Role(name="r", type=role_type, description="test role", model_tier="small"),
    )


def make_tiers(adapter_support: str = "none") -> ModelTiers:
    return ModelTiers(
        small=ModelTierConfig(
            provider="vllm",
            model="qwen/qwen2.5-7b",
            adapter_support=adapter_support,  # type: ignore[arg-type]
        ),
        frontier=ModelTierConfig(provider="anthropic", model="claude-opus-4-7"),
    )


def make_litellm_response(content: str, input_tokens: int = 10, output_tokens: int = 5):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None
    response.usage = MagicMock()
    response.usage.prompt_tokens = input_tokens
    response.usage.completion_tokens = output_tokens
    return response


def _register_adapter(tmp_path, name="tdd-workflow", version="3") -> AdapterRegistry:
    reg = AdapterRegistry(base_dir=tmp_path / "adapters")
    artifact_dir = tmp_path / f"src-{name}-{version}"
    artifact_dir.mkdir()
    (artifact_dir / "adapter_config.json").write_text("{}")
    meta = AdapterMetadata(
        name=name,
        version=version,
        base_model="qwen/qwen2.5-7b",
        rank=16,
        alpha=32,
    )
    reg.register(meta, artifact_dir)
    return reg


def test_resolve_active_adapters_when_registry_has_adapter(tmp_path):
    reg = _register_adapter(tmp_path)
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write tests first.",
        adapter=SkillAdapterRef(name="tdd-workflow", version="3"),
    )
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
        adapter_registry=reg,
    )
    active = node._resolve_active_adapters(node._tiers.small)
    assert "tdd" in active
    assert active["tdd"].metadata.name == "tdd-workflow"
    assert active["tdd"].metadata.version == "3"


def test_resolve_active_adapters_empty_when_tier_support_none(tmp_path):
    reg = _register_adapter(tmp_path)
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write tests first.",
        adapter=SkillAdapterRef(name="tdd-workflow", version="3"),
    )
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="none"),
        skill_library={"tdd": skill},
        adapter_registry=reg,
    )
    active = node._resolve_active_adapters(node._tiers.small)
    assert active == {}


def test_resolve_active_adapters_empty_when_registry_missing(tmp_path):
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write tests first.",
        adapter=SkillAdapterRef(name="missing-adapter"),
    )
    empty_reg = AdapterRegistry(base_dir=tmp_path / "empty_adapters")
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
        adapter_registry=empty_reg,
    )
    active = node._resolve_active_adapters(node._tiers.small)
    assert active == {}


def test_adapter_kwargs_for_vllm_dynamic(tmp_path):
    reg = _register_adapter(tmp_path)
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write tests first.",
        adapter=SkillAdapterRef(name="tdd-workflow", version="3"),
    )
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
        adapter_registry=reg,
    )
    active = node._resolve_active_adapters(node._tiers.small)
    kwargs = node._adapter_kwargs(node._tiers.small, active)
    assert "extra_body" in kwargs
    assert kwargs["extra_body"]["lora_request"]["name"] == "tdd-workflow"
    assert kwargs["extra_body"]["lora_request"]["path"].endswith("tdd-workflow/3")


def test_adapter_kwargs_empty_for_none_tier(tmp_path):
    reg = _register_adapter(tmp_path)
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write tests first.",
        adapter=SkillAdapterRef(name="tdd-workflow", version="3"),
    )
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="none"),
        skill_library={"tdd": skill},
        adapter_registry=reg,
    )
    kwargs = node._adapter_kwargs(node._tiers.small, {})
    assert kwargs == {}


def test_apply_fallback_fail_raises_on_missing_adapter():
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write tests first.",
        adapter=SkillAdapterRef(name="missing", fallback="fail"),
    )
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
    )
    with pytest.raises(RuntimeError, match="could not be resolved"):
        node._apply_adapter_fallback({}, node._tiers.small)


def test_apply_fallback_none_omits_skill():
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write tests first.",
        adapter=SkillAdapterRef(name="missing", fallback="none"),
    )
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
    )
    omitted = node._apply_adapter_fallback({}, node._tiers.small)
    assert omitted == {"tdd"}


def test_apply_fallback_text_keeps_skill():
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write tests first.",
        adapter=SkillAdapterRef(name="missing", fallback="text"),
    )
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
    )
    omitted = node._apply_adapter_fallback({}, node._tiers.small)
    assert omitted == set()


async def test_execute_omits_skill_text_when_adapter_active(tmp_path):
    reg = _register_adapter(tmp_path)
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write a failing test first.",
        adapter=SkillAdapterRef(name="tdd-workflow", version="3"),
    )
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
        adapter_registry=reg,
    )

    captured = {}

    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_litellm_response("ok")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert result["content"] == "ok"
    assert "extra_body" in captured
    assert captured["extra_body"]["lora_request"]["name"] == "tdd-workflow"
    system = captured["messages"][0]["content"]
    assert "Write a failing test first" not in system
    assert "## Active Adapters" in system
    assert "tdd-workflow@3" in system


async def test_execute_falls_back_to_text_when_adapter_missing(tmp_path):
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write a failing test first.",
        adapter=SkillAdapterRef(name="missing-adapter"),
    )
    empty_reg = AdapterRegistry(base_dir=tmp_path / "empty_adapters")
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
        adapter_registry=empty_reg,
    )

    captured = {}

    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_litellm_response("ok")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert result["content"] == "ok"
    assert "extra_body" not in captured
    system = captured["messages"][0]["content"]
    assert "Write a failing test first" in system
    assert "## Active Adapters" not in system


async def test_execute_raises_when_fallback_fail_and_adapter_missing(tmp_path):
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write a failing test first.",
        adapter=SkillAdapterRef(name="missing-adapter", fallback="fail"),
    )
    empty_reg = AdapterRegistry(base_dir=tmp_path / "empty_adapters")
    node = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
        adapter_registry=empty_reg,
    )

    with pytest.raises(RuntimeError, match="could not be resolved"):
        await node.execute({})


async def test_cache_key_differs_with_active_adapter(tmp_path):
    """The LLM cache key must include active adapter identifiers."""
    db_path = tmp_path / "cache.sqlite"
    cache = LLMCache(db_path)
    await cache.init()

    reg = _register_adapter(tmp_path, name="tdd-workflow", version="3")
    stage = make_stage()
    stage.role.skills = ["tdd"]
    skill = SkillDef(
        id="tdd",
        description="TDD workflow",
        content="Write a failing test first.",
        adapter=SkillAdapterRef(name="tdd-workflow", version="3"),
    )

    # First call with adapter active.
    node_with = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
        adapter_registry=reg,
        cache=cache,
    )

    async def mock_completion(**kwargs):
        return make_litellm_response("with adapter")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result_with = await node_with.execute({})
    assert result_with["content"] == "with adapter"

    # Second call with the same messages but no adapter registry.
    node_without = LLMNode(
        stage=stage,
        tiers=make_tiers(adapter_support="dynamic"),
        skill_library={"tdd": skill},
        cache=cache,
    )

    async def mock_completion2(**kwargs):
        return make_litellm_response("without adapter")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion2):
        result_without = await node_without.execute({})

    # Without a registry the skill text is injected, but the cache key must still
    # be different from the adapter-backed call so results are not shared.
    assert result_without["content"] == "without adapter"

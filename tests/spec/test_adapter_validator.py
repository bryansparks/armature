"""Tests for adapter-related spec validation."""
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType, ToolCallConfig, ModelTiers, ModelTierConfig,
    SkillDef, SkillAdapterRef, AdapterFactoryConfig,
)
from armature.spec.validator import validate_spec, SpecError


def codes(errors: list[SpecError]) -> set[str]:
    return {e.code for e in errors}


def _small_tiers() -> ModelTiers:
    return ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))


def test_unknown_adapter_backend_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        adapter_factory=AdapterFactoryConfig(backend="unknown"),
    )
    errors = validate_spec(spec, strict=False)
    assert "UNKNOWN_ADAPTER_BACKEND" in codes(errors)


def test_known_adapter_backend_passes():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        adapter_factory=AdapterFactoryConfig(backend="modal"),
    )
    errors = validate_spec(spec, strict=False)
    assert "UNKNOWN_ADAPTER_BACKEND" not in codes(errors)


def test_adapter_factory_no_base_model_warning():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        adapter_factory=AdapterFactoryConfig(backend="modal"),
    )
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert "ADAPTER_FACTORY_NO_BASE_MODEL" in warning_codes


def test_adapter_base_model_mismatch_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        skill_library={
            "tdd": SkillDef(
                id="tdd",
                description="TDD",
                adapter=SkillAdapterRef(name="tdd"),
            ),
        },
        adapter_factory=AdapterFactoryConfig(backend="modal", base_model="different/model"),
    )
    errors = validate_spec(spec, strict=False)
    assert "ADAPTER_BASE_MODEL_MISMATCH" in codes(errors)


def test_adapter_base_model_match_passes():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        skill_library={
            "tdd": SkillDef(
                id="tdd",
                description="TDD",
                adapter=SkillAdapterRef(name="tdd"),
            ),
        },
        adapter_factory=AdapterFactoryConfig(backend="modal", base_model="gpt-4o-mini"),
    )
    errors = validate_spec(spec, strict=False)
    assert "ADAPTER_BASE_MODEL_MISMATCH" not in codes(errors)


def test_adapter_no_fallback_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        skill_library={
            "tdd": SkillDef(
                id="tdd",
                description="TDD",
                adapter=SkillAdapterRef(name="tdd", fallback="text"),
            ),
        },
    )
    errors = validate_spec(spec, strict=False)
    assert "ADAPTER_NO_FALLBACK" in codes(errors)


def test_adapter_with_text_fallback_passes():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        skill_library={
            "tdd": SkillDef(
                id="tdd",
                description="TDD",
                content="do tdd",
                adapter=SkillAdapterRef(name="tdd", fallback="text"),
            ),
        },
    )
    errors = validate_spec(spec, strict=False)
    assert "ADAPTER_NO_FALLBACK" not in codes(errors)

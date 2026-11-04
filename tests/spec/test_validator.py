"""Tests for HarnessSpec static validation."""
import pytest
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType, Adapter, ModelTiers, ModelTierConfig,
    ToolCallConfig, LoopConfig, OnFailConfig, Contract, ToolSafetyRule, SafetyCondition,
    OutputMode, RoleTypeDefaults,
)
from armature.spec.validator import validate_spec, SpecError, SpecValidationError


# ── Helpers ──────────────────────────────────────────────────────────────────

def _small_tiers() -> ModelTiers:
    return ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))


def _valid_stage(sid="s") -> Stage:
    return Stage(id=sid, tool_call=ToolCallConfig(name="t"), depends_on=[])


def _valid_spec(**overrides) -> HarnessSpec:
    defaults = dict(
        name="wf",
        stages=[_valid_stage()],
        model_tiers=_small_tiers(),
    )
    defaults.update(overrides)
    return HarnessSpec(**defaults)


def codes(errors: list[SpecError]) -> set[str]:
    return {e.code for e in errors}


# ── Valid spec passes ─────────────────────────────────────────────────────────

def test_valid_spec_returns_no_errors():
    errors = validate_spec(_valid_spec(), strict=False)
    assert errors == []


def test_valid_spec_strict_does_not_raise():
    validate_spec(_valid_spec())  # no exception


# ── Duplicate stage IDs ───────────────────────────────────────────────────────

def test_duplicate_stage_id_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[_valid_stage("s"), _valid_stage("s")],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "DUPLICATE_STAGE_ID" in codes(errors)


def test_unique_stage_ids_no_error():
    spec = _valid_spec(stages=[_valid_stage("a"), _valid_stage("b")])
    assert validate_spec(spec, strict=False) == []


# ── Undefined depends_on references ──────────────────────────────────────────

def test_undefined_dependency_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=["nonexistent"])],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_DEPENDENCY" in codes(errors)
    assert errors[0].stage_id == "s"


def test_valid_dependency_no_error():
    spec = _valid_spec(stages=[
        _valid_stage("a"),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"]),
    ])
    assert validate_spec(spec, strict=False) == []


# ── Cycle detection ───────────────────────────────────────────────────────────

def test_cycle_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="a", tool_call=ToolCallConfig(name="t"), depends_on=["b"]),
            Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"]),
        ],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "CIRCULAR_DEPENDENCY" in codes(errors)


def test_linear_chain_no_cycle():
    spec = _valid_spec(stages=[
        _valid_stage("a"),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"]),
        Stage(id="c", tool_call=ToolCallConfig(name="t"), depends_on=["b"]),
    ])
    assert validate_spec(spec, strict=False) == []


# ── Undefined adapter references ──────────────────────────────────────────────

def test_undefined_adapter_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", adapter="missing_adapter", depends_on=[])],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_ADAPTER" in codes(errors)


def test_defined_adapter_no_error():
    adapter = Adapter(name="run_sh", type="script", cmd="echo hi")
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", adapter="run_sh", depends_on=[])],
        adapters={"run_sh": adapter},
        model_tiers=_small_tiers(),
    )
    assert validate_spec(spec, strict=False) == []


# ── No execution type ─────────────────────────────────────────────────────────

def test_stage_with_no_execution_type_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", depends_on=[])],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "NO_EXECUTION_TYPE" in codes(errors)


def test_stage_with_role_passes():
    spec = _valid_spec(stages=[Stage(
        id="s",
        role=Role(name="r", type=RoleType.WORKER, description="d"),
        depends_on=[],
    )])
    assert validate_spec(spec, strict=False) == []


def test_stage_with_gate_passes():
    spec = _valid_spec(stages=[Stage(id="s", gate="human", depends_on=[])])
    assert validate_spec(spec, strict=False) == []


# ── fan_out / partition_source consistency ────────────────────────────────────

def test_fan_out_without_partition_source_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"),
                      fan_out=4, depends_on=[])],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "FAN_OUT_MISSING_PARTITION_SOURCE" in codes(errors)


def test_partition_source_without_fan_out_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"),
                      partition_source="{{ items }}", depends_on=[])],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "PARTITION_SOURCE_MISSING_FAN_OUT" in codes(errors)


def test_fan_out_with_partition_source_passes():
    spec = _valid_spec(stages=[Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        fan_out=4, partition_source="{{ items }}", depends_on=[],
    )])
    assert validate_spec(spec, strict=False) == []


def test_fan_out_zero_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"),
                      fan_out=0, partition_source="{{ items }}", depends_on=[])],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "INVALID_FAN_OUT" in codes(errors)


def test_fan_out_negative_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"),
                      fan_out=-3, partition_source="{{ items }}", depends_on=[])],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "INVALID_FAN_OUT" in codes(errors)


def test_fan_out_one_passes():
    spec = _valid_spec(stages=[Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        fan_out=1, partition_source="{{ items }}", depends_on=[],
    )])
    assert validate_spec(spec, strict=False) == []


def test_inject_file_as_without_partition_source_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"),
                      inject_file_as="body", depends_on=[])],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "INJECT_FILE_MISSING_PARTITION_SOURCE" in codes(errors)


def test_inject_file_as_with_partition_source_passes():
    spec = _valid_spec(stages=[Stage(
        id="s", tool_call=ToolCallConfig(name="t"),
        fan_out=2, partition_source="{{ files }}",
        inject_file_as="body", depends_on=[],
    )])
    assert validate_spec(spec, strict=False) == []


# ── on_fail.loop stage reference ──────────────────────────────────────────────

def test_on_fail_loop_undefined_stage_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(
            id="s",
            tool_call=ToolCallConfig(name="t"),
            on_fail=OnFailConfig(loop=LoopConfig(stage="nonexistent")),
            depends_on=[],
        )],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_LOOP_STAGE" in codes(errors)


def test_on_fail_loop_valid_stage_passes():
    spec = _valid_spec(stages=[Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s")),
        depends_on=[],
    )])
    assert validate_spec(spec, strict=False) == []


# ── Model tier references ─────────────────────────────────────────────────────

def test_undefined_model_tier_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(
            id="s",
            role=Role(name="r", type=RoleType.WORKER, description="d", model_tier="xlarge"),
            depends_on=[],
        )],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_MODEL_TIER" in codes(errors)


def test_defined_model_tier_passes():
    spec = _valid_spec(stages=[Stage(
        id="s",
        role=Role(name="r", type=RoleType.WORKER, description="d", model_tier="small"),
        depends_on=[],
    )])
    assert validate_spec(spec, strict=False) == []


def test_null_model_tier_not_checked():
    # role.model_tier=None means "use default" — no error when default tier is configured
    spec = _valid_spec(stages=[Stage(
        id="s",
        role=Role(name="r", type=RoleType.WORKER, description="d"),
        depends_on=[],
    )])
    assert validate_spec(spec, strict=False) == []


# ── DEFAULT_TIER_NOT_CONFIGURED ───────────────────────────────────────────────

def test_default_tier_not_configured_detected():
    """Worker role with no explicit model_tier uses role_type_defaults.worker='small',
    but small is not configured in model_tiers (frontier IS configured) →
    DEFAULT_TIER_NOT_CONFIGURED. The check only fires when at least one tier is defined."""
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(
            id="s",
            role=Role(name="r", type=RoleType.WORKER, description="d"),
            depends_on=[],
        )],
        model_tiers=ModelTiers(
            frontier=ModelTierConfig(provider="anthropic", model="claude-opus-4-7")
        ),  # frontier configured but not small
    )
    errors = validate_spec(spec, strict=False)
    assert "DEFAULT_TIER_NOT_CONFIGURED" in codes(errors)
    stage_errors = [e for e in errors if e.code == "DEFAULT_TIER_NOT_CONFIGURED"]
    assert stage_errors[0].stage_id == "s"


def test_default_tier_configured_passes():
    """Worker role with no explicit model_tier — default 'small' is configured."""
    spec = _valid_spec(stages=[Stage(
        id="s",
        role=Role(name="r", type=RoleType.WORKER, description="d"),
        depends_on=[],
    )])
    errors = validate_spec(spec, strict=False)
    assert "DEFAULT_TIER_NOT_CONFIGURED" not in codes(errors)


def test_explicit_tier_triggers_undefined_not_default_check():
    """Stage with explicit model_tier='xlarge' triggers UNDEFINED_MODEL_TIER,
    not DEFAULT_TIER_NOT_CONFIGURED."""
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(
            id="s",
            role=Role(name="r", type=RoleType.WORKER, description="d", model_tier="xlarge"),
            depends_on=[],
        )],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_MODEL_TIER" in codes(errors)
    assert "DEFAULT_TIER_NOT_CONFIGURED" not in codes(errors)


# ── requires: capabilities ───────────────────────────────────────────────────

def test_requires_accepts_capabilities():
    errors = validate_spec(_valid_spec(requires=["llm", "git", "claude-code", "platform:ios"]), strict=False)
    assert "INVALID_REQUIREMENT" not in codes(errors)


def test_requires_rejects_a_name_that_is_not_a_capability():
    errors = validate_spec(_valid_spec(requires=["git", "Platform iOS"]), strict=False)
    assert [e.code for e in errors if e.code == "INVALID_REQUIREMENT"] == ["INVALID_REQUIREMENT"]


# ── Contract.inputs name field ────────────────────────────────────────────────

def test_contract_input_missing_name_detected():
    spec = _valid_spec()
    spec = HarnessSpec(
        name="wf",
        stages=[_valid_stage()],
        contracts=Contract(inputs=[{"required": True}]),  # no "name" key
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "CONTRACT_INPUT_MISSING_NAME" in codes(errors)


def test_contract_input_with_name_passes():
    spec = HarnessSpec(
        name="wf",
        stages=[_valid_stage()],
        contracts=Contract(inputs=[{"name": "repo_path", "required": True}]),
        model_tiers=_small_tiers(),
    )
    assert validate_spec(spec, strict=False) == []


# ── strict mode ───────────────────────────────────────────────────────────────

def test_strict_mode_raises_on_error():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", depends_on=[])],  # no execution type
        model_tiers=_small_tiers(),
    )
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(spec, strict=True)
    assert "NO_EXECUTION_TYPE" in str(exc_info.value)


def test_strict_mode_error_contains_all_errors():
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="s", depends_on=["missing"]),  # undefined dep + no exec type
        ],
        model_tiers=_small_tiers(),
    )
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(spec, strict=True)
    assert len(exc_info.value.errors) >= 2


def test_strict_false_returns_errors_without_raising():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", depends_on=[])],
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert len(errors) > 0


def test_strict_mode_does_not_raise_on_warnings():
    """Warnings (severity='warning') must not raise in strict mode — only errors do."""
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        MemoryConfig, Contract,
    )
    spec = HarnessSpec(
        name="wf", version="1.0",
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        contracts=Contract(inputs=[{"name": "topic"}]),
        memory=MemoryConfig(navigation_tools=True, extract_knowledge=False),
        stages=[Stage(id="worker", role=Role(
            name="Worker", type=RoleType.WORKER, description="do {{ topic }}",
            model_tier="small", tools=["memory.search_records"]))],
    )
    # strict=True must NOT raise: the only finding is a warning
    # (MEMORY_NAV_TOOLS_REQUIRES_EXTRACT), not an error.
    errors = validate_spec(spec, strict=True)
    codes = [e.code for e in errors]
    assert "MEMORY_NAV_TOOLS_REQUIRES_EXTRACT" in codes
    assert all(e.severity == "warning" for e in errors)


# ── Contract.outputs validation ───────────────────────────────────────────────

def test_contract_output_missing_stage_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[_valid_stage()],
        contracts=Contract(outputs=[{"key": "result", "required": True}]),  # no "stage"
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "CONTRACT_OUTPUT_MISSING_STAGE" in codes(errors)


def test_contract_output_undefined_stage_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[_valid_stage()],
        contracts=Contract(outputs=[{"stage": "nonexistent", "key": "x", "required": True}]),
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "CONTRACT_OUTPUT_UNDEFINED_STAGE" in codes(errors)


def test_contract_output_missing_key_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[_valid_stage()],
        contracts=Contract(outputs=[{"stage": "s", "required": True}]),  # no "key"
        model_tiers=_small_tiers(),
    )
    errors = validate_spec(spec, strict=False)
    assert "CONTRACT_OUTPUT_MISSING_KEY" in codes(errors)


def test_contract_output_valid_passes():
    spec = HarnessSpec(
        name="wf",
        stages=[_valid_stage()],
        contracts=Contract(outputs=[{"stage": "s", "key": "result", "required": True}]),
        model_tiers=_small_tiers(),
    )
    assert validate_spec(spec, strict=False) == []


# ── Harness constructor integration ──────────────────────────────────────────

def test_harness_raises_on_invalid_spec(tmp_path):
    from armature.runtime.engine import Harness
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", depends_on=["nonexistent"])],
        model_tiers=_small_tiers(),
    )
    with pytest.raises(SpecValidationError):
        Harness(spec=spec, session_dir=tmp_path)


def test_harness_validate_false_skips_validation(tmp_path):
    from armature.runtime.engine import Harness
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", depends_on=[])],  # no execution type
        model_tiers=_small_tiers(),
    )
    # validate=False suppresses validation; harness is created without error
    harness = Harness(spec=spec, session_dir=tmp_path, validate=False)
    assert harness.name == "wf"


# ── Only-tighten safety rule composition (KYA-inspired) ──────────────────────

def _safety_rule(tool: str, action: str, value: str = "rm") -> ToolSafetyRule:
    return ToolSafetyRule(
        tool=tool,
        condition=SafetyCondition(field="cmd", op="contains", value=value),
        action=action,
        message="test rule",
    )


def test_conflicting_safety_rules_allow_loosens_block_same_tool():
    """CONFLICTING_SAFETY_RULES fires when allow and block rules target the same tool."""
    spec = _valid_spec(safety_rules=[
        _safety_rule("bash", "block"),
        _safety_rule("bash", "allow"),
    ])
    errors = validate_spec(spec, strict=False)
    assert "CONFLICTING_SAFETY_RULES" in codes(errors)


def test_conflicting_safety_rules_allow_loosens_wildcard_block():
    """allow for a specific tool conflicts with a wildcard block rule."""
    spec = _valid_spec(safety_rules=[
        _safety_rule("*", "block"),
        _safety_rule("bash", "allow"),
    ])
    errors = validate_spec(spec, strict=False)
    assert "CONFLICTING_SAFETY_RULES" in codes(errors)


def test_no_conflict_when_only_block_rules():
    """Multiple block rules for the same tool are fine — only tightening."""
    spec = _valid_spec(safety_rules=[
        _safety_rule("bash", "block", value="rm"),
        _safety_rule("bash", "block", value="curl"),
    ])
    errors = validate_spec(spec, strict=False)
    assert "CONFLICTING_SAFETY_RULES" not in codes(errors)


def test_no_conflict_when_allow_and_block_target_different_tools():
    """allow for tool A, block for tool B — no conflict."""
    spec = _valid_spec(safety_rules=[
        _safety_rule("bash", "block"),
        _safety_rule("python", "allow"),
    ])
    errors = validate_spec(spec, strict=False)
    assert "CONFLICTING_SAFETY_RULES" not in codes(errors)


# ── guided_json low-tier risk warning ─────────────────────────────────────────

def _guided_json_stage(sid="s", model_tier: str | None = None) -> Stage:
    return Stage(
        id=sid,
        role=Role(name="r", type=RoleType.WORKER, description="d", model_tier=model_tier),
        output_mode=OutputMode.GUIDED_JSON,
        output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
        depends_on=[],
    )


def test_guided_json_small_tier_emits_warning():
    """GUIDED_JSON_LOW_TIER_RISK warning fires for explicit small tier + guided_json."""
    tiers = ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))
    spec = HarnessSpec(name="wf", stages=[_guided_json_stage(model_tier="small")], model_tiers=tiers)
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert "GUIDED_JSON_LOW_TIER_RISK" in warning_codes


def test_guided_json_tiny_tier_emits_warning():
    """GUIDED_JSON_LOW_TIER_RISK warning fires for explicit tiny tier + guided_json."""
    tiers = ModelTiers(
        tiny=ModelTierConfig(provider="openai", model="gpt-4o-mini"),
        small=ModelTierConfig(provider="openai", model="gpt-4o-mini"),
    )
    spec = HarnessSpec(name="wf", stages=[_guided_json_stage(model_tier="tiny")], model_tiers=tiers)
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert "GUIDED_JSON_LOW_TIER_RISK" in warning_codes


def test_guided_json_medium_tier_no_warning():
    """No warning for medium (or higher) tier with guided_json."""
    tiers = ModelTiers(medium=ModelTierConfig(provider="openai", model="gpt-4o"))
    spec = HarnessSpec(name="wf", stages=[_guided_json_stage(model_tier="medium")], model_tiers=tiers)
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert "GUIDED_JSON_LOW_TIER_RISK" not in warning_codes


def test_guided_json_small_via_role_type_default_emits_warning():
    """Warning fires when effective tier resolves through role_type_defaults to small."""
    tiers = ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))
    defaults = RoleTypeDefaults(worker="small")
    # No explicit model_tier on stage; role type is worker → defaults to small
    stage = _guided_json_stage(model_tier=None)
    spec = HarnessSpec(name="wf", stages=[stage], model_tiers=tiers, role_type_defaults=defaults)
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert "GUIDED_JSON_LOW_TIER_RISK" in warning_codes


def test_guided_json_no_model_tiers_no_warning():
    """No warning when no model tiers are defined (e.g. unit-test specs)."""
    spec = HarnessSpec(name="wf", stages=[_guided_json_stage(model_tier=None)], model_tiers=ModelTiers())
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert "GUIDED_JSON_LOW_TIER_RISK" not in warning_codes


# ── reasoning_effort with a non-default temperature ──────────────────────────

def _effort_spec(tier_temp=None, tier_effort=None, role_temp=None, role_effort=None) -> HarnessSpec:
    tiers = ModelTiers(small=ModelTierConfig(
        provider="anthropic", model="claude-haiku-4-5-20251001",
        temperature=tier_temp, reasoning_effort=tier_effort,
    ))
    role = Role(name="r", type=RoleType.WORKER, description="d", model_tier="small",
                temperature=role_temp, reasoning_effort=role_effort)
    return HarnessSpec(name="wf", stages=[Stage(id="s", role=role, depends_on=[])], model_tiers=tiers)


@pytest.mark.parametrize("kwargs, warns", [
    (dict(tier_temp=0.2, role_effort="high"), True),
    (dict(role_temp=0.5, tier_effort="low"), True),
    (dict(tier_temp=0.2, tier_effort="high", role_temp=1.0), False),
    (dict(tier_effort="high"), False),
    (dict(tier_temp=0.2), False),
])
def test_reasoning_effort_with_temperature_warning(kwargs, warns):
    errors = validate_spec(_effort_spec(**kwargs), strict=False)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert ("REASONING_EFFORT_WITH_TEMPERATURE" in warning_codes) is warns


# ── Memory pyramid (Phase 2): validator warnings + harness-injected _memory_index ──

def test_validator_memory_index_is_harness_injected():
    from armature.spec.validator import _harness_injected_keys_set
    # _memory_index must be in the harness-injected set so stages can reference it.
    assert "_memory_index" in _harness_injected_keys_set()


def test_nav_tools_without_extract_warns():
    from armature.spec.validator import validate_spec
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        MemoryConfig, Contract,
    )
    spec = HarnessSpec(
        name="w", version="1.0",
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        contracts=Contract(inputs=[{"name": "topic"}]),
        memory=MemoryConfig(navigation_tools=True, extract_knowledge=False),
        stages=[Stage(id="worker", role=Role(
            name="Worker", type=RoleType.WORKER, description="do {{ topic }}",
            model_tier="small", tools=["memory.search_records"]))],
    )
    errors = validate_spec(spec, strict=False)
    codes = [e.code for e in errors]
    assert "MEMORY_NAV_TOOLS_REQUIRES_EXTRACT" in codes
    nav_err = [e for e in errors if e.code == "MEMORY_NAV_TOOLS_REQUIRES_EXTRACT"][0]
    assert nav_err.severity == "warning"


def test_reconcile_llm_without_extract_warns():
    from armature.spec.validator import validate_spec
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        MemoryConfig, Contract,
    )
    spec = HarnessSpec(
        name="w", version="1.0",
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        contracts=Contract(inputs=[{"name": "topic"}]),
        memory=MemoryConfig(reconcile_llm=True, extract_knowledge=False),
        stages=[Stage(id="worker", role=Role(
            name="Worker", type=RoleType.WORKER, description="do {{ topic }}",
            model_tier="small"))],
    )
    errors = validate_spec(spec, strict=False)
    codes = [e.code for e in errors]
    assert "MEMORY_RECONCILE_LLM_WITHOUT_EXTRACT" in codes
    assert [e for e in errors if e.code == "MEMORY_RECONCILE_LLM_WITHOUT_EXTRACT"][0].severity == "warning"


def test_nav_tools_with_extract_no_warning():
    from armature.spec.validator import validate_spec
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        MemoryConfig, Contract,
    )
    spec = HarnessSpec(
        name="w", version="1.0",
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        contracts=Contract(inputs=[{"name": "topic"}]),
        memory=MemoryConfig(navigation_tools=True, extract_knowledge=True),
        stages=[Stage(id="worker", role=Role(
            name="Worker", type=RoleType.WORKER, description="do {{ topic }}",
            model_tier="small", tools=["memory.search_records"]))],
    )
    errors = validate_spec(spec, strict=False)
    codes = [e.code for e in errors]
    assert "MEMORY_NAV_TOOLS_REQUIRES_EXTRACT" not in codes
    assert "MEMORY_RECONCILE_LLM_WITHOUT_EXTRACT" not in codes


# ── Memory navigation: nav-requires-enabled ───────

def _spec_with_memory(**memory_kw):
    from armature.spec.models import MemoryConfig
    spec = _valid_spec()
    spec.memory = MemoryConfig(enabled=True, **memory_kw)
    return spec


def test_nav_tools_requires_enabled_warning():
    from armature.spec.models import MemoryConfig
    spec = _valid_spec()
    spec.memory = MemoryConfig(enabled=False, navigation_tools=True)
    errors = validate_spec(spec, strict=False)
    assert "MEMORY_NAV_TOOLS_REQUIRES_ENABLED" in codes(errors)


def test_memory_index_in_harness_keys_when_navigation_tools():
    from armature.spec.models import MemoryConfig, Signature
    spec = _valid_spec()
    spec.memory = MemoryConfig(enabled=True, navigation_tools=True,
                                extract_knowledge=True)
    st = _valid_stage("nav_worker")
    st.depends_on = ["s"]
    st.role = Role(name="NavWorker", type=RoleType.WORKER, description="d",
                   tools=["memory.search_records"])
    st.signature = Signature(input={"_memory_index": "navigation index"})
    spec.stages.append(st)
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_SIGNATURE_INPUT" not in codes(errors)


# ── resident: grammar + validation ────────────────────────────────────────────

def _resident_spec(**overrides) -> HarnessSpec:
    """A minimal valid spec plus a resident block; overrides land in resident."""
    block = {
        "instance_key": "{{ event.payload.vendor_id }}",
        "on": {"request": {"workflow": "self"}},
    }
    block.update(overrides)
    return HarnessSpec.model_validate({
        "name": "wf", "stages": [{"id": "s", "tool_call": {"name": "t"}}],
        "resident": block,
    })


def _resident_errors(spec: HarnessSpec) -> list[SpecError]:
    return [e for e in validate_spec(spec, strict=False) if e.code.startswith("RESIDENT_")]


def test_parse_duration_accepts_documented_forms():
    from datetime import timedelta
    from armature.spec.models import parse_duration
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("12h") == timedelta(hours=12)
    assert parse_duration("30d") == timedelta(days=30)
    assert parse_duration("1d12h") == timedelta(days=1, hours=12)
    assert parse_duration("1d12h30m") == timedelta(days=1, hours=12, minutes=30)


def test_parse_duration_rejects_garbage():
    from armature.spec.models import parse_duration
    for bad in ["30x", "", "m", "1.5d", "12 h", "d"]:
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_resident_full_block_parses():
    """The design doc's vendor-intake example, with state.schema inline (not a
    filename) and the flat backoff fields the brief defines."""
    from armature.spec.models import ResidentCfg
    block = {
        "instance_key": "{{ event.payload.vendor_id }}",
        "concurrency": 1,
        "batch": 10,
        "state": {
            "schema": {"type": "object",
                       "properties": {"queue": {"type": "array"}},
                       "required": ["queue"]},
            "max_bytes": 16384,
            "publish": ["metrics"],
        },
        "on": {
            "request": {"workflow": "self"},
            "ask": {"workflow": "vendor-intake-answer"},
            "schedule": {"cron": "0 6 * * *", "timezone": "America/Denver",
                          "workflow": "vendor-intake-rollup"},
            "signal": {"workflow": "self"},
        },
        "idle": {
            "after": "30m",
            "workflow": "vendor-intake-housekeeping",
            "budget_usd_per_day": 2.0,
            "max_turns_per_day": 24,
            "backoff_factor": 2.0,
            "backoff_max": "12h",
        },
        "lifetime": {"close_after_idle": "30d", "max_turns": 5000},
        "close_on": {"signal": "pr_merged"},
        "budget": {"usd_per_day": 25.0},
    }
    spec = HarnessSpec.model_validate({
        "name": "vendor-intake",
        "stages": [{"id": "s", "tool_call": {"name": "t"}}],
        "resident": block,
    })
    assert isinstance(spec.resident, ResidentCfg)
    from datetime import timedelta
    assert spec.resident.idle.after == timedelta(minutes=30)
    assert spec.resident.idle.backoff_max == timedelta(hours=12)
    assert spec.resident.lifetime.close_after_idle == timedelta(days=30)
    assert not _resident_errors(spec)


def test_resident_defaults():
    spec = _resident_spec()
    r = spec.resident
    assert r.concurrency == 1
    assert r.batch == 10
    assert r.state.max_bytes == 16384
    assert r.state.publish == []
    assert r.state.schema is None
    assert r.on["request"].workflow == "self"
    assert r.on["request"].cron is None
    assert r.on["request"].timezone == "UTC"
    assert r.idle is None
    assert r.lifetime.close_after_idle is None
    assert r.lifetime.max_turns is None
    assert r.close_on is None
    assert r.budget.usd_per_day is None


def test_resident_absent_is_none():
    assert _valid_spec().resident is None
    assert not _resident_errors(_valid_spec())


def test_resident_concurrency_above_one_rejected():
    errors = _resident_errors(_resident_spec(concurrency=2))
    assert codes(errors) == {"RESIDENT_CONCURRENCY"}


def test_resident_idle_is_not_an_on_key():
    errors = _resident_errors(_resident_spec(on={"idle": {"workflow": "self"}}))
    assert "RESIDENT_ON_KEY" in codes(errors)


def test_resident_unknown_on_key_rejected():
    errors = _resident_errors(_resident_spec(on={"webhook": {"workflow": "self"}}))
    assert "RESIDENT_ON_KEY" in codes(errors)


def test_resident_custom_on_keys_accepted():
    spec = _resident_spec(on={"custom.vendor_ping": {"workflow": "self"}})
    assert not _resident_errors(spec)


def test_resident_bad_custom_on_key_rejected():
    errors = _resident_errors(_resident_spec(on={"custom.": {"workflow": "self"}}))
    assert "RESIDENT_ON_KEY" in codes(errors)


def test_resident_bad_cron_rejected():
    for cron in ["0 6 * *", "not a cron", "0 6 * * * *"]:
        errors = _resident_errors(
            _resident_spec(on={"schedule": {"cron": cron, "workflow": "self"}}))
        assert "RESIDENT_SCHEDULE" in codes(errors), cron


def test_resident_schedule_requires_cron():
    errors = _resident_errors(_resident_spec(on={"schedule": {"workflow": "self"}}))
    assert "RESIDENT_SCHEDULE" in codes(errors)


def test_resident_bad_timezone_rejected():
    errors = _resident_errors(_resident_spec(
        on={"schedule": {"cron": "0 6 * * *", "timezone": "Mars/Olympus",
                         "workflow": "self"}}))
    assert "RESIDENT_TIMEZONE" in codes(errors)


def test_resident_valid_cron_timezone_pass():
    spec = _resident_spec(on={"schedule": {"cron": "0 6 * * 1-5",
                                           "timezone": "America/Denver",
                                           "workflow": "self"}})
    assert not _resident_errors(spec)


def test_resident_bad_instance_key_rejected():
    for key in ["{{ x }}", "{{ 'a' ~ 'b' }}", "vendor_id",
                "{{ event.payload.vendor_id }} extra"]:
        errors = _resident_errors(_resident_spec(instance_key=key))
        assert "RESIDENT_INSTANCE_KEY" in codes(errors), key


def test_resident_indexed_instance_key_accepted():
    spec = _resident_spec(instance_key="{{ event.payload['vendor id'] }}")
    assert not _resident_errors(spec)
    spec = _resident_spec(instance_key="{{event.payload.rows[0].id}}")
    assert not _resident_errors(spec)


def test_resident_close_on_signal_outside_the_name_grammar_rejected():
    # close_on can only ever match a signal the routes and the emit tool deliver, and those
    # never leave the bare-name grammar — a close_on outside it is a silently dead fleet
    # closure (I2).
    for bad in ["Pr Merged", "custom.pr_merged", "PR", "", "no spaces", "b" * 65]:
        errors = _resident_errors(_resident_spec(close_on={"signal": bad}))
        assert codes(errors) == {"RESIDENT_CLOSE_ON"}, bad


def test_resident_close_on_valid_signal_accepted():
    spec = _resident_spec(close_on={"signal": "pr_merged"})
    assert not _resident_errors(spec)


def test_resident_workflow_name_validated():
    errors = _resident_errors(_resident_spec(on={"request": {"workflow": "bad name!"}}))
    assert "RESIDENT_WORKFLOW" in codes(errors)
    errors = _resident_errors(_resident_spec(
        idle={"after": "30m", "workflow": "nope!!", "backoff_max": "12h"}))
    assert "RESIDENT_WORKFLOW" in codes(errors)


def test_resident_named_handler_workflow_accepted():
    # engine-side does NOT resolve named handlers; the name grammar is enough
    spec = _resident_spec(on={"request": {"workflow": "vendor-intake-answer"}})
    assert not _resident_errors(spec)


def test_resident_batch_bounds():
    for bad in [0, 101]:
        errors = _resident_errors(_resident_spec(batch=bad))
        assert "RESIDENT_BATCH" in codes(errors), bad
    for good in [1, 100]:
        assert not _resident_errors(_resident_spec(batch=good))


def test_resident_max_bytes_bounds():
    for bad in [1023, 65537]:
        errors = _resident_errors(_resident_spec(state={"max_bytes": bad}))
        assert "RESIDENT_STATE_MAX_BYTES" in codes(errors), bad
    for good in [1024, 65536]:
        assert not _resident_errors(_resident_spec(state={"max_bytes": good}))


def test_resident_unknown_field_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        HarnessSpec.model_validate({
            "name": "wf", "stages": [{"id": "s", "tool_call": {"name": "t"}}],
            "resident": {"retries": 3},
        })
    with pytest.raises(ValidationError):
        HarnessSpec.model_validate({
            "name": "wf", "stages": [{"id": "s", "tool_call": {"name": "t"}}],
            "resident": {"idle": {"after": "30m", "factor": 2}},
        })


def test_resident_bad_duration_string_rejected_at_load():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _resident_spec(idle={"after": "30x", "backoff_max": "12h"})


def test_resident_strict_mode_raises():
    with pytest.raises(SpecValidationError):
        validate_spec(_resident_spec(concurrency=2), strict=True)

"""Tests for HarnessSpec static validation."""
import pytest
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType, Adapter, ModelTiers, ModelTierConfig,
    ToolCallConfig, LoopConfig, OnFailConfig, Contract,
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
    # role.model_tier=None means "use default" — should not trigger validation
    spec = _valid_spec(stages=[Stage(
        id="s",
        role=Role(name="r", type=RoleType.WORKER, description="d"),
        depends_on=[],
    )])
    assert validate_spec(spec, strict=False) == []


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

"""closure: / mission_source: grammar + validation (design §3, §4)."""
import pytest
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType, OutputMode, ModelTiers, ModelTierConfig,
    ClosureConfig,
)
from armature.spec.validator import validate_spec, SpecError


def codes(errors: list[SpecError]) -> set[str]:
    return {e.code for e in errors}


def _small_tiers() -> ModelTiers:
    return ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))


def _closure_stage(sid="closer", schema=None) -> Stage:
    return Stage(
        id=sid,
        role=Role(name="r", type=RoleType.WORKER, description="d"),
        output_mode=OutputMode.GUIDED_JSON,
        output_schema=schema if schema is not None else {
            "type": "object",
            "required": ["reason"],
            "properties": {
                "reason": {"type": "string", "enum": [
                    "done_no_follow_on", "handed_off", "blocked_on", "escalation"]},
                "notes": {"type": "string"},
                "follow_on": {"type": "array", "items": {"type": "object"}},
            },
        },
        depends_on=[],
    )


def _spec(stages, closure=None, **overrides) -> HarnessSpec:
    defaults = dict(name="wf", stages=stages, model_tiers=_small_tiers(),
                    closure=closure)
    defaults.update(overrides)
    return HarnessSpec(**defaults)


def test_valid_closure_passes():
    spec = _spec([_closure_stage()], closure=ClosureConfig(stage="closer"))
    errors = validate_spec(spec, strict=False)
    assert not any(c.startswith("CLOSURE_") for c in codes(errors))


def test_mission_source_default_is_static():
    spec = _spec([_closure_stage()])
    assert spec.mission_source == "static"


def test_mission_source_work_unit_accepted():
    spec = _spec([_closure_stage()], mission_source="work_unit")
    assert spec.mission_source == "work_unit"


def test_mission_source_invalid_rejected_at_load():
    with pytest.raises(ValueError):
        _spec([_closure_stage()], mission_source="bogus")


def test_closure_stage_undefined():
    spec = _spec([_closure_stage()], closure=ClosureConfig(stage="nope"))
    errors = validate_spec(spec, strict=False)
    assert "CLOSURE_STAGE_UNDEFINED" in codes(errors)


def test_closure_stage_must_be_guided_json():
    plain = Stage(id="texty",
                  role=Role(name="r", type=RoleType.WORKER, description="d"),
                  output_mode=OutputMode.TEXT, depends_on=[])
    spec = _spec([plain], closure=ClosureConfig(stage="texty"))
    errors = validate_spec(spec, strict=False)
    assert "CLOSURE_STAGE_NOT_GUIDED_JSON" in codes(errors)


def test_closure_stage_missing_output_schema():
    no_schema = Stage(id="bare",
                      role=Role(name="r", type=RoleType.WORKER, description="d"),
                      output_mode=OutputMode.GUIDED_JSON, output_schema=None,
                      depends_on=[])
    spec = _spec([no_schema], closure=ClosureConfig(stage="bare"))
    errors = validate_spec(spec, strict=False)
    assert "CLOSURE_STAGE_NOT_GUIDED_JSON" in codes(errors)


def test_closure_schema_reason_not_required():
    schema = {"type": "object", "properties": {
        "reason": {"type": "string", "enum": ["done_no_follow_on", "handed_off",
                                              "blocked_on", "escalation"]}}}
    spec = _spec([_closure_stage(schema=schema)], closure=ClosureConfig(stage="closer"))
    errors = validate_spec(spec, strict=False)
    assert "CLOSURE_SCHEMA_INVALID" in codes(errors)


def test_closure_schema_bogus_enum_value():
    schema = {"type": "object", "required": ["reason"], "properties": {
        "reason": {"type": "string", "enum": ["done", "maybe"]}}}
    spec = _spec([_closure_stage(schema=schema)], closure=ClosureConfig(stage="closer"))
    errors = validate_spec(spec, strict=False)
    assert "CLOSURE_SCHEMA_INVALID" in codes(errors)


def test_closure_schema_enum_subset_is_valid():
    # restricting to a subset of CLOSURE_REASONS is legitimate (a workflow that
    # can never hand off) — the enum must be non-empty and ⊆ CLOSURE_REASONS
    schema = {"type": "object", "required": ["reason"], "properties": {
        "reason": {"type": "string", "enum": ["done_no_follow_on"]}}}
    spec = _spec([_closure_stage(schema=schema)], closure=ClosureConfig(stage="closer"))
    errors = validate_spec(spec, strict=False)
    assert "CLOSURE_SCHEMA_INVALID" not in codes(errors)


def test_existing_specs_gain_no_new_errors():
    """Backward-compat regression: no closure: → zero CLOSURE_* codes."""
    spec = _spec([_closure_stage()])
    errors = validate_spec(spec, strict=False)
    assert not any(c.startswith("CLOSURE_") for c in codes(errors))
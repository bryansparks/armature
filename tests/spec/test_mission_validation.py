"""Mission validation: lifecycle-referential + budget rules (design §2.2)."""
from pathlib import Path
import pytest

from armature.spec.mission import (
    MissionSpec, WorkUnit, load_mission, validate_mission, resolve_posture,
)
from armature.spec.validator import SpecError, SpecValidationError


def codes(errors: list[SpecError]) -> set[str]:
    return {e.code for e in errors}


def _unit(uid: str, **overrides) -> WorkUnit:
    defaults = dict(id=uid, title=f"unit {uid}", workflow="some-registered-workflow")
    defaults.update(overrides)
    return WorkUnit(**defaults)


def _mission(work: list[WorkUnit], **overrides) -> MissionSpec:
    defaults = dict(name="m", objective="do the thing", work=work)
    defaults.update(overrides)
    return MissionSpec(**defaults)


def test_valid_mission_has_no_errors():
    errors = validate_mission(_mission([
        _unit("a"), _unit("b", requires=["a"]),
    ]), strict=False)
    # bare-name workflows always warn (WORKFLOW_UNVERIFIED_NAME); valid = no errors
    assert {e.code for e in errors if e.severity != "warning"} == set()


def test_duplicate_work_unit():
    errors = validate_mission(_mission([_unit("a"), _unit("a")]), strict=False)
    assert "DUPLICATE_WORK_UNIT" in codes(errors)


def test_unknown_requires_reference():
    errors = validate_mission(_mission([_unit("a", requires=["nope"])]), strict=False)
    assert "UNKNOWN_WORK_UNIT" in codes(errors)


def test_requires_is_order_independent():
    # a requires b, which is declared later in the file — valid
    errors = validate_mission(_mission([
        _unit("a", requires=["b"]), _unit("b"),
    ]), strict=False)
    assert "UNKNOWN_WORK_UNIT" not in codes(errors)
    assert "CIRCULAR_DEPENDENCY" not in codes(errors)


def test_circular_dependency_detected():
    errors = validate_mission(_mission([
        _unit("a", requires=["b"]), _unit("b", requires=["a"]),
    ]), strict=False)
    assert "CIRCULAR_DEPENDENCY" in codes(errors)


def test_self_reference_is_a_cycle():
    errors = validate_mission(_mission([_unit("a", requires=["a"])]), strict=False)
    assert "CIRCULAR_DEPENDENCY" in codes(errors)


def test_budget_conflict():
    errors = validate_mission(
        _mission([_unit("a", max_budget_usd=5.0)], budget_usd=2.0), strict=False)
    assert "MISSION_BUDGET_CONFLICT" in codes(errors)


def test_budget_ok_when_ceiling_within_mission():
    errors = validate_mission(
        _mission([_unit("a", max_budget_usd=2.0)], budget_usd=5.0), strict=False)
    assert "MISSION_BUDGET_CONFLICT" not in codes(errors)


def test_unresolvable_workflow_path_is_error(tmp_path):
    mission = _mission([_unit("a", workflow="../missing.yml")])
    # simulate the loader stamp: path-like workflow must carry a stamped workflow_path
    mission.work[0].workflow_path = str(tmp_path / "../missing.yml")
    errors = validate_mission(mission, strict=False)
    assert "WORKFLOW_NOT_REGISTERED" in codes(errors)


def test_bare_name_is_warning_not_error():
    errors = validate_mission(_mission([_unit("a", workflow="armature-code-worker")]), strict=False)
    assert "WORKFLOW_NOT_REGISTERED" not in codes(errors)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert "WORKFLOW_UNVERIFIED_NAME" in warning_codes


def test_empty_work_list_is_warning():
    errors = validate_mission(_mission([]), strict=False)
    assert "MISSION_NO_WORK_UNITS" in codes(errors)
    assert all(e.severity == "warning" for e in errors)


def test_strict_raises_on_any_error():
    with pytest.raises(SpecValidationError):
        validate_mission(_mission([_unit("a", requires=["nope"])]))


def test_resolve_posture_unit_overrides_mission():
    mission = _mission([_unit("a", posture="delegated")], posture="human-led")
    assert resolve_posture(mission, mission.work[0]) == "delegated"


def test_resolve_posture_inherits_mission_default():
    mission = _mission([_unit("a")], posture="delegated")
    assert resolve_posture(mission, mission.work[0]) == "delegated"


def test_resolve_posture_defaults_human_led():
    mission = _mission([_unit("a")])
    assert resolve_posture(mission, mission.work[0]) == "human-led"

# ── Slice 2: numeric/field sanity hardening ──────────────────────────────────

def test_zero_max_attempts_invalid():
    errors = validate_mission(_mission([_unit("a", max_attempts=0)]), strict=False)
    assert "MISSION_FIELD_INVALID" in codes(errors)


def test_negative_budget_invalid():
    errors = validate_mission(
        _mission([_unit("a", max_budget_usd=-1.0)], budget_usd=-5.0), strict=False)
    assert "MISSION_FIELD_INVALID" in codes(errors)
    assert sum(1 for e in errors if e.code == "MISSION_FIELD_INVALID") == 2


def test_negative_timeout_invalid():
    errors = validate_mission(_mission([_unit("a", timeout_hours=-0.5)]), strict=False)
    assert "MISSION_FIELD_INVALID" in codes(errors)


def test_blank_unit_id_invalid():
    errors = validate_mission(_mission([_unit("  ")]), strict=False)
    assert "MISSION_FIELD_INVALID" in codes(errors)


def test_numeric_bounds_valid_mission_clean():
    errors = validate_mission(
        _mission([_unit("a", max_attempts=3, timeout_hours=1.0, max_budget_usd=2.0)],
                 budget_usd=10.0), strict=False)
    assert "MISSION_FIELD_INVALID" not in codes(errors)


def test_mission_errors_carry_unit_stage_id():
    errors = validate_mission(_mission([_unit("a", requires=["nope"])]), strict=False)
    assert all(e.stage_id == "a" for e in errors if e.code == "UNKNOWN_WORK_UNIT")

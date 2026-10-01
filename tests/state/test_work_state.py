"""Work-unit state machine: states, legality table, records (design §2.2)."""
import pytest
from armature.state.work import (
    WorkUnitState, TRANSITIONS, TransitionError, ensure_legal,
    WorkUnitRecord, TransitionRecord,
)


def test_states_match_design():
    assert {s.value for s in WorkUnitState} == {
        "pending", "in_progress", "done", "failed", "retry_pending",
        "blocked_on", "handed_off", "escalation", "canceled",
    }


def test_lifecycle_happy_path_legal():
    ensure_legal(WorkUnitState.PENDING, WorkUnitState.IN_PROGRESS)
    ensure_legal(WorkUnitState.IN_PROGRESS, WorkUnitState.DONE)


def test_retry_path_legal():
    ensure_legal(WorkUnitState.IN_PROGRESS, WorkUnitState.FAILED)
    ensure_legal(WorkUnitState.FAILED, WorkUnitState.RETRY_PENDING)
    ensure_legal(WorkUnitState.RETRY_PENDING, WorkUnitState.IN_PROGRESS)


def test_escalation_and_resolution_legal():
    ensure_legal(WorkUnitState.IN_PROGRESS, WorkUnitState.ESCALATION)
    ensure_legal(WorkUnitState.ESCALATION, WorkUnitState.PENDING)
    ensure_legal(WorkUnitState.ESCALATION, WorkUnitState.IN_PROGRESS)


def test_terminal_states_admit_nothing():
    for terminal in (WorkUnitState.DONE, WorkUnitState.HANDED_OFF, WorkUnitState.CANCELED):
        assert TRANSITIONS[terminal] == frozenset()


def test_illegal_transition_raises():
    with pytest.raises(TransitionError, match="pending.*done"):
        ensure_legal(WorkUnitState.PENDING, WorkUnitState.DONE)
    with pytest.raises(TransitionError, match="done"):
        ensure_legal(WorkUnitState.DONE, WorkUnitState.IN_PROGRESS)


def test_record_defaults():
    rec = WorkUnitRecord(mission="m", unit_id="a", title="A", workflow="w.yml")
    assert rec.state == WorkUnitState.PENDING
    assert rec.attempts == 0
    assert rec.max_attempts == 2
    assert rec.spent_usd == 0.0
    assert rec.inputs == {}
    assert rec.last_job_id is None


def test_record_extra_fields_rejected():
    with pytest.raises(Exception):
        WorkUnitRecord(mission="m", unit_id="a", title="A", workflow="w.yml", bogus=1)


def test_transition_record_from_state_optional():
    tr = TransitionRecord(seq=1, ts="2026-10-01T00:00:00Z", mission="m",
                         unit_id="a", from_state=None, to_state=WorkUnitState.PENDING)
    assert tr.actor == "system"


def test_records_json_round_trip():
    rec = WorkUnitRecord(mission="m", unit_id="a", title="A", workflow="w.yml")
    assert WorkUnitRecord.model_validate_json(rec.model_dump_json()) == rec
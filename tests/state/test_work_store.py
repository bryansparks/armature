"""LocalWorkStore: persistence, legality enforcement, audit trail (design §5)."""
import json
import pytest

from armature.spec.mission import MissionSpec, WorkUnit
from armature.state.work import (
    LocalWorkStore, WorkUnitRecord, WorkUnitState, TransitionError,
)


def _mission(**overrides) -> MissionSpec:
    defaults = dict(name="m", objective="ship it", work=[
        WorkUnit(id="a", title="A", workflow="../wf.yml"),
        WorkUnit(id="b", title="B", workflow="../wf.yml", requires=["a"], max_attempts=3),
    ])
    defaults.update(overrides)
    return MissionSpec(**defaults)


def test_ensure_unit_seeds_pending_record(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = _mission()
    rec = store.ensure_unit(mission, mission.work[1])
    assert rec.state == WorkUnitState.PENDING
    assert rec.max_attempts == 3
    assert rec.posture == "human-led"          # resolved from mission default
    assert rec.requires == ["a"]


def test_ensure_unit_does_not_clobber_live_state(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = _mission()
    store.ensure_unit(mission, mission.work[0])
    store.apply("m", "a", WorkUnitState.IN_PROGRESS, reason="start", job_id="j1")
    again = store.ensure_unit(mission, mission.work[0])
    assert again.state == WorkUnitState.IN_PROGRESS


def test_apply_enforces_legality(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = _mission()
    store.ensure_unit(mission, mission.work[0])
    with pytest.raises(TransitionError):
        store.apply("m", "a", WorkUnitState.DONE, reason="skip the work")


def test_apply_on_missing_unit_refuses(tmp_path):
    store = LocalWorkStore(tmp_path)
    with pytest.raises(KeyError, match="no-such-unit"):
        store.apply("m", "no-such-unit", WorkUnitState.IN_PROGRESS)


def test_apply_records_transition_audit(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = _mission()
    store.ensure_unit(mission, mission.work[0])
    store.apply("m", "a", WorkUnitState.IN_PROGRESS, reason="attempt 1/2", job_id="j1")
    store.apply("m", "a", WorkUnitState.FAILED, reason="boom", job_id="j1")
    transitions = store.list_transitions("m")
    assert [(t.from_state, t.to_state) for t in transitions] == [
        (None, WorkUnitState.PENDING),                      # ensure_unit seeds
        (WorkUnitState.PENDING, WorkUnitState.IN_PROGRESS),
        (WorkUnitState.IN_PROGRESS, WorkUnitState.FAILED),
    ]
    assert transitions[-2].job_id == "j1"
    assert transitions[-2].reason == "attempt 1/2"
    assert [t.seq for t in transitions] == [1, 2, 3]


def test_ensure_unit_writes_seeding_transition(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = _mission()
    store.ensure_unit(mission, mission.work[0])
    (transitions,) = store.list_transitions("m")
    assert transitions.from_state is None
    assert transitions.to_state == WorkUnitState.PENDING


def test_list_units_round_trips(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = _mission()
    for u in mission.work:
        store.ensure_unit(mission, u)
    units = store.list_units("m")
    assert {u.unit_id for u in units} == {"a", "b"}


def test_records_survive_store_reopen(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = _mission()
    store.ensure_unit(mission, mission.work[0])
    store.apply("m", "a", WorkUnitState.IN_PROGRESS, reason="start", job_id="j1")
    reopened = LocalWorkStore(tmp_path)
    rec = reopened.load("m", "a")
    assert rec.state == WorkUnitState.IN_PROGRESS
    assert rec.last_job_id == "j1"
    assert len(reopened.list_transitions("m")) == 2


def test_transitions_jsonl_is_append_only_json_lines(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = _mission()
    store.ensure_unit(mission, mission.work[0])
    store.apply("m", "a", WorkUnitState.IN_PROGRESS, reason="start")
    lines = (tmp_path / "m" / "transitions.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["mission"] == "m"
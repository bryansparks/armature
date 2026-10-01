"""apply_closure: one run's judgment becomes more work, by records (design §3)."""
import pytest

from armature.spec.mission import MissionSpec, WorkUnit
from armature.state.closure import ClosureRecord, FollowOnUnit, apply_closure
from armature.state.work import LocalWorkStore, WorkUnitState, TransitionError


def _store_with_unit(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = MissionSpec(name="m", objective="ship it", work=[
        WorkUnit(id="a", title="A", workflow="../wf.yml")])
    store.ensure_unit(mission, mission.work[0])
    store.apply("m", "a", WorkUnitState.IN_PROGRESS, reason="start", job_id="j1")
    return store, mission


def test_done_no_follow_on_moves_done(tmp_path):
    store, _ = _store_with_unit(tmp_path)
    rec = apply_closure(store, "m", "a",
                        ClosureRecord(reason="done_no_follow_on", notes="clean"))
    assert rec.state == WorkUnitState.DONE


def test_handed_off_seeds_pending_follow_ons(tmp_path):
    store, _ = _store_with_unit(tmp_path)
    rec = apply_closure(store, "m", "a", ClosureRecord(
        reason="handed_off", follow_on=[
            FollowOnUnit(id="polish", title="Polish", workflow="../wf.yml"),
            FollowOnUnit(id="verify", title="Verify", workflow="../wf.yml",
                         objective="Check the polish"),
        ]))
    assert rec.state == WorkUnitState.HANDED_OFF
    polish = store.load("m", "polish")
    assert polish.state == WorkUnitState.PENDING
    assert polish.title == "Polish"
    assert polish.workflow == "../wf.yml"
    assert store.load("m", "verify").objective == "Check the polish"


def test_apply_closure_handed_off_idempotent_reseed(tmp_path):
    """Review Focus #2 + at-least-once (design §6): a closure redelivered with
    the same job_id is a no-op — no double-seeded transitions, no clobbered
    live state. A DIFFERENT job against the terminal unit still raises
    (test_illegal_apply_closure_raises)."""
    store, _ = _store_with_unit(tmp_path)
    closure = ClosureRecord(reason="handed_off",
                            follow_on=[FollowOnUnit(id="polish", title="P", workflow="w")])
    apply_closure(store, "m", "a", closure, job_id="run-9")
    store.apply("m", "polish", WorkUnitState.IN_PROGRESS, reason="someone started")
    before = store.load("m", "polish")
    # a redelivery of the same job's closure record
    apply_closure(store, "m", "a", closure, job_id="run-9")
    after = store.load("m", "polish")
    assert after.state == WorkUnitState.IN_PROGRESS      # live state untouched
    assert after.updated_at == before.updated_at
    seeded = [t for t in store.list_transitions("m")
              if t.from_state is None and t.unit_id == "polish"]
    assert len(seeded) == 1                              # exactly one seeding


def test_blocked_on_rewrites_requires(tmp_path):
    store, _ = _store_with_unit(tmp_path)
    rec = apply_closure(store, "m", "a", ClosureRecord(
        reason="blocked_on", notes="needs review",
        follow_on=[FollowOnUnit(id="review", title="Review", workflow="../wf.yml")]))
    assert rec.state == WorkUnitState.BLOCKED_ON
    assert "review" in rec.requires                       # dynamic blocker added
    assert store.load("m", "review").state == WorkUnitState.PENDING


def test_escalation_moves_unit_to_escalation(tmp_path):
    store, _ = _store_with_unit(tmp_path)
    rec = apply_closure(store, "m", "a",
                        ClosureRecord(reason="escalation", notes="brand risk"))
    assert rec.state == WorkUnitState.ESCALATION


def test_apply_closure_stamps_job_id_and_audits(tmp_path):
    store, _ = _store_with_unit(tmp_path)
    apply_closure(store, "m", "a",
                  ClosureRecord(reason="done_no_follow_on"), job_id="job-7")
    assert store.load("m", "a").last_job_id == "job-7"
    last = store.list_transitions("m")[-1]
    assert last.to_state == WorkUnitState.DONE
    assert last.job_id == "job-7"


def test_illegal_apply_closure_raises(tmp_path):
    """Terminal state admits nothing: applying a closure to a done unit refuses."""
    store, _ = _store_with_unit(tmp_path)
    store.apply("m", "a", WorkUnitState.DONE, reason="already done")
    with pytest.raises(TransitionError):
        apply_closure(store, "m", "a",
                      ClosureRecord(reason="done_no_follow_on"))
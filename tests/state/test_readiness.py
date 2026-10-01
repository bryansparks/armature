"""compute_readiness: pure readiness — deps done, posture permits, budget remains."""
from armature.spec.mission import MissionSpec, WorkUnit
from armature.state.work import LocalWorkStore, WorkUnitState, compute_readiness


def _records(tmp_path):
    store = LocalWorkStore(tmp_path)
    mission = MissionSpec(name="m", objective="o", work=[
        WorkUnit(id="a", title="A", workflow="../wf.yml", posture="delegated"),
        WorkUnit(id="b", title="B", workflow="../wf.yml", requires=["a"]),
        WorkUnit(id="human", title="H", workflow="../wf.yml", posture="human-led"),
    ])
    for u in mission.work:
        store.ensure_unit(mission, u)
    return mission, store


def test_ready_delegated_unit_is_launchable(tmp_path):
    mission, store = _records(tmp_path)
    (r,) = [x for x in compute_readiness(mission, store.list_units("m")) if x.unit_id == "a"]
    assert r.launchable is True and r.notify_only is False


def test_advance_human_led_is_never_launchable(tmp_path):
    """Review Focus #5a: human-led posture is notify-only — an executor may
    never act on it (design §2.3)."""
    mission, store = _records(tmp_path)
    r = [x for x in compute_readiness(mission, store.list_units("m"))
         if x.unit_id == "human"][0]
    assert r.launchable is False
    assert r.notify_only is True


def test_unit_with_unmet_requires_is_not_launchable(tmp_path):
    mission, store = _records(tmp_path)
    store.apply("m", "a", WorkUnitState.IN_PROGRESS, reason="t")
    r = [x for x in compute_readiness(mission, store.list_units("m"))
         if x.unit_id == "b"][0]
    assert r.launchable is False
    assert any("a" in reason for reason in r.reasons)


def test_blocked_on_with_requires_done_is_launchable(tmp_path):
    """Task 5's run gate admits blocked_on re-entry once requires are done —
    advance must surface the same decision to executors (design §2.3:
    a delegated executor may unblock blocked_on)."""
    mission, store = _records(tmp_path)
    store.apply("m", "a", WorkUnitState.IN_PROGRESS, reason="t")
    store.apply("m", "a", WorkUnitState.BLOCKED_ON, reason="d")
    r = [x for x in compute_readiness(mission, store.list_units("m"))
         if x.unit_id == "a"][0]
    assert r.launchable is True


def test_unit_without_attempts_remaining_is_not_launchable(tmp_path):
    mission, store = _records(tmp_path)
    rec = store.load("m", "a")
    rec.attempts = rec.max_attempts
    rec.state = WorkUnitState.RETRY_PENDING
    store.save(rec)
    r = [x for x in compute_readiness(mission, store.list_units("m"))
         if x.unit_id == "a"][0]
    assert r.launchable is False
    assert any("attempts" in x for x in r.reasons)


def test_advance_mission_budget_exhausted(tmp_path):
    """Review Focus #5b: mission-level exhaustion gates every delegated unit."""
    mission, store = _records(tmp_path)
    mission = mission.model_copy(update={"budget_usd": 1.0})
    rec = store.load("m", "a")
    rec.spent_usd = 2.0
    store.save(rec)
    ready = compute_readiness(mission, store.list_units("m"))
    assert all(not r.launchable for r in ready)
    assert any("mission budget" in reason for r in ready for reason in r.reasons)


def test_unit_budget_spent_is_not_launchable(tmp_path):
    mission, store = _records(tmp_path)
    rec = store.load("m", "a")
    rec.max_budget_usd = 1.0
    rec.spent_usd = 1.0
    store.save(rec)
    r = [x for x in compute_readiness(mission, store.list_units("m"))
         if x.unit_id == "a"][0]
    assert r.launchable is False
    assert any("budget" in x for x in r.reasons)


def test_dynamic_follow_on_records_are_considered(tmp_path):
    """Closure-seeded units (not in the doc) participate in readiness."""
    from armature.state.closure import FollowOnUnit
    mission, store = _records(tmp_path)
    store.seed_follow_on("m", FollowOnUnit(id="dyn", title="D", workflow="../wf.yml"))
    ids = {r.unit_id for r in compute_readiness(mission, store.list_units("m"))}
    assert "dyn" in ids
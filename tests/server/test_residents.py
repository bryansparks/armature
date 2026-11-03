"""Residents and their event queue: the tables, and the jobs column that links a run to one."""
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, inspect, select, update

from armature.server import defs, residents, worker
from armature.server.db import new_id
from armature.server.db.schema import (
    audit as audit_rows,
)
from armature.server.db.schema import (
    jobs,
    projects,
    resident_events,
    teams,
    workflows,
)
from armature.server.db.schema import (
    residents as resident_rows,
)
from tests.server.app_support import Clock


def test_resident_tables_exist(engine):
    names = inspect(engine).get_table_names()
    assert "residents" in names and "resident_events" in names
    cols = {c["name"] for c in inspect(engine).get_columns("residents")}
    assert {"workflow_version", "instance_key", "staged", "spent_usd_day", "spent_day",
            "turns_day", "idle_turns_day", "emits_day", "active_job_id"} <= cols
    event_cols = {c["name"] for c in inspect(engine).get_columns("resident_events")}
    assert {"seq", "kind", "reply", "reply_to", "dedupe_key", "dry_run", "handled"} <= event_cols
    jobs_cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert "resident_id" in jobs_cols


@pytest.fixture
def db(engine):
    clock = Clock()
    team, project, workflow_id = new_id(), new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="own", created_at=clock()))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=clock()))
        conn.execute(insert(workflows).values(id=workflow_id, name="bot", current_version=1,
                                             team_id=team, project_id=project, scope=project,
                                             created_at=clock(), updated_at=clock()))
    return SimpleNamespace(engine=engine, clock=clock, team=team, project=project, workflow_id=workflow_id)


def _ensure(db, *, instance_key=""):
    with db.engine.begin() as conn:
        return residents.ensure(conn, team_id=db.team, project_id=db.project,
                                workflow_id=db.workflow_id, instance_key=instance_key, now=db.clock())


def _events(db, resident_id):
    with db.engine.begin() as conn:
        return conn.execute(select(resident_events).where(resident_events.c.resident_id == resident_id)
                            .order_by(resident_events.c.seq)).all()


def test_ensure_creates_once_and_pins_version(db):
    first = _ensure(db)
    assert (first.state, first.instance_key, first.workflow_version) == ("idle", "", 1)
    with db.engine.begin() as conn:
        conn.execute(update(workflows).where(workflows.c.id == db.workflow_id).values(current_version=2))
    second = _ensure(db)
    assert second.id == first.id
    assert second.workflow_version == 1  # pinned at creation, not the workflow's current version
    with db.engine.begin() as conn:
        created = conn.execute(select(audit_rows.c.team_id, audit_rows.c.target_id).where(
            audit_rows.c.action == "resident.create")).all()
    assert created == [(db.team, first.id)]  # only the new row is audited, the second ensure nothing


def test_enqueue_assigns_monotonic_seq(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:
        seqs = [residents.enqueue(conn, resident.id, "request", payload={"n": n}, now=db.clock())
                for n in range(3)]
    assert seqs == [1, 2, 3]


def test_duplicate_dedupe_key_returns_none(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:
        assert residents.enqueue(conn, resident.id, "request", dedupe_key="d1", now=db.clock()) == 1
        assert residents.enqueue(conn, resident.id, "request", dedupe_key="d1", now=db.clock()) is None
    assert len(_events(db, resident.id)) == 1


def test_null_dedupe_keys_do_not_collide(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:
        seqs = [residents.enqueue(conn, resident.id, "signal", now=db.clock()) for _ in range(2)]
    assert seqs == [1, 2]
    assert len(_events(db, resident.id)) == 2


def test_enqueue_bumps_lifetime_counters(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id)
                     .values(idle_streak=3, next_idle_at=db.clock()))
    db.clock.advance(minutes=5)
    at = db.clock()
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "ask", now=at)
    with db.engine.begin() as conn:
        row = conn.execute(select(resident_rows).where(resident_rows.c.id == resident.id)).one()
    assert row.last_event_at == at
    assert row.idle_streak == 0
    assert row.next_idle_at is None


def test_enqueue_on_closed_raises(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(state="closed"))
    with db.engine.begin() as conn:
        with pytest.raises(residents.ResidentError) as raised:
            residents.enqueue(conn, resident.id, "request", now=db.clock())
        assert (raised.value.status, raised.value.message) == (409, "instance is closed")
    assert _events(db, resident.id) == []


def test_enqueue_on_paused_accepted(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(state="paused"))
        assert residents.enqueue(conn, resident.id, "request", now=db.clock()) == 1
    assert [e.kind for e in _events(db, resident.id)] == ["request"]


def test_custom_kind_stores_custom_and_name(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:
        assert residents.enqueue(conn, resident.id, "custom.beach", now=db.clock()) == 1
        assert residents.enqueue(conn, resident.id, "custom", name="dune", now=db.clock()) == 2
    kinds = [(e.kind, e.name) for e in _events(db, resident.id)]
    assert kinds == [("custom", "beach"), ("custom", "dune")]

def _wake(db, resident_id):
    with db.engine.begin() as conn:
        return residents.wake(conn, resident_id, db.clock())


def _add_events(db, resident_id, kinds, *, dry_run=False):
    with db.engine.begin() as conn:
        start = conn.execute(select(func.coalesce(func.max(resident_events.c.seq), 0)).where(
            resident_events.c.resident_id == resident_id)).scalar_one()
        for n, kind in enumerate(kinds):
            conn.execute(insert(resident_events).values(
                id=new_id(), resident_id=resident_id, seq=start + n + 1, kind=kind,
                dry_run=dry_run, created_at=db.clock()))


def _job(db, job_id):
    with db.engine.connect() as conn:
        return conn.execute(select(jobs).where(jobs.c.id == job_id)).first()


def _resident(db, resident_id):
    with db.engine.connect() as conn:
        return conn.execute(select(resident_rows).where(resident_rows.c.id == resident_id)).one()


def _jobs_count(db):
    with db.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(jobs)).scalar_one()


def test_wake_queues_turn_for_oldest_event(db):
    resident = _ensure(db, instance_key="acme")
    _add_events(db, resident.id, ["request", "request"])
    job_id = _wake(db, resident.id)
    assert job_id is not None
    job = _job(db, job_id)
    assert (job.kind, job.resident_id, job.workflow_version) == ("resident", resident.id, 1)
    assert (job.concurrency_key, job.thread) == (f"resident:{resident.id}", "resident:acme")
    assert job.inputs == {"resident_id": resident.id, "instance_key": "acme", "event_seq_from": 1,
                          "event_seq_to": 2, "kind": "request"}
    assert _resident(db, resident.id).active_job_id == job_id


def test_wake_lock_is_atomic(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    first = _wake(db, resident.id)
    assert first is not None
    _add_events(db, resident.id, ["request", "request"])  # more events arrive while the turn runs
    assert _wake(db, resident.id) is None
    assert _jobs_count(db) == 1  # no second job was queued
    assert _resident(db, resident.id).active_job_id == first


def test_wake_lost_race_deletes_job(db, monkeypatch):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    with db.engine.begin() as conn:
        winner = worker.queue_job(conn, team_id=db.team, project_id=db.project,
                                  workflow_id=db.workflow_id, inputs={}, now=db.clock())
    real = worker.queue_job

    def racing(conn, **kwargs):
        job_id = real(conn, **kwargs)
        # another server takes the lock between the queue and the guard
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id)
                     .values(active_job_id=winner))
        return job_id

    monkeypatch.setattr(residents.worker, "queue_job", racing)
    assert _wake(db, resident.id) is None
    assert _jobs_count(db) == 1  # only the winner's job; the lost turn's job was deleted
    assert _resident(db, resident.id).active_job_id == winner


def test_wake_skips_paused_and_closed(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(state="paused"))
    assert _wake(db, resident.id) is None
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(state="closed"))
    assert _wake(db, resident.id) is None
    assert _jobs_count(db) == 0


def test_wake_drops_idle_when_real_event_waits(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["idle", "request", "idle"])
    job_id = _wake(db, resident.id)
    assert job_id is not None
    job = _job(db, job_id)
    assert (job.inputs["event_seq_from"], job.inputs["event_seq_to"], job.inputs["kind"]) == (2, 2, "request")
    assert [(e.seq, e.kind, e.handled, e.handled_by_job) for e in _events(db, resident.id)] == [
        (1, "idle", True, None),    # skipped, not turned: handled with no job
        (2, "request", False, None),
        (3, "idle", True, None)]


def test_wake_pins_instance_version(db):
    resident = _ensure(db)  # pinned at version 1, its creation version
    with db.engine.begin() as conn:
        conn.execute(update(workflows).where(workflows.c.id == db.workflow_id).values(current_version=2))
    _add_events(db, resident.id, ["request"])
    job_id = _wake(db, resident.id)
    assert _job(db, job_id).workflow_version == 1


def test_wake_marks_unhandled_kinds_skipped(db):
    # Phase 1 handles every kind with the instance's own workflow; the skip filter arrives with
    # the spec's on: handlers (Task 7). The kinds after the oldest still break the batch.
    resident = _ensure(db)
    _add_events(db, resident.id, ["signal", "custom", "ask"])
    job_id = _wake(db, resident.id)
    assert job_id is not None
    job = _job(db, job_id)
    assert (job.inputs["event_seq_from"], job.inputs["event_seq_to"], job.inputs["kind"]) == (1, 1, "signal")
    assert [e.handled for e in _events(db, resident.id)] == [False, False, False]


def test_wake_batch_cap_and_contiguity(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"] * 15)
    job_id = _wake(db, resident.id)
    assert (_job(db, job_id).inputs["event_seq_from"],
            _job(db, job_id).inputs["event_seq_to"]) == (1, 10)  # capped at 10, seq range contiguous
    keyed = _ensure(db, instance_key="acme")
    _add_events(db, keyed.id, ["request", "ask", "request"])
    job_id = _wake(db, keyed.id)
    job = _job(db, job_id)
    assert (job.inputs["event_seq_from"], job.inputs["event_seq_to"], job.inputs["kind"]) == (1, 1, "request")


def test_wake_max_turns_closes(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id)
                     .values(turns=residents.MAX_TURNS))
    assert _wake(db, resident.id) is None
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason, row.closed_at) == ("closed", "max_turns", db.clock())
    with db.engine.connect() as conn:
        closed = conn.execute(select(audit_rows.c.target_id, audit_rows.c.detail).where(
            audit_rows.c.action == "resident.close")).all()
    assert closed == [(resident.id, {"reason": "max_turns"})]
    assert _jobs_count(db) == 0


def test_wake_counts_attempts_when_a_callee_wont_resolve(db):
    body = "name: bot\nstages:\n  - id: call\n    call: {workflow: missing}\n"
    with db.engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, db.workflow_id, 1, body, "seed", None, db.clock())
    resident = _ensure(db)  # pinned to version 1, whose call does not resolve
    _add_events(db, resident.id, ["request", "request", "ask"])
    assert _wake(db, resident.id) is None
    assert _jobs_count(db) == 0
    assert [(e.seq, e.attempts, e.handled) for e in _events(db, resident.id)] == [
        (1, 1, False), (2, 1, False), (3, 0, False)]  # the batch only, and it turns nothing yet


def test_enqueue_wakes_the_instance(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:
        seq = residents.enqueue(conn, resident.id, "request", now=db.clock())
    assert seq == 1  # enqueue still returns the seq; the turn is a side effect
    row = _resident(db, resident.id)
    assert row.active_job_id is not None
    job = _job(db, row.active_job_id)
    assert (job.kind, job.inputs["event_seq_from"], job.inputs["event_seq_to"],
            job.inputs["kind"]) == ("resident", 1, 1, "request")

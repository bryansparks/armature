"""Residents and their event queue: the tables, and the jobs column that links a run to one."""
import json
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, inspect, select, update

from armature.server import defs, residents, worker
from armature.server.config import ServerConfig
from armature.server.db import new_id
from armature.server.db.schema import (
    audit as audit_rows,
)
from armature.server.db.schema import (
    jobs,
    projects,
    resident_events,
    runs,
    settings,
    teams,
    versions,
    workflows,
)
from armature.server.db.schema import (
    residents as resident_rows,
)
from tests.server.app_support import Clock, add_user, make_client, mutation, sign_in_as


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


# The workflow every fixture resident is pinned to: request/schedule/signal turn on the instance's
# own workflow, ask hands off to the separate `answer` workflow, and custom kinds have no handler
# until a spec says otherwise (so the skip filter has something to skip in tests).
BOT = """\
name: bot
resident:
  on:
    request: {workflow: self}
    ask: {workflow: answer}
    schedule: {workflow: self}
    signal: {workflow: self}
  idle: {after: 30m, backoff_max: 12h}
stages: []
"""
ANSWER = "name: answer\nstages: []\n"


@pytest.fixture
def db(engine):
    clock = Clock()
    team, project, workflow_id, answer_id = new_id(), new_id(), new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="own", created_at=clock()))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=clock()))
        for id_, name, body in ((workflow_id, "bot", BOT), (answer_id, "answer", ANSWER)):
            conn.execute(insert(workflows).values(id=id_, name=name, current_version=1,
                                                 team_id=team, project_id=project, scope=project,
                                                 created_at=clock(), updated_at=clock()))
            defs._add_version(conn, defs.WORKFLOW, id_, 1, body, "seed", None, clock())
    return SimpleNamespace(engine=engine, clock=clock, team=team, project=project,
                           workflow_id=workflow_id, answer_id=answer_id)


def _ensure(db, *, instance_key="", workflow_id=None):
    with db.engine.begin() as conn:
        return residents.ensure(conn, team_id=db.team, project_id=db.project,
                                workflow_id=workflow_id or db.workflow_id, instance_key=instance_key,
                                now=db.clock())


def _version(db, workflow_id, body, version):
    """Save a new workflow version and make it current, so the next _ensure pins to its spec."""
    with db.engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, workflow_id, version, body, "seed", None, db.clock())
        conn.execute(update(workflows).where(workflows.c.id == workflow_id).values(current_version=version))


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
    assert (job.kind, job.resident_id, job.workflow_id, job.workflow_version) == (
        "resident", resident.id, db.workflow_id, 1)
    assert (job.concurrency_key, job.thread) == (f"resident:{resident.id}", "resident:acme")
    assert job.inputs == {"resident_id": resident.id, "instance_key": "acme", "event_seq_from": 1,
                          "event_seq_to": 2, "kind": "request",
                          "_event": {"kind": "request", "name": None,
                                     "is": {"request": True, "ask": False, "schedule": False,
                                            "signal": False, "idle": False, "custom": False}},
                          "_resident": {"state": {}, "meta": {"turns": 0,
                                                              "created_at": db.clock().isoformat(),
                                                              "last_turn_at": None, "idle_streak": 0}}}
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


def test_wake_dispatches_named_handler(db):
    _version(db, db.answer_id, ANSWER, 3)  # the handler's current version moves on after creation
    resident = _ensure(db)  # the default spec hands ask to the answer workflow
    _add_events(db, resident.id, ["ask"])
    job_id = _wake(db, resident.id)
    job = _job(db, job_id)
    assert (job.kind, job.resident_id, job.workflow_id, job.workflow_version) == (
        "resident", resident.id, db.answer_id, 3)  # pinned to the named workflow's current version
    assert (job.concurrency_key, job.inputs["kind"], job.inputs["_event"]["is"]["ask"]) == (
        f"resident:{resident.id}", "ask", True)


def test_wake_custom_kind_uses_custom_handler(db):
    body = ("name: bot\nresident:\n  on:\n"
            "    request: {workflow: self}\n    custom.beach: {workflow: answer}\nstages: []\n")
    _version(db, db.workflow_id, body, 2)
    resident = _ensure(db)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "custom.beach", now=db.clock())
    job = _job(db, _resident(db, resident.id).active_job_id)
    assert (job.workflow_id, job.inputs["kind"]) == (db.answer_id, "custom")
    assert (job.inputs["event_seq_from"], job.inputs["event_seq_to"]) == (1, 1)
    assert (job.inputs["_event"]["name"], job.inputs["_event"]["is"]["custom"]) == ("beach", True)


def test_wake_uses_instance_version_not_current(db):
    v2 = "name: bot\nresident:\n  on:\n    request: {workflow: answer}\nstages: []\n"
    v3 = "name: bot\nresident:\n  on:\n    request: {workflow: self}\nstages: []\n"
    _version(db, db.workflow_id, v2, 2)
    resident = _ensure(db)  # pins version 2, whose request handler is the answer workflow
    _version(db, db.workflow_id, v3, 3)  # the current version now handles requests itself
    _add_events(db, resident.id, ["request"])
    job = _job(db, _wake(db, resident.id))
    assert (job.workflow_id, job.workflow_version) == (db.answer_id, 1)
    assert job.inputs["kind"] == "request"  # dispatched by the pinned spec, not the current one


GATED = "stages:\n  - id: hold\n    gate: human\n"


def test_wake_pauses_when_a_handler_is_gated_at_dispatch(db):
    # The save gate can be bypassed (a gate saved onto the named handler after the spec, a gated
    # version rolled back in): dispatch is the backstop that never runs a gated turn.
    resident = _ensure(db)  # the default spec hands ask to the answer workflow
    _version(db, db.answer_id, "name: answer\n" + GATED, 2)  # the gate arrives after the spec was saved
    _add_events(db, resident.id, ["ask"])
    assert _wake(db, resident.id) is None
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason) == ("paused", "handler_gated")
    assert _events(db, resident.id)[0].handled is False  # kept for when the handler is ungated
    assert _jobs_count(db) == 0
    _version(db, db.workflow_id, "name: bot\nresident:\n  on:\n    request: {workflow: self}\n" + GATED, 2)
    pinned = _ensure(db, instance_key="acme")  # a new instance pins the gated version directly
    _add_events(db, pinned.id, ["request"])
    assert _wake(db, pinned.id) is None
    assert (_resident(db, pinned.id).state,
            _resident(db, pinned.id).close_reason) == ("paused", "handler_gated")
    assert _jobs_count(db) == 0


def test_unresolvable_handler_poisons_and_drains(db):
    # A handler naming no workflow in scope counts one attempt per wake, and past MAX_ATTEMPTS
    # the batch is given up as poison — handled, audited — so the events behind it still turn.
    _version(db, db.workflow_id,
             "name: bot\nresident:\n  on:\n    request: {workflow: ghost}\n"
             "    signal: {workflow: self}\nstages: []\n", 2)
    resident = _ensure(db)
    _add_events(db, resident.id, ["request", "signal"])
    for attempt in range(1, residents.MAX_ATTEMPTS):
        assert _wake(db, resident.id) is None
        assert _events(db, resident.id)[0].attempts == attempt
    job_id = _wake(db, resident.id)  # the last attempt poisons the batch; the signal behind turns
    job = _job(db, job_id)
    assert (job.inputs["kind"], job.inputs["event_seq_from"]) == ("signal", 2)
    events = _events(db, resident.id)
    assert (events[0].kind, events[0].attempts, events[0].handled) == ("request", residents.MAX_ATTEMPTS, True)
    assert events[1].handled is False  # the signal is turned by the job, not skipped
    with db.engine.connect() as conn:
        poison = conn.execute(select(audit_rows.c.target_id, audit_rows.c.detail).where(
            audit_rows.c.action == "resident.event_poison")).all()
    assert poison == [(resident.id, {"job_id": None, "seq": [1]})]


def test_inputs_carry_event_and_resident_snapshot(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            data={"mood": "calm"}, turns=4, idle_streak=2, last_turn_at=db.clock()))
    _add_events(db, resident.id, ["request"])
    inputs = _job(db, _wake(db, resident.id)).inputs
    assert inputs["_event"] == {"kind": "request", "name": None,
                                "is": {"request": True, "ask": False, "schedule": False,
                                       "signal": False, "idle": False, "custom": False}}
    assert inputs["_resident"] == {"state": {"mood": "calm"},
                                   "meta": {"turns": 4, "created_at": db.clock().isoformat(),
                                            "last_turn_at": db.clock().isoformat(), "idle_streak": 2}}


def test_budget_pause_when_spent_exceeds_usd_per_day(db):
    body = ("name: bot\nresident:\n  on:\n    request: {workflow: self}\n"
            "  budget: {usd_per_day: 1.0}\nstages: []\n")
    _version(db, db.workflow_id, body, 2)
    spent = _ensure(db)
    _add_events(db, spent.id, ["request"])
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == spent.id).values(
            spent_usd_day=1_500_000, spent_day=db.clock().date().isoformat()))
    assert _wake(db, spent.id) is None
    row = _resident(db, spent.id)
    assert (row.state, row.close_reason) == ("paused", "budget")
    assert _jobs_count(db) == 0
    assert _events(db, spent.id)[0].handled is False  # the event waits out the pause for the sweep's resume
    yesterday = _ensure(db, instance_key="acme")
    _add_events(db, yesterday.id, ["request"])
    with db.engine.begin() as conn:  # yesterday's spend is not today's: the day reset runs before the check
        conn.execute(update(resident_rows).where(resident_rows.c.id == yesterday.id).values(
            spent_usd_day=9_876_543, spent_day="2026-10-22"))
    assert _wake(db, yesterday.id) is not None
    assert _resident(db, yesterday.id).state == "idle"


def test_budget_pause_survives_enqueue_until_the_sweep_resumes(db):
    # An event landing on a paused instance must not roll its budget day over: the sweep finds
    # the pause through yesterday's spent_day stamp, so the enqueue-triggered wake leaves it be.
    body = ("name: bot\nresident:\n  on:\n    request: {workflow: self}\n"
            "  budget: {usd_per_day: 1.0}\nstages: []\n")
    _version(db, db.workflow_id, body, 2)
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    with db.engine.begin() as conn:  # yesterday's budget closed the instance
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            state="paused", close_reason="budget", spent_usd_day=1_500_000,
            spent_day=db.clock().date().isoformat()))
    db.clock.advance(days=1)
    with db.engine.begin() as conn:  # a new event lands before the sweep runs
        residents.enqueue(conn, resident.id, "request", now=db.clock())
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason) == ("paused", "budget")
    assert row.spent_day != db.clock().date().isoformat()  # the stale stamp is what the sweep matches on
    residents.sweep(db.engine, db.clock())
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason) == ("idle", None)  # the sweep resumed it
    assert row.active_job_id is not None  # and the events that waited out the pause now turn
    job = _job(db, row.active_job_id)
    assert (job.inputs["kind"], job.inputs["event_seq_from"], job.inputs["event_seq_to"]) == ("request", 1, 2)


def test_close_after_idle_closes(db):
    body = ("name: bot\nresident:\n  on:\n    request: {workflow: self}\n"
            "  lifetime: {close_after_idle: 30d}\nstages: []\n")
    _version(db, db.workflow_id, body, 2)
    resident = _ensure(db, instance_key="acme")
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            last_event_at=db.clock() - timedelta(days=31)))
    _add_events(db, resident.id, ["idle"])  # a waiting idle event: lifetime measures real events only
    assert _wake(db, resident.id) is None
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason, row.closed_at) == ("closed", "lifetime_idle", db.clock())
    assert _jobs_count(db) == 0
    assert _events(db, resident.id)[0].handled is False  # never turned, just outlived
    recent = _ensure(db, instance_key="live")  # same spec, a fresh instance: turns fine
    _add_events(db, recent.id, ["request"])
    assert _wake(db, recent.id) is not None
    assert _resident(db, recent.id).state == "idle"


def test_spec_removed_pauses_instances(db):
    plain = new_id()  # a workflow whose current version carries no resident: block
    with db.engine.begin() as conn:
        conn.execute(insert(workflows).values(id=plain, name="plain", current_version=1,
                                              team_id=db.team, project_id=db.project, scope=db.project,
                                              created_at=db.clock(), updated_at=db.clock()))
        defs._add_version(conn, defs.WORKFLOW, plain, 1, "name: plain\nstages: []\n",
                          "seed", None, db.clock())
    resident = _ensure(db, workflow_id=plain)
    _add_events(db, resident.id, ["request"])
    assert _wake(db, resident.id) is None
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason) == ("paused", "spec_removed")
    assert _events(db, resident.id)[0].handled is False  # kept for a version that brings the block back
    assert _jobs_count(db) == 0
    assert _wake(db, resident.id) is None  # already paused: waking again changes nothing


GATED_SELF = ("name: bot\nresident:\n  on:\n    request: {workflow: self}\n"
              "stages:\n  - id: hold\n    gate: human\n")


def _count_versions(db, workflow_id):
    with db.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(versions).where(
            versions.c.object_kind == "workflow", versions.c.object_id == workflow_id)).scalar_one()


def test_save_rejects_human_gate_handler(db):
    with db.engine.begin() as conn:
        residents.check_handlers(conn, BOT, db.workflow_id)  # ungated handlers save clean
    with db.engine.begin() as conn, pytest.raises(ValueError, match="human gate"):
        residents.check_handlers(conn, GATED_SELF, db.workflow_id)  # self: the gate is on the spec saved
    with db.engine.begin() as conn:  # named: the gate is on the handler's current version
        defs._add_version(conn, defs.WORKFLOW, db.answer_id, 2,
                          "name: answer\nstages:\n  - id: hold\n    gate: human\n",
                          "seed", None, db.clock())
        conn.execute(update(workflows).where(workflows.c.id == db.answer_id).values(current_version=2))
        with pytest.raises(ValueError, match="human gate"):
            residents.check_handlers(
                conn, "name: bot\nresident:\n  on:\n    ask: {workflow: answer}\nstages: []\n",
                db.workflow_id)
        with pytest.raises(ValueError, match="not in the caller"):  # a handler naming no real workflow
            residents.check_handlers(
                conn, "name: bot\nresident:\n  on:\n    ask: {workflow: ghost}\nstages: []\n",
                db.workflow_id)


def test_save_route_rejects_gated_handler(db, keyring):
    client = make_client(db.engine, keyring, db.clock)
    user_id, _ = add_user(db.engine, keyring, db.clock, "root", is_admin=True)
    csrf = sign_in_as(client, db.engine, keyring, db.clock, user_id)
    saved = client.post(f"/api/workflows/{db.workflow_id}",
                        json={"body": GATED_SELF, "base_version": 1}, headers=mutation(csrf))
    assert saved.status_code == 400
    assert "human gate" in saved.text  # the route's own refusal, before any version is written
    assert _count_versions(db, db.workflow_id) == 1


def test_wake_marks_kind_without_handler_skipped(db):
    # The default spec has no handler for a bare custom or an unnamed custom.<name>: both drain as
    # skipped — handled with no job — and the next-oldest event is what turns.
    resident = _ensure(db)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "custom", now=db.clock())
        residents.enqueue(conn, resident.id, "custom.unhandled", now=db.clock())
    assert _resident(db, resident.id).active_job_id is None  # nothing turned: no handler anywhere
    assert [(e.kind, e.name, e.handled, e.handled_by_job) for e in _events(db, resident.id)] == [
        ("custom", None, True, None), ("custom", "unhandled", True, None)]
    _add_events(db, resident.id, ["signal"])
    job_id = _wake(db, resident.id)
    job = _job(db, job_id)
    assert (job.inputs["event_seq_from"], job.inputs["event_seq_to"], job.inputs["kind"]) == (3, 3, "signal")


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
    body = ("name: bot\nresident:\n  on:\n    request: {workflow: self}\n"
            "stages:\n  - id: call\n    call: {workflow: missing}\n")
    _version(db, db.workflow_id, body, 2)
    resident = _ensure(db)  # pinned to version 2, whose call does not resolve
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


def _stage(db, resident_id, staged):
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident_id).values(staged=staged))


def _finish(db, job_id, status="succeeded", cost=None):
    with db.engine.begin() as conn:
        run_id = new_id()
        conn.execute(insert(runs).values(id=run_id, team_id=db.team, status=status, started_at=db.clock(),
                                         cost_usd=Decimal(str(cost)) if cost is not None else None))
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status=status, run_id=run_id))


def _settle(db):
    with db.engine.begin() as conn:
        return residents.settle(conn, db.clock())


def test_settle_success_applies_staged_and_marks_handled(db):
    resident = _ensure(db, instance_key="acme")
    _add_events(db, resident.id, ["request", "request"])
    job_id = _wake(db, resident.id)  # the batch (1, 2)
    _stage(db, resident.id, {"state": {"mood": "calm"}, "idle_nothing_to_do": False})
    db.clock.advance(minutes=5)
    _finish(db, job_id, cost=0.123456)
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert row.data == {"mood": "calm"} and row.data_version == 1  # the staged state became the data
    assert row.staged is None
    assert (row.turns, row.turns_day, row.idle_turns_day, row.spent_usd_day) == (1, 1, 0, 123456)
    assert row.idle_streak == 0
    assert row.last_turn_at == db.clock()
    assert row.active_job_id is None  # no events wait, so nothing rewakes
    assert row.state == "idle"  # the state column is not settle's to touch
    assert [(e.seq, e.handled, e.handled_by_job) for e in _events(db, resident.id)] == [
        (1, True, job_id), (2, True, job_id)]
    with db.engine.connect() as conn:
        writes = conn.execute(select(audit_rows.c.team_id, audit_rows.c.target_id, audit_rows.c.detail).where(
            audit_rows.c.action == "resident.state_write")).all()
    assert writes == [(db.team, resident.id,
                       {"data_version": 1, "bytes": len(json.dumps({"mood": "calm"}, default=str).encode()),
                        "keys": ["mood"]})]  # keys, never the staged values


def test_settle_failed_turn_reoffers_events_and_discards_staged(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request", "request", "ask"])
    job_id = _wake(db, resident.id)  # the batch (1, 2); the ask breaks it
    _stage(db, resident.id, {"state": {"mood": "calm"}, "idle_nothing_to_do": True})
    db.clock.advance(minutes=5)
    _finish(db, job_id, status="failed", cost=0.5)
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert row.staged is None and row.data == {} and row.data_version == 0  # a failed turn stages nothing
    assert (row.turns, row.turns_day, row.spent_usd_day, row.idle_streak) == (0, 0, 0, 0)
    assert row.last_turn_at == db.clock()
    assert [(e.seq, e.attempts, e.handled) for e in _events(db, resident.id)] == [
        (1, 1, False), (2, 1, False), (3, 0, False)]  # the batch counts one attempt; the ask none yet
    assert row.active_job_id not in (None, job_id)  # the failed batch is re-offered at once
    job = _job(db, row.active_job_id)
    assert (job.inputs["event_seq_from"], job.inputs["event_seq_to"], job.inputs["kind"]) == (1, 2, "request")


def test_poison_event_given_up_after_max_attempts(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    job_ids = [_wake(db, resident.id)]
    for attempt in range(1, residents.MAX_ATTEMPTS + 1):
        _finish(db, job_ids[-1], status="failed")
        assert _settle(db) == [resident.id]
        event = _events(db, resident.id)[0]
        assert event.attempts == attempt
        active = _resident(db, resident.id).active_job_id
        if attempt < residents.MAX_ATTEMPTS:
            assert event.handled is False and active not in (None,) + tuple(job_ids)
            job_ids.append(active)
        else:
            assert event.handled is True and event.handled_by_job == job_ids[-1]  # given up as poison
            assert active is None  # nothing is re-offered past the cap
    with db.engine.connect() as conn:
        poison = conn.execute(select(audit_rows.c.detail).where(
            audit_rows.c.action == "resident.event_poison")).scalars().all()
    assert poison == [{"job_id": job_ids[-1], "seq": [1]}]


def test_settle_lost_turn_behaves_like_failed(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    job_id = _wake(db, resident.id)
    _stage(db, resident.id, {"state": {"mood": "calm"}})
    _finish(db, job_id, status="lost")
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert row.staged is None and row.turns == 0 and row.last_turn_at == db.clock()
    [event] = _events(db, resident.id)
    assert (event.attempts, event.handled, event.handled_by_job) == (1, False, None)
    assert row.active_job_id not in (None, job_id)  # lost or not, the events are re-offered


def test_settle_unknown_cost_counts_turn_but_not_spend(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    job_id = _wake(db, resident.id)
    _finish(db, job_id)  # the run exists but never reported a cost
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert (row.turns_day, row.spent_usd_day, row.turns) == (1, 0, 1)  # the turn counts, the spend does not


def test_settle_reschedules_and_rewakes(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request", "ask"])
    first = _wake(db, resident.id)  # the ask breaks the batch (its handler differs), so only the request turns
    assert (_job(db, first).inputs["event_seq_from"], _job(db, first).inputs["event_seq_to"]) == (1, 1)
    _finish(db, first)
    assert _settle(db) == [resident.id]
    second = _resident(db, resident.id).active_job_id
    assert second not in (None, first)  # the ask's turn queues immediately
    job = _job(db, second)
    assert (job.inputs["event_seq_from"], job.inputs["event_seq_to"], job.inputs["kind"]) == (2, 2, "ask")
    assert job.workflow_id == db.answer_id  # the default spec hands ask to the answer workflow
    row = _resident(db, resident.id)
    assert (row.state, row.data, row.data_version) == ("idle", {}, 0)  # nothing staged, nothing written


def test_settle_idle_turn_counts_idle_day(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["idle", "idle"])
    job_id = _wake(db, resident.id)  # idle events with no real one behind still turn
    assert _job(db, job_id).inputs["kind"] == "idle"
    _stage(db, resident.id, {"idle_nothing_to_do": True})
    _finish(db, job_id)
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert (row.turns_day, row.idle_turns_day) == (1, 1)
    assert row.idle_streak == 1  # nothing to do: the streak starts
    assert row.data_version == 0  # no staged state, so no write is audited


def test_budget_day_resets_at_utc_boundary(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    with db.engine.begin() as conn:  # the day before the clock's start, counters still on the books
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            spent_usd_day=987654, turns_day=9, idle_turns_day=4, emits_day=6, spent_day="2026-10-22"))
    job_id = _wake(db, resident.id)  # wake resets today's counters before any budget check
    row = _resident(db, resident.id)
    assert (row.spent_usd_day, row.turns_day, row.idle_turns_day, row.emits_day) == (0, 0, 0, 0)
    assert row.spent_day == db.clock().date().isoformat()
    with db.engine.begin() as conn:  # the instance spends through its turn's day
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            spent_usd_day=111111, turns_day=2, idle_turns_day=1, emits_day=3))
    db.clock.advance(days=1)  # the turn ends past the UTC midnight
    _finish(db, job_id, cost=0.25)
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert (row.turns_day, row.spent_usd_day, row.idle_turns_day, row.emits_day) == (1, 250000, 0, 0)
    assert row.spent_day == db.clock().date().isoformat()  # settled into the new day's budget, not yesterday's


def _worker_for(db, keyring, tmp_path):
    cfg = ServerConfig(database_url=str(db.engine.url), packs_dir="/srv/packs")
    hub = SimpleNamespace(engine=db.engine, clock=db.clock, running=False, unix_endpoint=None)
    return worker.Worker(db.engine, keyring, cfg, hub, clock=db.clock, work_dir=tmp_path / "work")


def _enable(db, value=True):
    with db.engine.begin() as conn:
        conn.execute(insert(settings).values(key=residents.ENABLED_KEY, value=value))


def test_sweep_settles_finished_turns(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    job_id = _wake(db, resident.id)
    db.clock.advance(minutes=5)
    _finish(db, job_id)  # the turn ended; only the sweep would settle it
    residents.sweep(db.engine, db.clock())
    row = _resident(db, resident.id)
    assert row.active_job_id is None and row.turns == 1  # the sweep settled the finished turn
    assert [(e.seq, e.handled, e.handled_by_job) for e in _events(db, resident.id)] == [(1, True, job_id)]


def test_sweep_disabled_by_default(db, keyring, tmp_path):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    job_id = _wake(db, resident.id)
    db.clock.advance(minutes=5)
    _finish(db, job_id)  # the turn ended; only the sweep would settle it
    w = _worker_for(db, keyring, tmp_path)
    w.tick()
    assert _resident(db, resident.id).active_job_id == job_id  # the setting is absent: the tables are inert
    assert _events(db, resident.id)[0].handled is False
    _enable(db)
    w.tick()
    row = _resident(db, resident.id)
    assert row.active_job_id is None and row.turns == 1  # on: the tick settles the turn
    assert _events(db, resident.id)[0].handled is True


def test_sweep_resumes_budget_paused_at_day_rollover(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request"])
    with db.engine.begin() as conn:  # yesterday's budget closed the instance
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            state="paused", close_reason="budget", spent_day="2026-10-22"))
    same_day = _ensure(db, instance_key="later")
    _add_events(db, same_day.id, ["request"])
    with db.engine.begin() as conn:  # paused today: the day, and so the budget, has not rolled over
        conn.execute(update(resident_rows).where(resident_rows.c.id == same_day.id).values(
            state="paused", close_reason="budget", spent_day=db.clock().date().isoformat()))
    residents.sweep(db.engine, db.clock())
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason) == ("idle", None)  # the daily pause died with its day
    assert (row.spent_day, row.spent_usd_day, row.turns_day) == (db.clock().date().isoformat(), 0, 0)
    assert row.active_job_id is not None  # and it woke: the event that waited now turns
    assert _job(db, row.active_job_id).inputs["kind"] == "request"
    still = _resident(db, same_day.id)
    assert (still.state, still.close_reason, still.active_job_id) == ("paused", "budget", None)


# The resident.* tools: a turn's only reach into its instance, staged and validated.

TOOLBOT = """\
name: bot
resident:
  on:
    request: {workflow: self}
  state:
    schema:
      type: object
      required: [mood]
      properties:
        mood: {type: string}
        metrics: {type: object}
    max_bytes: 1024
    publish: [metrics]
stages: []
"""

VALID = {  # one good call per tool, for the loops that must reach every tool
    "resident.events": {},
    "resident.state.get": {},
    "resident.state.set": {"state": {"mood": "calm"}},
    "resident.state.patch": {"patch": {"mood": "calm"}},
    "resident.metrics.publish": {"values": {"metrics": {"uptime": 99.5}}},
    "resident.reply": {"event_seq": 1, "text": "done"},
    "resident.idle.report": {"nothing_to_do": True},
    "resident.close": {"reason": "finished"},
}


def _tooled(db, *, reply_to=None):
    """A resident pinned to a state-validated spec, one request turn running over seq 1."""
    _version(db, db.workflow_id, TOOLBOT, 2)
    resident = _ensure(db)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "request", payload={"text": "watch the fence"},
                           reply_to=reply_to, now=db.clock())
    job_id = _resident(db, resident.id).active_job_id
    run_id = new_id()
    with db.engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=db.team, status="running", started_at=db.clock()))
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(run_id=run_id))
    return resident, job_id, run_id


def _call(db, run_id, job, tool, params):
    result, _ = residents.call_tool(db.engine, run_id=run_id, job=job, tool=tool, params=params,
                                    now=db.clock())
    return result


def test_tools_refuse_unbound_run(db):
    _resident, _job_id, run_id = _tooled(db)
    with db.engine.begin() as conn:  # a job that is not the instance's turn, under the same run
        other = worker.queue_job(conn, team_id=db.team, project_id=db.project,
                                 workflow_id=db.workflow_id, inputs={}, now=db.clock())
    for tool in residents.TOOLS:
        with pytest.raises(residents.ResidentError) as raised:
            _call(db, run_id, _job(db, other), tool, VALID[tool])
        assert raised.value.status == 409  # the lock decides, so a foreign job reaches nothing
    plain_run = new_id()  # a run that is no resident turn at all
    with db.engine.begin() as conn:
        conn.execute(insert(runs).values(id=plain_run, team_id=db.team, status="running",
                                         started_at=db.clock()))
        conn.execute(update(jobs).where(jobs.c.id == other).values(run_id=plain_run))
    for tool in residents.TOOLS:
        with pytest.raises(residents.ResidentError) as raised:
            _call(db, plain_run, _job(db, other), tool, VALID[tool])
        assert raised.value.status == 403


def test_state_set_stages_and_validates(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    assert _call(db, run_id, job, "resident.state.set", {"state": {"mood": "calm"}}) == {"staged": True}
    row = _resident(db, resident.id)
    assert row.staged == {"state": {"mood": "calm"}}
    assert (row.data, row.data_version) == ({}, 0)  # data is settle's to touch, never the tool's
    with pytest.raises(residents.ResidentError) as over:  # schema-valid but over max_bytes
        _call(db, run_id, job, "resident.state.set", {"state": {"mood": "x" * 2000}})
    assert over.value.status == 413
    with pytest.raises(residents.ResidentError) as bad:  # under max_bytes but schema-invalid
        _call(db, run_id, job, "resident.state.set", {"state": {"mood": 42}})
    assert bad.value.status == 422
    assert _resident(db, resident.id).staged == {"state": {"mood": "calm"}}  # refusals changed nothing


def test_state_patch_merges(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    with db.engine.begin() as conn:  # the applied state the patch starts from
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            data={"mood": "calm", "gone": True, "nested": {"a": 1, "b": 2}}))
    _call(db, run_id, job, "resident.state.patch",
          {"patch": {"nested": {"b": 3}, "gone": None, "extra": 5}})
    assert _resident(db, resident.id).staged["state"] == \
        {"mood": "calm", "nested": {"a": 1, "b": 3}, "extra": 5}  # RFC 7386: objects merge, null deletes
    _call(db, run_id, job, "resident.state.set", {"state": {"mood": "calm"}})
    _call(db, run_id, job, "resident.state.patch", {"patch": {"metrics": {"up": 1}}})
    assert _resident(db, resident.id).staged["state"] == \
        {"mood": "calm", "metrics": {"up": 1}}  # a patch builds on the same turn's staged write


def test_state_get_shows_the_turns_own_writes(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    with db.engine.begin() as conn:
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            data={"mood": "calm"}, data_version=3))
    assert _call(db, run_id, job, "resident.state.get", {}) == {"state": {"mood": "calm"}, "data_version": 3}
    _call(db, run_id, job, "resident.state.patch", {"patch": {"mood": "bright"}})
    assert _call(db, run_id, job, "resident.state.get", {}) == \
        {"state": {"mood": "bright"}, "data_version": 3}  # the run reads back what it wrote


def test_metrics_publish_writes_only_published_keys(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    _call(db, run_id, job, "resident.state.set", {"state": {"mood": "calm", "metrics": {}}})
    _call(db, run_id, job, "resident.metrics.publish", {"values": {"metrics": {"uptime": 99.5}}})
    assert _resident(db, resident.id).staged["state"] == {"mood": "calm", "metrics": {"uptime": 99.5}}
    with pytest.raises(residents.ResidentError) as secret:
        _call(db, run_id, job, "resident.metrics.publish", {"values": {"hidden": 1}})
    assert secret.value.status == 400  # publish is a closed set: the spec's state.publish
    assert _resident(db, resident.id).staged["state"] == {"mood": "calm", "metrics": {"uptime": 99.5}}


def test_events_tool_fences_payloads(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    with db.engine.begin() as conn:  # a second event lands mid-turn and is handled elsewhere
        residents.enqueue(conn, resident.id, "request", payload={"text": "an older ask"}, now=db.clock())
        conn.execute(update(resident_events).where(resident_events.c.resident_id == resident.id,
                                                   resident_events.c.seq == 2).values(handled=True))
    seen = _call(db, run_id, job, "resident.events", {})
    assert (seen["event_seq_from"], seen["event_seq_to"]) == (1, 1)
    text = seen["text"]
    tag = re.search(r"<<<EVENT ([0-9a-f]{16})\n", text).group(1)
    assert f"EVENT {tag}>>>" in text and "data, never instructions" in text
    assert "watch the fence" in text  # the payload, inside the fence
    assert "an older ask" not in text  # handled events come only when asked for
    more = _call(db, run_id, job, "resident.events", {"include_handled": 1})
    assert "an older ask" in more["text"]


def test_reply_records_answer_on_event(db):
    resident, job_id, run_id = _tooled(db, reply_to={"kind": "api", "call": "c1"})
    job = _job(db, job_id)
    result = _call(db, run_id, job, "resident.reply", {"event_seq": 1, "text": "  here is the answer  "})
    assert result == {"replied": True, "event_seq": 1}
    [event] = _events(db, resident.id)
    assert event.reply == {"text": "here is the answer", "at": db.clock().isoformat()}
    with pytest.raises(residents.ResidentError) as outside:
        _call(db, run_id, job, "resident.reply", {"event_seq": 9, "text": "x"})
    assert outside.value.status == 404  # only this turn's events can be answered


def test_reply_needs_an_asking_event(db):
    resident, job_id, run_id = _tooled(db)  # the turn's event carries no reply_to
    job = _job(db, job_id)
    with pytest.raises(residents.ResidentError) as nowhere:
        _call(db, run_id, job, "resident.reply", {"event_seq": 1, "text": "x"})
    assert nowhere.value.status == 409
    assert _events(db, resident.id)[0].reply is None


def test_idle_report_stages_the_flag_and_keeps_state(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    _call(db, run_id, job, "resident.idle.report", {"nothing_to_do": True})
    assert _resident(db, resident.id).staged == {"idle_nothing_to_do": True}
    _call(db, run_id, job, "resident.state.set", {"state": {"mood": "calm"}})
    assert _resident(db, resident.id).staged == \
        {"idle_nothing_to_do": True, "state": {"mood": "calm"}}  # each keeps the other
    _call(db, run_id, job, "resident.idle.report", {"nothing_to_do": False})
    assert _resident(db, resident.id).staged == \
        {"idle_nothing_to_do": False, "state": {"mood": "calm"}}


def test_close_tool_closes_without_stopping_job(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    with db.engine.begin() as conn:  # a second event waits behind the running turn
        residents.enqueue(conn, resident.id, "request", now=db.clock())
    _call(db, run_id, job, "resident.state.set", {"state": {"mood": "done"}})
    assert _call(db, run_id, job, "resident.close", {"reason": "all done"}) == {"closed": True}
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason, row.closed_at) == ("closed", "all done", db.clock())
    assert row.active_job_id == job_id  # the running job is unaffected
    with pytest.raises(residents.ResidentError) as shut:  # a closed instance stages nothing more
        _call(db, run_id, job, "resident.state.set", {"state": {"mood": "x"}})
    assert shut.value.status == 409
    db.clock.advance(minutes=5)
    _finish(db, job_id)
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert row.active_job_id is None  # the lock cleared
    assert _jobs_count(db) == 1  # and nothing rewaked: a closed instance has no future turns
    assert row.data == {"mood": "done"} and row.data_version == 1  # the turn succeeded, so its write applied
    assert _events(db, resident.id)[1].handled is False  # the waiting event stays
    with db.engine.connect() as conn:
        closed = conn.execute(select(audit_rows.c.target_id, audit_rows.c.detail).where(
            audit_rows.c.action == "resident.close")).all()
    assert closed == [(resident.id, {"reason": "all done"})]


def test_settle_revalidates_staged_state(db):
    resident, job_id, _run_id = _tooled(db)
    _stage(db, resident.id, {"state": {"nope": "not the mood"}})  # schema-invalid: mood is required
    _finish(db, job_id)
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert row.staged is None and row.data == {} and row.data_version == 0  # refused at settle too
    assert row.turns == 0  # the violation takes the failure path, not the success one
    assert _events(db, resident.id)[0].attempts == 1  # the turn's events paid an attempt
    assert row.active_job_id not in (None, job_id)  # and the events are re-offered
    with db.engine.connect() as conn:
        refused = conn.execute(select(audit_rows.c.detail).where(
            audit_rows.c.action == "resident.state_refused")).all()
    assert refused and "mood" in refused[0].detail["reason"]


def test_settle_refuses_oversized_staged_state(db):
    resident, job_id, _run_id = _tooled(db)
    _stage(db, resident.id, {"state": {"mood": "x" * 2000}})  # schema-valid, over max_bytes
    _finish(db, job_id)
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert row.staged is None and row.data == {} and row.data_version == 0
    assert row.turns == 0 and _events(db, resident.id)[0].attempts == 1


def test_pack_validates(db):
    from typer.testing import CliRunner

    from armature.cli import app
    from armature.packs import BUILTIN_DIR, catalog
    result = CliRunner().invoke(app, ["pack", "validate", str(BUILTIN_DIR / "resident")])
    assert result.exit_code == 0, result.output
    cat = catalog([BUILTIN_DIR])
    assert cat.errors == []
    pack = cat.pack("resident")
    assert pack.builtin and {f"resident.{name}" for name in pack.adapters} == set(residents.TOOLS)

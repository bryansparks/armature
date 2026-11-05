"""Residents and their event queue: the tables, and the jobs column that links a run to one."""
import contextlib
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, inspect, select, update
from sqlalchemy.exc import OperationalError

from armature.server import defs, residents, worker
from armature.server.config import ServerConfig
from armature.server.db import new_id
from armature.spec.loader import load_spec
from armature.spec.validator import validate_spec
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
    tool_calls,
    versions,
    workflows,
)
from armature.server.db.schema import (
    residents as resident_rows,
)
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)


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


def test_ensure_rereads_with_a_locking_read_after_losing_the_insert_race(db, monkeypatch):
    first = _ensure(db)
    calls = []
    real = residents._instance

    def spy(conn, workflow_id, instance_key, *, lock=False):
        calls.append(lock)
        return None if not lock else real(conn, workflow_id, instance_key, lock=lock)

    monkeypatch.setattr(residents, "_instance", spy)
    second = _ensure(db)  # the first read misses, the unique key rejects the insert, then the winner's row is re-read
    assert second.id == first.id
    assert calls == [False, True]


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
    with db.engine.begin() as conn:
        with pytest.raises(residents.ResidentError) as raised:  # defensive: routes and the emit tool
            # validate the whole grammar, but the column's width alone would break the insert
            residents.enqueue(conn, resident.id, "custom", name="b" * 65, now=db.clock())
        assert raised.value.status == 400

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
                          "_event": {"kind": "request", "name": None, "fields": {},
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


def test_wake_bounds_the_thread_label_at_the_columns_width(db):
    resident = _ensure(db, instance_key="k" * residents.KEY_MAX)  # the widest key the column holds
    _add_events(db, resident.id, ["request"])
    job_id = _wake(db, resident.id)
    assert job_id is not None  # the key asks at its full width; only the derived label is bounded
    assert len(_job(db, job_id).thread) <= 191  # jobs.thread is String(191): the label is a grouping
    # hint, not identity — active_job_id and concurrency_key keep the instance distinct — so MySQL
    # strict mode never refuses the queue_job INSERT the way an unbounded "resident:" + 191-char
    # key would


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
    assert inputs["_event"] == {"kind": "request", "name": None, "fields": {},
                                "is": {"request": True, "ask": False, "schedule": False,
                                       "signal": False, "idle": False, "custom": False}}
    assert inputs["_resident"] == {"state": {"mood": "calm"},
                                   "meta": {"turns": 4, "created_at": db.clock().isoformat(),
                                            "last_turn_at": db.clock().isoformat(), "idle_streak": 2}}



def test_inputs_merge_event_fields_across_the_batch(db):
    resident = _ensure(db)
    _add_events(db, resident.id, ["request", "request", "request"])
    with db.engine.begin() as conn:
        for seq, source in ((1, {"delivery_id": "d-1", "fields": {"number": 7, "sha": "old"}}),
                            (2, {"delivery_id": "d-2", "fields": {"sha": "new", "requester": "octo"}}),
                            (3, None)):  # a legacy row with no fields gives nothing
            conn.execute(update(resident_events).where(resident_events.c.resident_id == resident.id,
                                                       resident_events.c.seq == seq).values(source=source))
    inputs = _job(db, _wake(db, resident.id)).inputs
    assert inputs["event_seq_to"] == 3
    assert inputs["_event"]["fields"] == {"number": 7, "sha": "new", "requester": "octo"}  # newest wins per key

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


def test_create_route_rejects_gated_handler(db, keyring):
    client = make_client(db.engine, keyring, db.clock)
    user_id, _ = add_user(db.engine, keyring, db.clock, "root", is_admin=True)
    csrf = sign_in_as(client, db.engine, keyring, db.clock, user_id)
    with db.engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(workflows)).scalar_one()
    created = client.post("/api/workflows", json={"name": "gated_bot", "scope_kind": "project",
                                                  "scope_id": db.project, "body": GATED_SELF},
                          headers=mutation(csrf))
    assert created.status_code == 400
    assert "human gate" in created.text  # the save path's stall guard, on version 1 too
    with db.engine.connect() as conn:  # the refusal rolled the create back — no row survives
        assert conn.execute(select(func.count()).select_from(workflows)).scalar_one() == before


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
    with db.engine.connect() as conn:  # the auto-resume is a governed act, audited like the rest
        resumed = conn.execute(select(audit_rows.c.target_id, audit_rows.c.detail).where(
            audit_rows.c.action == "resident.resumed")).all()
    assert resumed == [(resident.id, {"reason": "budget"})]


# ── The idle loop: scheduled idle turns, backoff, daily caps, one claim per turn ──


def test_idle_not_queued_before_next_idle_at(db):
    resident = _ensure(db)
    assert _resident(db, resident.id).next_idle_at == db.clock() + timedelta(minutes=30)  # ensure scheduled it
    db.clock.advance(minutes=29)
    residents.sweep(db.engine, db.clock())  # not due yet: the sweep queues nothing
    row = _resident(db, resident.id)
    assert row.next_idle_at == db.clock() + timedelta(minutes=1)  # the schedule stands untouched
    assert row.active_job_id is None
    assert _events(db, resident.id) == []


def test_idle_backs_off_on_nothing_to_do(db):
    resident = _ensure(db)
    db.clock.advance(minutes=30)
    residents.sweep(db.engine, db.clock())  # the first idle turn is due
    job_id = _resident(db, resident.id).active_job_id
    assert _job(db, job_id).inputs["kind"] == "idle"
    _stage(db, resident.id, {"idle_nothing_to_do": True})
    db.clock.advance(minutes=5)
    _finish(db, job_id)
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert row.idle_streak == 1  # nothing to do: the streak starts
    assert row.next_idle_at == db.clock() + timedelta(minutes=60)  # after × 2¹: the gap doubles
    db.clock.advance(minutes=60)
    residents.sweep(db.engine, db.clock())  # the second idle turn comes due at the doubled gap
    job_id = _resident(db, resident.id).active_job_id
    _stage(db, resident.id, {"idle_nothing_to_do": True})
    db.clock.advance(minutes=5)
    _finish(db, job_id)
    assert _settle(db) == [resident.id]
    row = _resident(db, resident.id)
    assert row.idle_streak == 2
    assert row.next_idle_at == db.clock() + timedelta(minutes=120)  # after × 2²: twice the doubled gap


def test_real_event_resets_backoff(db):
    resident = _ensure(db)
    db.clock.advance(minutes=30)
    residents.sweep(db.engine, db.clock())  # the first idle turn, with nothing to do
    job_id = _resident(db, resident.id).active_job_id
    _stage(db, resident.id, {"idle_nothing_to_do": True})
    db.clock.advance(minutes=5)
    _finish(db, job_id)
    _settle(db)
    assert _resident(db, resident.id).next_idle_at == db.clock() + timedelta(minutes=60)
    db.clock.advance(minutes=10)  # a real event lands mid-backoff
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "request", now=db.clock())
    row = _resident(db, resident.id)
    assert (row.idle_streak, row.next_idle_at) == (0, None)  # the streak and its schedule died with it
    job_id = row.active_job_id
    assert _job(db, job_id).inputs["kind"] == "request"
    db.clock.advance(minutes=5)
    _finish(db, job_id)
    _settle(db)
    # the next idle turn goes back to the base gap: 30m, not the doubled 60m
    assert _resident(db, resident.id).next_idle_at == db.clock() + timedelta(minutes=30)


def test_idle_stops_at_daily_budget(db):
    body = ("name: bot\nresident:\n  idle: {after: 10m, backoff_max: 12h, "
            "budget_usd_per_day: 1.0, max_turns_per_day: 2}\nstages: []\n")
    _version(db, db.workflow_id, body, 2)
    spent = _ensure(db)
    capped = _ensure(db, instance_key="acme")
    with db.engine.begin() as conn:  # today's idle spend and turn count already at their caps
        conn.execute(update(resident_rows).where(resident_rows.c.id == spent.id).values(
            spent_usd_day=1_000_000, spent_day=db.clock().date().isoformat()))
        conn.execute(update(resident_rows).where(resident_rows.c.id == capped.id).values(
            idle_turns_day=2, spent_day=db.clock().date().isoformat()))
    db.clock.advance(minutes=10)
    residents.sweep(db.engine, db.clock())  # due, but the day's caps are spent
    tomorrow = datetime(2026, 10, 24, tzinfo=UTC)
    for row_id in (spent.id, capped.id):
        row = _resident(db, row_id)
        assert row.active_job_id is None
        assert row.next_idle_at == tomorrow  # pushed past the rollover, not dropped
        assert _events(db, row_id) == []
    db.clock.advance(hours=12)  # the day rolls over, and the caps with it
    residents.sweep(db.engine, db.clock())
    assert _resident(db, spent.id).active_job_id is not None  # the idle turn finally runs
    assert _job(db, _resident(db, spent.id).active_job_id).inputs["kind"] == "idle"


def test_two_servers_sweep_one_idle_turn(db):
    resident = _ensure(db)
    with db.engine.begin() as conn:  # the idle turn came due between sweeps
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id)
                     .values(next_idle_at=db.clock() - timedelta(minutes=1)))
    now = db.clock()
    with db.engine.begin() as conn:  # server one claims the due idle turn
        assert residents._claim_idle(conn, resident.id, now) is True
    with db.engine.begin() as conn:  # server two races the same row: the claim is already taken
        assert residents._claim_idle(conn, resident.id, now) is False
    assert _resident(db, resident.id).next_idle_at is None


def test_idle_event_does_not_touch_lifetime_counters(db):
    body = ("name: bot\nresident:\n  on:\n    request: {workflow: self}\n"
            "  idle: {after: 30m, backoff_max: 12h}\n"
            "  lifetime: {close_after_idle: 30d}\nstages: []\n")
    _version(db, db.workflow_id, body, 2)
    resident = _ensure(db)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "request", now=db.clock())  # one real event
    _finish(db, _resident(db, resident.id).active_job_id)
    _settle(db)
    last_real = _resident(db, resident.id).last_event_at
    with db.engine.begin() as conn:  # the idle loop has backed off since
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            idle_streak=3, next_idle_at=db.clock() + timedelta(minutes=30)))
    db.clock.advance(minutes=30)
    residents.sweep(db.engine, db.clock())  # the idle turn is due; the scheduler queues it
    row = _resident(db, resident.id)
    assert row.last_event_at == last_real  # the idle event bumped nothing
    assert row.idle_streak == 3  # and reset nothing
    job_id = row.active_job_id
    job = _job(db, job_id)
    assert (job.inputs["kind"], job.inputs["_resident"]["meta"]["idle_streak"]) == ("idle", 3)
    event = _events(db, resident.id)[-1]  # the request's event is handled history; the idle event waits
    assert (event.kind, event.payload["idle_streak"],
            event.payload["since_last_event"], event.payload["inbox_depth"]) == (
        "idle", 3, last_real.isoformat(), 0)
    db.clock.advance(days=31)  # the idle cadence stays healthy; no real event ever comes
    _finish(db, job_id)
    assert _settle(db) == [resident.id]
    db.clock.advance(minutes=30)  # the next idle turn comes due — and finds the instance outlived
    residents.sweep(db.engine, db.clock())
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason) == ("closed", "lifetime_idle")  # lifetime measures real events only


def test_housekeeping_example_spec_validates():
    example = Path(__file__).parent.parent.parent / "examples" / "residents" / "housekeeping.yml"
    assert example.exists(), "The starter resident spec is missing"
    spec = load_spec(example)
    assert spec.resident is not None and spec.resident.idle is not None
    assert spec.resident.state.publish == ["metrics"]  # read serves the metrics the idle turn recomputes
    assert [stage.adapter for stage in spec.stages] == ["resident/state.patch"]
    assert not [e for e in validate_spec(spec, strict=False) if e.severity == "error"]


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
    "resident.emit": {"resident": "answer", "key": "acme", "kind": "signal"},
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


# A spec whose matching signal closes the instance without spending a turn (the close_on grammar).
CLOSEBOT = """\
name: bot
resident:
  on:
    request: {workflow: self}
    signal: {workflow: self}
  close_on: {signal: pr_merged}
stages: []
"""

# The workflow an emit targets, with a handler so the emitted event turns.
EMITBOT = """\
name: answer
resident:
  on:
    signal: {workflow: self}
stages: []
"""


def _keyed_turn(db, instance_key):
    """A second instance of the bot workflow with a running request turn, as _tooled builds the first."""
    resident = _ensure(db, instance_key=instance_key)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "request", now=db.clock())
    job_id = _resident(db, resident.id).active_job_id
    run_id = new_id()
    with db.engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=db.team, status="running", started_at=db.clock()))
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(run_id=run_id))
    return resident, job_id, run_id


def _emit_audits(db):
    with db.engine.connect() as conn:
        return conn.execute(select(audit_rows.c.target_id, audit_rows.c.detail).where(
            audit_rows.c.action == "resident.emit")).all()


def test_emit_creates_keyed_target_instance(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    _version(db, db.answer_id, EMITBOT, 2)
    result = _call(db, run_id, job, "resident.emit",
                   {"resident": "answer", "key": "acme", "kind": "signal", "payload": {"pr": 17}})
    with db.engine.connect() as conn:
        [target] = conn.execute(select(resident_rows).where(resident_rows.c.workflow_id == db.answer_id)).all()
    assert result == {"emitted": True, "target_resident_id": target.id, "event_seq": 1, "chain": 1}
    assert (target.instance_key, target.workflow_version, target.state) == ("acme", 2, "idle")
    [event] = _events(db, target.id)
    assert (event.kind, event.payload) == ("signal", {"pr": 17})
    assert event.source == {"emitted_by": run_id, "chain": 1}  # the causal chain, recorded in the source
    assert _resident(db, target.id).active_job_id is not None  # the emitted event turned the target
    assert _resident(db, resident.id).emits_day == 1  # the emitting instance spent one of today's emits
    assert _emit_audits(db) == [(target.id, {"seq": 1, "kind": "signal", "name": None, "chain": 1})]
    with pytest.raises(residents.ResidentError) as missing:  # a name no workflow carries reaches nothing
        _call(db, run_id, job, "resident.emit", {"resident": "nowhere", "key": "", "kind": "signal"})
    assert missing.value.status == 404


def test_emit_caps_chain_depth(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    with db.engine.begin() as conn:  # this turn's own event arrived as the chain's fifth link
        conn.execute(update(resident_events).where(resident_events.c.resident_id == resident.id)
                     .values(source={"emitted_by": new_id(), "chain": residents.EMIT_CHAIN_MAX}))
    with pytest.raises(residents.ResidentError) as raised:
        _call(db, run_id, job, "resident.emit", {"resident": "answer", "key": "acme", "kind": "signal"})
    assert raised.value.status == 429  # the sixth link is refused; five is the cap
    assert _emit_audits(db) == [(resident.id, {"refused": "chain", "target": "answer",
                                               "key": "acme", "chain": residents.EMIT_CHAIN_MAX + 1})]
    with db.engine.connect() as conn:
        assert conn.execute(select(resident_rows.c.id).where(
            resident_rows.c.workflow_id == db.answer_id)).all() == []  # nothing was ensured
    assert len(_events(db, resident.id)) == 1  # and nothing enqueued anywhere
    assert _resident(db, resident.id).emits_day == 0  # a refused emit spends no cap


def test_emit_run_and_day_caps(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    with db.engine.begin() as conn:  # the broker's records of this run's ten prior emits
        for _ in range(residents.EMIT_CAP_RUN):
            conn.execute(insert(tool_calls).values(id=new_id(), run_id=run_id, team_id=db.team,
                                                   step_id="s", tool="resident.emit", mode="live",
                                                   outcome="ok", created_at=db.clock()))
    with pytest.raises(residents.ResidentError) as raised:
        _call(db, run_id, job, "resident.emit", {"resident": "answer", "key": "acme", "kind": "signal"})
    assert raised.value.status == 429  # one run's fan-out stops at ten
    assert _emit_audits(db) == [(resident.id, {"refused": "run_cap", "target": "answer", "key": "acme"})]
    loud, loud_job, loud_run = _keyed_turn(db, "fan")
    with db.engine.begin() as conn:  # today's fifty emits already spent
        conn.execute(update(resident_rows).where(resident_rows.c.id == loud.id).values(
            emits_day=residents.EMIT_CAP_DAY, spent_day=db.clock().date().isoformat()))
    with pytest.raises(residents.ResidentError) as capped:
        _call(db, loud_run, _job(db, loud_job), "resident.emit",
              {"resident": "answer", "key": "acme", "kind": "signal"})
    assert capped.value.status == 429  # and one instance's daily fan-out stops at fifty
    assert _emit_audits(db) == [
        (resident.id, {"refused": "run_cap", "target": "answer", "key": "acme"}),
        (loud.id, {"refused": "day_cap", "target": "answer", "key": "acme"})]
    with db.engine.connect() as conn:
        assert conn.execute(select(resident_rows.c.id).where(
            resident_rows.c.workflow_id == db.answer_id)).all() == []


def test_close_on_closes_without_a_turn(db):
    _version(db, db.workflow_id, CLOSEBOT, 2)
    resident = _ensure(db)
    with db.engine.begin() as conn:
        seq = residents.enqueue(conn, resident.id, "signal", name="pr_merged", payload={"pr": 17},
                                now=db.clock())
    assert seq == 1
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason, row.closed_at) == ("closed", "close_on", db.clock())
    assert _jobs_count(db) == 0  # fleet closures spend nothing: no turn was queued
    [event] = _events(db, resident.id)
    assert event.handled is True  # the closing event lands handled; the timeline keeps it
    keyed = _ensure(db, instance_key="fan")
    with db.engine.begin() as conn:  # a custom kind closes on its bare name too
        residents.enqueue(conn, keyed.id, "custom.pr_merged", now=db.clock())
    row = _resident(db, keyed.id)
    assert (row.state, row.close_reason) == ("closed", "close_on")
    with db.engine.connect() as conn:
        closed = conn.execute(select(audit_rows.c.target_id, audit_rows.c.detail).where(
            audit_rows.c.action == "resident.close")).all()
    assert closed == [(resident.id, {"reason": "close_on"}), (keyed.id, {"reason": "close_on"})]


def test_close_on_non_matching_signal_stays_open(db):
    _version(db, db.workflow_id, CLOSEBOT, 2)
    resident = _ensure(db)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "signal", name="deploy_failed", now=db.clock())
    row = _resident(db, resident.id)
    assert (row.state, row.closed_at) == ("idle", None)  # another name is just a signal
    assert _jobs_count(db) == 1  # it turns like any other
    assert _events(db, resident.id)[0].handled is False
    with db.engine.connect() as conn:
        assert conn.execute(select(func.count()).where(
            audit_rows.c.action == "resident.close")).scalar_one() == 0



DONEBOT = """\
name: bot
resident:
  on:
    request: {workflow: self}
  close_on: {signal: done}
stages: []
"""


def test_close_on_asks_the_running_turn_to_stop(db):
    _version(db, db.workflow_id, DONEBOT, 2)
    resident = _ensure(db)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "request", now=db.clock())
    job_id = _resident(db, resident.id).active_job_id
    with db.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="running"))
    db.clock.advance(minutes=5)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "signal", name="done", now=db.clock())
    row = _resident(db, resident.id)
    assert (row.state, row.close_reason) == ("closed", "close_on")
    assert _job(db, job_id).cancel_requested_at == db.clock()
    assert _jobs_count(db) == 1  # the close queued nothing new


def _closing_turn(db, instance_key, **job):
    resident = _ensure(db, instance_key=instance_key)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "request", now=db.clock())
    job_id = _resident(db, resident.id).active_job_id
    if job:
        with db.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(**job))
    db.clock.advance(minutes=5)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "signal", name="done", now=db.clock())
    assert _resident(db, resident.id).state == "closed"
    return _job(db, job_id)


def test_close_on_ends_a_queued_turn(db):
    _version(db, db.workflow_id, DONEBOT, 2)
    job = _closing_turn(db, "queued")
    assert (job.status, job.finished_at) == ("superseded", db.clock())
    with db.engine.connect() as conn:
        assert job.id not in worker.claimable(conn, capabilities=None, now=db.clock())


def test_close_on_leaves_an_earlier_cancel_and_a_finished_turn_alone(db):
    _version(db, db.workflow_id, DONEBOT, 2)
    earlier = db.clock() - timedelta(hours=1)
    job = _closing_turn(db, "asked", status="running", cancel_requested_at=earlier)
    assert (job.status, job.cancel_requested_at) == ("running", earlier)
    done = _closing_turn(db, "done", status="succeeded")
    assert (done.status, done.cancel_requested_at, done.finished_at) == ("succeeded", None, None)


def test_a_turn_superseded_by_its_close_costs_no_attempt(db):
    _version(db, db.workflow_id, DONEBOT, 2)
    job = _closing_turn(db, "queued")
    resident_id = job.resident_id
    assert _settle(db) == [resident_id]
    assert [e.attempts for e in _events(db, resident_id)] == [0, 0]
    assert _resident(db, resident_id).active_job_id is None
    with db.engine.connect() as conn:
        assert conn.execute(select(func.count()).where(
            audit_rows.c.action == "resident.event_poison")).scalar_one() == 0


SELFCLOSE = TOOLBOT.replace("stages: []", "  close_on: {signal: done}\nstages: []")


def test_a_turn_that_emits_its_own_close_keeps_its_work(db):
    _version(db, db.workflow_id, SELFCLOSE, 2)
    resident = _ensure(db)
    with db.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "request", now=db.clock())
    job_id = _resident(db, resident.id).active_job_id
    run_id = new_id()
    with db.engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=db.team, status="running", started_at=db.clock()))
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(run_id=run_id, status="running"))
    _call(db, run_id, _job(db, job_id), "resident.state.set", {"state": {"mood": "done"}})
    _call(db, run_id, _job(db, job_id), "resident.emit",
          {"resident": "bot", "key": "", "kind": "signal", "name": "done"})
    assert (_resident(db, resident.id).state, _resident(db, resident.id).close_reason) == ("closed", "close_on")
    assert _job(db, job_id).cancel_requested_at is None  # the emitting turn isn't cancelled by its own close
    with db.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(status="succeeded"))
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="succeeded"))
    assert _settle(db) == [resident.id]
    assert _resident(db, resident.id).data == {"mood": "done"}

def test_emit_refuses_names_outside_the_grammar(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    _version(db, db.answer_id, EMITBOT, 2)
    for bad in (  # the router's own grammar, on the fan-out path too
        "x" * 100,          # wider than resident_events.name stores
        "Not_Lower",        # outside the custom-name vocabulary
        "custom.deploy",    # the prefixed kind itself, never a bare name
    ):
        with pytest.raises(residents.ResidentError) as raised:
            _call(db, run_id, job, "resident.emit",
                  {"resident": "answer", "key": "acme", "kind": "signal", "name": bad})
        assert raised.value.status == 400
    with pytest.raises(residents.ResidentError) as suffixed:  # a custom.<name> kind carries it too
        _call(db, run_id, job, "resident.emit",
              {"resident": "answer", "key": "acme", "kind": f"custom.{'x' * 100}"})
    assert suffixed.value.status == 400
    with db.engine.connect() as conn:  # every refusal is validation, not a spend: nothing written
        assert conn.execute(select(resident_rows.c.id).where(
            resident_rows.c.workflow_id == db.answer_id)).all() == []
        assert conn.execute(select(func.count()).where(
            audit_rows.c.action == "resident.emit")).scalar_one() == 0
    assert _resident(db, resident.id).emits_day == 0


def test_emit_from_a_dry_turn_is_dry(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    _version(db, db.answer_id, EMITBOT, 2)
    with db.engine.begin() as conn:  # the turn's own event arrived as a dry delivery
        conn.execute(update(resident_events).where(resident_events.c.resident_id == resident.id)
                     .values(dry_run=True))
    result = _call(db, run_id, job, "resident.emit", {"resident": "answer", "key": "acme",
                                                     "kind": "signal", "payload": {"pr": 17}})
    with db.engine.connect() as conn:
        [target] = conn.execute(select(resident_rows).where(
            resident_rows.c.workflow_id == db.answer_id)).all()
    [event] = _events(db, target.id)
    assert event.dry_run is True  # the fan-out carries the turn's dry flag onto the target
    assert result["emitted"] is True
    assert _job(db, _resident(db, target.id).active_job_id).dry_run is True  # ... so the turn it
    # wakes spends nothing real — the same carry a dry webhook rule gets (Task 12)


def test_emit_refused_from_an_idle_turn(db):
    resident, job_id, run_id = _tooled(db)
    job = _job(db, job_id)
    _version(db, db.answer_id, EMITBOT, 2)
    with db.engine.begin() as conn:  # the turn answers an idle event, not a real one
        conn.execute(update(resident_events).where(resident_events.c.resident_id == resident.id)
                     .values(kind="idle"))
    with pytest.raises(residents.ResidentError) as raised:
        _call(db, run_id, job, "resident.emit", {"resident": "answer", "key": "acme", "kind": "signal"})
    assert raised.value.status == 400  # idle turns ask; only event turns act outward (decision 2)
    with db.engine.connect() as conn:
        assert conn.execute(select(resident_rows.c.id).where(
            resident_rows.c.workflow_id == db.answer_id)).all() == []  # nothing ensured
        assert conn.execute(select(func.count()).where(
            audit_rows.c.action == "resident.emit")).scalar_one() == 0  # nothing audited
    assert _resident(db, resident.id).emits_day == 0  # and no cap was spent


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


# The resident API: read without a run, ask that turns, signals, and the admin acts.

APIBOT = """\
name: bot
resident:
  on:
    request: {workflow: self}
    ask: {workflow: answer}
    signal: {workflow: self}
  state:
    publish: [metrics]
stages: []
"""

# A later version whose schema wants a key the instance's data doesn't have.
SCHEMA_BOT = """\
name: bot
resident:
  on:
    request: {workflow: self}
    ask: {workflow: answer}
    signal: {workflow: self}
  state:
    schema:
      type: object
      required: [mood]
      properties:
        mood: {type: string}
        metrics: {type: object}
    publish: [metrics]
stages: []
"""


@pytest.fixture
def web(sqlite_engine, keyring):
    clock = Clock()
    team, project = new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="own", created_at=clock()))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=clock()))
        conn.execute(insert(settings).values(team_id=None, key="worker.enabled", value=False))
        conn.execute(insert(settings).values(key=residents.ENABLED_KEY, value=True))
    workflow_id, answer_id = new_id(), new_id()
    with sqlite_engine.begin() as conn:
        for id_, name, body in ((workflow_id, "bot", APIBOT), (answer_id, "answer", ANSWER)):
            conn.execute(insert(workflows).values(id=id_, name=name, current_version=1, team_id=team,
                                                 project_id=project, scope=project, created_at=clock(),
                                                 updated_at=clock()))
            defs._add_version(conn, defs.WORKFLOW, id_, 1, body, "seed", None, clock())
    users_ = {}
    for name, kind, scope, role in (("lead", "project", project, "project_lead"),
                                    ("viewer", "project", project, "viewer")):
        users_[name] = add_user(sqlite_engine, keyring, clock, name)[0]
        add_membership(sqlite_engine, clock, users_[name], kind, scope, role)
    users_["admin"] = add_user(sqlite_engine, keyring, clock, "admin", is_admin=True)[0]
    users_["stranger"] = add_user(sqlite_engine, keyring, clock, "stranger")[0]
    client = make_client(sqlite_engine, keyring, clock, require_totp=False)
    return SimpleNamespace(engine=sqlite_engine, keyring=keyring, clock=clock, team=team, project=project,
                           workflow_id=workflow_id, answer_id=answer_id, users=users_, client=client)


def _as(web, user):
    return mutation(sign_in_as(web.client, web.engine, web.keyring, web.clock, web.users[user]))


def _web_resident(web, *, instance_key="", data=None, **values):
    with web.engine.begin() as conn:
        row = residents.ensure(conn, team_id=web.team, project_id=web.project,
                               workflow_id=web.workflow_id, instance_key=instance_key, now=web.clock())
        updates = {"data": data, "data_version": 1 if data is not None else 0, **values}
        conn.execute(update(resident_rows).where(resident_rows.c.id == row.id).values(**updates))
    return row


def _web_find(web, instance_key=""):
    with web.engine.connect() as conn:
        return conn.execute(select(resident_rows).where(
            resident_rows.c.workflow_id == web.workflow_id,
            resident_rows.c.instance_key == instance_key)).one()


def _web_row(web, resident_id):
    with web.engine.connect() as conn:
        return conn.execute(select(resident_rows).where(resident_rows.c.id == resident_id)).one()


def _web_job(web, job_id):
    with web.engine.connect() as conn:
        return conn.execute(select(jobs).where(jobs.c.id == job_id)).one()


def _counts(web):
    """The jobs and runs the invariant tests compare: a read must move neither."""
    with web.engine.connect() as conn:
        return (conn.execute(select(func.count()).select_from(jobs)).scalar_one(),
                conn.execute(select(func.count()).select_from(runs)).scalar_one())


def _audited(web, action, actor):
    with web.engine.connect() as conn:
        return conn.execute(select(audit_rows.c.target_id).where(
            audit_rows.c.action == action, audit_rows.c.actor_id == web.users[actor])).all()


def test_read_serves_published_metrics_and_spends_nothing(web):
    _web_resident(web, data={"metrics": {"waiting": 3}, "notes": "hidden"},
                  turns=2, last_turn_at=web.clock(), spent_usd_day=1_500_000)
    before = _counts(web)
    read = web.client.get(f"/api/residents/{web.workflow_id}/read", headers=_as(web, "lead"))
    assert read.status_code == 200, read.text
    shown = read.json()["resident"]
    assert shown["data"] == {"metrics": {"waiting": 3}}  # only the published key
    assert (shown["data_version"], shown["turns"], shown["state"], shown["inbox"]) == (1, 2, "idle", 0)
    assert shown["last_turn_at"] == web.clock().isoformat().replace("+00:00", "Z")
    assert shown["spent_usd_today"] == 1.5  # micro-dollars, served as dollars
    filtered = web.client.get(f"/api/residents/{web.workflow_id}/read",
                              params={"keys": "metrics,notes"}).json()["resident"]["data"]
    assert filtered == {"metrics": {"waiting": 3}}  # the filter can't reach past publish
    assert web.client.get(f"/api/residents/{web.workflow_id}/read",
                          headers=_as(web, "viewer")).status_code == 200  # read is a view permission
    assert web.client.get(f"/api/residents/{web.workflow_id}/read",
                          headers=_as(web, "stranger")).status_code == 404
    assert _counts(web) == before  # no job, no run, only rows read
    assert _audited(web, "resident.read", "lead")


def test_a_keyed_instance_is_addressed_by_its_key(web):
    keyed = _web_resident(web, instance_key="acme", data={"metrics": {"open": 1}})
    _web_resident(web)  # the singleton beside it
    lead = _as(web, "lead")
    listed = web.client.get(f"/api/residents/{web.workflow_id}", headers=lead).json()["residents"]
    assert {r["instance_key"] for r in listed} == {"", "acme"}
    read = web.client.get(f"/api/residents/{web.workflow_id}/acme/read", headers=lead).json()["resident"]
    assert (read["id"], read["data"]) == (keyed.id, {"metrics": {"open": 1}})
    aliased = web.client.get(f"/api/residents/{web.workflow_id}/read",
                             params={"instance_key": "acme"}, headers=lead).json()["resident"]
    assert aliased["instance_key"] == "" and aliased["id"] != keyed.id  # the singleton path is pinned
    assert web.client.get(f"/api/residents/{web.workflow_id}/nobody/read",
                         headers=lead).status_code == 404  # reading never creates
    assert web.client.get(f"/api/residents/{web.workflow_id}",
                          headers=_as(web, "stranger")).status_code == 404


def test_ask_async_returns_the_event_and_turns_the_instance(web):
    lead = _as(web, "lead")
    posted = web.client.post(f"/api/residents/{web.workflow_id}/ask",
                             json={"question": "how many are waiting?"}, headers=lead)
    assert posted.status_code == 200, posted.text
    seq, job_id = posted.json()["event_seq"], posted.json()["job_id"]
    resident = _web_find(web)  # the ask created the singleton
    assert (seq, resident.active_job_id) == (1, job_id)  # the ask's own event, the turn it woke
    job = _web_job(web, job_id)
    assert (job.kind, job.resident_id, job.inputs["kind"]) == \
        (residents.RESIDENT_JOB_KIND, resident.id, "ask")
    [event] = _events(web, resident.id)
    assert (event.kind, event.payload) == ("ask", {"text": "how many are waiting?"})
    assert event.reply_to == {"channel": "api", "user_id": web.users["lead"]}
    assert web.client.post(f"/api/residents/{web.workflow_id}/ask", json={"question": "mine?"},
                           headers=_as(web, "viewer")).status_code == 403  # asking is a run permission
    assert web.client.post(f"/api/residents/{web.workflow_id}/ask", json={"question": "mine?"},
                          headers=_as(web, "stranger")).status_code == 404
    assert _audited(web, "resident.ask", "lead")


def _reply_later(web, instance_key, seq, text):
    """A turn's resident.reply, from a thread: the ask is waiting in another one."""
    def run():
        for _ in range(1000):
            with contextlib.suppress(OperationalError), web.engine.begin() as conn:  # writers serialize
                rid = conn.execute(select(resident_rows.c.id).where(
                    resident_rows.c.workflow_id == web.workflow_id,
                    resident_rows.c.instance_key == instance_key)).scalar()
                if rid is not None:
                    wrote = conn.execute(update(resident_events).where(
                        resident_events.c.resident_id == rid, resident_events.c.seq == seq,
                        resident_events.c.reply.is_(None)
                    ).values(reply={"text": text, "at": web.clock().isoformat()})).rowcount
                    if wrote:
                        return
            time.sleep(0.01)

    threading.Thread(target=run, daemon=True).start()


def test_ask_sync_waits_for_the_turns_reply(web):
    lead = _as(web, "lead")
    _reply_later(web, "", 1, "42")
    answered = web.client.post(f"/api/residents/{web.workflow_id}/ask", params={"wait": 5},
                               json={"question": "how many?"}, headers=lead)
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"event_seq": 1, "reply": {"text": "42", "at": web.clock().isoformat()}}
    silent = web.client.post(f"/api/residents/{web.workflow_id}/ask", params={"wait": 1},
                             json={"question": "and now?"}, headers=lead)
    assert silent.status_code == 200, silent.text
    assert silent.json() == {"event_seq": 2, "reply": None}  # no answer came: poll the event


def test_signal_enqueues_plain_and_named_events(web):
    lead = _as(web, "lead")
    posted = web.client.post(f"/api/residents/{web.workflow_id}/signal",
                             json={"payload": {"pr": 7}}, headers=lead)
    assert posted.status_code == 200, posted.text
    assert posted.json()["event_seq"] == 1
    named = web.client.post(f"/api/residents/{web.workflow_id}/signal",
                            json={"name": "pr-merged", "payload": {"pr": 7}}, headers=lead)
    assert named.status_code == 200, named.text
    resident = _web_resident(web)  # the signal created the singleton
    assert [(e.kind, e.name) for e in _events(web, resident.id)] == \
        [("signal", None), ("custom", "pr-merged")]
    for bad in ("custom.beep", "Beep", "no spaces", "b" * 65):
        refused = web.client.post(f"/api/residents/{web.workflow_id}/signal",
                                  json={"name": bad, "payload": {}}, headers=lead)
        assert refused.status_code == 400, bad  # a bare name only, in the on-key vocabulary
    assert web.client.post(f"/api/residents/{web.workflow_id}/signal", json={},
                           headers=_as(web, "viewer")).status_code == 403
    assert web.client.post(f"/api/residents/{web.workflow_id}/signal", json={},
                           headers=_as(web, "stranger")).status_code == 404
    assert _audited(web, "resident.signal", "lead")


def test_off_means_no_turns_not_even_rows(web):
    with web.engine.begin() as conn:
        conn.execute(update(settings).where(settings.c.key == residents.ENABLED_KEY).values(value=False))
    lead = _as(web, "lead")
    asked = web.client.post(f"/api/residents/{web.workflow_id}/ask",
                            json={"question": "anyone?"}, headers=lead)
    assert asked.status_code == 503
    assert web.client.post(f"/api/residents/{web.workflow_id}/ask", json={"question": "anyone?"},
                           headers=_as(web, "stranger")).status_code == 404  # RBAC first: no feature oracle
    assert web.client.post(f"/api/residents/{web.workflow_id}/signal",
                           json={"payload": {}}, headers=_as(web, "lead")).status_code == 503
    with web.engine.connect() as conn:  # the gate comes before the create
        assert conn.execute(select(func.count()).select_from(resident_rows)).scalar_one() == 0


def _residents_off(web):
    with web.engine.begin() as conn:
        conn.execute(update(settings).where(settings.c.key == residents.ENABLED_KEY).values(value=False))


def _jobs_total(web):
    with web.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(jobs)).scalar_one()


def test_resume_while_off_writes_the_row_but_wakes_nothing(web):
    resident = _web_resident(web)
    admin = _as(web, "admin")
    assert web.client.post(f"/api/residents/{web.workflow_id}/pause",
                           headers=admin).status_code == 200
    with web.engine.begin() as conn:  # an event waits out the pause
        residents.enqueue(conn, resident.id, "request", now=web.clock())
    _residents_off(web)
    resumed = web.client.post(f"/api/residents/{web.workflow_id}/resume", headers=admin)
    assert resumed.status_code == 200, resumed.text  # the row write is the act's own and stays;
    # the wake it would start is gated like every turn (the T5 ruling: no new turns while off)
    row = _web_row(web, resident.id)
    assert (row.state, row.active_job_id) == ("idle", None)
    assert _events(web, resident.id)[0].handled is False  # the event stays in the inbox
    assert _jobs_total(web) == 0
    assert _audited(web, "resident.resume", "admin")  # the act itself is still audited


def test_upgrade_while_off_repins_but_wakes_nothing(web):
    resident = _web_resident(web)
    _version(web, web.workflow_id, SCHEMA_BOT, 2)  # wants a mood the data doesn't have
    admin = _as(web, "admin")
    assert web.client.post(f"/api/residents/{web.workflow_id}/upgrade",
                           headers=admin).status_code == 200  # paused for the migrate, no turn
    with web.engine.begin() as conn:  # the migrate lands; an event waits meanwhile
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id)
                     .values(data={"mood": "calm", "metrics": {}}))
        residents.enqueue(conn, resident.id, "request", now=web.clock())
    _residents_off(web)
    upgraded = web.client.post(f"/api/residents/{web.workflow_id}/upgrade", headers=admin)
    assert upgraded.status_code == 200, upgraded.text
    row = _web_row(web, resident.id)
    assert (row.state, row.workflow_version) == ("idle", 2)  # re-pin and un-pause: rows, written
    assert row.active_job_id is None  # but the wake is gated: no new turns while off
    assert _events(web, resident.id)[0].handled is False  # the waiting event stays in the inbox
    assert _jobs_total(web) == 0


def test_an_over_length_key_answers_404_and_writes_nothing(web):
    lead = _as(web, "lead")
    for path, body in (("ask", {"question": "anyone?"}), ("signal", {"payload": {}})):
        posted = web.client.post(f"/api/residents/{web.workflow_id}/{'k' * (residents.KEY_MAX + 1)}/{path}",
                                 json=body, headers=lead)
        assert posted.status_code == 404, path  # an over-length key can name no instance
    with web.engine.connect() as conn:  # sqlite would have stored both: the guard refused, not the DB
        assert conn.execute(select(func.count()).select_from(resident_rows)).scalar_one() == 0
    fits = "k" * residents.KEY_MAX  # exactly the column's width still asks
    posted = web.client.post(f"/api/residents/{web.workflow_id}/{fits}/ask",
                             json={"question": "anyone?"}, headers=lead)
    assert posted.status_code == 200, posted.text
    with web.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(resident_rows)).scalar_one() == 1
    job_id = _web_find(web, instance_key=fits).active_job_id
    assert len(_web_job(web, job_id).thread) <= 191  # the MySQL matrix's regression guard: the
    # derived label is bounded even at the key's full width, or strict mode rolls the ask back
    _web_resident(web, data={"metrics": {"waiting": 0}})  # reading still works when off
    assert web.client.get(f"/api/residents/{web.workflow_id}/read",
                          headers=lead).status_code == 200
    assert web.client.get(f"/api/residents/{web.workflow_id}/events",
                          headers=lead).status_code == 200
    paused = web.client.post(f"/api/residents/{web.workflow_id}/pause", headers=_as(web, "admin"))
    assert paused.status_code == 200  # the admin row-write stays ungated


def test_the_events_timeline_and_one_event(web):
    resident = _web_resident(web, data={"metrics": {"seen": 1}})
    with web.engine.begin() as conn:
        residents.enqueue(conn, resident.id, "ask", payload={"text": "hi"},
                           reply_to={"channel": "api", "user_id": "u1"}, now=web.clock())
        residents.enqueue(conn, resident.id, "signal", payload={"pr": 7}, now=web.clock())
        conn.execute(update(resident_events).where(resident_events.c.resident_id == resident.id,
                                                   resident_events.c.seq == 1)
                     .values(handled=True, attempts=2,
                             reply={"text": "hi yourself", "at": web.clock().isoformat()}))
    lead = _as(web, "lead")
    listed = web.client.get(f"/api/residents/{web.workflow_id}/events", headers=lead).json()["events"]
    assert [e["seq"] for e in listed] == [2, 1]  # newest first
    waiting = web.client.get(f"/api/residents/{web.workflow_id}/events",
                             params={"unhandled": 1}, headers=lead).json()["events"]
    assert [e["seq"] for e in waiting] == [2]  # the inbox, oldest first
    one = web.client.get(f"/api/residents/{web.workflow_id}/events/1", headers=lead).json()["event"]
    assert (one["kind"], one["attempts"], one["reply"]["text"]) == ("ask", 2, "hi yourself")
    assert one["payload"] == {"text": "hi"} and one["handled"] is True
    assert web.client.get(f"/api/residents/{web.workflow_id}/events/9",
                          headers=lead).status_code == 404
    assert web.client.get(f"/api/residents/{web.workflow_id}/events",
                          headers=_as(web, "stranger")).status_code == 404
    assert _audited(web, "resident.read", "lead")


def test_admin_pause_resume_and_close(web):
    resident = _web_resident(web)
    assert web.client.post(f"/api/residents/{web.workflow_id}/pause",
                           headers=_as(web, "lead")).status_code == 403  # admin acts are admin-only
    paused = web.client.post(f"/api/residents/{web.workflow_id}/pause", headers=_as(web, "admin"))
    assert paused.status_code == 200, paused.text
    assert paused.json()["resident"]["state"] == "paused"
    with web.engine.begin() as conn:  # a paused instance keeps its events for the resume
        residents.enqueue(conn, resident.id, "request", now=web.clock())
    resumed = web.client.post(f"/api/residents/{web.workflow_id}/resume", headers=_as(web, "admin"))
    assert resumed.status_code == 200, resumed.text
    row = _web_row(web, resident.id)
    assert (row.state, row.close_reason) == ("idle", None)
    assert row.active_job_id is not None  # resumed, then woken: the waiting event turns
    assert web.client.post(f"/api/residents/{web.workflow_id}/resume",
                           headers=_as(web, "admin")).status_code == 409  # nothing is paused
    too_long = web.client.post(f"/api/residents/{web.workflow_id}/close",
                               json={"reason": "r" * 33}, headers=_as(web, "admin"))
    assert too_long.status_code == 422, too_long.text  # a 32-char column: refuse, don't truncate
    assert _web_row(web, resident.id).state == "idle"  # nothing was written
    closed = web.client.post(f"/api/residents/{web.workflow_id}/close",
                             json={"reason": "all done"}, headers=_as(web, "admin"))
    assert closed.status_code == 200, closed.text
    row = _web_row(web, resident.id)
    assert (row.state, row.close_reason, row.closed_at) == ("closed", "all done", web.clock())
    assert web.client.post(f"/api/residents/{web.workflow_id}/close",
                          json={"reason": "again"}, headers=_as(web, "admin")).status_code == 409
    assert web.client.post(f"/api/residents/{web.workflow_id}/ask",
                           json={"question": "still there?"},
                           headers=_as(web, "lead")).status_code == 409
    for action in ("pause", "resume", "close"):
        assert _audited(web, f"resident.{action}", "admin")


def test_upgrade_repins_or_pauses_on_schema(web):
    resident = _web_resident(web, data={"metrics": {"waiting": 0}})
    _version(web, web.workflow_id, SCHEMA_BOT, 2)  # wants a mood the data doesn't have
    assert web.client.post(f"/api/residents/{web.workflow_id}/upgrade",
                           headers=_as(web, "lead")).status_code == 403
    admin = _as(web, "admin")
    refused = web.client.post(f"/api/residents/{web.workflow_id}/upgrade", headers=admin)
    assert refused.status_code == 200, refused.text
    shown = refused.json()["resident"]
    assert (shown["state"], shown["close_reason"], shown["workflow_version"]) == \
        ("paused", "state_schema", 2)  # paused for a migrate, not closed
    with web.engine.begin() as conn:  # the migrated state the new schema accepts
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id)
                     .values(data={"mood": "calm", "metrics": {"waiting": 0}}))
    upgraded = web.client.post(f"/api/residents/{web.workflow_id}/upgrade", headers=admin)
    assert upgraded.status_code == 200, upgraded.text
    shown = upgraded.json()["resident"]
    assert (shown["state"], shown["close_reason"], shown["workflow_version"]) == ("idle", None, 2)
    with web.engine.begin() as conn:  # a live turn: the pin never moves under it
        residents.enqueue(conn, resident.id, "request", now=web.clock())
    assert web.client.post(f"/api/residents/{web.workflow_id}/upgrade",
                          headers=admin).status_code == 409
    with web.engine.begin() as conn:  # the turn ended; the lock is free again
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(active_job_id=None))
    _version(web, web.workflow_id, "name: bot\nstages: []\n", 3)  # the block is gone
    assert web.client.post(f"/api/residents/{web.workflow_id}/upgrade",
                          headers=admin).status_code == 409
    assert len(_audited(web, "resident.upgrade", "admin")) == 2


# The UI: a Residents tab on the workflow page and an instance page, client-rendered ES modules
# served as static assets over the endpoints above. Two light tests hold the wiring in place:
# the module must be served, and the workflow page's tab set (definition.js) plus the view
# registry (app.js) must both reach it.

def test_resident_view_module_is_served(sqlite_engine, keyring):
    client = make_client(sqlite_engine, keyring, Clock())
    response = client.get("/static/views/residents.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")


def test_workflow_page_lists_the_resident_tab(sqlite_engine, keyring):
    client = make_client(sqlite_engine, keyring, Clock())
    definition = client.get("/static/views/definition.js")
    assert definition.status_code == 200
    assert "'Residents'" in definition.text  # the tab on the workflow page's tab row
    assert "views/residents.js" in client.get("/static/app.js").text  # the instance page's route

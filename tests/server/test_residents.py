"""Residents and their event queue: the tables, and the jobs column that links a run to one."""
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, inspect, select, update

from armature.server import residents
from armature.server.db import new_id
from armature.server.db.schema import (
    audit as audit_rows,
)
from armature.server.db.schema import (
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
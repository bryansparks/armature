"""The resident.* tools through the broker: the grant's tools, the pack's params, and the call bookkeeping."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, update

from armature.server import broker, defs, residents, worker
from armature.server.db import new_id
from armature.server.db.schema import (
    audit as audit_rows,
)
from armature.server.db.schema import (
    jobs,
    projects,
    runs,
    teams,
    tool_calls,
    workflows,
)
from armature.server.db.schema import (
    residents as resident_rows,
)
from tests.server.app_support import Clock

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
    max_bytes: 1024
    publish: [metrics]
stages: []
"""


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
        defs._add_version(conn, defs.WORKFLOW, workflow_id, 1, TOOLBOT, "seed", None, clock())
        resident = residents.ensure(conn, team_id=team, project_id=project, workflow_id=workflow_id,
                                    now=clock())
        residents.enqueue(conn, resident.id, "request", payload={"text": "hi"}, now=clock())
        job_id = conn.execute(select(resident_rows.c.active_job_id).where(
            resident_rows.c.id == resident.id)).scalar()
        run_id = new_id()
        conn.execute(insert(runs).values(id=run_id, team_id=team, status="running", started_at=clock()))
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(run_id=run_id))
    return SimpleNamespace(engine=engine, clock=clock, team=team, project=project,
                           workflow_id=workflow_id, resident_id=resident.id, job_id=job_id, run_id=run_id)


def _token(keyring, db, tools):
    return broker.encode(keyring, broker.Grant(db.run_id, "work", tuple(tools), db.clock() + timedelta(minutes=5)))


def _call(keyring, db, tool, params, tools=None):
    return broker.call_tool(db.engine, keyring, public_url="https://arm.test", token=_token(keyring, db, tools or (tool,)),
                            tool=tool, params=params, now=db.clock(), ip="127.0.0.1")


def _staged(db):
    with db.engine.connect() as conn:
        return conn.execute(select(resident_rows.c.staged).where(resident_rows.c.id == db.resident_id)).scalar()


def _calls(db):
    with db.engine.connect() as conn:
        return conn.execute(select(tool_calls.c.tool, tool_calls.c.outcome, tool_calls.c.target,
                                   tool_calls.c.mode, tool_calls.c.detail).order_by(
            tool_calls.c.created_at, tool_calls.c.id)).all()


def test_broker_runs_a_resident_tool_and_records_it(db, keyring):
    result = _call(keyring, db, "resident.state.set", {"state": {"mood": "calm"}})
    assert result == {"staged": True}
    assert _staged(db) == {"state": {"mood": "calm"}}  # the tool ran against the instance
    [recorded] = _calls(db)
    assert (recorded.tool, recorded.outcome, recorded.target, recorded.mode) == (
        "resident.state.set", "ok", db.resident_id, "live")  # the target is the resident, like a conversation
    with db.engine.connect() as conn:
        audited = conn.execute(select(audit_rows.c.action).where(audit_rows.c.target_id == db.run_id)).scalars().all()
    assert audited == ["tool.call"]


def test_broker_checks_the_grant_and_the_packs_params(db, keyring):
    with pytest.raises(broker.BrokerError) as uncovered:  # the grant names other tools
        _call(keyring, db, "resident.state.set", {"state": {"mood": "calm"}},
               tools=("resident.events",))
    assert uncovered.value.status == 403
    with pytest.raises(broker.BrokerError) as malformed:  # the pack's params schema refuses the shape
        _call(keyring, db, "resident.state.set", {"patch": {"mood": "calm"}})
    assert malformed.value.status == 400
    with pytest.raises(broker.BrokerError) as unnamed:  # and a name no pack carries
        _call(keyring, db, "resident.state.merge", {})
    assert unnamed.value.status == 404
    assert _calls(db) == []  # nothing ran, so nothing is recorded


def test_broker_records_a_refused_resident_tool(db, keyring):
    with pytest.raises(broker.BrokerError) as refused:  # schema-invalid: the spec's state schema
        _call(keyring, db, "resident.state.set", {"state": {"mood": 42}})
    assert refused.value.status == 422
    assert _staged(db) is None
    [recorded] = _calls(db)
    assert (recorded.outcome, recorded.detail) == ("refused", {"error": refused.value.message})


def test_broker_refuses_a_run_that_is_no_resident_turn(db, engine, keyring):
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=db.team, status="running",
                                         started_at=db.clock()))
        job_id = worker.queue_job(conn, team_id=db.team, project_id=db.project,
                                  workflow_id=db.workflow_id, inputs={}, now=db.clock())
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(run_id=run_id))
    token = broker.encode(keyring, broker.Grant(run_id, "work", ("resident.state.get",),
                                               db.clock() + timedelta(minutes=5)))
    with pytest.raises(broker.BrokerError) as raised:
        broker.call_tool(engine, keyring, public_url="https://arm.test", token=token,
                         tool="resident.state.get", params={}, now=db.clock(), ip=None)
    assert raised.value.status == 403
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tool_calls)).scalar_one() == 1
        [recorded] = _calls(db)
    assert (recorded.outcome, recorded.detail) == ("refused", {"error": raised.value.message})
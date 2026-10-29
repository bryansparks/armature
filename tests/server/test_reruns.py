"""Rerunning a finished run from its page: who may, when the server refuses, and what the new job carries."""
from __future__ import annotations

import pytest
from sqlalchemy import insert, select, update

from armature.server import defs, worker
from armature.server.db import new_id
from armature.server.db.schema import audit, jobs, projects, runs, teams, triggers, webhooks
from tests.server.app_support import Clock, add_membership, add_user, make_client, mutation, sign_in_as

BODY = "name: review\nstages: []\n"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(sqlite_engine, clock):
    team, other, project, hook, rule, workflow = (new_id() for _ in range(6))
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values([{"id": team, "name": "own", "created_at": clock()},
                                           {"id": other, "name": "other", "created_at": clock()}]))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=clock()))
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="review", current_version=2,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, BODY, "seed", None, clock())
        defs._add_version(conn, defs.WORKFLOW, workflow, 2, BODY, "edit", None, clock())
        conn.execute(insert(webhooks).values(id=hook, team_id=team, name="gh", source="github",
                                             path_token=new_id(), secret_name="HOOK", created_at=clock(),
                                             updated_at=clock()))
        conn.execute(insert(triggers).values(id=rule, team_id=team, workflow_id=workflow, kind="webhook",
                                             config={}, webhook_id=hook, created_at=clock()))
    return {"team": team, "other": other, "project": project, "workflow": workflow, "rule": rule}


def _finished(engine, clock, world, *, status="failed", job_status="failed", summary=None, with_job=True, **job):
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world["team"], project_id=world["project"],
                                         workflow_id=world["workflow"], workflow_version=1, status=status,
                                         started_at=clock(), summary=summary))
        if with_job:
            conn.execute(insert(jobs).values(**{
                "id": new_id(), "team_id": world["team"], "project_id": world["project"],
                "workflow_id": world["workflow"], "run_id": run_id, "inputs": {"number": 7}, "status": job_status,
                "attempts": 1, "created_at": clock(), "trigger_id": world["rule"], "delivery_id": "del-1",
                "workflow_version": 1, "thread": "acme/app#7", "concurrency_key": "acme/app#7", **job}))
    clock.advance(seconds=1)
    return run_id


def _user(engine, keyring, clock, team, role):
    user, _ = add_user(engine, keyring, clock, f"u-{role}-{new_id()[:6]}")
    add_membership(engine, clock, user, "team", team, role)
    client = make_client(engine, keyring, clock)
    client.headers.update(mutation(sign_in_as(client, engine, keyring, clock, user)))
    return client


def _reruns(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(jobs).where(jobs.c.rerun_of == run_id)).all()


def test_a_contributor_reruns_a_finished_run_with_its_inputs_trigger_and_key(sqlite_engine, keyring, clock, world):
    run_id = _finished(sqlite_engine, clock, world, dry_run=True)
    contributor = _user(sqlite_engine, keyring, clock, world["team"], "contributor")
    assert contributor.get(f"/api/runs/{run_id}").json()["run"]["can_rerun"] is True
    response = contributor.post(f"/api/runs/{run_id}/rerun")
    assert response.status_code == 200, response.text
    assert response.json()["job"]["status"] == "queued"
    [job] = _reruns(sqlite_engine, run_id)
    assert job.id == response.json()["job"]["id"]
    assert (job.inputs, job.trigger_id, job.delivery_id, job.thread, job.concurrency_key) == (
        {"number": 7}, world["rule"], "del-1", "acme/app#7", "acme/app#7")
    assert job.dry_run is True and job.workflow_version == 2 and job.project_id == world["project"]
    page = contributor.get(f"/api/runs/{run_id}").json()["run"]
    assert page["can_rerun"] is False and page["rerun"] == {"id": job.id, "status": "queued", "run_id": None}
    with sqlite_engine.connect() as conn:
        recorded = conn.execute(select(audit).where(audit.c.action == "run.rerun")).one()
    assert recorded.target_id == run_id and recorded.detail["job_id"] == job.id


def test_the_rerun_job_opens_a_run_that_links_back_and_stays_dry(sqlite_engine, keyring, clock, world):
    run_id = _finished(sqlite_engine, clock, world, dry_run=True)
    _user(sqlite_engine, keyring, clock, world["team"], "contributor").post(f"/api/runs/{run_id}/rerun")
    [job] = _reruns(sqlite_engine, run_id)
    with sqlite_engine.begin() as conn:
        bundle = worker.build_bundle(conn, keyring, job, now=clock(), lease=worker.LEASE, events="x")
        opened = conn.execute(select(runs).where(runs.c.id == bundle.run_id)).one()
    assert opened.rerun_of == run_id and opened.trigger_ref["dry_run"] is True and opened.thread == "acme/app#7"


def test_a_refused_looking_succeeded_run_can_be_rerun(sqlite_engine, keyring, clock, world):
    run_id = _finished(sqlite_engine, clock, world, status="succeeded", job_status="succeeded",
                       summary={"verdict": "denied"})
    contributor = _user(sqlite_engine, keyring, clock, world["team"], "contributor")
    assert contributor.get(f"/api/runs/{run_id}").json()["run"]["can_rerun"] is True
    assert contributor.post(f"/api/runs/{run_id}/rerun").status_code == 200


def test_only_a_contributor_on_the_runs_team_may_rerun(sqlite_engine, keyring, clock, world):
    run_id = _finished(sqlite_engine, clock, world)
    viewer = _user(sqlite_engine, keyring, clock, world["team"], "viewer")
    outsider = _user(sqlite_engine, keyring, clock, world["other"], "team_lead")
    assert viewer.get(f"/api/runs/{run_id}").json()["run"]["can_rerun"] is False
    assert viewer.post(f"/api/runs/{run_id}/rerun").status_code == 403
    assert outsider.post(f"/api/runs/{run_id}/rerun").status_code == 404
    assert _reruns(sqlite_engine, run_id) == []


@pytest.mark.parametrize("case, message", [
    ("running", "hasn't finished"),
    ("held", "still held"),
    ("no_job", "Only a run the server queued"),
    ("deleted", "no longer exists"),
    ("same_key", "same work"),
    ("conversation", "conversation"),
])
def test_the_server_refuses_a_rerun_that_cannot_run_now(sqlite_engine, keyring, clock, world, case, message):
    run_id = _finished(sqlite_engine, clock, world,
                       **{"running": {"status": "running", "job_status": "running"},
                          "held": {"status": "lost", "job_status": "running"},
                          "no_job": {"with_job": False},
                          "conversation": {"inputs": {"conversation_id": "c1", "message_seq": 1}}}.get(case, {}))
    with sqlite_engine.begin() as conn:
        if case == "deleted":
            conn.execute(update(defs.WORKFLOW.table).where(defs.WORKFLOW.table.c.id == world["workflow"])
                         .values(current_version=0))
        if case == "same_key":
            worker.queue_job(conn, team_id=world["team"], project_id=world["project"], workflow_id=world["workflow"],
                             inputs={}, now=clock(), key="acme/app#7")
    contributor = _user(sqlite_engine, keyring, clock, world["team"], "contributor")
    assert contributor.get(f"/api/runs/{run_id}").json()["run"]["can_rerun"] is False
    response = contributor.post(f"/api/runs/{run_id}/rerun")
    assert response.status_code == 409 and message in response.json()["error"], response.text
    assert _reruns(sqlite_engine, run_id) == []


def test_a_second_rerun_waits_until_the_first_is_done(sqlite_engine, keyring, clock, world):
    run_id = _finished(sqlite_engine, clock, world, concurrency_key=None)
    contributor = _user(sqlite_engine, keyring, clock, world["team"], "contributor")
    assert contributor.post(f"/api/runs/{run_id}/rerun").status_code == 200
    again = contributor.post(f"/api/runs/{run_id}/rerun")
    assert again.status_code == 409 and "already has a rerun" in again.json()["error"]
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.rerun_of == run_id).values(status="failed"))
    assert contributor.post(f"/api/runs/{run_id}/rerun").status_code == 200
    assert len(_reruns(sqlite_engine, run_id)) == 2


def test_rerunning_an_unknown_run_is_not_found(sqlite_engine, keyring, clock, world):
    contributor = _user(sqlite_engine, keyring, clock, world["team"], "contributor")
    assert contributor.post(f"/api/runs/{new_id()}/rerun").status_code == 404

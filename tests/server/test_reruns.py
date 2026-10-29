"""Rerunning a finished run from its page: who may, when the server refuses, and what the new job carries."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, insert, select, update

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
                                             path_token=new_id(), secret_name="HOOK", approval="approved",
                                             created_at=clock(), updated_at=clock()))
        conn.execute(insert(triggers).values(id=rule, team_id=team, workflow_id=workflow, kind="webhook",
                                             config={}, webhook_id=hook, created_at=clock()))
    return {"team": team, "other": other, "project": project, "workflow": workflow, "rule": rule, "hook": hook}


def _finished(engine, clock, world, *, status="failed", job_status="failed", summary=None, with_job=True,
              trigger_kind="jira", trigger_ref=None, **job):
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world["team"], project_id=world["project"],
                                         workflow_id=world["workflow"], workflow_version=1, status=status,
                                         started_at=clock(), summary=summary, trigger_kind=trigger_kind,
                                         trigger_ref=trigger_ref or {"trigger_id": world["rule"], "issue": "ACME-7",
                                                                     "dry_run": True}))
        if with_job:
            conn.execute(insert(jobs).values(**{
                "id": new_id(), "team_id": world["team"], "project_id": world["project"],
                "workflow_id": world["workflow"], "run_id": run_id, "inputs": {"number": 7}, "status": job_status,
                "attempts": 1, "created_at": clock(), "trigger_id": world["rule"], "delivery_id": "del-1",
                "workflow_version": 1, "thread": "acme/app#7", "concurrency_key": "acme/app#7", **job}))
    clock.advance(seconds=1)
    return run_id


def _user(engine, keyring, clock, team, role, *, user=None):
    user = user or add_user(engine, keyring, clock, f"u-{role}-{new_id()[:6]}")[0]
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


def _open(engine, keyring, clock, run_id):
    [job] = _reruns(engine, run_id)
    with engine.begin() as conn:
        bundle = worker.build_bundle(conn, keyring, job, now=clock(), lease=worker.LEASE, events="x")
        return conn.execute(select(runs).where(runs.c.id == bundle.run_id)).one()


def test_the_rerun_opens_a_run_that_links_back_with_the_trigger_and_who_asked(sqlite_engine, keyring, clock, world):
    run_id = _finished(sqlite_engine, clock, world, dry_run=True)
    user, _ = add_user(sqlite_engine, keyring, clock, "asker")
    _user(sqlite_engine, keyring, clock, world["team"], "contributor", user=user).post(f"/api/runs/{run_id}/rerun")
    opened = _open(sqlite_engine, keyring, clock, run_id)
    assert opened.rerun_of == run_id and opened.thread == "acme/app#7" and opened.triggered_by == user
    assert opened.trigger_kind == "jira"
    assert opened.trigger_ref == {"trigger_id": world["rule"], "issue": "ACME-7", "dry_run": True}


def test_a_live_rerun_of_a_dry_run_stays_live_and_keeps_the_pr_ref(sqlite_engine, keyring, clock, world):
    ref = {"webhook_id": "h1", "delivery_id": "del-1", "pr": "acme/app#7", "dry_run": True}
    run_id = _finished(sqlite_engine, clock, world, trigger_kind="webhook", trigger_ref=ref, dry_run=False)
    _user(sqlite_engine, keyring, clock, world["team"], "contributor").post(f"/api/runs/{run_id}/rerun")
    opened = _open(sqlite_engine, keyring, clock, run_id)
    assert opened.trigger_kind == "webhook"
    assert opened.trigger_ref == {"webhook_id": "h1", "delivery_id": "del-1", "pr": "acme/app#7"}


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


@pytest.mark.parametrize("change, message", [
    ("rule_off", "off or gone"),
    ("rule_gone", "off or gone"),
    ("shadow", "shadow mode"),
    ("hook_off", "not approved"),
    ("hook_pending", "not approved"),
])
def test_a_rerun_obeys_the_triggers_controls_now(sqlite_engine, keyring, clock, world, change, message):
    run_id = _finished(sqlite_engine, clock, world)
    with sqlite_engine.begin() as conn:
        if change == "rule_off":
            conn.execute(update(triggers).where(triggers.c.id == world["rule"]).values(enabled=False))
        if change == "rule_gone":
            defs.detach_triggers(conn, select(triggers.c.id).where(triggers.c.id == world["rule"]))
            conn.execute(delete(triggers).where(triggers.c.id == world["rule"]))
        if change == "shadow":
            conn.execute(update(triggers).where(triggers.c.id == world["rule"]).values(config={"shadow": True}))
        if change == "hook_off":
            conn.execute(update(webhooks).where(webhooks.c.id == world["hook"]).values(enabled=False))
        if change == "hook_pending":
            conn.execute(update(webhooks).where(webhooks.c.id == world["hook"]).values(approval="pending"))
    contributor = _user(sqlite_engine, keyring, clock, world["team"], "contributor")
    assert contributor.get(f"/api/runs/{run_id}").json()["run"]["can_rerun"] is False
    response = contributor.post(f"/api/runs/{run_id}/rerun")
    assert response.status_code == 409 and message in response.json()["error"], response.text
    assert _reruns(sqlite_engine, run_id) == []


def test_a_rule_made_dry_since_keeps_the_rerun_dry(sqlite_engine, keyring, clock, world):
    run_id = _finished(sqlite_engine, clock, world, dry_run=False)
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == world["rule"]).values(config={"dry": True}))
    assert _user(sqlite_engine, keyring, clock, world["team"], "contributor").post(
        f"/api/runs/{run_id}/rerun").status_code == 200
    [job] = _reruns(sqlite_engine, run_id)
    assert job.dry_run is True


def test_a_double_click_queues_one_rerun_and_refuses_the_other(sqlite_engine, keyring, clock, world):
    import threading
    run_id = _finished(sqlite_engine, clock, world, concurrency_key=None)
    clients = [_user(sqlite_engine, keyring, clock, world["team"], "contributor") for _ in range(2)]
    gate, codes = threading.Barrier(2), []

    def go(client):
        gate.wait()
        codes.append(client.post(f"/api/runs/{run_id}/rerun").status_code)

    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(codes) == [200, 409] and len(_reruns(sqlite_engine, run_id)) == 1

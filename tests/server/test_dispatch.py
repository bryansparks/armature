"""The runner API: hello, capability and team matching, claims, bundles, heartbeats, finishing, and canceling."""
from __future__ import annotations

import subprocess
from datetime import timedelta

import pytest
from sqlalchemy import insert, select, update

from armature.server import defs, dispatch, runners, secrets, webhooks, worker
from armature.server.config import ServerConfig
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    jobs,
    metadata,
    projects,
    runner_keys,
    runs,
    settings,
    teams,
    triggers,
    webhooks as webhooks_table,
)
from armature.spec.loader import STORED_WORKFLOW_HEADER
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)

BODY = "name: review\nrequires: [git]\nstages: []\n"
SECRET = "s3cr3t-value-that-must-not-persist"


class FakeHub:
    running = False
    unix_endpoint = None

    def __init__(self, engine, clock):
        self.engine = engine
        self.clock = clock


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(sqlite_engine, keyring, clock):
    team, other, project, hook, rule, workflow = (new_id() for _ in range(6))
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values([{"id": team, "name": "own", "created_at": clock()},
                                           {"id": other, "name": "other", "created_at": clock()}]))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=clock()))
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="review", current_version=1,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, BODY, "seed", None, clock())
        conn.execute(insert(webhooks_table).values(id=hook, team_id=team, name="gh", source="github",
                                                   path_token=new_id(), secret_name="HOOK", created_at=clock(),
                                                   updated_at=clock()))
        conn.execute(insert(triggers).values(id=rule, team_id=team, workflow_id=workflow, kind="webhook",
                                             config={"secrets": ["API_TOKEN"]}, webhook_id=hook, created_at=clock()))
        secrets.set_secret(conn, keyring, team_id=team, name="API_TOKEN", value=SECRET, actor_id=None)
        conn.execute(insert(settings).values(team_id=team, key="env.REGION", value="us-east-1"))
    return {"team": team, "other": other, "project": project, "workflow": workflow, "hook": hook, "rule": rule}


def _job(engine, clock, world, *, team=None, requires=None, **values):
    job_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(jobs).values(id=job_id, team_id=team or world["team"], project_id=world["project"],
                                         workflow_id=world["workflow"], inputs={"number": 7},
                                         delivery_id="del-1", created_at=clock(), requires=requires,
                                         **{"status": "queued", "attempts": 0, "workflow_version": 1,
                                            "trigger_id": None if team else world["rule"], **values}))
    clock.advance(seconds=1)
    return job_id


def _get(engine, job_id):
    with engine.connect() as conn:
        return conn.execute(select(jobs).where(jobs.c.id == job_id)).one()


def _key(engine, keyring, clock, team_id=None, capabilities=None, **values):
    with engine.begin() as conn:
        record, key = runners.create_key(conn, keyring, team_id=team_id, name=f"k-{new_id()[:8]}", actor_id=None,
                                         now=clock(), ip=None)
        conn.execute(update(runner_keys).where(runner_keys.c.id == record["id"])
                     .values(capabilities=capabilities, **values))
    return key


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _hello(client, key, name="host-1", capabilities=()):
    response = client.post("/api/runner/hello", json={"name": name, "capabilities": list(capabilities), "slots": 2,
                                                      "labels": {"pool": "warm"}, "version": "0.7.0"},
                           headers=_auth(key))
    assert response.status_code == 200, response.text
    return response.json()


def _claim(client, key, runner_id):
    return client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=_auth(key))


def _claimed(client, key, runner_id):
    response = _claim(client, key, runner_id)
    return None if response.status_code == 204 else response.json()["job"]["job_id"]


@pytest.fixture
def client(sqlite_engine, keyring, clock):
    return make_client(sqlite_engine, keyring, clock)


def test_hello_records_a_runner_and_answers_what_the_key_allows(sqlite_engine, keyring, clock, client):
    key = _key(sqlite_engine, keyring, clock, capabilities=["git", "llm"])
    first = _hello(client, key, capabilities=["git", "platform:ios"])
    again = _hello(client, key, capabilities=["git", "claude-code"])
    assert first["capabilities"] == ["git"] and again["runner_id"] == first["runner_id"]
    with sqlite_engine.connect() as conn:
        row = conn.execute(select(dispatch.runners)).one()
    assert row.capabilities == ["claude-code", "git"] and row.labels == {"pool": "warm"} and row.slots == 2
    bad = client.post("/api/runner/hello", json={"name": "x", "capabilities": ["Not Valid"]}, headers=_auth(key))
    assert bad.status_code == 400
    assert client.post("/api/runner/hello", json={"name": "x"}).status_code == 401


def test_a_job_without_requirements_goes_to_any_runner(sqlite_engine, keyring, clock, world, client):
    legacy = _job(sqlite_engine, clock, world)
    key = _key(sqlite_engine, keyring, clock, world["team"])
    runner_id = _hello(client, key)["runner_id"]
    assert _claimed(client, key, runner_id) == legacy
    row = _get(sqlite_engine, legacy)
    assert row.status == "running" and row.runner_id == runner_id and row.claimed_by == f"runner:{runner_id}"


def test_a_run_on_a_runner_outside_aws_is_located_by_the_platform_it_reported(sqlite_engine, keyring, clock, world,
                                                                               client):
    _job(sqlite_engine, clock, world)
    old = _job(sqlite_engine, clock, world)
    key = _key(sqlite_engine, keyring, clock, world["team"])
    platform = {"kind": "local", "chip": "Apple M1", "machine": "arm64", "host": "m1", "label": "ignored"}
    runner_id = client.post("/api/runner/hello", json={"name": "m1", "platform": platform},
                            headers=_auth(key)).json()["runner_id"]
    first = _claim(client, key, runner_id).json()["job"]["run_id"]
    silent = client.post("/api/runner/hello", json={"name": "old"}, headers=_auth(key)).json()["runner_id"]
    second = _claim(client, key, silent).json()["job"]["run_id"]
    with sqlite_engine.connect() as conn:
        located = dict(conn.execute(select(runs.c.id, runs.c.location)).all())
    assert located[first] == {"kind": "local", "label": "M1 (local)", "chip": "Apple M1", "machine": "arm64"}
    assert located[second] == {"kind": "unknown", "label": "Unknown"} and _get(sqlite_engine, old).status == "running"


def test_claims_need_the_capabilities_the_allowlist_and_the_team(sqlite_engine, keyring, clock, world, client):
    ios = _job(sqlite_engine, clock, world, requires=["git", "platform:ios"])
    elsewhere = _job(sqlite_engine, clock, world, team=world["other"])
    git = _job(sqlite_engine, clock, world, requires=["git"])
    team_key = _key(sqlite_engine, keyring, clock, world["team"])
    team_runner = _hello(client, team_key, capabilities=["git"])["runner_id"]
    assert _claimed(client, team_key, team_runner) == git
    assert _claimed(client, team_key, team_runner) is None
    narrow_key = _key(sqlite_engine, keyring, clock, capabilities=["git"])
    narrow = _hello(client, narrow_key, capabilities=["git", "platform:ios"])["runner_id"]
    assert _claimed(client, narrow_key, narrow) == elsewhere
    assert _claimed(client, narrow_key, narrow) is None
    instance_key = _key(sqlite_engine, keyring, clock)
    wide = _hello(client, instance_key, capabilities=["git", "platform:ios"])["runner_id"]
    assert _claimed(client, instance_key, wide) == ios
    assert _claim(client, instance_key, team_runner).status_code == 404


def test_only_one_claimant_wins_a_job(sqlite_engine, keyring, clock, world, client):
    job_id = _job(sqlite_engine, clock, world)
    key = _key(sqlite_engine, keyring, clock)
    first = _hello(client, key, name="a")["runner_id"]
    second = _hello(client, key, name="b")["runner_id"]
    embedded = worker.Worker(sqlite_engine, keyring, ServerConfig(database_url=str(sqlite_engine.url)),
                             FakeHub(sqlite_engine, clock), clock=clock)
    assert _claimed(client, key, first) == job_id
    assert _claimed(client, key, second) is None
    assert embedded.claim(clock()) is None
    assert _get(sqlite_engine, job_id).attempts == 1


def test_the_bundle_carries_the_run_and_secrets_never_reach_the_database(sqlite_engine, keyring, clock, world,
                                                                         client):
    job_id = _job(sqlite_engine, clock, world)
    key = _key(sqlite_engine, keyring, clock, world["team"])
    runner_id = _hello(client, key)["runner_id"]
    response = _claim(client, key, runner_id)
    assert response.headers["cache-control"] == "no-store"
    bundle = response.json()["job"]
    assert bundle["job_id"] == job_id and bundle["workflow"]["version"] == 1
    assert bundle["workflow"]["body"] == f"{STORED_WORKFLOW_HEADER}\n{BODY}"
    assert bundle["inputs"] == {"number": 7} and bundle["args"] == ["--input", "number=7"]
    assert bundle["settings"] == {"REGION": "us-east-1"} and bundle["secrets"] == {"API_TOKEN": SECRET}
    assert bundle["events_url"] == "https://arm.test/api/events" and bundle["calls"] == {}
    with sqlite_engine.begin() as conn:
        token = runners.lookup_token(conn, runners.digest(keyring, bundle["run_token"]))
        claim = conn.execute(select(audit.c.detail).where(audit.c.action == "job.claim")).scalar_one()
        dumped = [str(row) for table in metadata.sorted_tables for row in conn.execute(select(table)).all()]
    assert token.run_id == bundle["run_id"] == _get(sqlite_engine, job_id).run_id
    assert (claim["runner_id"], claim["run_id"], claim["secrets"]) == (runner_id, bundle["run_id"], ["API_TOKEN"])
    assert not [row for row in dumped if SECRET in row or bundle["run_token"] in row]


def test_a_job_that_cannot_be_prepared_fails_and_the_next_is_claimed(sqlite_engine, keyring, clock, world, client):
    broken = _job(sqlite_engine, clock, world, workflow_version=9)
    good = _job(sqlite_engine, clock, world)
    key = _key(sqlite_engine, keyring, clock)
    runner_id = _hello(client, key)["runner_id"]
    assert _claimed(client, key, runner_id) == good
    assert _get(sqlite_engine, broken).status == "failed" and "no version 9" in _get(sqlite_engine, broken).error


def test_heartbeats_extend_the_lease_and_a_silent_runner_is_reaped(sqlite_engine, keyring, clock, world, client):
    job_id = _job(sqlite_engine, clock, world)
    key = _key(sqlite_engine, keyring, clock)
    runner_id = _hello(client, key)["runner_id"]
    _claimed(client, key, runner_id)
    clock.advance(seconds=90)
    beat = client.post(f"/api/runner/jobs/{job_id}/heartbeat", headers=_auth(key)).json()
    assert beat == {"status": "running", "cancel_requested": False, "run_status": "running"}
    assert _get(sqlite_engine, job_id).lease_expires_at == clock() + worker.LEASE
    stranger = _key(sqlite_engine, keyring, clock)
    assert client.post(f"/api/runner/jobs/{job_id}/heartbeat", headers=_auth(stranger)).status_code == 404
    clock.advance(seconds=121)
    embedded = worker.Worker(sqlite_engine, keyring, ServerConfig(database_url=str(sqlite_engine.url)),
                             FakeHub(sqlite_engine, clock), clock=clock)
    assert embedded.reap(clock()) == 1
    row = _get(sqlite_engine, job_id)
    assert row.status == "lost" and row.attempts == 1
    assert _claimed(client, key, runner_id) is None


@pytest.mark.parametrize(("run_status", "reported", "job_status"), [
    ("succeeded", "failed", "succeeded"),
    ("failed", "succeeded", "failed"),
    ("running", "succeeded", "failed"),
])
def test_finish_settles_the_job_from_its_run(sqlite_engine, keyring, clock, world, client, run_status, reported,
                                             job_status):
    job_id = _job(sqlite_engine, clock, world)
    key = _key(sqlite_engine, keyring, clock)
    runner_id = _hello(client, key)["runner_id"]
    _claimed(client, key, runner_id)
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == _get(sqlite_engine, job_id).run_id)
                     .values(status=run_status, error="boom" if run_status == "failed" else None))
    response = client.post(f"/api/runner/jobs/{job_id}/finish", json={"status": reported, "error": "exit 1"},
                           headers=_auth(key))
    assert response.json() == {"status": job_status}
    row = _get(sqlite_engine, job_id)
    assert row.status == job_status and row.finished_at is not None
    assert row.error == {"succeeded": None, "failed": "boom" if run_status == "failed" else "exit 1"}[job_status]
    again = client.post(f"/api/runner/jobs/{job_id}/finish", json={"status": "failed"}, headers=_auth(key))
    assert again.json() == {"status": job_status}
    assert client.post(f"/api/runner/jobs/{job_id}/finish", json={"status": "done"},
                       headers=_auth(key)).status_code == 400


def _user_client(engine, keyring, clock, world, role):
    user, _ = add_user(engine, keyring, clock, f"u-{role}")
    add_membership(engine, clock, user, "team", world["team"], role)
    user_client = make_client(engine, keyring, clock)
    user_client.headers.update(mutation(sign_in_as(user_client, engine, keyring, clock, user)))
    return user_client


def test_canceling_a_queued_job_settles_it_and_a_held_one_asks_its_runner(sqlite_engine, keyring, clock, world,
                                                                         client):
    queued = _job(sqlite_engine, clock, world)
    held = _job(sqlite_engine, clock, world)
    contributor = _user_client(sqlite_engine, keyring, clock, world, "contributor")
    viewer = _user_client(sqlite_engine, keyring, clock, world, "viewer")
    assert viewer.post(f"/api/jobs/{queued}/cancel").status_code == 403
    canceled = contributor.post(f"/api/jobs/{queued}/cancel")
    assert canceled.status_code == 200 and canceled.json()["job"]["status"] == "canceled"
    assert _get(sqlite_engine, queued).finished_at is not None
    assert contributor.post(f"/api/jobs/{queued}/cancel").status_code == 409

    key = _key(sqlite_engine, keyring, clock)
    runner_id = _hello(client, key)["runner_id"]
    assert _claimed(client, key, runner_id) == held
    run_id = _get(sqlite_engine, held).run_id
    asked = contributor.post(f"/api/runs/{run_id}/cancel")
    assert asked.status_code == 200 and asked.json()["job"]["status"] == "running"
    beat = client.post(f"/api/runner/jobs/{held}/heartbeat", headers=_auth(key)).json()
    assert beat["cancel_requested"] is True
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(status="canceled"))
    done = client.post(f"/api/runner/jobs/{held}/finish", json={"status": "canceled"}, headers=_auth(key))
    assert done.json() == {"status": "canceled"}
    with sqlite_engine.connect() as conn:
        actions = conn.execute(select(audit.c.target_id).where(audit.c.action == "job.cancel")).scalars().all()
    assert sorted(actions) == sorted([queued, held])


def test_a_user_outside_the_team_cannot_see_or_cancel_its_jobs(sqlite_engine, keyring, clock, world):
    job_id = _job(sqlite_engine, clock, world)
    outsider, _ = add_user(sqlite_engine, keyring, clock, "outsider")
    add_membership(sqlite_engine, clock, outsider, "team", world["other"], "team_lead")
    outside = make_client(sqlite_engine, keyring, clock)
    outside.headers.update(mutation(sign_in_as(outside, sqlite_engine, keyring, clock, outsider)))
    assert outside.post(f"/api/jobs/{job_id}/cancel").status_code == 404
    assert _get(sqlite_engine, job_id).cancel_requested_at is None


def test_the_embedded_worker_ends_a_canceled_run_and_stops_it(sqlite_engine, keyring, clock, world, tmp_path,
                                                             monkeypatch):
    procs = []

    def spawn(binary, launch, args, env, hub, **kwargs):
        procs.append(subprocess.Popen(["sleep", "60"], start_new_session=True))
        return procs[-1]
    monkeypatch.setattr(worker.dryrun, "armature_binary", lambda: "/bin/armature")
    monkeypatch.setattr(worker.dryrun, "spawn", spawn)
    job_id = _job(sqlite_engine, clock, world)
    embedded = worker.Worker(sqlite_engine, keyring, ServerConfig(database_url=str(sqlite_engine.url)),
                             FakeHub(sqlite_engine, clock), clock=clock, work_dir=tmp_path / "work")
    embedded.tick()
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(cancel_requested_at=clock()))
    embedded.tick()
    assert procs[0].poll() is not None
    row = _get(sqlite_engine, job_id)
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(runs.c.status).where(runs.c.id == row.run_id)).scalar() == "canceled"
    assert row.status == "canceled"


def test_the_embedded_worker_honors_its_capabilities_setting(sqlite_engine, keyring, clock, world):
    needs_git = _job(sqlite_engine, clock, world, requires=["git"])
    legacy = _job(sqlite_engine, clock, world)
    embedded = worker.Worker(sqlite_engine, keyring, ServerConfig(database_url=str(sqlite_engine.url)),
                             FakeHub(sqlite_engine, clock), clock=clock)
    assert embedded.claim(clock()) == legacy
    assert embedded.claim(clock()) is None
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=None, key=worker.CAPABILITIES_KEY, value=["llm"]))
    assert embedded.claim(clock()) is None
    with sqlite_engine.begin() as conn:
        conn.execute(update(settings).where(settings.c.key == worker.CAPABILITIES_KEY).values(value=["git", "llm"]))
    assert embedded.claim(clock()) == needs_git


def test_enqueue_stamps_what_the_pinned_workflow_requires(sqlite_engine, clock, world):
    with sqlite_engine.begin() as conn:
        matched = webhooks.enqueue(conn, team_id=world["team"], delivery_id="d", now=clock(), decisions=[
            {"matched": True, "shadow": False, "workflow_id": world["workflow"], "project_id": None,
             "inputs": {}, "trigger_id": world["rule"]}])
    assert _get(sqlite_engine, matched[0]["job_id"]).requires == ["git"]
    assert worker.requirements("name: x\nstages: []\n") is None
    assert worker.requirements("name: x\nrequires: [llm, git, llm]\n") == ["git", "llm"]
    assert worker.requirements(": not yaml : [") is None


def test_an_expired_key_is_refused(sqlite_engine, keyring, clock, client):
    key = _key(sqlite_engine, keyring, clock, kind="ephemeral", expires_at=clock() - timedelta(seconds=1))
    assert client.post("/api/runner/hello", json={"name": "x"}, headers=_auth(key)).status_code == 401


def test_runners_page_lists_runners_for_admins_only(sqlite_engine, keyring, clock, world, client):
    key = _key(sqlite_engine, keyring, clock, world["team"])
    _hello(client, key, capabilities=["git"])
    admin, _ = add_user(sqlite_engine, keyring, clock, "admin", is_admin=True)
    boss = make_client(sqlite_engine, keyring, clock)
    boss.headers.update(mutation(sign_in_as(boss, sqlite_engine, keyring, clock, admin)))
    [listed] = boss.get("/api/runners").json()["runners"]
    assert listed["name"] == "host-1" and listed["team"] == "own" and listed["capabilities"] == ["git"]
    assert listed["online"] is True and listed["running"] == 0
    assert listed["same_user"] is False
    member = _user_client(sqlite_engine, keyring, clock, world, "team_lead")
    assert member.get("/api/runners").status_code == 403


def test_runners_page_marks_a_same_user_runner(sqlite_engine, keyring, clock, world, client):
    key = _key(sqlite_engine, keyring, clock, world["team"])
    client.post("/api/runner/hello", json={"name": "mine", "platform": {"kind": "local", "same_user": True}},
                headers=_auth(key))
    admin, _ = add_user(sqlite_engine, keyring, clock, "admin", is_admin=True)
    boss = make_client(sqlite_engine, keyring, clock)
    boss.headers.update(mutation(sign_in_as(boss, sqlite_engine, keyring, clock, admin)))
    [listed] = boss.get("/api/runners").json()["runners"]
    assert listed["same_user"] is True


def test_run_detail_names_its_job(sqlite_engine, keyring, clock, world, client):
    job_id = _job(sqlite_engine, clock, world)
    key = _key(sqlite_engine, keyring, clock)
    _claimed(client, key, _hello(client, key)["runner_id"])
    viewer = _user_client(sqlite_engine, keyring, clock, world, "viewer")
    detail = viewer.get(f"/api/runs/{_get(sqlite_engine, job_id).run_id}").json()
    assert detail["job"] == {"id": job_id, "status": "running", "cancel_requested_at": None}



def test_revoking_a_key_ends_the_event_streams_of_runs_it_claimed(sqlite_engine, keyring, clock, world, client):
    job_id = _job(sqlite_engine, clock, world)
    key = _key(sqlite_engine, keyring, clock, world["team"])
    bundle = _claim(client, key, _hello(client, key)["runner_id"]).json()["job"]
    digest = runners.digest(keyring, bundle["run_token"])
    with sqlite_engine.begin() as conn:
        key_id = conn.execute(select(runner_keys.c.id).where(runner_keys.c.key_hmac == runners.digest(keyring, key)))\
            .scalar_one()
        assert conn.execute(select(runs.c.runner_key_id).where(runs.c.id == bundle["run_id"])).scalar() == key_id
        assert runners.revoke_key(conn, team_id=world["team"], key_id=key_id, actor_id=None, now=clock(), ip=None)
        assert runners.lookup_token(conn, digest) is None
    assert _get(sqlite_engine, job_id).run_id == bundle["run_id"]


def test_a_team_key_skips_a_job_that_needs_an_instance_secret(sqlite_engine, keyring, clock, world, client):
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == world["rule"])
                     .values(config={"secrets": ["API_TOKEN", "SHARED"]}))
        secrets.set_secret(conn, keyring, team_id=None, name="SHARED", value="instance-only", actor_id=None)
    waiting = _job(sqlite_engine, clock, world)
    team_key = _key(sqlite_engine, keyring, clock, world["team"])
    assert _claimed(client, team_key, _hello(client, team_key)["runner_id"]) is None
    row = _get(sqlite_engine, waiting)
    assert (row.status, row.attempts, row.run_id) == ("queued", 0, None)
    instance_key = _key(sqlite_engine, keyring, clock)
    bundle = _claim(client, instance_key, _hello(client, instance_key)["runner_id"]).json()["job"]
    assert bundle["job_id"] == waiting and bundle["secrets"] == {"API_TOKEN": SECRET, "SHARED": "instance-only"}


def test_a_team_key_finds_a_team_secret_that_shadows_an_instance_one(sqlite_engine, keyring, clock, world, client):
    with sqlite_engine.begin() as conn:
        secrets.set_secret(conn, keyring, team_id=None, name="API_TOKEN", value="instance", actor_id=None)
    job_id = _job(sqlite_engine, clock, world)
    team_key = _key(sqlite_engine, keyring, clock, world["team"])
    bundle = _claim(client, team_key, _hello(client, team_key)["runner_id"]).json()["job"]
    assert bundle["job_id"] == job_id and bundle["secrets"] == {"API_TOKEN": SECRET}


def test_the_bundle_still_refuses_an_instance_secret_to_a_team_claim(sqlite_engine, keyring, clock, world):
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == world["rule"]).values(config={"secrets": ["SHARED"]}))
        secrets.set_secret(conn, keyring, team_id=None, name="SHARED", value="instance-only", actor_id=None)
    job_id = _job(sqlite_engine, clock, world)
    with sqlite_engine.begin() as conn:
        job = conn.execute(select(jobs).where(jobs.c.id == job_id)).one()
        with pytest.raises(worker.JobError, match="SHARED is set only for the instance"):
            worker.build_bundle(conn, keyring, job, now=clock(), lease=worker.LEASE, events="x",
                                instance_secrets=False)


def test_concurrent_claims_never_share_a_job(sqlite_engine, keyring, clock, world, client):
    import threading
    key = _key(sqlite_engine, keyring, clock)
    wanted = {_job(sqlite_engine, clock, world) for _ in range(6)}
    runner_ids = [_hello(client, key, name=f"r{i}")["runner_id"] for i in range(8)]
    with sqlite_engine.begin() as conn:
        record = runners.authenticate(conn, keyring, key, clock())
    won, errors, gate = [], [], threading.Barrier(len(runner_ids))

    def go(runner_id):
        try:
            gate.wait()
            bundle = dispatch.claim(sqlite_engine, keyring, key=record, runner_id=runner_id, now=clock(),
                                    lease=worker.LEASE, events="https://x/api/events")
            if bundle is not None:
                won.append(bundle.job_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(r,)) for r in runner_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == len(set(won))
    assert set(won) <= wanted
    with sqlite_engine.connect() as conn:
        attempts = conn.execute(select(jobs.c.attempts).where(jobs.c.id.in_(won))).scalars().all()
    assert attempts == [1] * len(won)


def test_concurrent_runner_claims_start_one_job_of_a_key(sqlite_engine, keyring, clock, world, client):
    import threading
    key = _key(sqlite_engine, keyring, clock)
    keyed = {_job(sqlite_engine, clock, world, concurrency_key="acme/app#7") for _ in range(4)}
    waiting = _job(sqlite_engine, clock, world, not_before=clock() + timedelta(minutes=10))
    runner_ids = [_hello(client, key, name=f"r{i}", capabilities=["git"])["runner_id"] for i in range(4)]
    with sqlite_engine.begin() as conn:
        record = runners.authenticate(conn, keyring, key, clock())
    won, errors, gate = [], [], threading.Barrier(len(runner_ids))

    def go(runner_id):
        try:
            gate.wait()
            bundle = dispatch.claim(sqlite_engine, keyring, key=record, runner_id=runner_id, now=clock(),
                                    lease=worker.LEASE, events="https://x/api/events")
            if bundle is not None:
                won.append(bundle.job_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(r,)) for r in runner_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(won) == 1 and set(won) <= keyed
    assert _get(sqlite_engine, waiting).status == "queued"
    clock.advance(minutes=10)
    assert _claimed(client, key, runner_ids[0]) == waiting

"""AWS environments with a fake provider: dispatch decisions, the reaper, cost guards, Mac hosts, and the AWS page."""
from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import defs, environments, runners
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    aws_connections,
    environment_hosts,
    environment_types,
    jobs,
    metadata,
    projects,
    runner_keys,
    settings,
    teams,
)
from armature.server.db.schema import (
    environments as environments_table,
)
from armature.transport.provision import ProvisionError
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)
from tests.transport.fake_provisioner import FakeProvisioner

BODY = "name: build\nrequires: [git, platform:android]\nstages: []\n"
ACCESS_KEY = "AKIAABCDEFGHIJKLWXYZ"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(clock):
    return FakeProvisioner(clock)


@pytest.fixture
def world(sqlite_engine, keyring, clock):
    team, project, bare, workflow, kind = (new_id() for _ in range(5))
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="mobile", created_at=clock()))
        conn.execute(insert(environment_types).values(
            id=kind, name="android", kind="ec2-linux", image="ami-0123456789abcdef0", instance_types=["m6i.large"],
            market="spot-then-on-demand", capabilities=["git", "platform:android"], job_timeout_s=3600,
            hourly_cost_usd=1, enabled=True, created_at=clock(), updated_at=clock()))
        conn.execute(insert(projects).values([
            {"id": project, "team_id": team, "name": "android", "environment_type_id": kind, "created_at": clock()},
            {"id": bare, "team_id": team, "name": "bare", "environment_type_id": None, "created_at": clock()}]))
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="build", current_version=1,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, BODY, "seed", None, clock())
        conn.execute(insert(settings).values(team_id=None, key="worker.enabled", value=False))
        conn.execute(insert(aws_connections).values(
            id=new_id(), name="default", region="us-east-1", auth_kind="access_key", tag_project="armature-platform",
            install_id="install-1", allow_host_allocation=False, max_concurrent=4, max_daily_launches=50,
            boot_timeout_s=420, max_lifetime_s=5400, state="ok",
            subnets=[{"zone": "us-east-1a", "subnet_id": "subnet-0123456789abcdef0"}],
            created_at=clock(), updated_at=clock()))
    return {"team": team, "project": project, "bare": bare, "workflow": workflow, "type": kind}


@pytest.fixture
def service(sqlite_engine, keyring, clock, fake):
    from armature.server.config import ServerConfig
    cfg = ServerConfig(database_url=str(sqlite_engine.url), public_origin="https://arm.test")
    return environments.EnvironmentService(sqlite_engine, keyring, cfg, clock=clock, loader=lambda *_: fake,
                                           builder=lambda *_: fake)


def _job(engine, clock, world, *, project="project", requires=("git", "platform:android"), age=30):
    job_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(jobs).values(id=job_id, team_id=world["team"], project_id=world.get(project, project),
                                         workflow_id=world["workflow"], workflow_version=1, status="queued",
                                         attempts=0, requires=list(requires) if requires is not None else None,
                                         created_at=clock() - timedelta(seconds=age)))
    clock.advance(seconds=1)
    return job_id


def _row(engine, table, row_id):
    with engine.connect() as conn:
        return conn.execute(select(table).where(table.c.id == row_id)).one()


def _envs(engine, job_id=None):
    with engine.connect() as conn:
        query = select(environments_table).order_by(environments_table.c.requested_at)
        if job_id:
            query = query.where(environments_table.c.job_id == job_id)
        return conn.execute(query).all()


def _launches(fake):
    return [c for c in fake.calls if c[0] == "launch"]


def _connection(engine, **values):
    with engine.begin() as conn:
        conn.execute(update(aws_connections).values(**values))


# Dispatch

def test_a_job_that_needs_the_source_gets_one_environment(sqlite_engine, service, fake, world, clock):
    job_id = _job(sqlite_engine, clock, world)
    service.tick()
    service.tick()
    (env,) = _envs(sqlite_engine)
    job = _row(sqlite_engine, jobs, job_id)
    assert job.status == "provisioning" and job.claimed_by == f"env:{env.id}" and job.attempts == 0
    assert env.state == "booting" and env.provider_id.startswith("i-") and env.job_id == job_id
    assert env.terminate_after == clock() + timedelta(seconds=5400) and env.launch_attempts == 1
    assert len(_launches(fake)) == 1
    spec = fake.specs[env.id]
    assert spec.markets == ("spot", "on-demand") and spec.tags == {} and "--job-user armature-job" in spec.user_data
    assert "--job-user armature-job --work-dir /var/lib/armature-runner --agent-user armature-agent" in spec.user_data
    assert "--uid-owner armature-agent -d 169.254.169.254 -j REJECT" in spec.user_data
    assert spec.user_data.count("are_") == 1 and "shutdown -h +90" in spec.user_data
    assert env.enroll_hmac == runners.digest(service.keyring, fake.token(env.id))


def test_a_fresh_job_waits_for_runners_first(sqlite_engine, service, fake, world, clock):
    _job(sqlite_engine, clock, world, age=2)
    service.tick()
    assert _envs(sqlite_engine) == [] and fake.calls == []


def test_a_job_an_online_runner_can_take_launches_nothing(sqlite_engine, keyring, service, fake, world, clock):
    _job(sqlite_engine, clock, world)
    with sqlite_engine.begin() as conn:
        _, key = runners.create_key(conn, keyring, team_id=world["team"], name="ci", actor_id=None, now=clock(),
                                         ip=None)
    client = make_client(sqlite_engine, keyring, clock)
    client.post("/api/runner/hello", json={"name": "mac", "capabilities": ["git", "platform:android"]},
                headers={"Authorization": f"Bearer {key}"})
    service.tick()
    assert _envs(sqlite_engine) == []
    clock.advance(minutes=3)
    service.tick()
    assert len(_envs(sqlite_engine)) == 1


def test_llm_only_jobs_never_launch_and_say_why(sqlite_engine, keyring, service, fake, world, clock):
    llm = _job(sqlite_engine, clock, world, requires=["llm"])
    bare = _job(sqlite_engine, clock, world, project="bare")
    service.tick()
    assert _envs(sqlite_engine) == []
    admin, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    client = make_client(sqlite_engine, keyring, clock)
    client.headers.update(mutation(sign_in_as(client, sqlite_engine, keyring, clock, admin)))
    listed = {j["id"]: j["warning"] for j in client.get("/api/jobs").json()["jobs"]}
    assert listed[llm] == "No runner with llm is online. Start a warm runner on the AWS page."
    assert listed[bare] == "Project has no environment type."


def test_a_type_that_lacks_a_capability_fails_the_job(sqlite_engine, service, fake, world, clock):
    job_id = _job(sqlite_engine, clock, world, requires=["git", "platform:ios"])
    service.tick()
    job = _row(sqlite_engine, jobs, job_id)
    assert job.status == "failed" and job.error == "No environment type provides platform:ios."
    assert fake.calls == []


def test_the_service_is_inert_until_the_connection_verifies(sqlite_engine, keyring, clock, world):
    from armature.server.config import ServerConfig
    _job(sqlite_engine, clock, world)
    _connection(sqlite_engine, state="unverified")
    cfg = ServerConfig(database_url=str(sqlite_engine.url), public_origin="https://arm.test")
    real = environments.EnvironmentService(sqlite_engine, keyring, cfg, clock=clock)
    real.tick()
    assert _envs(sqlite_engine) == [] and real.snapshot()["message"] == "AWS not configured"


def test_a_crash_between_the_row_and_the_launch_relaunches_the_same_environment(sqlite_engine, service, fake, world,
                                                                                clock):
    job_id = _job(sqlite_engine, clock, world)
    with sqlite_engine.connect() as conn:
        row = environments.connection(conn)
        kind = conn.execute(select(environment_types)).one()
        job = conn.execute(select(jobs).where(jobs.c.id == job_id)).one()
    env_id = service.claim(row, kind, job, clock())
    fake.launch(service.spec(sqlite_engine.connect(), row, _row(sqlite_engine, environments_table, env_id)))
    clock.advance(minutes=3)
    service.tick()
    env = _row(sqlite_engine, environments_table, env_id)
    assert env.state == "booting" and len(fake.instances) == 1 and env.provider_id in fake.instances
    assert [c[1] for c in _launches(fake)] == [env_id, env_id]


def test_a_retryable_failure_requeues_and_waits(sqlite_engine, service, fake, world, clock):
    job_id = _job(sqlite_engine, clock, world)
    fake.fail.append(ProvisionError("no capacity", retryable=True))
    service.tick()
    job = _row(sqlite_engine, jobs, job_id)
    assert job.status == "queued" and job.attempts == 0 and _envs(sqlite_engine)[0].state == "failed"
    service.tick()
    assert len(_launches(fake)) == 1 and "retry" in service.reasons[job_id]
    clock.advance(minutes=2)
    service.tick()
    assert _row(sqlite_engine, jobs, job_id).status == "provisioning"


def test_repeated_hard_failures_trip_the_breaker(sqlite_engine, service, fake, world, clock):
    first = [_job(sqlite_engine, clock, world) for _ in range(3)]
    fake.fail.extend(ProvisionError("UnauthorizedOperation") for _ in range(3))
    service.tick()
    assert {_row(sqlite_engine, jobs, j).status for j in first} == {"failed"}
    assert _row(sqlite_engine, jobs, first[0]).error.startswith("The environment could not start")
    later = _job(sqlite_engine, clock, world)
    service.tick()
    assert _row(sqlite_engine, jobs, later).status == "queued" and "paused" in service.reasons[later]
    clock.advance(minutes=11)
    service.tick()
    assert _row(sqlite_engine, jobs, later).status == "provisioning"


# Guards

@pytest.mark.parametrize("values, words", [
    ({"max_concurrent": 1}, "the most the AWS connection allows"),
    ({"max_daily_launches": 1}, "in the last 24 hours"),
    ({"daily_budget_usd": 0.5}, "daily budget"),
])
def test_each_connection_guard_holds_the_next_launch(sqlite_engine, service, fake, world, clock, values, words):
    first = _job(sqlite_engine, clock, world)
    _connection(sqlite_engine, **values)
    service.tick()
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == first).values(status="running",
                                                                   lease_expires_at=clock() + timedelta(hours=9)))
    clock.advance(hours=1)
    held = _job(sqlite_engine, clock, world)
    service.tick()
    assert _row(sqlite_engine, jobs, held).status == "queued" and words in service.reasons[held]
    assert len(_launches(fake)) == 1


def test_the_team_setting_caps_environments_per_project(sqlite_engine, service, fake, world, clock):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world["team"], key="environment.max_concurrent", value=1))
    _job(sqlite_engine, clock, world)
    held = _job(sqlite_engine, clock, world)
    service.tick()
    assert _row(sqlite_engine, jobs, held).status == "queued" and "its team allows" in service.reasons[held]


# The reaper

def test_a_boot_timeout_requeues_once_without_spending_an_attempt(sqlite_engine, service, fake, world, clock):
    job_id = _job(sqlite_engine, clock, world)
    service.tick()
    clock.advance(seconds=421)
    service.tick()
    first, second = _envs(sqlite_engine)
    assert first.state == "failed" and first.error == "The environment did not boot in time."
    assert second.state == "booting" and first.provider_id in fake.terminated
    assert _row(sqlite_engine, jobs, job_id).attempts == 0
    clock.advance(seconds=421)
    service.tick()
    job = _row(sqlite_engine, jobs, job_id)
    assert job.status == "failed" and job.error == "environment did not boot" and len(_envs(sqlite_engine)) == 2


def test_a_finished_job_terminates_its_environment_and_revokes_the_key(sqlite_engine, keyring, service, fake, world,
                                                                     clock):
    job_id = _job(sqlite_engine, clock, world)
    service.tick()
    (env,) = _envs(sqlite_engine)
    client = make_client(sqlite_engine, keyring, clock)
    key = fake.boot(client, env.id, ["git", "platform:android"])
    assert _row(sqlite_engine, environments_table, env.id).state == "ready"
    runner_id = client.post("/api/runner/hello", json={"name": f"env-{env.id[:8]}"},
                            headers={"Authorization": f"Bearer {key}"}).json()["runner_id"]
    claimed = client.post("/api/runner/claim", json={"runner_id": runner_id},
                          headers={"Authorization": f"Bearer {key}"})
    assert claimed.status_code == 200 and claimed.json()["job"]["job_id"] == job_id
    assert _row(sqlite_engine, environments_table, env.id).state == "running"
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="succeeded", finished_at=clock()))
    service.tick()
    env = _row(sqlite_engine, environments_table, env.id)
    assert env.state == "terminated" and env.provider_id in fake.terminated
    assert _row(sqlite_engine, runner_keys, env.runner_key_id).revoked_at is not None
    assert client.post("/api/runner/hello", json={"name": "x"},
                       headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_a_canceled_provisioning_job_ends_now_and_its_environment_goes(sqlite_engine, keyring, service, fake, world,
                                                                      clock):
    job_id = _job(sqlite_engine, clock, world)
    service.tick()
    user, _ = add_user(sqlite_engine, keyring, clock, "dev")
    add_membership(sqlite_engine, clock, user, "team", world["team"], "contributor")
    client = make_client(sqlite_engine, keyring, clock)
    client.headers.update(mutation(sign_in_as(client, sqlite_engine, keyring, clock, user)))
    assert client.post(f"/api/jobs/{job_id}/cancel").json()["job"]["status"] == "canceled"
    service.tick()
    assert _envs(sqlite_engine)[0].state == "terminated"


def test_an_environment_past_its_lifetime_is_terminated_and_its_job_lost(sqlite_engine, service, fake, world, clock):
    job_id = _job(sqlite_engine, clock, world)
    service.tick()
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="running",
                                                                    lease_expires_at=clock() + timedelta(hours=9)))
    clock.advance(seconds=5401)
    service.tick()
    assert _envs(sqlite_engine)[0].state == "terminated"
    assert _row(sqlite_engine, jobs, job_id).status == "lost"


def test_the_orphan_sweep_terminates_unknown_instances_and_fails_missing_ones(sqlite_engine, service, fake, world,
                                                                             clock):
    job_id = _job(sqlite_engine, clock, world)
    service.tick()
    (env,) = _envs(sqlite_engine)
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="running",
                                                                    lease_expires_at=clock() + timedelta(hours=1)))
    host = _mac(sqlite_engine, clock, world)
    with sqlite_engine.begin() as conn:
        conn.execute(update(projects).where(projects.c.id == world["project"]).values(environment_type_id=world["type"]))
        mac_instance = conn.execute(select(environment_hosts.c.instance_id).where(
            environment_hosts.c.id == host)).scalar_one()
    fake.add_owned("i-0000000000000fresh", "fresh-env", clock())
    fake.add_owned("i-00000000000000old", "gone", clock() - timedelta(minutes=11))
    fake.add_owned(mac_instance, None, clock() - timedelta(days=2))
    fake.add_owned("i-000000000000build", None, clock() - timedelta(hours=1))
    del fake.instances[env.provider_id]
    service.tick()
    assert "i-00000000000000old" in fake.terminated and "i-0000000000000fresh" not in fake.terminated
    assert _row(sqlite_engine, environments_table, env.id).state == "booting"
    clock.advance(minutes=11)
    service.tick()
    assert "i-0000000000000fresh" in fake.terminated
    assert mac_instance not in fake.terminated and "i-000000000000build" not in fake.terminated
    assert _row(sqlite_engine, environments_table, env.id).state == "failed"
    job = _row(sqlite_engine, jobs, job_id)
    assert job.status == "failed" and job.error == "The environment's instance is gone."


# Mac hosts

def _mac(sqlite_engine, clock, world, slots=1, **values):
    kind, host = new_id(), new_id()
    with sqlite_engine.begin() as conn:
        connection_id = conn.execute(select(aws_connections.c.id)).scalar_one()
        conn.execute(insert(environment_types).values(
            id=kind, name="ios", kind="ec2-mac", image="ghcr.io/org/macos:15", market="on-demand",
            capabilities=["git", "platform:ios"], job_timeout_s=3600, enabled=True, created_at=clock(),
            updated_at=clock()))
        conn.execute(update(projects).where(projects.c.id == world["project"]).values(environment_type_id=kind))
        conn.execute(insert(environment_hosts).values(
            id=host, connection_id=connection_id, name="mac-1", instance_id="i-0000000000000mac1",
            dedicated_host_id="h-0000000000000001", state="running", slots=slots, allocated_at=clock(),
            min_release_at=clock() + timedelta(hours=24), **values))
    return host


def test_mac_jobs_take_free_slots_and_wait_when_every_slot_is_busy(sqlite_engine, service, fake, world, clock):
    host = _mac(sqlite_engine, clock, world, slots=1)
    first = _job(sqlite_engine, clock, world, requires=["platform:ios"])
    second = _job(sqlite_engine, clock, world, requires=["platform:ios"])
    service.tick()
    (env,) = _envs(sqlite_engine)
    assert env.job_id == first and (env.host_id, env.slot_index) == (host, 0)
    assert env.provider_id == f"i-0000000000000mac1/env-{env.id}"
    assert fake.specs[env.id].host_instance_id == "i-0000000000000mac1"
    assert _row(sqlite_engine, jobs, second).status == "queued"
    assert service.reasons[second] == "Every Mac host slot is busy."
    with pytest.raises(IntegrityError), sqlite_engine.begin() as conn:
        conn.execute(insert(environments_table).values(
            id=new_id(), connection_id=env.connection_id, purpose="job", host_id=host, slot_index=0,
            state="requested", launch_attempts=0, requested_at=clock()))


def test_a_mac_host_is_never_released_before_24_hours(sqlite_engine, service, fake, world, clock):
    _connection(sqlite_engine, allow_host_allocation=True)
    host = service.allocate_host({"name": "mac-2", "zone": "us-east-1a", "confirm_cost": True}, actor_id="admin",
                                 ip=None)
    assert host.min_release_at == clock() + timedelta(hours=24)
    with pytest.raises(environments.EnvError) as early:
        service.release(host.id, actor_id="admin", ip=None)
    assert early.value.status == 409 and "24 hours" in early.value.message and fake.released == []
    clock.advance(hours=24)
    service.release(host.id, actor_id="admin", ip=None)
    assert fake.released == [host.dedicated_host_id]
    assert _row(sqlite_engine, environment_hosts, host.id).state == "released"


def test_an_idle_host_is_released_after_its_idle_time_and_the_minimum_hold(sqlite_engine, service, fake, world, clock):
    host = _mac(sqlite_engine, clock, world, auto_release_after_s=3600)
    service.tick()
    assert _row(sqlite_engine, environment_hosts, host).idle_since == clock()
    clock.advance(hours=2)
    service.tick()
    assert fake.released == []
    clock.advance(hours=23)
    service.tick()
    assert fake.released == ["h-0000000000000001"]


# The AWS page

@pytest.fixture
def admin_client(sqlite_engine, keyring, clock, fake):
    admin, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    client = make_client(sqlite_engine, keyring, clock)
    client.app.state.environments.loader = lambda *_: fake
    client.app.state.environments.builder = lambda *_: fake
    client.headers.update(mutation(sign_in_as(client, sqlite_engine, keyring, clock, admin)))
    return client


CONNECTION = {"region": "us-west-2", "auth_kind": "access_key", "access_key_id": ACCESS_KEY,
              "secret_access_key": SECRET_KEY, "subnets": [{"zone": "us-west-2a", "subnet_id": "subnet-0abc12345"}],
              "security_group_id": "sg-0abc12345", "max_concurrent": 2}


def test_the_aws_page_is_for_admins_with_step_up(sqlite_engine, keyring, clock):
    user, _ = add_user(sqlite_engine, keyring, clock, "dev")
    client = make_client(sqlite_engine, keyring, clock)
    client.headers.update(mutation(sign_in_as(client, sqlite_engine, keyring, clock, user)))
    assert client.get("/api/aws").status_code == 403
    assert client.post("/api/aws/connection", json=CONNECTION).status_code == 403
    admin, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    stale = make_client(sqlite_engine, keyring, clock)
    stale.headers.update(mutation(sign_in_as(stale, sqlite_engine, keyring, clock, admin, step_up=False)))
    assert stale.get("/api/aws").status_code == 200
    refused = stale.post("/api/aws/connection", json=CONNECTION)
    assert refused.status_code == 403 and refused.json()["error"] == "step_up_required"


def test_credentials_are_write_only_and_never_logged(sqlite_engine, admin_client):
    saved = admin_client.post("/api/aws/connection", json=CONNECTION)
    assert saved.status_code == 200, saved.text
    shown = saved.json()["connection"]
    assert shown["credentials"] == {"set": True, "access_key_id_last4": "WXYZ", "role_arn": None,
                                    "external_id_set": False}
    assert shown["state"] == "unverified" and shown["tag_project"] == "armature-platform"
    again = admin_client.post("/api/aws/connection", json={**CONNECTION, "access_key_id": None,
                                                           "secret_access_key": None, "max_concurrent": 3})
    assert again.json()["connection"]["credentials"]["access_key_id_last4"] == "WXYZ"
    page = admin_client.get("/api/aws").text
    with sqlite_engine.connect() as conn:
        dumped = [str(row) for table in metadata.sorted_tables for row in conn.execute(select(table)).all()]
        logged = conn.execute(select(audit.c.detail).where(audit.c.action == "aws.connection.set")).scalars().all()
    assert SECRET_KEY not in page and "v1:" not in page and ACCESS_KEY not in page
    assert not [row for row in dumped if SECRET_KEY in row or ACCESS_KEY in row]
    assert logged and not [d for d in logged if SECRET_KEY in json.dumps(d)]


def test_the_armature_tag_project_is_refused(admin_client):
    refused = admin_client.post("/api/aws/connection", json={**CONNECTION, "tag_project": "armature"})
    assert refused.status_code == 400 and "armature-ops" in refused.json()["error"]


def test_verify_turns_the_connection_on_and_reports_problems(sqlite_engine, admin_client, fake):
    admin_client.post("/api/aws/connection", json=CONNECTION)
    fake.problems = ["Can't find the subnets: InvalidSubnetID.NotFound: nope"]
    failed = admin_client.post("/api/aws/connection/verify").json()["connection"]
    assert failed["state"] == "error" and "InvalidSubnetID" in failed["last_error"]
    fake.problems = []
    ok = admin_client.post("/api/aws/connection/verify").json()["connection"]
    assert ok["state"] == "ok" and ok["verified_at"]
    with sqlite_engine.connect() as conn:
        states = conn.execute(select(audit.c.detail).where(audit.c.action == "aws.connection.verify")
                              .order_by(audit.c.at)).scalars().all()
    assert [s["state"] for s in states] == ["error", "ok"]


def test_host_allocation_needs_the_owner_flag_and_a_cost_confirmation(sqlite_engine, world, admin_client):
    body = {"name": "mac-1", "zone": "us-east-1a"}
    refused = admin_client.post("/api/aws/hosts", json=body)
    assert refused.status_code == 409 and refused.json()["error"] == "Ask the account owner to allocate a host"
    _connection(sqlite_engine, allow_host_allocation=True)
    unconfirmed = admin_client.post("/api/aws/hosts", json=body)
    assert unconfirmed.status_code == 400 and "24 hours" in unconfirmed.json()["error"]
    made = admin_client.post("/api/aws/hosts", json={**body, "confirm_cost": True})
    assert made.status_code == 200 and made.json()["host"]["state"] == "allocating"
    early = admin_client.post(f"/api/aws/hosts/{made.json()['host']['id']}/release")
    assert early.status_code == 409


def test_types_and_the_warm_runner(sqlite_engine, world, admin_client, fake):
    made = admin_client.post("/api/aws/types", json={"name": "node", "kind": "ec2-linux", "image": "ami-0123456789abcdef1",
                                                     "instance_types": ["t3.large"], "capabilities": ["git", "node"],
                                                     "installs": [{"name": "node", "version": "22.1.0",
                                                                   "url": "https://nodejs.org/node.tar.xz",
                                                                   "sha256": "a" * 64}]})
    assert made.status_code == 200, made.text
    bad = admin_client.post("/api/aws/types", json={"name": "x", "kind": "ec2-linux", "image": "not-an-ami"})
    assert bad.status_code == 400
    started = admin_client.post("/api/aws/warm/start", json={"type_id": made.json()["type"]["id"]})
    assert started.status_code == 200, started.text
    spec = fake.specs[started.json()["environment_id"]]
    assert spec.markets == ("on-demand",) and "--capability llm" in spec.user_data and "shutdown" not in spec.user_data
    assert admin_client.post("/api/aws/warm/start", json={"type_id": made.json()["type"]["id"]}).status_code == 409
    admin_client.app.state.environments.tick()
    page = admin_client.get("/api/aws").json()
    assert page["warm"]["state"] == "booting" and page["status"]["configured"] is True
    assert admin_client.post("/api/aws/warm/stop").status_code == 200
    assert admin_client.get("/api/aws").json()["warm"] is None


def test_the_reaper_leaves_the_warm_runner_alone(sqlite_engine, world, admin_client, fake, clock):
    service = admin_client.app.state.environments
    started = admin_client.post("/api/aws/warm/start", json={"type_id": world["type"]}).json()["environment_id"]
    clock.advance(hours=12)
    service.tick()
    assert _row(sqlite_engine, environments_table, started).state == "booting"
    assert fake.terminated == []


def test_the_project_picker_needs_a_project_lead_and_is_audited(sqlite_engine, keyring, clock, world):
    lead, _ = add_user(sqlite_engine, keyring, clock, "lead")
    viewer, _ = add_user(sqlite_engine, keyring, clock, "viewer")
    add_membership(sqlite_engine, clock, lead, "project", world["bare"], "project_lead")
    add_membership(sqlite_engine, clock, viewer, "project", world["bare"], "contributor")
    path = f"/api/projects/{world['bare']}/environment-type"
    client = make_client(sqlite_engine, keyring, clock)
    client.headers.update(mutation(sign_in_as(client, sqlite_engine, keyring, clock, viewer)))
    assert client.post(path, json={"environment_type_id": world["type"]}).status_code == 403
    assert client.get("/api/environment-types").json()["types"][0]["name"] == "android"
    client = make_client(sqlite_engine, keyring, clock)
    client.headers.update(mutation(sign_in_as(client, sqlite_engine, keyring, clock, lead)))
    assert client.post(path, json={"environment_type_id": "nope"}).status_code == 400
    saved = client.post(path, json={"environment_type_id": world["type"]})
    assert saved.status_code == 200 and saved.json()["project"]["environment_type_id"] == world["type"]
    assert client.get(f"/api/projects/{world['bare']}").json()["project"]["environment_type_id"] == world["type"]
    with sqlite_engine.connect() as conn:
        detail = conn.execute(select(audit.c.detail).where(audit.c.action == "project.environment_type")).scalar_one()
        from armature.server.export import _teams
        exported = _teams(conn, world["team"])
    assert detail == {"from": None, "to": world["type"]}
    assert {p["name"]: p["environment_type"] for p in exported[0]["projects"]} == {"android": "android",
                                                                                   "bare": "android"}


def test_status_says_when_aws_is_not_configured(sqlite_engine, keyring, clock):
    admin, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    client = make_client(sqlite_engine, keyring, clock)
    client.headers.update(mutation(sign_in_as(client, sqlite_engine, keyring, clock, admin)))
    assert client.get("/api/status").json()["environments"]["message"] == "AWS not configured"


def test_the_user_data_holds_only_the_enrollment_token(keyring):
    token = environments.mint_enrollment(keyring, "11111111-2222-3333-4444-555555555555")
    script = environments.render_user_data(server_url="https://arm.test", token=token,
                                           env_id="11111111-2222-3333-4444-555555555555", capabilities=["git"],
                                           kind="ec2-linux", max_lifetime_s=600)
    assert token in script and "ark_" not in script and "--enroll-token-env ARMATURE_ENROLL_TOKEN" in script
    assert "iptables -I OUTPUT -m owner --uid-owner armature-job -d 169.254.169.254 -j REJECT" in script
    assert "iptables -I OUTPUT -m owner --uid-owner armature-git -d 169.254.169.254 -j REJECT" in script
    assert "shutdown -h +10" in script and "sudoers" not in script
    assert environments.mint_enrollment(keyring, "11111111-2222-3333-4444-555555555555") == token
    assert len(runners.digest(keyring, token)) == 64
    with pytest.raises(environments.EnvError):
        environments.set_type(None, {"kind": "ec2-linux", "image": "ami-0123456789abcdef0", "name": "x",
                                     "instance_types": []}, type_id=None, actor_id="a", now=None, ip=None)



def test_types_outside_the_allowed_instance_types_are_flagged(sqlite_engine, world, admin_client, fake, clock):
    _connection(sqlite_engine, allowed_instance_types=["m7i.large", "mac2.metal"], allow_host_allocation=True)
    body = {"name": "node", "kind": "ec2-linux", "image": "ami-0123456789abcdef1", "instance_types": ["t3.large"]}
    refused = admin_client.post("/api/aws/types", json=body)
    assert refused.status_code == 400 and "t3.large" in refused.json()["error"]
    assert "AccessDenied" in refused.json()["error"] and "AllowedInstanceTypes" in refused.json()["error"]
    assert admin_client.post("/api/aws/types", json={**body, "instance_types": ["m7i.large"]}).status_code == 200
    verified = admin_client.post("/api/aws/connection/verify").json()["connection"]
    assert verified["state"] == "error" and "Type android uses m6i.large" in verified["last_error"]
    host = admin_client.post("/api/aws/hosts", json={"name": "mac-1", "zone": "us-east-1a",
                                                     "instance_type": "mac-m4.metal", "confirm_cost": True})
    assert host.status_code == 400 and "mac-m4.metal" in host.json()["error"]


def test_a_launch_skips_instance_types_the_connection_no_longer_allows(sqlite_engine, service, fake, world, clock):
    with sqlite_engine.begin() as conn:
        conn.execute(update(environment_types).values(instance_types=["m6i.large", "m7i.large"]))
    _connection(sqlite_engine, allowed_instance_types=["m7i.large"],
                launch_template_ids={"linux": "lt-0123456789abcdef0"})
    _job(sqlite_engine, clock, world)
    service.tick()
    spec = next(iter(fake.specs.values()))
    assert spec.instance_types == ("m7i.large",) and spec.launch_template_id == "lt-0123456789abcdef0"


def test_allocating_a_host_starts_its_macos_instance_from_the_mac_template(sqlite_engine, service, fake, world, clock):
    _connection(sqlite_engine, allow_host_allocation=True, launch_template_ids={"mac": "lt-0123456789abcdef1"},
                mac_instance_profile="mac-profile")
    host = service.allocate_host({"name": "mac-1", "zone": "us-east-1a", "confirm_cost": True}, actor_id="a", ip=None)
    (call,) = [c for c in fake.calls if c[0] == "launch_host_instance"]
    assert call[1][1:] == ("subnet-0123456789abcdef0", "lt-0123456789abcdef1", "mac2.metal")
    assert host.state == "running" and host.instance_id in fake.instances


def test_env_build_runs_packer_with_the_type_installs(sqlite_engine, world, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from armature.cli import app
    installs = [{"name": "gradle", "version": "8.10", "url": "https://example.test/gradle.zip", "sha256": "a" * 64}]
    with sqlite_engine.begin() as conn:
        conn.execute(update(environment_types).values(installs=installs, disk_gb=80))
        conn.execute(update(aws_connections).values(extra_tags={"owner": "platform"},
                                                    security_group_id="sg-0123456789abcdef0"))
    deploy = tmp_path / "deploy"
    (deploy / "images").mkdir(parents=True)
    (deploy / "images" / "base.pkr.hcl").write_text("# stub\n")
    bin_dir, seen = tmp_path / "bin", tmp_path / "seen"
    bin_dir.mkdir()
    packer = bin_dir / "packer"
    packer.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {seen}/args\n"
                      f"for a in \"$@\"; do case \"$a\" in type_installs=*) cp \"${{a#type_installs=}}\" {seen}/installs;; esac; done\n")
    packer.chmod(0o755)
    seen.mkdir()
    base = tmp_path / "base.json"
    base.write_text("[]")
    config = tmp_path / "server.yaml"
    config.write_text(f"database_url: {sqlite_engine.url}\npublic_origin: https://arm.test\nmaster_key: env:NOPE\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    result = CliRunner().invoke(app, ["env", "build", "android", "--base-installs", str(base), "--deploy-dir",
                                      str(deploy), "--config", str(config)])
    assert result.exit_code == 0, result.output
    args = (seen / "args").read_text().splitlines()
    assert args[0] == "build" and args[-1] == str(deploy / "images" / "base.pkr.hcl")
    pairs = dict(a.split("=", 1) for a in args if "=" in a)
    assert pairs["type_name"] == "android" and pairs["region"] == "us-east-1" and pairs["install_id"] == "install-1"
    assert pairs["owner"] == "platform" and pairs["volume_size"] == "80" and pairs["instance_type"] == "m6i.large"
    assert json.loads((seen / "installs").read_text()) == installs
    missing = CliRunner().invoke(app, ["env", "build", "nope", "--base-installs", str(base), "--config", str(config)])
    assert missing.exit_code == 1 and "No environment type named nope" in missing.output


def test_env_build_runs_the_ios_script_for_a_mac_type(tmp_path):
    from types import SimpleNamespace

    from armature.server import envbuild
    script = tmp_path / "images" / "ios-vm" / "build.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    kind = SimpleNamespace(kind="ec2-mac", name="ios", base_image="macos-base", installs=[], instance_types=[],
                           disk_gb=None)
    with pytest.raises(envbuild.BuildError, match="--key-file"):
        envbuild.command(kind, None, installs=tmp_path / "i.json", base_installs=tmp_path / "b.json",
                         deploy_dir=tmp_path)
    command = envbuild.command(kind, None, installs=tmp_path / "i.json", base_installs=tmp_path / "b.json",
                               deploy_dir=tmp_path, key_file=tmp_path / "key")
    assert command == [str(script), "macos-base", "ios", str(tmp_path / "key"), str(tmp_path / "b.json"),
                       str(tmp_path / "i.json")]


# Review round 1

def test_the_worker_backstop_unsticks_a_provisioning_job_without_a_provider(sqlite_engine, keyring, world, clock):
    from armature.server import worker
    from armature.server.config import ServerConfig
    job_id = _job(sqlite_engine, clock, world)
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="provisioning", claimed_by="env:x",
                                                                    lease_expires_at=clock()))
    embedded = worker.Worker(sqlite_engine, keyring, ServerConfig(database_url=str(sqlite_engine.url)), None,
                             clock=clock)
    clock.advance(minutes=5)
    embedded.reap(clock())
    assert _row(sqlite_engine, jobs, job_id).status == "provisioning"
    clock.advance(minutes=6)
    embedded.reap(clock())
    assert _row(sqlite_engine, jobs, job_id).status == "queued"
    with sqlite_engine.begin() as conn:
        connection_id = conn.execute(select(aws_connections.c.id)).scalar_one()
        for _ in range(2):
            conn.execute(insert(environments_table).values(id=new_id(), connection_id=connection_id, purpose="job",
                                                           job_id=job_id, state="failed", launch_attempts=1,
                                                           requested_at=clock()))
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="provisioning", lease_expires_at=clock()))
    clock.advance(minutes=11)
    embedded.reap(clock())
    job = _row(sqlite_engine, jobs, job_id)
    assert job.status == "failed" and job.error == "No environment started for this job."


def test_an_unverified_connection_stops_launches_but_keeps_reaping(sqlite_engine, keyring, service, fake, world, clock):
    job_id = _job(sqlite_engine, clock, world)
    service.tick()
    (env,) = _envs(sqlite_engine)
    service.loader = lambda keyring, row: fake if row.state == "ok" else None
    _connection(sqlite_engine, state="unverified", updated_at=clock())
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="succeeded", finished_at=clock()))
    later = _job(sqlite_engine, clock, world)
    service.tick()
    assert _row(sqlite_engine, environments_table, env.id).state == "terminated"
    assert _row(sqlite_engine, jobs, later).status == "queued" and len(_launches(fake)) == 1


def test_only_identity_changes_unverify_the_connection(sqlite_engine, admin_client):
    admin_client.post("/api/aws/connection", json=CONNECTION)
    assert admin_client.post("/api/aws/connection/verify").json()["connection"]["state"] == "ok"
    same = {**CONNECTION, "access_key_id": None, "secret_access_key": None}
    kept = admin_client.post("/api/aws/connection", json={**same, "max_concurrent": 9}).json()["connection"]
    assert kept["state"] == "ok" and kept["verified_at"]
    moved = admin_client.post("/api/aws/connection", json={**same, "region": "us-east-2",
                                                           "subnets": [{"zone": "us-east-2a",
                                                                        "subnet_id": "subnet-0abc12345"}]})
    assert moved.json()["connection"]["state"] == "unverified"
    admin_client.post("/api/aws/connection/verify")
    rekeyed = admin_client.post("/api/aws/connection", json={**CONNECTION, "region": "us-east-2"})
    assert rekeyed.json()["connection"]["state"] == "unverified"
    assert admin_client.post("/api/aws/connection", json={**CONNECTION, "install_id": "a" * 13}).status_code == 400
    assert admin_client.post("/api/aws/connection", json={**CONNECTION, "install_id": "ab"}).status_code == 200


def test_retryable_failures_count_toward_the_cap(sqlite_engine, service, fake, world, clock):
    job_id = _job(sqlite_engine, clock, world)
    fake.fail.extend(ProvisionError("no capacity", retryable=True) for _ in range(2))
    service.tick()
    assert _row(sqlite_engine, jobs, job_id).status == "queued"
    clock.advance(minutes=2)
    service.tick()
    job = _row(sqlite_engine, jobs, job_id)
    assert job.status == "failed" and "no capacity" in job.error and len(_envs(sqlite_engine)) == 2


def test_failed_launches_without_an_instance_skip_the_daily_count(sqlite_engine, service, fake, world, clock):
    _connection(sqlite_engine, max_daily_launches=1)
    _job(sqlite_engine, clock, world)
    fake.fail.append(ProvisionError("UnauthorizedOperation"))
    service.tick()
    later = _job(sqlite_engine, clock, world)
    service.tick()
    assert _row(sqlite_engine, jobs, later).status == "provisioning"


def test_two_servers_launch_an_environment_once(sqlite_engine, service, fake, world, clock, monkeypatch):
    job_id = _job(sqlite_engine, clock, world)
    with sqlite_engine.connect() as conn:
        row = environments.connection(conn)
        kind = conn.execute(select(environment_types)).one()
        job = conn.execute(select(jobs).where(jobs.c.id == job_id)).one()
    env_id = service.claim(row, kind, job, clock())
    real_spec = service.spec

    def other_server_bumps_first(conn, row, env):
        with sqlite_engine.begin() as other:
            other.execute(update(environments_table).where(environments_table.c.id == env_id)
                          .values(launch_attempts=environments_table.c.launch_attempts + 1))
        return real_spec(conn, row, env)

    monkeypatch.setattr(service, "spec", other_server_bumps_first)
    assert service.launch(fake, env_id, clock()) is False
    assert _launches(fake) == [] and _row(sqlite_engine, environments_table, env_id).launch_attempts == 1


def test_verify_flags_types_without_a_template_or_bucket(sqlite_engine, world, admin_client, clock):
    admin_client.post("/api/aws/connection", json=CONNECTION)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(environment_types).values(
            id=new_id(), name="ios", kind="ec2-mac", image="ghcr.io/org/macos:15", market="on-demand",
            capabilities=["platform:ios"], job_timeout_s=3600, enabled=True, created_at=clock(), updated_at=clock()))
    error = admin_client.post("/api/aws/connection/verify").json()["connection"]["last_error"]
    assert "Type android is ec2-linux, and the connection has no Linux launch template." in error
    assert "Type ios is ec2-mac, which needs the connection's artifact bucket" in error
    fixed = admin_client.post("/api/aws/connection", json={**CONNECTION, "artifact_bucket": "env-artifacts",
                                                           "launch_template_ids": {"linux": "lt-0123456789abcdef0"}})
    assert fixed.status_code == 200
    assert admin_client.post("/api/aws/connection/verify").json()["connection"]["state"] == "ok"


def test_the_runner_unit_keeps_retrying_until_the_boot_timeout(keyring):
    script = environments.render_user_data(server_url="https://arm.test", token="are_x", env_id="e" * 36,
                                           capabilities=[], kind="ec2-linux", max_lifetime_s=600)
    unit = script.split("[Unit]", 1)[1].split("EOF", 1)[0]
    head, service_part = unit.split("[Service]")
    assert "StartLimitIntervalSec=0" in head and "RestartSec=10" in service_part


# Review round 3

def test_install_id_and_project_tag_hold_while_anything_is_live(sqlite_engine, world, admin_client, fake, clock):
    admin_client.post("/api/aws/connection", json={**CONNECTION, "install_id": "install-1"})
    admin_client.post("/api/aws/connection/verify")
    same = {**CONNECTION, "access_key_id": None, "secret_access_key": None, "install_id": "install-1"}
    _job(sqlite_engine, clock, world)
    admin_client.app.state.environments.tick()
    for change in ({"install_id": "install-2"}, {"tag_project": "armature-other"}):
        refused = admin_client.post("/api/aws/connection", json={**same, **change})
        assert refused.status_code == 409 and "install id or project tag" in refused.json()["error"]
    with sqlite_engine.begin() as conn:
        conn.execute(update(environments_table).values(state="terminated"))
    host = _mac(sqlite_engine, clock, world)
    assert admin_client.post("/api/aws/connection", json={**same, "install_id": "install-2"}).status_code == 409
    with sqlite_engine.begin() as conn:
        conn.execute(update(environment_hosts).where(environment_hosts.c.id == host).values(state="released"))
    moved = admin_client.post("/api/aws/connection", json={**same, "install_id": "install-2"}).json()["connection"]
    assert moved["install_id"] == "install-2" and moved["state"] == "unverified"
    admin_client.post("/api/aws/connection/verify")
    retagged = admin_client.post("/api/aws/connection", json={**same, "install_id": "install-2",
                                                              "tag_project": "armature-other"}).json()["connection"]
    assert retagged["tag_project"] == "armature-other" and retagged["state"] == "unverified"


def test_the_backstop_fails_an_environment_left_booting_by_a_restart(sqlite_engine, keyring, service, fake, world,
                                                                     clock):
    from armature.server import worker
    from armature.server.config import ServerConfig
    job_id = _job(sqlite_engine, clock, world)
    service.tick()
    (env,) = _envs(sqlite_engine)
    key = fake.boot(make_client(sqlite_engine, keyring, clock), env.id)
    with sqlite_engine.begin() as conn:
        conn.execute(update(environments_table).values(state="booting"))
        conn.execute(update(jobs).values(lease_expires_at=clock() + timedelta(days=1)))
    embedded = worker.Worker(sqlite_engine, keyring, ServerConfig(database_url=str(sqlite_engine.url)), None,
                             clock=clock)
    clock.advance(seconds=420 + 9 * 60)
    embedded.reap(clock())
    assert _row(sqlite_engine, environments_table, env.id).state == "booting"
    clock.advance(minutes=2)
    embedded.reap(clock())
    env = _row(sqlite_engine, environments_table, env.id)
    assert env.state == "failed" and env.slot_index is None
    assert _row(sqlite_engine, jobs, job_id).status == "queued" and fake.terminated == []
    assert _row(sqlite_engine, runner_keys, env.runner_key_id).revoked_at is not None
    assert make_client(sqlite_engine, keyring, clock).post(
        "/api/runner/hello", json={"name": "x"}, headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_an_unverified_connection_never_relaunches(sqlite_engine, service, fake, world, clock):
    job_id = _job(sqlite_engine, clock, world)
    service.tick()
    with sqlite_engine.connect() as conn:
        row = environments.connection(conn)
        kind = conn.execute(select(environment_types)).one()
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="succeeded"))
    second = _job(sqlite_engine, clock, world)
    with sqlite_engine.connect() as conn:
        job = conn.execute(select(jobs).where(jobs.c.id == second)).one()
    pending = service.claim(row, kind, job, clock())
    service.loader = lambda keyring, row: fake if row.state == "ok" else None
    _connection(sqlite_engine, state="unverified", updated_at=clock())
    clock.advance(minutes=3)
    service.tick()
    assert [c[1] for c in _launches(fake)] == [_envs(sqlite_engine)[0].id]
    assert _row(sqlite_engine, environments_table, pending).state == "requested"

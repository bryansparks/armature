"""`armature runner` against a real server: it claims a stored workflow's job, runs it, and the job settles."""
from __future__ import annotations

import fcntl
import grp
import json
import os
import resource
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
import typer
from sqlalchemy import insert, select, update

from armature.runner import cli, process
from armature.runner import client as runner_client
from armature.runner.client import Client, Runner, RunnerLocked, ServerError
from armature.runner.process import armature_binary
from armature.server import defs, environments, runners
from armature.server.app.routes_runner import ENROLL_LIMIT
from armature.server.db import new_id
from armature.server.db.schema import (
    aws_connections,
    environment_hosts,
    environment_types,
    jobs,
    runner_keys,
    runs,
    settings,
)
from armature.server.db.schema import environments as environments_table
from tests.server.app_support import Clock, add_membership, add_user, make_client
from tests.server.live_support import LiveServer, add_team

REPO = Path(__file__).resolve().parents[2]

GREET = """name: greet
requires: [llm]
model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
role_type_defaults:
  worker: small
contracts:
  inputs:
    - name: who
stages:
  - id: greet
    role:
      name: Greeter
      type: worker
      description: "Say hello to {{ who }}."
    output_mode: text
    depends_on: []
"""

SLOW = """name: slow
stages:
  - id: wait
    adapter: wait
    depends_on: []
adapters:
  wait:
    name: wait
    type: script
    cmd: sleep 60
"""


def _wait(check, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def binary():
    found = armature_binary()
    if found is None:
        pytest.skip("the armature command is not installed")
    return found


@pytest.fixture
def world(sqlite_engine, keyring, tmp_path):
    clock = Clock()
    team, project = add_team(sqlite_engine, clock, "own")
    stubs = tmp_path / "stubs.json"
    stubs.write_text(json.dumps({"greet": {"content": "hello, octocat"}}))
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values([
            {"team_id": None, "key": "worker.enabled", "value": False},
            {"team_id": team, "key": "env.ARMATURE_STUB_OUTPUTS", "value": str(stubs)},
            {"team_id": team, "key": "env.PYTHONPATH", "value": str(REPO)}]))
        _, key = runners.create_key(conn, keyring, team_id=team, name="ci", actor_id=None, now=clock(), ip=None)
    return {"clock": clock, "team": team, "project": project, "key": key}


def _queue(engine, world, name, body, inputs, requires=None):
    workflow, job_id = new_id(), new_id()
    clock = world["clock"]
    with engine.begin() as conn:
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name=name, current_version=1, created_at=clock(),
                                                        updated_at=clock(),
                                                        **defs.DefScope("team", world["team"]).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, body, "seed", None, clock())
        conn.execute(insert(jobs).values(id=job_id, team_id=world["team"], project_id=world["project"],
                                         workflow_id=workflow, workflow_version=1, inputs=inputs, requires=requires,
                                         status="queued", attempts=0, created_at=clock()))
    return job_id


def _job(engine, job_id):
    with engine.connect() as conn:
        return conn.execute(select(jobs).where(jobs.c.id == job_id)).one()


def _run(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(runs).where(runs.c.id == run_id)).one()


def test_a_runner_process_claims_and_runs_a_stored_workflow(sqlite_engine, keyring, world, binary, tmp_path):
    job_id = _queue(sqlite_engine, world, "greet", GREET, {"who": "octocat"}, requires=["llm"])
    work = tmp_path / "work"
    with LiveServer(sqlite_engine, keyring, world["clock"], socket=False) as live:
        env = {**os.environ, "PYTHONPATH": str(REPO), "RUNNER_KEY": world["key"]}
        proc = subprocess.Popen([sys.executable, "-m", "armature", "runner", "--server", live.origin,
                                 "--key-env", "RUNNER_KEY", "--capability", "llm", "--name", "e2e",
                                 "--work-dir", str(work), "--poll", "0.2"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            done = _wait(lambda: (row := _job(sqlite_engine, job_id)).finished_at is not None and row, timeout=90)
        finally:
            proc.terminate()
            output = proc.communicate(timeout=30)[0].decode()
        assert done.status == "succeeded", (done.error, output)
        live.flush()
        run = _run(sqlite_engine, done.run_id)
    assert run.status == "succeeded" and run.workflow_name == "greet" and run.trigger_kind == "webhook"
    assert done.runner_id is not None and done.claimed_by == f"runner:{done.runner_id}"
    assert proc.returncode == 0 and "polling" in output
    assert [p.name for p in (work / "e2e").iterdir()] == [".lock"]


def test_a_canceled_job_ends_its_run_as_canceled(sqlite_engine, keyring, world, binary, tmp_path):
    job_id = _queue(sqlite_engine, world, "slow", SLOW, {})
    lead, _ = add_user(sqlite_engine, keyring, world["clock"], "lead")
    add_membership(sqlite_engine, world["clock"], lead, "team", world["team"], "contributor")
    stop = threading.Event()
    with LiveServer(sqlite_engine, keyring, world["clock"], socket=False) as live:
        client = Client(live.origin, world["key"])
        runner = Runner(client, name="in-process", capabilities=[], slots=1, work_dir=tmp_path / "work",
                        binary=binary, poll=0.2, heartbeat=0.5)
        thread = threading.Thread(target=runner.run, args=(stop,), daemon=True)
        thread.start()
        try:
            running = _wait(lambda: (row := _job(sqlite_engine, job_id)).status == "running" and row)
            user = live.client(lead)
            response = user.post(f"/api/runs/{running.run_id}/cancel", headers=live.csrf(user))
            assert response.status_code == 200, response.text
            done = _wait(lambda: (row := _job(sqlite_engine, job_id)).finished_at is not None and row, timeout=60)
        finally:
            stop.set()
            thread.join(30)
            client.close()
        live.flush()
        run = _run(sqlite_engine, done.run_id)
    assert done.status == "canceled" and run.status == "canceled"


def test_a_runner_stops_a_job_the_server_gave_up_and_clears_old_bundles(sqlite_engine, keyring, world, binary,
                                                                        tmp_path):
    work = tmp_path / "work"
    home = work / "in-process"
    (home / "job-stale").mkdir(parents=True)
    (home / "job-stale" / "workflow.yaml").write_text("secret: left behind")
    (home / "notes.txt").write_text("keep me")
    (work / "other-runner" / "job-theirs").mkdir(parents=True)
    job_id = _queue(sqlite_engine, world, "slow", SLOW, {})
    stop = threading.Event()
    with LiveServer(sqlite_engine, keyring, world["clock"], socket=False) as live:
        client = Client(live.origin, world["key"])
        runner = Runner(client, name="in-process", capabilities=[], slots=1, work_dir=work, binary=binary,
                        poll=0.2, heartbeat=0.5)
        thread = threading.Thread(target=runner.run, args=(stop,), daemon=True)
        thread.start()
        try:
            running = _wait(lambda: (row := _job(sqlite_engine, job_id)).status == "running" and row)
            assert not (home / "job-stale").exists()
            assert (home / "notes.txt").exists() and (work / "other-runner" / "job-theirs").is_dir()
            with sqlite_engine.begin() as conn:
                conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="lost", error="lease expired"))
            _wait(lambda: not list(home.glob("job-*")), timeout=30)
            live.flush()
            run = _wait(lambda: (row := _run(sqlite_engine, running.run_id)).status != "running" and row)
        finally:
            stop.set()
            thread.join(30)
            client.close()
    assert run.status == "failed" and "gave this job up" in run.error
    assert _job(sqlite_engine, job_id).status == "lost"


def test_a_second_runner_with_the_same_name_refuses_to_start(world, tmp_path):
    work = tmp_path / "work"
    (work / "twin").mkdir(parents=True)
    with open(work / "twin" / ".lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = Runner(Client("http://127.0.0.1:9", world["key"]), name="twin", capabilities=[], slots=1,
                        work_dir=work, binary="/bin/false")
        with pytest.raises(RunnerLocked, match="already running"):
            runner.run(threading.Event())
        env = {**os.environ, "PYTHONPATH": str(REPO), "RUNNER_KEY": world["key"]}
        refused = subprocess.run([sys.executable, "-m", "armature", "runner", "--server", "http://127.0.0.1:9",
                                  "--key-env", "RUNNER_KEY", "--name", "twin", "--work-dir", str(work)],
                                 env=env, capture_output=True, text=True, timeout=60)
    assert refused.returncode == 1 and "A runner named twin is already running" in refused.stderr


def test_the_child_process_env_holds_only_the_allowed_names(world, tmp_path, monkeypatch):
    monkeypatch.setenv("ARMATURE_RUNNER_KEY", "leak-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-aws")
    monkeypatch.setenv("ARMATURE_PACKS_PATH", "/opt/packs")
    fake = tmp_path / "fake-armature"
    fake.write_text("#!/bin/sh\nenv\n")
    fake.chmod(0o755)
    runner = Runner(Client("http://127.0.0.1:9", world["key"]), name="envy", capabilities=[], slots=1,
                    work_dir=tmp_path / "work", binary=str(fake))
    bundle = {"settings": {"REGION": "us-east-1"}, "secrets": {"API_TOKEN": "tok"},
              "events_url": "https://x/api/events", "run_token": "rt"}
    workdir = tmp_path / "job"
    workdir.mkdir()
    from armature.runner.process import popen
    proc = popen(str(fake), workdir, [], runner.env(bundle))
    proc.wait(30)
    seen = {line.split("=", 1)[0] for line in (workdir / "run.log").read_text().splitlines() if "=" in line}
    seen -= {"PWD", "SHLVL", "_", "OLDPWD"}
    assert seen == {"PATH", "HOME", "ARMATURE_PACKS_PATH", "REGION", "API_TOKEN", "ARMATURE_EVENTS",
                    "ARMATURE_EVENTS_TOKEN", "ARMATURE_TOOLS_URL"}


@pytest.mark.parametrize(("memory_mb", "limit"), [(1, 1 << 20), (None, resource.getrlimit(resource.RLIMIT_FSIZE)[0])])
def test_popen_sets_the_memory_limit_only_when_asked(tmp_path, monkeypatch, memory_mb, limit):
    monkeypatch.setattr(process, "MEMORY_LIMIT", resource.RLIMIT_FSIZE)
    fake = tmp_path / "fake-armature"
    fake.write_text(f"#!{sys.executable}\nimport resource\nprint(resource.getrlimit(resource.RLIMIT_FSIZE)[0])\n")
    fake.chmod(0o755)
    workdir = tmp_path / "job"
    workdir.mkdir()
    process.popen(str(fake), workdir, [], {"PATH": "/bin:/usr/bin"}, memory_mb=memory_mb).wait(30)
    assert int((workdir / "run.log").read_text().strip()) == limit


@pytest.fixture
def booted(sqlite_engine, keyring, world):
    """A job with a launched environment, as the environment service leaves it."""
    clock = world["clock"]
    connection_id, kind, env_id = new_id(), new_id(), new_id()
    job_id = _queue(sqlite_engine, world, "greet", GREET, {"who": "octocat"}, requires=["git"])
    other = _queue(sqlite_engine, world, "greet-2", GREET, {"who": "octocat"}, requires=["git"])
    token = environments.mint_enrollment(keyring, env_id)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(aws_connections).values(
            id=connection_id, name="default", region="us-east-1", auth_kind="access_key",
            tag_project="armature-platform", install_id="install-1", allow_host_allocation=False, max_concurrent=4,
            max_daily_launches=50, boot_timeout_s=420, max_lifetime_s=5400, state="ok", created_at=clock(),
            updated_at=clock()))
        conn.execute(insert(environment_types).values(
            id=kind, name="linux", kind="ec2-linux", image="ami-0123456789abcdef0", instance_types=["m6i.large"],
            market="on-demand", capabilities=["git"], job_timeout_s=3600, enabled=True, created_at=clock(),
            updated_at=clock()))
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="provisioning", claimed_by=f"env:{env_id}"))
        conn.execute(insert(environments_table).values(
            id=env_id, connection_id=connection_id, type_id=kind, purpose="job", job_id=job_id, team_id=world["team"],
            project_id=world["project"], state="booting", launch_attempts=1, requested_at=clock(),
            enroll_hmac=runners.digest(keyring, token), enroll_expires_at=clock() + timedelta(seconds=420)))
    return {"env": env_id, "job": job_id, "other": other, "token": token}


def _environment(engine, env_id):
    with engine.connect() as conn:
        return conn.execute(select(environments_table).where(environments_table.c.id == env_id)).one()


def test_an_enrollment_token_works_once(sqlite_engine, keyring, world, booted):
    client = make_client(sqlite_engine, keyring, world["clock"])
    assert client.post("/api/runner/enroll", json={"token": "are_" + "x" * 43}).status_code == 401
    assert client.post("/api/runner/enroll", json={"token": "nope"}).status_code == 401
    first = client.post("/api/runner/enroll", json={"token": booted["token"], "name": "env-1"})
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.json()["server"] == "https://arm.test" and first.json()["key"].startswith("ark_")
    replay = client.post("/api/runner/enroll", json={"token": booted["token"]})
    assert replay.status_code == 410
    env = _environment(sqlite_engine, booted["env"])
    with sqlite_engine.connect() as conn:
        key = conn.execute(select(runner_keys).where(runner_keys.c.id == env.runner_key_id)).one()
    assert key.kind == "ephemeral" and key.team_id is None
    assert key.capabilities == sorted(["git", f"job:{booted['job']}"])
    assert key.expires_at == world["clock"]() + timedelta(seconds=3600, minutes=15)


def test_an_expired_enrollment_token_is_refused(sqlite_engine, keyring, world, booted):
    world["clock"].advance(seconds=421)
    client = make_client(sqlite_engine, keyring, world["clock"])
    expired = client.post("/api/runner/enroll", json={"token": booted["token"]})
    assert expired.status_code == 410 and "expired" in expired.json()["error"]
    assert _environment(sqlite_engine, booted["env"]).enrolled_at is None


def test_enrollment_is_rate_limited_per_address(sqlite_engine, keyring, world, booted):
    client = make_client(sqlite_engine, keyring, world["clock"])
    codes = [client.post("/api/runner/enroll", json={"token": "nope"}).status_code for _ in range(ENROLL_LIMIT + 1)]
    assert codes[:-1] == [401] * ENROLL_LIMIT and codes[-1] == 429
    assert client.post("/api/runner/enroll", json={"token": booted["token"]}).status_code == 429


def test_an_ephemeral_key_claims_only_its_own_job(sqlite_engine, keyring, world, booted):
    client = make_client(sqlite_engine, keyring, world["clock"])
    key = client.post("/api/runner/enroll", json={"token": booted["token"]}).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    runner_id = client.post("/api/runner/hello", json={"name": "env", "capabilities": ["git"]},
                            headers=auth).json()["runner_id"]
    assert _environment(sqlite_engine, booted["env"]).state == "ready"
    claimed = client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=auth)
    assert claimed.status_code == 200 and claimed.json()["job"]["job_id"] == booted["job"]
    assert client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=auth).status_code == 204
    assert _job(sqlite_engine, booted["other"]).status == "queued"
    assert _job(sqlite_engine, booted["job"]).status == "running"
    assert _environment(sqlite_engine, booted["env"]).state == "running"
    standing = world["key"]
    other_runner = client.post("/api/runner/hello", json={"name": "ci", "capabilities": ["git"]},
                               headers={"Authorization": f"Bearer {standing}"}).json()["runner_id"]
    taken = client.post("/api/runner/claim", json={"runner_id": other_runner},
                        headers={"Authorization": f"Bearer {standing}"})
    assert taken.json()["job"]["job_id"] == booted["other"]


def _claim_in_environment(sqlite_engine, keyring, world, booted):
    client = make_client(sqlite_engine, keyring, world["clock"])
    key = client.post("/api/runner/enroll", json={"token": booted["token"]}).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    runner_id = client.post("/api/runner/hello", json={"name": "env", "capabilities": ["git"],
                                                       "platform": {"kind": "local", "chip": "Apple M1"}},
                            headers=auth).json()["runner_id"]
    claimed = client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=auth)
    run_id = claimed.json()["job"]["run_id"]
    with sqlite_engine.connect() as conn:
        return conn.execute(select(runs.c.location).where(runs.c.id == run_id)).scalar_one()


def test_a_run_claimed_in_a_linux_environment_is_located_there(sqlite_engine, keyring, world, booted):
    with sqlite_engine.begin() as conn:
        conn.execute(update(environments_table).where(environments_table.c.id == booted["env"])
                     .values(provider_id="i-0c35a1b2c3d4e5f60", instance_type="m6i.large"))
    assert _claim_in_environment(sqlite_engine, keyring, world, booted) == {
        "kind": "aws-linux", "label": "AWS Linux", "instance": "i-0c35a1b2c3d4e5f60", "environment_type": "linux",
        "instance_type": "m6i.large"}


def test_a_run_claimed_on_a_mac_host_is_located_on_aws_macos(sqlite_engine, keyring, world, booted):
    clock, host_id = world["clock"], new_id()
    with sqlite_engine.begin() as conn:
        connection_id = conn.execute(select(environments_table.c.connection_id)
                                     .where(environments_table.c.id == booted["env"])).scalar_one()
        conn.execute(insert(environment_hosts).values(
            id=host_id, connection_id=connection_id, name="mac-1", instance_id="i-0mac0000000000001",
            instance_type="mac2.metal", state="running", slots=2, allocated_at=clock(), min_release_at=clock()))
        conn.execute(update(environments_table).where(environments_table.c.id == booted["env"])
                     .values(host_id=host_id, slot_index=0, provider_id="i-0mac0000000000001/0"))
    located = _claim_in_environment(sqlite_engine, keyring, world, booted)
    assert located["kind"] == "aws-mac" and located["label"] == "AWS macOS"
    assert located["instance"] == "i-0mac0000000000001"


def test_a_standing_key_never_sees_a_provisioning_job(sqlite_engine, keyring, world, booted):
    client = make_client(sqlite_engine, keyring, world["clock"])
    auth = {"Authorization": f"Bearer {world['key']}"}
    runner_id = client.post("/api/runner/hello", json={"name": "ci", "capabilities": ["git"]},
                            headers=auth).json()["runner_id"]
    assert client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=auth).json()["job"]["job_id"] \
        == booted["other"]
    assert client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=auth).status_code == 204


def test_the_runner_enrolls_once_and_keeps_its_key(sqlite_engine, keyring, world, booted, tmp_path, monkeypatch):
    server = make_client(sqlite_engine, keyring, world["clock"])
    monkeypatch.setattr(runner_client, "enroll", lambda origin, token, name: _via(server, token, name))
    monkeypatch.setenv("ENROLL", booted["token"])
    key_file = tmp_path / "runner.env"
    key = cli._enrolled_key("https://arm.test", "ENROLL", key_file, "env-1")
    assert key.startswith("ark_") and oct(key_file.stat().st_mode & 0o777) == "0o600"
    assert key_file.read_text() == f"ARMATURE_RUNNER_KEY={key}\n"
    assert cli._enrolled_key("https://arm.test", "ENROLL", key_file, "env-1") == key
    key_file.unlink()
    with pytest.raises(typer.Exit):
        cli._enrolled_key("https://arm.test", "ENROLL", key_file, "env-1")


def _via(server, token, name):
    response = server.post("/api/runner/enroll", json={"token": token, "name": name})
    if response.status_code >= 400:
        raise ServerError(response.status_code, response.json()["error"])
    return response.json()["key"]


def test_a_job_user_runs_through_sudo_with_only_variable_names_on_the_command_line(tmp_path, monkeypatch):
    started = {}

    class Popen:
        def __init__(self, command, **kwargs):
            started.update(command=command, **kwargs)

    monkeypatch.setattr(process.subprocess, "Popen", Popen)
    monkeypatch.setattr(process.grp, "getgrnam", lambda name: grp.getgrgid(os.getgid()))
    workdir = tmp_path / "bundle"
    process.write_bundle(workdir, "name: x\n", {"manifest.json": "{}", "child@1.yaml": "name: c\n"})
    run_dir = process.share(workdir, "job-1", "armature-job", base=tmp_path)
    process.popen("/usr/local/bin/armature", workdir, ["--input", "a=b"], {"API_TOKEN": "s3cr3t", "PATH": "/bin"},
                  job_user="armature-job", run_dir=run_dir)
    assert started["command"][:6] == ["sudo", "-n", "-u", "armature-job", "--preserve-env=API_TOKEN,PATH", "--"]
    assert started["command"][6:9] == ["/usr/local/bin/armature", "run", str(run_dir / "workflow.yaml")]
    assert "s3cr3t" not in " ".join(started["command"]) and started["cwd"] == run_dir
    assert oct(run_dir.stat().st_mode & 0o7777) == "0o2770" and oct(workdir.stat().st_mode & 0o777) == "0o700"
    assert (run_dir / "calls" / "child@1.yaml").read_text() == "name: c\n"
    assert (run_dir / "calls" / "manifest.json").read_text() == "{}"
    assert oct((run_dir / "workflow.yaml").stat().st_mode & 0o777) == "0o640"


def test_an_environment_key_cannot_register_runs(sqlite_engine, keyring, world, booted):
    client = make_client(sqlite_engine, keyring, world["clock"])
    key = client.post("/api/runner/enroll", json={"token": booted["token"]}).json()["key"]
    refused = client.post("/api/runner/runs", json={"workflow": "greet"}, headers={"Authorization": f"Bearer {key}"})
    assert refused.status_code == 403
    with sqlite_engine.begin() as conn:
        conn.execute(update(environments_table).where(environments_table.c.id == booted["env"]).values(purpose="warm"))
        conn.execute(update(runner_keys).values(kind="standing"))
    warm = client.post("/api/runner/runs", json={"workflow": "greet"}, headers={"Authorization": f"Bearer {key}"})
    assert warm.status_code == 403
    ordinary = client.post("/api/runner/runs", json={"workflow": "greet"},
                           headers={"Authorization": f"Bearer {world['key']}"})
    assert ordinary.status_code == 200


def test_a_bound_key_claims_only_while_its_environment_and_job_are_provisioning(sqlite_engine, keyring, world, booted):
    client = make_client(sqlite_engine, keyring, world["clock"])
    key = client.post("/api/runner/enroll", json={"token": booted["token"]}).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    runner_id = client.post("/api/runner/hello", json={"name": "env"}, headers=auth).json()["runner_id"]
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == booted["job"]).values(status="queued"))
    assert client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=auth).status_code == 204
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == booted["job"]).values(status="provisioning"))
        conn.execute(update(environments_table).values(state="terminating"))
    assert client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=auth).status_code == 204
    with sqlite_engine.begin() as conn:
        conn.execute(update(environments_table).values(state="ready"))
    assert client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=auth).json()["job"]["job_id"] \
        == booted["job"]


def test_enrollment_refuses_an_environment_that_is_no_longer_booting(sqlite_engine, keyring, world, booted):
    with sqlite_engine.begin() as conn:
        conn.execute(update(environments_table).values(state="terminating"))
    client = make_client(sqlite_engine, keyring, world["clock"])
    assert client.post("/api/runner/enroll", json={"token": booted["token"]}).status_code == 410
    assert _environment(sqlite_engine, booted["env"]).enrolled_at is None


def test_unshare_kills_the_job_users_processes_before_removing_its_files(tmp_path, monkeypatch):
    issued = []
    monkeypatch.setattr(process.subprocess, "run", lambda argv, **kwargs: issued.append(argv))
    run_dir = tmp_path / "armature-job-1"
    run_dir.mkdir()
    process.unshare(run_dir, "armature-job")
    assert issued[0] == ["sudo", "-n", "-u", "armature-job", "/bin/kill", "-KILL", "-1"]
    assert not run_dir.exists() and len(issued) == 1


def test_a_bound_claim_needs_its_environment_to_move_to_running(sqlite_engine, keyring, world, booted,
                                                                 monkeypatch):
    from armature.server import dispatch
    client = make_client(sqlite_engine, keyring, world["clock"])
    key = client.post("/api/runner/enroll", json={"token": booted["token"]}).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    runner_id = client.post("/api/runner/hello", json={"name": "env"}, headers=auth).json()["runner_id"]
    # The environment ends between the listing and the claim, as when the reaper gets there first.
    monkeypatch.setattr(dispatch.worker, "claimable", lambda conn, **kwargs: [booted["job"]])
    with sqlite_engine.begin() as conn:
        conn.execute(update(environments_table).values(state="terminating"))
    assert client.post("/api/runner/claim", json={"runner_id": runner_id}, headers=auth).status_code == 204
    assert _job(sqlite_engine, booted["job"]).status == "provisioning"
    assert _environment(sqlite_engine, booted["env"]).state == "terminating"


def test_job_user_refuses_more_than_one_slot(world, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO), "RUNNER_KEY": world["key"]}
    refused = subprocess.run([sys.executable, "-m", "armature", "runner", "--server", "http://127.0.0.1:9",
                              "--key-env", "RUNNER_KEY", "--job-user", "nobody", "--slots", "2",
                              "--work-dir", str(tmp_path)], env=env, capture_output=True, text=True, timeout=60,
                             check=False)
    assert refused.returncode == 1 and "--job-user needs --slots 1" in refused.stderr


def test_skills_are_written_and_shared_with_the_job_user(tmp_path, monkeypatch):
    monkeypatch.setattr(process.grp, "getgrnam", lambda name: grp.getgrgid(os.getgid()))
    workdir = tmp_path / "bundle"
    process.write_bundle(workdir, "name: x\n", {}, {"kotlin-review": "---\nname: kotlin-review\n---\n"})
    assert oct((workdir / "skills" / "kotlin-review").stat().st_mode & 0o777) == "0o700"
    run_dir = process.share(workdir, "job-1", "armature-job", base=tmp_path)
    shared = run_dir / "skills" / "kotlin-review" / "SKILL.md"
    assert shared.read_text() == "---\nname: kotlin-review\n---\n"
    assert oct(shared.stat().st_mode & 0o777) == "0o640"
    assert oct((run_dir / "skills").stat().st_mode & 0o7777) == "0o2770"
    with pytest.raises(ValueError, match="skill name"):
        process.write_bundle(tmp_path / "other", "name: x\n", {}, {"../escape": "x"})


@pytest.mark.parametrize("args, message", [
    (["--capability", "claude-code"], "--capability claude-code needs --job-user and --agent-user"),
    (["--capability", "codex"], "--capability codex needs --job-user and --agent-user"),
    (["--agent-user", "nobody"], "--agent-user needs --job-user"),
])
def test_agent_sessions_need_a_job_user(world, tmp_path, args, message):
    env = {**os.environ, "PYTHONPATH": str(REPO), "RUNNER_KEY": world["key"]}
    refused = subprocess.run([sys.executable, "-m", "armature", "runner", "--server", "http://127.0.0.1:9",
                              "--key-env", "RUNNER_KEY", "--work-dir", str(tmp_path), *args], env=env,
                             capture_output=True, text=True, timeout=60, check=False)
    assert refused.returncode == 1 and message in refused.stderr


def test_an_agent_user_reaches_the_run_and_is_cleaned_up(world, tmp_path, monkeypatch):
    started, cleaned = {}, []
    monkeypatch.setattr(runner_client, "share", lambda workdir, job_id, user: tmp_path / "shared")
    monkeypatch.setattr(runner_client, "popen", lambda binary, workdir, args, env, **kw: started.update(env=env))
    monkeypatch.setattr(runner_client, "unshare", lambda *args: cleaned.append(args))
    runner = Runner(Client("http://127.0.0.1:9", world["key"]), name="agents", capabilities=[], slots=1,
                    work_dir=tmp_path / "work", binary="/bin/true", job_user="armature-job",
                    agent_user="armature-agent")
    monkeypatch.setattr(runner, "_watch", lambda bundle, proc, workdir: ("succeeded", None))
    monkeypatch.setattr(runner, "_finish", lambda *args: None)
    runner.run_job({"job_id": "j1", "workflow": {"body": "name: x\n"}, "args": [], "settings": {}, "secrets": {},
                    "events_url": "https://x/api/events", "run_token": "rt"})
    agent_dir = Path(process.tempfile.gettempdir()) / "armature-agent-j1"
    assert started["env"]["ARMATURE_AGENT_USER"] == "armature-agent"
    assert started["env"]["ARMATURE_AGENT_DIR"] == str(agent_dir)
    assert cleaned == [(tmp_path / "shared", "armature-job", "armature-agent", agent_dir)]


def test_same_user_cant_go_with_a_job_or_agent_user(world, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO), "RUNNER_KEY": world["key"]}
    refused = subprocess.run([sys.executable, "-m", "armature", "runner", "--server", "http://127.0.0.1:9",
                              "--key-env", "RUNNER_KEY", "--work-dir", str(tmp_path), "--same-user",
                              "--job-user", "nobody"], env=env, capture_output=True, text=True, timeout=60,
                             check=False)
    assert refused.returncode == 1 and "--same-user can't go with --job-user or --agent-user" in refused.stderr


def test_a_job_finds_the_running_armature_first_on_its_path(monkeypatch):
    from armature.runner.process import job_path
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert job_path("/opt/venv/bin/armature") == "/opt/venv/bin:/usr/bin:/bin"
    assert job_path(None) == "/usr/bin:/bin"
    assert job_path("armature") == "/usr/bin:/bin"


def test_same_user_advertises_agent_sessions_and_warns(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from armature.cli import app

    started = {}
    monkeypatch.setattr(process, "armature_binary", lambda: "/bin/true")
    monkeypatch.setattr(Runner, "run", lambda self, stop: started.update(runner=self))
    monkeypatch.setenv("RUNNER_KEY", "ark_test")
    result = CliRunner().invoke(app, ["runner", "--server", "http://127.0.0.1:9", "--key-env", "RUNNER_KEY",
                                      "--work-dir", str(tmp_path), "--same-user", "--capability", "claude-code",
                                      "--capability", "codex"])
    assert result.exit_code == 0, result.output
    runner = started["runner"]
    assert runner.same_user is True and runner.job_user is None and runner.agent_user is None
    assert runner.capabilities == ["claude-code", "codex"]
    assert "can read everything this user can" in result.output


def test_a_same_user_runner_starts_the_run_without_sudo(world, tmp_path, monkeypatch):
    started, posted = {}, []
    monkeypatch.setattr(runner_client, "share", lambda *args: pytest.fail("share needs a job user"))
    monkeypatch.setattr(runner_client, "unshare", lambda *args: pytest.fail("unshare needs a job user"))
    monkeypatch.setattr(runner_client, "popen",
                        lambda binary, workdir, args, env, **kw: started.update(env=env, workdir=workdir, **kw))
    client = Client("http://127.0.0.1:9", world["key"])
    monkeypatch.setattr(client, "post", lambda path, body=None: posted.append(body) or {"runner_id": "r1"})
    runner = Runner(client, name="mine", capabilities=["claude-code"], slots=2, work_dir=tmp_path / "work",
                    binary="/bin/true", same_user=True)
    runner.hello()
    assert posted[0]["platform"]["same_user"] is True
    monkeypatch.setattr(runner, "_watch", lambda bundle, proc, workdir: ("succeeded", None))
    monkeypatch.setattr(runner, "_finish", lambda *args: None)
    runner.run_job({"job_id": "j1", "workflow": {"body": "name: x\n"}, "args": [], "settings": {}, "secrets": {},
                    "events_url": "https://x/api/events", "run_token": "rt", "engine_key": "ek"})
    assert started["job_user"] is None and started["run_dir"] is None and started["engine_key"] == "ek"
    assert started["env"]["ARMATURE_AGENT_SAME_USER"] == "1"
    assert "ARMATURE_AGENT_USER" not in started["env"] and "ARMATURE_AGENT_DIR" not in started["env"]
    assert not started["workdir"].exists()


def test_cancel_stops_a_same_user_session_the_engine_left_running(world, tmp_path, monkeypatch):
    procs = {}

    def popen(binary, workdir, args, env, **kw):
        session = subprocess.Popen(["sleep", "60"], start_new_session=True)
        sessions = Path(env["ARMATURE_AGENT_SESSIONS"])
        sessions.mkdir()
        (sessions / str(session.pid)).touch()
        procs.update(session=session, engine=subprocess.Popen(["sleep", "60"], start_new_session=True))
        return procs["engine"]
    monkeypatch.setattr(runner_client, "popen", popen)
    client = Client("http://127.0.0.1:9", world["key"])
    monkeypatch.setattr(client, "end_run", lambda *args: None)
    runner = Runner(client, name="mine", capabilities=[], slots=1, work_dir=tmp_path / "work", binary="/bin/true",
                    same_user=True, heartbeat=0)
    monkeypatch.setattr(runner, "_beat", lambda job_id: {"cancel_requested": True, "status": "running"})
    finished = []
    monkeypatch.setattr(runner, "_finish", lambda *args: finished.append(args))
    runner.run_job({"job_id": "j1", "workflow": {"body": "name: x\n"}, "args": [], "settings": {}, "secrets": {},
                    "events_url": "https://x/api/events", "run_token": "rt"})
    assert finished[0][1] == "canceled"
    assert procs["engine"].wait(5) is not None and procs["session"].wait(5) == -15
    assert not (tmp_path / "work" / "mine" / "job-j1").exists()


def test_stop_sessions_skips_the_runners_own_group_and_junk(tmp_path, monkeypatch):
    signalled = []
    monkeypatch.setattr(process.os, "killpg", lambda pgid, sig: signalled.append(pgid) or (_ for _ in ()).throw(
        ProcessLookupError()))
    for name in ("0", "1", str(os.getpgrp()), "-5", "abc", "12345678901", "4242"):
        (tmp_path / name).touch()
    process.stop_sessions(tmp_path)
    process.stop_sessions(tmp_path / "missing")
    assert signalled == [4242]


def test_a_bundle_cant_claim_same_user_mode(world, tmp_path, monkeypatch):
    started = {}
    monkeypatch.setattr(runner_client, "popen", lambda binary, workdir, args, env, **kw: started.update(env=env))
    runner = Runner(Client("http://127.0.0.1:9", world["key"]), name="plain", capabilities=[], slots=1,
                    work_dir=tmp_path / "work", binary="/bin/true")
    monkeypatch.setattr(runner, "_watch", lambda bundle, proc, workdir: ("succeeded", None))
    monkeypatch.setattr(runner, "_finish", lambda *args: None)
    runner.run_job({"job_id": "j1", "workflow": {"body": "name: x\n"}, "args": [],
                    "settings": {"ARMATURE_AGENT_SAME_USER": "1"}, "secrets": {"ARMATURE_AGENT_SAME_USER": "1"},
                    "events_url": "https://x/api/events", "run_token": "rt"})
    assert "ARMATURE_AGENT_SAME_USER" not in started["env"]


@pytest.mark.parametrize(("memory_mb", "expected"), [(8192, 8192), (0, None)])
def test_a_runner_run_gets_its_memory_cap(world, tmp_path, monkeypatch, memory_mb, expected):
    started = {}
    monkeypatch.setattr(runner_client, "popen", lambda binary, workdir, args, env, **kw: started.update(kw))
    runner = Runner(Client("http://127.0.0.1:9", world["key"]), name="capped", capabilities=[], slots=1,
                    work_dir=tmp_path / "work", binary="/bin/true", memory_mb=memory_mb)
    monkeypatch.setattr(runner, "_watch", lambda bundle, proc, workdir: ("succeeded", None))
    monkeypatch.setattr(runner, "_finish", lambda *args: None)
    runner.run_job({"job_id": "j1", "workflow": {"body": "name: x\n"}, "args": [], "settings": {}, "secrets": {},
                    "events_url": "https://x/api/events", "run_token": "rt"})
    assert started["memory_mb"] == expected


def test_unshare_stops_and_removes_as_the_agent_user_first(tmp_path, monkeypatch):
    issued = []
    monkeypatch.setattr(process.subprocess, "run", lambda argv, **kwargs: issued.append(argv))
    monkeypatch.setattr(process.shutil, "rmtree", lambda path, ignore_errors: None)
    run_dir, agent_dir = tmp_path / "run", tmp_path / "agent"
    run_dir.mkdir()
    agent_dir.mkdir()
    process.unshare(run_dir, "armature-job", "armature-agent", agent_dir)
    assert issued[:2] == [["sudo", "-n", "-u", "armature-agent", "/bin/kill", "-KILL", "-1"],
                          ["sudo", "-n", "-u", "armature-job", "/bin/kill", "-KILL", "-1"]]
    assert ["sudo", "-n", "-u", "armature-agent", "/bin/rm", "-rf", str(agent_dir)] in issued
    assert issued.index(["sudo", "-n", "-u", "armature-agent", "/bin/rm", "-rf", str(agent_dir)]) < \
        issued.index(["sudo", "-n", "-u", "armature-job", "/bin/rm", "-rf", str(agent_dir)])
    assert issued[-1] == ["sudo", "-n", "-u", "armature-agent", "/usr/bin/find", "/tmp", "/var/tmp", "/dev/shm",
                          process.os.path.expanduser("~armature-agent"), "-mindepth", "1", "-user",
                          "armature-agent", "-delete"]


def test_runner_env_drops_host_only_agent_names(world, tmp_path):
    runner = Runner(Client("http://127.0.0.1:9", world["key"]), name="envy", capabilities=[], slots=1,
                    work_dir=tmp_path / "work", binary="/bin/true")
    env = runner.env({"settings": {"ARMATURE_AGENT_USER": "root", "ARMATURE_SKILLS_DIR": "/etc"},
                      "secrets": {"ARMATURE_AGENT_DIR": "/etc"}, "events_url": "https://x/api/events",
                      "run_token": "rt"})
    assert not {"ARMATURE_AGENT_USER", "ARMATURE_AGENT_DIR", "ARMATURE_SKILLS_DIR"} & set(env)
    assert env["ARMATURE_TOOLS_URL"] == "http://127.0.0.1:9"

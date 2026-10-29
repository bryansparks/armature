"""Test runs in stub mode: who may start one, what the child process gets, and how the run ends."""
from __future__ import annotations

import json
import os
import resource
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from armature.runner import process
from armature.server import defs, dryrun, runners
from armature.server.db import new_id
from armature.server.db.schema import audit, projects, run_steps, runs, teams
from armature.spec.loader import STORED_WORKFLOW_HEADER
from tests.server.app_support import Clock, add_membership, add_user, make_client, mutation, sign_in_as

WORKFLOW = """name: review
description: Review one pull request.
stages:
  - id: resolve
    adapter: resolve
  - id: review
    adapter: review
    depends_on: [resolve]
    fan_out: 2
    partition_source: "{{ resolve.files }}"
    partition_key: file
  - id: post
    adapter: post
    depends_on: [review]
adapters:
  resolve:
    name: resolve
    type: script
    cmd: exit 9
    parse: json
  review:
    name: review
    type: script
    cmd: exit 9
    parse: json
  post:
    name: post
    type: script
    cmd: exit 9
    parse: json
"""

FAKE_ARMATURE = """#!{python}
import json, os, resource, sys
with open({record!r}, "w") as out:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
               "memory": resource.getrlimit(resource.RLIMIT_AS),
               "workflow": open(sys.argv[2]).read(),
               "stubs": json.load(open(os.environ["ARMATURE_STUB_OUTPUTS"]))}}, out)
print("boom: the fake runner stopped")
sys.exit({status})
"""


@dataclass
class World:
    engine: object
    keyring: object
    clock: Clock
    team: str
    other: str
    workflow: str
    other_workflow: str
    instance_workflow: str
    users: dict
    socket_dir: str


def _add_workflow(conn, scope, name, body, clock):
    row_id = new_id()
    conn.execute(insert(defs.WORKFLOW.table).values(id=row_id, name=name, current_version=1, created_at=clock(),
                                                    updated_at=clock(), **scope.columns()))
    defs._add_version(conn, defs.WORKFLOW, row_id, 1, body, "seed", None, clock())
    return row_id


@pytest.fixture
def world(engine, keyring):
    clock = Clock()
    team, other, project = new_id(), new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values([{"id": team, "name": "own", "created_at": clock()},
                                           {"id": other, "name": "other", "created_at": clock()}]))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=clock()))
        workflow = _add_workflow(conn, defs.DefScope("team", team), "review", WORKFLOW, clock)
        other_workflow = _add_workflow(conn, defs.DefScope("team", team), "deploy",
                                       WORKFLOW.replace("name: review", "name: deploy"), clock)
        instance_workflow = _add_workflow(conn, defs.INSTANCE, "review", WORKFLOW, clock)
    users = {name: add_user(engine, keyring, clock, name, is_admin=name == "admin")[0]
             for name in ("admin", "contrib", "viewer", "outsider")}
    add_membership(engine, clock, users["contrib"], "team", team, "contributor")
    add_membership(engine, clock, users["viewer"], "team", team, "viewer")
    add_membership(engine, clock, users["outsider"], "team", other, "team_lead")
    socket_dir = tempfile.mkdtemp(prefix="adr")
    yield World(engine, keyring, clock, team, other, workflow, other_workflow, instance_workflow, users, socket_dir)
    shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(dryrun, "END_GRACE_S", 0)
    monkeypatch.setattr(dryrun, "END_WAIT_S", 0)


def _fake_armature(tmp_path, monkeypatch, status=0):
    record = tmp_path / "armature-call.json"
    binary = tmp_path / "fakebin" / "armature"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text(FAKE_ARMATURE.format(python=sys.executable, record=str(record), status=status))
    binary.chmod(0o755)
    monkeypatch.setattr(dryrun, "armature_binary", lambda: str(binary))
    return record


def _client(world, who, **overrides):
    client = make_client(world.engine, world.keyring, world.clock,
                         events_socket=os.path.join(world.socket_dir, "e.sock"), packs_dir="/srv/packs", **overrides)
    client.headers.update(mutation(sign_in_as(client, world.engine, world.keyring, world.clock, world.users[who])))
    return client


def _wait(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def _run(world, run_id):
    with world.engine.connect() as conn:
        return conn.execute(select(runs).where(runs.c.id == run_id)).one()


def _past_run(world, team, workflow_name, outputs, workflow_id=None):
    run_id = new_id()
    with world.engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=team, workflow_id=workflow_id, workflow_name=workflow_name,
                                         status="succeeded", started_at=world.clock()))
        for step, output in outputs.items():
            conn.execute(insert(run_steps).values(run_id=run_id, step_key=step, step_id=step, status="succeeded",
                                                  output=output))
        conn.execute(insert(run_steps).values(run_id=run_id, step_key="review/inner", step_id="inner",
                                              status="succeeded", output={"nested": True}, parent_step="review"))
    return run_id


def test_a_test_run_launches_with_a_clean_env_and_ends_when_the_process_does(world, tmp_path, monkeypatch):
    monkeypatch.setenv("PLANTED_SECRET", "do-not-pass")
    record = _fake_armature(tmp_path, monkeypatch, status=3)
    past = _past_run(world, world.team, "review", {"resolve": {"files": ["a.py"]}, "post": {"posted": True}})
    with _client(world, "contrib") as client:
        response = client.post(f"/api/workflows/{world.workflow}/test", json={
            "mode": "stub", "from_run": past, "outputs": {"post": {"posted": False}},
            "inputs": {"number": 7, "repo": "fs/app"}})
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        seen = json.loads(_wait(lambda: record.exists() and record.read_text() or None))
        ended = _wait(lambda: (row := _run(world, run_id)).status != "running" and row)
        detail = client.get(f"/api/runs/{run_id}").json()["run"]
    env = seen["env"]
    for added in ("__CF_USER_TEXT_ENCODING", "LC_CTYPE"):
        env.pop(added, None)
    assert set(env) == {"PATH", "HOME", "ARMATURE_STUB_OUTPUTS", "ARMATURE_EVENTS", "ARMATURE_EVENTS_TOKEN",
                        "ARMATURE_PACKS_PATH"}
    assert env["ARMATURE_EVENTS"] == f"unix:{os.path.join(world.socket_dir, 'e.sock')}"
    assert env["ARMATURE_PACKS_PATH"].split(os.pathsep)[-1] == "/srv/packs"
    assert os.path.realpath(env["HOME"]) == os.path.realpath(seen["cwd"])
    assert "do-not-pass" not in json.dumps(seen)
    assert seen["argv"][:2] == ["run", str(Path(env["HOME"]) / "workflow.yaml")]
    assert ["--input", "number=7"] == seen["argv"][seen["argv"].index("number=7") - 1:][:2]
    assert "repo=fs/app" in seen["argv"]
    assert seen["workflow"] == f"{STORED_WORKFLOW_HEADER}\n{WORKFLOW}"
    assert seen["stubs"] == {"resolve": {"files": ["a.py"]}, "post": {"posted": False}}
    assert ended.status == "failed" and "exited with status 3" in ended.error and "boom" in ended.error
    assert ended.trigger_kind == "test" and ended.triggered_by == world.users["contrib"]
    assert ended.trigger_ref == {"dry_run": True, "mode": "stub", "from_run": past}
    assert ended.workflow_id == world.workflow and ended.workflow_version == 1
    assert detail["dry_run"] is True
    _wait(lambda: not os.path.exists(env["HOME"]))
    with world.engine.connect() as conn:
        row = conn.execute(select(audit).where(audit.c.action == "run.test")).one()
    assert row.actor_id == world.users["contrib"] and row.target_id == run_id
    assert row.detail["stubbed_steps"] == ["post", "resolve"]
    assert seen["memory"] == _memory_limit(2048)


def test_a_test_run_takes_its_memory_limit_from_the_config(world, tmp_path, monkeypatch):
    record = _fake_armature(tmp_path, monkeypatch)
    with _client(world, "contrib", dry_run={"memory_mb": 512}) as client:
        response = client.post(f"/api/workflows/{world.workflow}/test", json={"mode": "stub"})
        assert response.status_code == 200, response.text
        seen = json.loads(_wait(lambda: record.exists() and record.read_text() or None))
    assert seen["memory"] == _memory_limit(512)


def _memory_limit(mb):
    return [mb << 20, mb << 20] if process.MEMORY_LIMIT is not None else list(resource.getrlimit(resource.RLIMIT_AS))


@pytest.mark.parametrize(("who", "expected"), [("admin", 200), ("contrib", 200), ("viewer", 403),
                                                 ("outsider", 404)])
def test_contributors_and_up_in_scope_may_test(world, tmp_path, monkeypatch, who, expected):
    _fake_armature(tmp_path, monkeypatch)
    with _client(world, who) as client:
        response = client.post(f"/api/workflows/{world.workflow}/test", json={"mode": "stub"})
        assert response.status_code == expected, response.text
        if expected == 200:
            run_id = response.json()["run_id"]
            _wait(lambda: _run(world, run_id).status != "running")


def test_an_instance_workflow_needs_a_team(world, tmp_path, monkeypatch):
    _fake_armature(tmp_path, monkeypatch)
    with _client(world, "contrib") as client:
        assert client.post(f"/api/workflows/{world.instance_workflow}/test", json={"mode": "stub"}).status_code == 400
        assert client.post(f"/api/workflows/{world.instance_workflow}/test",
                           json={"mode": "stub", "team_id": world.other}).status_code == 403
        response = client.post(f"/api/workflows/{world.instance_workflow}/test",
                               json={"mode": "stub", "team_id": world.team})
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        assert _run(world, run_id).team_id == world.team
        _wait(lambda: _run(world, run_id).status != "running")


def test_dry_mode_waits_for_a_runner(world, tmp_path, monkeypatch):
    record = _fake_armature(tmp_path, monkeypatch)
    with _client(world, "contrib") as client:
        dry = client.post(f"/api/workflows/{world.workflow}/test", json={"mode": "dry"})
        assert dry.status_code == 400 and dry.json()["error"] == "needs a runner; coming with the job worker"
        assert client.post(f"/api/workflows/{world.workflow}/test", json={"mode": "live"}).status_code == 400
    assert not record.exists()
    with world.engine.connect() as conn:
        assert conn.execute(select(runs)).all() == []


def test_at_most_two_test_runs_per_user(world, tmp_path, monkeypatch):
    record = _fake_armature(tmp_path, monkeypatch)
    with world.engine.begin() as conn:
        for _ in range(dryrun.MAX_CONCURRENT):
            conn.execute(insert(runs).values(id=new_id(), team_id=world.team, workflow_name="review",
                                             status="running", trigger_kind="test",
                                             triggered_by=world.users["contrib"], started_at=world.clock()))
    with _client(world, "contrib") as client:
        assert client.post(f"/api/workflows/{world.workflow}/test", json={"mode": "stub"}).status_code == 429
    assert not record.exists()
    with _client(world, "admin") as client:
        response = client.post(f"/api/workflows/{world.workflow}/test", json={"mode": "stub"})
        assert response.status_code == 200
        _wait(lambda: _run(world, response.json()["run_id"]).status != "running")


def test_from_run_must_be_visible_and_of_the_same_workflow(world, tmp_path, monkeypatch):
    _fake_armature(tmp_path, monkeypatch)
    hidden = _past_run(world, world.other, "review", {"resolve": {}})
    other = _past_run(world, world.team, "deploy", {"resolve": {}})
    with _client(world, "contrib") as client:
        assert client.post(f"/api/workflows/{world.workflow}/test",
                           json={"mode": "stub", "from_run": hidden}).status_code == 404
        assert client.post(f"/api/workflows/{world.workflow}/test",
                           json={"mode": "stub", "from_run": other}).status_code == 400
        bad = client.post(f"/api/workflows/{world.workflow}/test", json={"mode": "stub", "inputs": {"a b": 1}})
        assert bad.status_code == 400


def test_runs_filter_by_workflow(world):
    _past_run(world, world.team, "review", {})
    _past_run(world, world.team, "deploy", {})
    client = _client(world, "contrib")
    listed = client.get("/api/runs", params={"workflow": "review", "limit": 20}).json()["runs"]
    assert [r["workflow"] for r in listed] == ["review"]


def test_a_real_stub_run_streams_to_success(world, tmp_path):
    binary = dryrun.armature_binary()
    if binary is None:
        pytest.skip("the armature command is not installed")
    with _client(world, "contrib") as client:
        response = client.post(f"/api/workflows/{world.workflow}/test", json={
            "mode": "stub", "outputs": {"resolve": {"files": ["a.py", "b.py"]}, "review": [{"ok": 1}, {"ok": 2}],
                                        "post": {"posted": False}}})
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        ended = _wait(lambda: (row := _run(world, run_id)).status != "running" and row, timeout=90)
        assert ended.status == "succeeded", ended.error

        def stored_steps():
            with world.engine.connect() as conn:
                rows = conn.execute(select(run_steps.c.step_id, run_steps.c.status)
                                    .where(run_steps.c.run_id == run_id)).all()
            return dict(rows) if len(rows) == 3 else None
        assert _wait(stored_steps) == {"resolve": "succeeded", "review": "succeeded", "post": "succeeded"}
        with world.engine.connect() as conn:
            outputs = dict(conn.execute(select(run_steps.c.step_id, run_steps.c.output)
                                        .where(run_steps.c.run_id == run_id)).all())
        assert outputs == {"resolve": {"files": ["a.py", "b.py"]}, "review": [{"ok": 1}, {"ok": 2}],
                           "post": {"posted": False}}
        listed = client.get("/api/runs", params={"workflow": "review", "with_outputs": "1"}).json()["runs"]
        assert [r["id"] for r in listed] == [run_id]

        replay = client.post(f"/api/workflows/{world.workflow}/test", json={"mode": "stub", "from_run": run_id})
        assert replay.status_code == 200, replay.text
        second = replay.json()["run_id"]
        again = _wait(lambda: (row := _run(world, second)).status != "running" and row, timeout=90)
        assert again.status == "succeeded", again.error
    with world.engine.connect() as conn:
        row = conn.execute(select(audit).where(audit.c.action == "run.test", audit.c.target_id == second)).one()
    assert row.detail["stubbed_steps"] == ["post", "resolve", "review"]


def test_the_instance_has_a_cap_on_active_test_runs(world, tmp_path, monkeypatch):
    record = _fake_armature(tmp_path, monkeypatch)
    with world.engine.begin() as conn:
        conn.execute(insert(runs).values(id=new_id(), team_id=world.other, workflow_name="review", status="running",
                                         trigger_kind="test", triggered_by=world.users["outsider"],
                                         started_at=world.clock()))
    with _client(world, "contrib", dry_run={"max_active": 1}) as client:
        refused = client.post(f"/api/workflows/{world.workflow}/test", json={"mode": "stub"})
        assert refused.status_code == 429 and "server" in refused.json()["error"]
    assert not record.exists()


def test_parallel_requests_cannot_pass_the_instance_cap(world, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    _fake_armature(tmp_path, monkeypatch)
    monkeypatch.setattr(dryrun, "TIMEOUT_S", 30)
    users = [world.users["admin"]] + [add_user(world.engine, world.keyring, world.clock, f"admin{i}",
                                               is_admin=True)[0] for i in range(3)]
    world.users.update({f"u{i}": user for i, user in enumerate(users)})
    clients = [_client(world, f"u{i}", dry_run={"max_active": 1}) for i in range(len(users))]
    for client in clients:
        client.__enter__()
    try:
        monkeypatch.setattr(dryrun, "start", lambda *args, **kwargs: None)
        with ThreadPoolExecutor(len(clients)) as pool:
            codes = list(pool.map(lambda c: c.post(f"/api/workflows/{world.workflow}/test",
                                                   json={"mode": "stub"}).status_code, clients))
    finally:
        for client in clients:
            client.__exit__(None, None, None)
    assert sorted(codes) == [200] + [429] * (len(clients) - 1)


class FakeProc:
    def __init__(self, code):
        self.returncode = code
        self.pid = -1

    def wait(self, timeout=None):
        return self.returncode


class FakeHub:
    running = False

    def __init__(self, engine, clock):
        self.engine = engine
        self.clock = clock


def _registered(world):
    with world.engine.begin() as conn:
        row, token = runners.open_run(conn, world.keyring, team_id=world.team, project_id=None, workflow_id=None,
                                      workflow_name="review", now=world.clock(), trigger_kind="test")
    workdir = Path(tempfile.mkdtemp(prefix="adw"))
    return dryrun.Launch(row["id"], token, workdir, {})


def _set_status(world, run_id, status):
    with world.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(status=status))


def test_the_watcher_never_fails_a_run_that_ended(world, monkeypatch):
    monkeypatch.setattr(dryrun, "END_WAIT_S", 5)
    launch = _registered(world)
    threading.Timer(0.3, _set_status, (world, launch.run_id, "succeeded")).start()
    dryrun.watch(FakeProc(0), launch, FakeHub(world.engine, world.clock), None)
    ended = _run(world, launch.run_id)
    assert ended.status == "succeeded" and ended.error is None
    assert not launch.workdir.exists()


def test_the_watcher_fails_a_clean_exit_that_never_ended_after_waiting(world, monkeypatch):
    monkeypatch.setattr(dryrun, "END_WAIT_S", 0.3)
    launch = _registered(world)
    started = time.monotonic()
    dryrun.watch(FakeProc(0), launch, FakeHub(world.engine, world.clock), None)
    assert time.monotonic() - started >= 0.3
    ended = _run(world, launch.run_id)
    assert ended.status == "failed" and "status 0" in ended.error

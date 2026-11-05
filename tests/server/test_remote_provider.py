"""A remote runner provider built only on armature.runner.api, against a real server."""
from __future__ import annotations

import ast
import json
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from armature.runner import api
from armature.server import defs, runners
from armature.server.db import new_id
from armature.server.db.schema import jobs, runs, settings
from tests.server.app_support import Clock
from tests.server.live_support import LiveServer, add_team

BODY = "name: remote\nstages: []\n"


class FakeProvider(api.Runner):
    """Runs each job on a pretend service: it relays the run's end and reports the job done."""

    def __init__(self, client, *, remote, label, work_dir):
        super().__init__(client, name=remote, capabilities=["llm", f"{api.REMOTE_PREFIX}{remote}"], slots=1,
                         work_dir=work_dir, binary="", same_user=True, poll=0.05)
        self.label = label
        self.ran: list[str] = []
        self.caps: list[int | None] = []

    def platform(self):
        return api.location.make(api.REMOTE, self.label, same_user=True)

    def hello(self):
        answer = super().hello()
        self.caps.append(answer.get("max_slots"))
        return answer

    def claim(self, answer_headers=None):
        headers: dict[str, str] = {}
        answer = super().claim(headers)
        sent = headers.get(api.MAX_SLOTS_HEADER.lower())
        self.caps.append(int(sent) if sent else None)
        return answer

    def run_job(self, bundle):
        self.ran.append(bundle["job_id"])
        line = {"v": 1, "token": bundle["run_token"], "kind": "run_end", "payload": {"status": "succeeded"}}
        self.client.events(bundle["events_url"], bundle["run_token"], json.dumps(line).encode() + b"\n")
        self.finish(bundle["job_id"], "succeeded", None)


@pytest.fixture
def world(sqlite_engine, keyring):
    clock = Clock()
    team, project = add_team(sqlite_engine, clock, "own")
    workflow = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=None, key="worker.enabled", value=False))
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="remote", current_version=1,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, BODY, "seed", None, clock())
        _, key = runners.create_key(conn, keyring, team_id=team, name="ci", actor_id=None, now=clock(), ip=None)
    return {"clock": clock, "team": team, "project": project, "workflow": workflow, "key": key}


def _queue(engine, world, requires):
    job_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(jobs).values(id=job_id, team_id=world["team"], project_id=world["project"],
                                         workflow_id=world["workflow"], workflow_version=1, inputs={},
                                         requires=requires, status="queued", attempts=0,
                                         created_at=world["clock"]()))
    return job_id


def _job(engine, job_id):
    with engine.connect() as conn:
        return conn.execute(select(jobs).where(jobs.c.id == job_id)).one()


def _until(check, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_a_provider_imports_only_the_api():
    tree = ast.parse(Path(__file__).read_text())
    provider = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FakeProvider")
    imported = {alias.asname or alias.name.split(".")[0] for node in tree.body
                if isinstance(node, ast.Import | ast.ImportFrom) for alias in node.names}
    used = {node.id for node in ast.walk(provider) if isinstance(node, ast.Name)}
    assert used & imported == {"api", "json"}
    assert not [name for name in api.__all__ if name.startswith("_")]
    from armature.runner import serve

    assert api.PROVIDER_API == 1 and api.serve is serve


def test_two_providers_on_one_server_each_take_only_their_own_jobs(sqlite_engine, keyring, world, tmp_path):
    on_a = _queue(sqlite_engine, world, ["llm", "remote:a"])
    on_b = _queue(sqlite_engine, world, ["remote:b"])
    plain = _queue(sqlite_engine, world, ["llm"])
    with LiveServer(sqlite_engine, keyring, world["clock"], socket=False) as live:
        clients = [api.Client(live.origin, world["key"]) for _ in range(2)]
        providers = [FakeProvider(clients[0], remote="a", label="Cloud A", work_dir=tmp_path),
                     FakeProvider(clients[1], remote="b", label="Cloud B", work_dir=tmp_path)]
        stop = threading.Event()
        threads = [threading.Thread(target=p.run, args=(stop,), daemon=True) for p in providers]
        for thread in threads:
            thread.start()
        try:
            _until(lambda: all(_job(sqlite_engine, j).finished_at is not None for j in (on_a, on_b)))
        finally:
            stop.set()
            for thread in threads:
                thread.join(10)
            for client in clients:
                client.close()
        live.flush()
    assert providers[0].ran == [on_a] and providers[1].ran == [on_b]
    assert _job(sqlite_engine, plain).status == "queued"
    for job_id, label in ((on_a, "Cloud A"), (on_b, "Cloud B")):
        job = _job(sqlite_engine, job_id)
        with sqlite_engine.connect() as conn:
            run = conn.execute(select(runs).where(runs.c.id == job.run_id)).one()
        assert job.status == "succeeded" and run.status == "succeeded"
        assert run.location == {"kind": "remote", "label": label, "same_user": True}
    assert providers[0].caps and set(providers[0].caps) == {None}


def test_a_provider_hears_max_slots_once_the_server_sets_it(sqlite_engine, keyring, world, tmp_path):
    with sqlite_engine.begin() as conn:
        defs.put_setting(conn, team_id=None, key="runner.max_slots", value=3, actor_id=None, now=world["clock"](),
                         ip=None)
    with LiveServer(sqlite_engine, keyring, world["clock"], socket=False) as live:
        client = api.Client(live.origin, world["key"])
        try:
            provider = FakeProvider(client, remote="a", label="Cloud A", work_dir=tmp_path)
            provider.hello()
            assert provider.claim() is None
            job_id = _queue(sqlite_engine, world, ["remote:a"])
            bundle = provider.claim()
            beat = provider.beat(bundle["job"]["job_id"])
        finally:
            client.close()
    assert provider.caps == [3, 3, 3] and bundle["job"]["job_id"] == job_id and beat["max_slots"] == 3

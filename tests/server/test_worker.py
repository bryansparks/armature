"""The job worker: claiming, launching with the team's env and secrets, settling, and lease expiry."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, insert, select, update

from armature.server import defs, dryrun, runners, secrets, worker
from armature.server.config import ServerConfig
from armature.server.db import new_id
from armature.server.db.schema import (
    jobs,
    projects,
    runs,
    settings,
    teams,
    triggers,
    webhooks,
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

V1 = "name: review\nstages: []\n"
V2 = "name: review\ndescription: edited\nstages: []\n"

FAKE_ARMATURE = """#!{python}
import json, os, sys
with open({record!r}, "w") as out:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}}, out)
"""


class FakeProc:
    def __init__(self, code=None):
        self.returncode = code
        self.pid = -1

    def poll(self):
        return self.returncode


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
def world(sqlite_engine, clock):
    team, other, project, hook, rule = new_id(), new_id(), new_id(), new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values([{"id": team, "name": "own", "created_at": clock()},
                                           {"id": other, "name": "other", "created_at": clock()}]))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=clock()))
        workflow = new_id()
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="review", current_version=1,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, V1, "seed", None, clock())
        conn.execute(insert(webhooks).values(id=hook, team_id=team, name="gh", source="github", path_token=new_id(),
                                             secret_name="HOOK", created_at=clock(), updated_at=clock()))
        conn.execute(insert(triggers).values(id=rule, team_id=team, workflow_id=workflow, kind="webhook",
                                             config={"secrets": ["API_TOKEN"]}, webhook_id=hook, created_at=clock()))
    return {"team": team, "other": other, "project": project, "workflow": workflow, "hook": hook, "rule": rule}


def _worker(engine, keyring, clock, tmp_path, **kwargs):
    cfg = ServerConfig(database_url=str(engine.url), packs_dir="/srv/packs")
    return worker.Worker(engine, keyring, cfg, FakeHub(engine, clock), clock=clock, work_dir=tmp_path / "work",
                         **kwargs)


def _job(engine, clock, world, *, version=1, trigger=True, **values):
    job_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(jobs).values(id=job_id, team_id=world["team"], project_id=world["project"],
                                         workflow_id=world["workflow"], workflow_version=version,
                                         inputs={"number": 7}, trigger_id=world["rule"] if trigger else None,
                                         delivery_id="del-1", created_at=clock(),
                                         **{"status": "queued", "attempts": 0, **values}))
    clock.advance(seconds=1)
    return job_id


def _get(engine, job_id):
    with engine.connect() as conn:
        return conn.execute(select(jobs).where(jobs.c.id == job_id)).one()


def _run(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(runs).where(runs.c.id == run_id)).one()


def _setting(engine, key, value, team_id=None):
    with engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=team_id, key=key, value=value))


def _secret(engine, keyring, name, value, team_id=None):
    with engine.begin() as conn:
        secrets.set_secret(conn, keyring, team_id=team_id, name=name, value=value, actor_id=None)


@pytest.fixture
def spawned(monkeypatch):
    calls = []

    def spawn(binary, launch, args, env, hub, **kwargs):
        calls.append({"binary": binary, "launch": launch, "args": args, "env": env,
                      "workflow": (launch.workdir / "workflow.yaml").read_text(), **kwargs})
        return FakeProc()
    monkeypatch.setattr(dryrun, "armature_binary", lambda: "/bin/armature")
    monkeypatch.setattr(dryrun, "spawn", spawn)
    return calls


@pytest.mark.parametrize(("runner", "expected"), [({}, 8192), ({"memory_mb": 1024}, 1024), ({"memory_mb": 0}, None)])
def test_a_run_gets_the_runner_memory_cap(sqlite_engine, keyring, clock, world, tmp_path, spawned, runner, expected):
    _secret(sqlite_engine, keyring, "API_TOKEN", "t")
    _job(sqlite_engine, clock, world)
    cfg = ServerConfig(database_url=str(sqlite_engine.url), packs_dir="/srv/packs", runner=runner)
    worker.Worker(sqlite_engine, keyring, cfg, FakeHub(sqlite_engine, clock), clock=clock,
                  work_dir=tmp_path / "work").tick()
    assert spawned[0]["memory_mb"] == expected


def test_only_one_worker_wins_a_claim(sqlite_engine, keyring, clock, world, tmp_path):
    job_id = _job(sqlite_engine, clock, world)
    first = _worker(sqlite_engine, keyring, clock, tmp_path, worker_id="a")
    second = _worker(sqlite_engine, keyring, clock, tmp_path, worker_id="b")
    assert first.try_claim(job_id, clock()) is True
    assert second.try_claim(job_id, clock()) is False
    assert second.claim(clock()) is None
    row = _get(sqlite_engine, job_id)
    assert row.status == "claimed" and row.claimed_by == "a" and row.attempts == 1
    assert row.lease_expires_at.replace(tzinfo=None) == (clock() + worker.LEASE).replace(tzinfo=None)


def test_claims_stop_at_the_slot_count(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    _secret(sqlite_engine, keyring, "API_TOKEN", "t")
    ids = [_job(sqlite_engine, clock, world) for _ in range(4)]
    w = _worker(sqlite_engine, keyring, clock, tmp_path)
    w.tick()
    assert [_get(sqlite_engine, i).status for i in ids] == ["running", "running", "queued", "queued"]
    _setting(sqlite_engine, worker.SLOTS_KEY, 3)
    w.tick()
    assert [_get(sqlite_engine, i).status for i in ids] == ["running", "running", "running", "queued"]
    _setting(sqlite_engine, worker.ENABLED_KEY, False)
    with sqlite_engine.begin() as conn:
        conn.execute(update(settings).where(settings.c.key == worker.SLOTS_KEY).values(value=9))
    w.tick()
    assert _get(sqlite_engine, ids[3]).status == "queued"


def test_launch_builds_env_from_settings_and_listed_secrets(sqlite_engine, keyring, clock, world, tmp_path,
                                                            spawned, monkeypatch):
    monkeypatch.setenv("PLANTED", "do-not-pass")
    _setting(sqlite_engine, "env.REGION", "instance-region")
    _setting(sqlite_engine, "env.ONLY_INSTANCE", "yes")
    _setting(sqlite_engine, "env.REGION", "team-region", team_id=world["team"])
    _setting(sqlite_engine, "env.NUMBER", 5, team_id=world["team"])
    _setting(sqlite_engine, "env.ARMATURE_EVENTS_TOKEN", "hijack", team_id=world["team"])
    _setting(sqlite_engine, "env.OTHER_TEAM", "no", team_id=world["other"])
    _secret(sqlite_engine, keyring, "API_TOKEN", "instance-token")
    _secret(sqlite_engine, keyring, "API_TOKEN", "team-token", team_id=world["team"])
    _secret(sqlite_engine, keyring, "UNLISTED", "nope", team_id=world["team"])
    job_id = _job(sqlite_engine, clock, world)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    [call] = spawned
    env = call["env"]
    assert env["REGION"] == "team-region" and env["ONLY_INSTANCE"] == "yes"
    assert env["API_TOKEN"] == "team-token"
    assert "NUMBER" not in env and "OTHER_TEAM" not in env and "UNLISTED" not in env and "PLANTED" not in env
    assert env["ARMATURE_EVENTS_TOKEN"] == call["launch"].token
    assert env["ARMATURE_EVENTS"] == "http://127.0.0.1:8098/api/events"
    assert env["ARMATURE_PACKS_PATH"].split(":")[-1] == "/srv/packs"
    assert env["ARMATURE_TOOLS_URL"] == "http://127.0.0.1:8098" and "ARMATURE_TOOL_TOKEN" not in env
    run_id = call["launch"].run_id
    assert call["launch"].engine_key == f"{run_id}:{runners.engine_key(keyring, run_id)}"
    assert not {"ARMATURE_ENGINE_KEY", "ARMATURE_ENGINE_FD"} & set(env)
    assert call["args"] == ["--input", "number=7"] and call["label"] == "run" and "timeout" not in call
    assert call["launch"].workdir == tmp_path / "work" / job_id
    job = _get(sqlite_engine, job_id)
    run = _run(sqlite_engine, job.run_id)
    assert job.status == "running" and run.status == "running"
    assert run.trigger_kind == "webhook" and run.triggered_by is None and run.actor is None
    assert run.trigger_ref == {"webhook_id": world["hook"], "delivery_id": "del-1", "trigger_id": world["rule"]}
    assert run.project_id == world["project"] and run.workflow_version == 1 and run.workflow_name == "review"


@pytest.mark.parametrize("change", ["untick", "delete"])
def test_a_dry_rules_job_runs_dry_whatever_the_rule_says_at_claim(sqlite_engine, keyring, clock, world, tmp_path,
                                                                  spawned, change):
    from armature.server import webhooks as hooks
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == world["rule"]).values(config={"dry": True}))
        rule = conn.execute(select(triggers).where(triggers.c.id == world["rule"])).one()
        decisions = [{**hooks._decide(conn, world["team"], "github", rule, None, {}), "project_id": world["project"]}]
        [queued] = hooks.enqueue(conn, team_id=world["team"], delivery_id="del-1", decisions=decisions, now=clock())
        if change == "untick":
            conn.execute(update(triggers).where(triggers.c.id == world["rule"]).values(config={"dry": False}))
        else:
            hooks.remove_rule(conn, rule, actor_id=None, ip=None)
    assert decisions[0]["dry"] is True and _get(sqlite_engine, queued["job_id"]).dry_run is True
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    run = _run(sqlite_engine, _get(sqlite_engine, queued["job_id"]).run_id)
    assert run.trigger_ref["dry_run"] is True


def test_a_job_queued_live_runs_live_even_if_its_rule_turns_dry(sqlite_engine, keyring, clock, world, tmp_path,
                                                                spawned):
    job_id = _job(sqlite_engine, clock, world)
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == world["rule"]).values(config={"dry": True}))
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    assert "dry_run" not in _run(sqlite_engine, _get(sqlite_engine, job_id).run_id).trigger_ref


def test_a_missing_secret_fails_the_job_without_launching(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    job_id = _job(sqlite_engine, clock, world)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    job = _get(sqlite_engine, job_id)
    assert job.status == "failed" and job.error == "missing secret API_TOKEN" and job.finished_at is not None
    assert job.run_id is None and spawned == []
    assert not (tmp_path / "work" / job_id).exists()
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(runs)).all() == []


def test_a_missing_binary_fails_the_job(sqlite_engine, keyring, clock, world, tmp_path, monkeypatch):
    monkeypatch.setattr(dryrun, "armature_binary", lambda: None)
    job_id = _job(sqlite_engine, clock, world)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    job = _get(sqlite_engine, job_id)
    assert job.status == "failed" and job.error == "armature executable not found"


def test_a_missing_version_fails_the_job(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    job_id = _job(sqlite_engine, clock, world, version=9, trigger=False)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    job = _get(sqlite_engine, job_id)
    assert job.status == "failed" and "no version 9" in job.error and spawned == []


def test_the_pinned_version_runs_after_the_workflow_is_edited(sqlite_engine, keyring, clock, world, tmp_path,
                                                              spawned):
    job_id = _job(sqlite_engine, clock, world, trigger=False)
    with sqlite_engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world["workflow"], 2, V2, "edit", None, clock())
        conn.execute(update(defs.WORKFLOW.table).where(defs.WORKFLOW.table.c.id == world["workflow"])
                     .values(current_version=2))
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    [call] = spawned
    assert call["workflow"] == f"{STORED_WORKFLOW_HEADER}\n{V1}"
    assert _run(sqlite_engine, _get(sqlite_engine, job_id).run_id).workflow_version == 1


@pytest.mark.parametrize(("run_status", "job_status"), [("succeeded", "succeeded"), ("failed", "failed"),
                                                        ("cancelled", "failed")])
def test_a_finished_run_settles_its_job(sqlite_engine, keyring, clock, world, tmp_path, spawned, run_status,
                                        job_status):
    job_id = _job(sqlite_engine, clock, world, trigger=False)
    w = _worker(sqlite_engine, keyring, clock, tmp_path)
    w.tick()
    run_id = _get(sqlite_engine, job_id).run_id
    w.tick()
    assert _get(sqlite_engine, job_id).status == "running"
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(status=run_status, error="x"))
    w.tick()
    job = _get(sqlite_engine, job_id)
    assert job.status == job_status and job.finished_at is not None


def _expired(engine, clock, world, *, run_status):
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world["team"], workflow_name="review",
                                         status=run_status, started_at=clock()))
    return _job(engine, clock, world, status="running", claimed_by="gone", claimed_at=clock(), attempts=1,
                run_id=run_id, lease_expires_at=clock() - timedelta(seconds=1))


def test_an_expired_lease_settles_from_an_ended_run_or_is_lost(sqlite_engine, keyring, clock, world, tmp_path,
                                                               spawned):
    ended = _expired(sqlite_engine, clock, world, run_status="succeeded")
    live = _expired(sqlite_engine, clock, world, run_status="running")
    unclaimed = _job(sqlite_engine, clock, world, status="claimed", claimed_by="gone", attempts=1,
                     lease_expires_at=clock() - timedelta(seconds=1))
    w = _worker(sqlite_engine, keyring, clock, tmp_path)
    w.tick()
    w.tick()
    assert _get(sqlite_engine, ended).status == "succeeded"
    for job_id in (live, unclaimed):
        job = _get(sqlite_engine, job_id)
        assert job.status == "lost" and job.error == "lease expired" and job.attempts == 1
        assert job.finished_at is not None
    assert spawned == []


def test_startup_recovers_expired_jobs(sqlite_engine, keyring, clock, world):
    live = _expired(sqlite_engine, clock, world, run_status="running")
    with make_client(sqlite_engine, keyring, clock, worker_interval=3600) as client:
        assert client.app.state.worker._thread is not None
        assert _get(sqlite_engine, live).status == "lost"


def test_a_heartbeat_extends_the_lease_while_the_process_lives(sqlite_engine, keyring, clock, world, tmp_path,
                                                               spawned):
    job_id = _job(sqlite_engine, clock, world, trigger=False)
    w = _worker(sqlite_engine, keyring, clock, tmp_path)
    w.tick()
    clock.advance(seconds=90)
    w.tick()
    job = _get(sqlite_engine, job_id)
    assert job.lease_expires_at.replace(tzinfo=None) == (clock() + worker.LEASE).replace(tzinfo=None)
    w._procs[job_id].returncode = 0
    clock.advance(seconds=90)
    w.tick()
    assert _get(sqlite_engine, job_id).status == "running"
    clock.advance(seconds=60)
    w.tick()
    assert _get(sqlite_engine, job_id).status == "lost"


def test_a_real_process_that_never_ends_its_run_fails_the_job(sqlite_engine, keyring, clock, world, tmp_path,
                                                              monkeypatch):
    monkeypatch.setattr(dryrun, "END_GRACE_S", 0)
    monkeypatch.setattr(dryrun, "END_WAIT_S", 0)
    record = tmp_path / "call.json"
    binary = tmp_path / "bin" / "armature"
    binary.parent.mkdir()
    binary.write_text(FAKE_ARMATURE.format(python=sys.executable, record=str(record)))
    binary.chmod(0o755)
    monkeypatch.setattr(dryrun, "armature_binary", lambda: str(binary))
    job_id = _job(sqlite_engine, clock, world, trigger=False)
    w = _worker(sqlite_engine, keyring, clock, tmp_path)
    w.tick()
    run_id = _get(sqlite_engine, job_id).run_id
    deadline = time.monotonic() + 20
    while _run(sqlite_engine, run_id).status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    w.tick()
    job = _get(sqlite_engine, job_id)
    assert job.status == "failed" and "exited with status 0" in job.error
    seen = json.loads(record.read_text())
    assert seen["argv"][2:] == ["--quiet", "--no-cache", "--input", "number=7"]
    assert not (tmp_path / "work" / job_id).exists()


def test_stop_ends_the_runs_it_started(sqlite_engine, keyring, clock, tmp_path):
    job_worker = _worker(sqlite_engine, keyring, clock, tmp_path)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    job_worker._procs["job"] = proc
    job_worker.stop(timeout=1)
    assert proc.poll() is not None


def test_jobs_list_shows_a_team_its_own_jobs(sqlite_engine, keyring, clock, world):
    mine = _job(sqlite_engine, clock, world)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(jobs).values(id=new_id(), team_id=world["other"], workflow_id=world["workflow"],
                                         status="queued", attempts=0, created_at=clock()))
    member, _ = add_user(sqlite_engine, keyring, clock, "member")
    admin, _ = add_user(sqlite_engine, keyring, clock, "admin", is_admin=True)
    add_membership(sqlite_engine, clock, member, "team", world["team"], "viewer")
    client = make_client(sqlite_engine, keyring, clock)
    client.headers.update(mutation(sign_in_as(client, sqlite_engine, keyring, clock, member)))
    listed = client.get("/api/jobs").json()["jobs"]
    assert [j["id"] for j in listed] == [mine]
    assert listed[0]["workflow"] == "review" and listed[0]["status"] == "queued"
    assert set(listed[0]) == {"id", "workflow", "status", "created_at", "claimed_at", "finished_at", "run_id",
                              "error", "trigger_id", "delivery_id", "warning", "concurrency_key", "not_before",
                              "superseded_by"}
    assert client.get("/api/jobs", params={"team_id": world["other"]}).json()["jobs"] == []
    assert client.get("/api/jobs", params={"status": "running"}).json()["jobs"] == []
    boss = make_client(sqlite_engine, keyring, clock)
    boss.headers.update(mutation(sign_in_as(boss, sqlite_engine, keyring, clock, admin)))
    assert len(boss.get("/api/jobs").json()["jobs"]) == 2


AGENT_BODY = """name: review
stages:
  - id: review
    agent:
      model: review
      prompt: Review it.
      skills: [kotlin-review]
"""


def _agent_world(engine, keyring, clock, world, *, skill_scope="team"):
    from armature.server.db.schema import documents, providers
    with engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world["workflow"], 2, AGENT_BODY, "agent", None, clock())
        conn.execute(insert(providers).values(id=new_id(), team_id=world["team"], kind="claude-subscription",
                                              config={"credential_secret": "CLAUDE_OAUTH"},
                                              aliases={"review": "claude-sonnet-5-5"}, created_at=clock(),
                                              updated_at=clock()))
        for scope_kind, body in (("instance", "instance skill"), (skill_scope, "team skill")):
            if scope_kind == "instance" and skill_scope == "instance":
                continue
            scope = defs.INSTANCE if scope_kind == "instance" else defs.DefScope("team", world["team"])
            doc = new_id()
            conn.execute(insert(documents).values(id=doc, kind="skill", key="kotlin-review", current_version=1,
                                                  created_at=clock(), updated_at=clock(), **scope.columns()))
            defs._add_version(conn, defs.DOCUMENT, doc, 1, body, None, None, clock())
    _secret(engine, keyring, "API_TOKEN", "t", team_id=world["team"])
    _secret(engine, keyring, "CLAUDE_OAUTH", "oauth-token", team_id=world["team"])


def test_an_agent_job_bundles_its_skills_and_model_credential(sqlite_engine, keyring, clock, world, tmp_path,
                                                              spawned):
    _agent_world(sqlite_engine, keyring, clock, world)
    job_id = _job(sqlite_engine, clock, world, version=2)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    [call] = spawned
    env = call["env"]
    assert json.loads(env["ARMATURE_AGENT_MODELS"]) == {
        "review": {"model": "claude-sonnet-5-5", "env": {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-token"}}}
    skills_dir = tmp_path / "work" / job_id / "skills"
    assert env["ARMATURE_SKILLS_DIR"] == str(skills_dir)
    assert (skills_dir / "kotlin-review" / "SKILL.md").read_text() == "team skill"


def test_an_agent_job_with_a_missing_skill_fails(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    _agent_world(sqlite_engine, keyring, clock, world)
    with sqlite_engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world["workflow"], 3, AGENT_BODY.replace("kotlin-review", "nope"),
                          None, None, clock())
    job_id = _job(sqlite_engine, clock, world, version=3)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    assert _get(sqlite_engine, job_id).error == "missing skill nope" and spawned == []


ROLE_BODY = AGENT_BODY.replace("      skills: [kotlin-review]\n", "      role: security\n")


def _role(engine, clock, scope, body, key="security"):
    from armature.server.db.schema import documents
    with engine.begin() as conn:
        doc = new_id()
        conn.execute(insert(documents).values(id=doc, kind="role", key=key, current_version=1, created_at=clock(),
                                              updated_at=clock(), **scope.columns()))
        defs._add_version(conn, defs.DOCUMENT, doc, 1, body, None, None, clock())


def test_an_agent_job_bundles_the_narrowest_role_without_its_front_matter(sqlite_engine, keyring, clock, world,
                                                                         tmp_path, spawned):
    _agent_world(sqlite_engine, keyring, clock, world)
    with sqlite_engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world["workflow"], 3, ROLE_BODY, None, None, clock())
    _role(sqlite_engine, clock, defs.INSTANCE, "Instance security.")
    _role(sqlite_engine, clock, defs.DefScope("team", world["team"]), "---\nname: security\n---\nTeam security.")
    job_id = _job(sqlite_engine, clock, world, version=3)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    roles_dir = tmp_path / "work" / job_id / "roles"
    assert spawned[0]["env"]["ARMATURE_ROLES_DIR"] == str(roles_dir)
    assert (roles_dir / "security.md").read_text() == "Team security."
    with sqlite_engine.connect() as conn:
        project = worker.bundle_roles(conn, world["team"], world["project"], ["security"])
    assert project == {"security": "Team security."}
    _role(sqlite_engine, clock, defs.DefScope("project", world["team"], world["project"]), "Project security.")
    with sqlite_engine.connect() as conn:
        assert worker.bundle_roles(conn, world["team"], world["project"], ["security"]) == {
            "security": "Project security."}
        assert worker.bundle_roles(conn, world["team"], None, ["security"]) == {"security": "Team security."}


def test_an_agent_job_with_an_unknown_role_fails(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    _agent_world(sqlite_engine, keyring, clock, world)
    with sqlite_engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world["workflow"], 3, ROLE_BODY, None, None, clock())
    job_id = _job(sqlite_engine, clock, world, version=3)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    assert _get(sqlite_engine, job_id).error == "missing role security" and spawned == []


def test_an_oversized_role_fails_the_bundle(sqlite_engine, clock, world):
    _role(sqlite_engine, clock, defs.INSTANCE, "x" * (defs.SKILL_MAX + 1))
    with sqlite_engine.connect() as conn, pytest.raises(worker.JobError, match="role security is over 64 KB"):
        worker.bundle_roles(conn, world["team"], None, ["security"])


def test_a_role_needs_a_runner_that_reads_roles():
    assert worker.requirements(ROLE_BODY) == ["claude-code", "git", worker.ROLES_CAPABILITY]


def test_requirements_add_git_and_the_harness_for_agent_steps():
    assert worker.requirements(AGENT_BODY) == ["claude-code", "git"]
    assert worker.requirements("requires: [llm]\nstages:\n  - id: a\n    agent: {harness: codex}\n") == [
        "codex", "git", "llm"]
    assert worker.requirements("stages: []\n") is None


def test_an_agent_job_carries_the_git_hosts(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    _agent_world(sqlite_engine, keyring, clock, world)
    _setting(sqlite_engine, "agent.git_hosts", ["git.example.com"])
    _job(sqlite_engine, clock, world, version=2)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    assert spawned[0]["env"]["ARMATURE_GIT_HOSTS"] == "git.example.com"


def test_an_agent_job_carries_the_projects_repo_and_no_secret_replaces_it(sqlite_engine, keyring, clock, world,
                                                                          tmp_path, spawned):
    _agent_world(sqlite_engine, keyring, clock, world)
    with sqlite_engine.begin() as conn:
        conn.execute(update(projects).where(projects.c.id == world["project"]).values(repo="Acme/App"))
    _setting(sqlite_engine, "env.ARMATURE_PROJECT_REPO", "evil/fork", team_id=world["team"])
    _job(sqlite_engine, clock, world, version=2)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    assert spawned[0]["env"]["ARMATURE_PROJECT_REPO"] == "Acme/App"


def test_git_hosts_default_to_github(sqlite_engine, world):
    with sqlite_engine.connect() as conn:
        assert worker.git_hosts(conn, world["team"]) == ["github.com"]


def test_the_server_worker_never_takes_an_agent_job(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    _setting(sqlite_engine, "worker.capabilities", ["claude-code", "codex", "git"])
    agent_job = _job(sqlite_engine, clock, world, trigger=False, requires=["claude-code", "git"])
    git_job = _job(sqlite_engine, clock, world, trigger=False, requires=["git"])
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    assert _get(sqlite_engine, agent_job).status == "queued"
    assert _get(sqlite_engine, git_job).status != "queued"


def test_a_project_skill_and_model_alias_win(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    from armature.server.db.schema import documents, providers
    _agent_world(sqlite_engine, keyring, clock, world)
    project = defs.DefScope("project", world["team"], world["project"])
    with sqlite_engine.begin() as conn:
        conn.execute(insert(providers).values(id=new_id(), team_id=world["team"], project_id=world["project"],
                                              kind="anthropic", config={"credential_secret": "PROJECT_KEY"},
                                              aliases={"review": "claude-opus-5-5"}, created_at=clock(),
                                              updated_at=clock()))
        doc = new_id()
        conn.execute(insert(documents).values(id=doc, kind="skill", key="kotlin-review", current_version=1,
                                              created_at=clock(), updated_at=clock(), **project.columns()))
        defs._add_version(conn, defs.DOCUMENT, doc, 1, "project skill", None, None, clock())
        conn.execute(insert(settings).values(**project.columns(), key="agent.git_hosts",
                                             value=["git.project.test"]))
    _secret(sqlite_engine, keyring, "PROJECT_KEY", "project-key", team_id=world["team"])
    job_id = _job(sqlite_engine, clock, world, version=2)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    env = spawned[0]["env"]
    assert json.loads(env["ARMATURE_AGENT_MODELS"]) == {
        "review": {"model": "claude-opus-5-5", "env": {"ANTHROPIC_API_KEY": "project-key"}}}
    assert (tmp_path / "work" / job_id / "skills" / "kotlin-review" / "SKILL.md").read_text() == "project skill"
    assert env["ARMATURE_GIT_HOSTS"] == "git.project.test"


def test_settings_cannot_set_host_only_agent_env(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    _setting(sqlite_engine, "env.ARMATURE_AGENT_USER", "root", team_id=world["team"])
    _setting(sqlite_engine, "env.ARMATURE_AGENT_DIR", "/etc", team_id=world["team"])
    _setting(sqlite_engine, "env.ARMATURE_SKILLS_DIR", "/etc", team_id=world["team"])
    _secret(sqlite_engine, keyring, "API_TOKEN", "t", team_id=world["team"])
    _job(sqlite_engine, clock, world)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    env = spawned[0]["env"]
    assert not {"ARMATURE_AGENT_USER", "ARMATURE_AGENT_DIR", "ARMATURE_SKILLS_DIR"} & set(env)


def test_a_secret_or_setting_cannot_widen_the_git_hosts(sqlite_engine, keyring, clock, world, tmp_path, spawned):
    _agent_world(sqlite_engine, keyring, clock, world)
    _secret(sqlite_engine, keyring, "ARMATURE_GIT_HOSTS", "evil.test", team_id=world["team"])
    _secret(sqlite_engine, keyring, "ARMATURE_AGENT_MODELS", '{"review": {"model": "x", "env": {}}}',
            team_id=world["team"])
    _setting(sqlite_engine, "env.ARMATURE_GIT_HOSTS", "evil.test", team_id=world["team"])
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == world["rule"]).values(
            config={"secrets": ["API_TOKEN", "ARMATURE_GIT_HOSTS", "ARMATURE_AGENT_MODELS"]}))
    _job(sqlite_engine, clock, world, version=2)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    env = spawned[0]["env"]
    assert env["ARMATURE_GIT_HOSTS"] == "github.com"
    assert json.loads(env["ARMATURE_AGENT_MODELS"])["review"]["model"] == "claude-sonnet-5-5"


def test_a_job_without_agent_steps_drops_agent_names_from_its_secrets(sqlite_engine, keyring, clock, world,
                                                                       tmp_path, spawned):
    _secret(sqlite_engine, keyring, "API_TOKEN", "t", team_id=world["team"])
    _secret(sqlite_engine, keyring, "ARMATURE_GIT_HOSTS", "evil.test", team_id=world["team"])
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == world["rule"]).values(
            config={"secrets": ["API_TOKEN", "ARMATURE_GIT_HOSTS"]}))
    _job(sqlite_engine, clock, world)
    _worker(sqlite_engine, keyring, clock, tmp_path).tick()
    assert "ARMATURE_GIT_HOSTS" not in spawned[0]["env"]


def _queue(engine, clock, world, key="acme/app#7", **kwargs):
    with engine.begin() as conn:
        job_id = worker.queue_job(conn, team_id=world["team"], project_id=world["project"],
                                  workflow_id=world["workflow"], inputs=kwargs.pop("inputs", {"number": 7}),
                                  now=clock(), trigger_id=world["rule"], key=key, **kwargs)
    clock.advance(seconds=1)
    return job_id


def test_the_newest_queued_job_of_a_key_supersedes_older_ones(sqlite_engine, clock, world):
    first = _queue(sqlite_engine, clock, world, supersede=True, inputs={"full": True}, sticky=["full"])
    other = _queue(sqlite_engine, clock, world, key="acme/app#8", supersede=True)
    second = _queue(sqlite_engine, clock, world, supersede=True, inputs={"full": False}, sticky=["full"])
    third = _queue(sqlite_engine, clock, world, supersede=True, sticky=["full"])
    rows = {job_id: _get(sqlite_engine, job_id) for job_id in (first, other, second, third)}
    assert [rows[j].status for j in (first, second)] == ["superseded", "superseded"]
    assert rows[first].superseded_by == second and rows[second].superseded_by == third
    assert rows[first].error == worker.SUPERSEDED and rows[first].finished_at is not None
    assert rows[third].status == "queued" and rows[third].inputs == {"number": 7, "full": True}
    assert rows[other].status == "queued" and rows[other].concurrency_key == "acme/app#8"


def test_a_running_job_of_the_key_finishes_unless_cancel_running(sqlite_engine, clock, world):
    running = _queue(sqlite_engine, clock, world, supersede=True)
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == running).values(status="running"))
    _queue(sqlite_engine, clock, world, supersede=True)
    assert _get(sqlite_engine, running).cancel_requested_at is None
    _queue(sqlite_engine, clock, world, supersede=True, cancel_running=True)
    row = _get(sqlite_engine, running)
    assert row.status == "running" and row.cancel_requested_at is not None


def test_a_debounced_job_waits_for_its_quiet_period(sqlite_engine, keyring, clock, world, tmp_path):
    job_id = _queue(sqlite_engine, clock, world, not_before=clock() + timedelta(minutes=10))
    w = _worker(sqlite_engine, keyring, clock, tmp_path)
    assert w.claim(clock()) is None
    assert w.try_claim(job_id, clock()) is False
    clock.advance(minutes=10)
    assert w.claim(clock()) == job_id


def test_one_job_of_a_key_holds_the_others_and_the_next_one_waits(sqlite_engine, keyring, clock, world, tmp_path):
    first = _queue(sqlite_engine, clock, world)
    second = _queue(sqlite_engine, clock, world)
    loose = _queue(sqlite_engine, clock, world, key=None)
    w = _worker(sqlite_engine, keyring, clock, tmp_path)
    assert w.claim(clock()) == first
    with sqlite_engine.connect() as conn:
        assert worker.claimable(conn, capabilities=None, now=clock()) == [loose]
    assert w.try_claim(second, clock()) is False
    assert w.claim(clock()) == loose
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == first).values(status="succeeded"))
    assert w.claim(clock()) == second


def test_two_concurrent_claims_of_one_key_start_one_job(sqlite_engine, keyring, clock, world, tmp_path):
    import threading
    ids = [_queue(sqlite_engine, clock, world) for _ in range(2)]
    workers = [_worker(sqlite_engine, keyring, clock, tmp_path, worker_id=name) for name in ("a", "b")]
    won, errors, gate = [], [], threading.Barrier(2)

    def go(w, job_id):
        try:
            gate.wait()
            if w.try_claim(job_id, clock()):
                won.append(job_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go, args=pair) for pair in zip(workers, ids, strict=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(won) == 1
    with sqlite_engine.connect() as conn:
        held = conn.execute(select(func.count()).select_from(jobs).where(jobs.c.status == "claimed")).scalar()
    assert held == 1

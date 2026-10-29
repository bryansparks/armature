"""Calls between stored workflows, copying to a project, and project providers and settings."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from armature.runner.client import Client, Runner
from armature.runner.process import (
    BUNDLE_VERSION,
    check_bundle_version,
    share,
    write_bundle,
)
from armature.server import calls, defs, dispatch, llm, runners, webhooks, worker
from armature.server.config import ServerConfig
from armature.server.db import new_id
from armature.server.db.schema import jobs, providers, runner_keys, settings, workflows
from armature.spec.calls import BundleCallResolver
from armature.spec.loader import STORED_WORKFLOW_HEADER
from tests.server import test_defs_write as base
from tests.server.test_defs_write import _add_def, _client

world = base.world
fake_catalog = base.fake_catalog
public_dns = base.public_dns

CHILD = """name: child
requires: [git]
contracts:
  inputs:
    - name: topic
stages:
  - id: post
    adapter: demo/post
"""

PARENT = """name: parent
stages:
  - id: run_child
    call:
      workflow: child
    with:
      topic: "{{ number }}"
"""


def _project(world):
    return defs.DefScope("project", world.own.team, world.own.project)


def _seed(world, scope, name, body):
    with world.engine.begin() as conn:
        return _add_def(conn, defs.WORKFLOW, scope, name, body, world.clock)


def _calling(name, callee, extra=""):
    return f"name: {name}\nstages:\n  - id: c\n    call:\n      workflow: {callee}\n{extra}"


def _save(client, workflow_id, body, base=1):
    return client.post(f"/api/workflows/{workflow_id}", json={"body": body, "base_version": base})


def _codes(response):
    return {e["code"] for e in response.json()["validation"]["errors"]}


def test_a_call_resolves_only_in_the_callers_own_scope(world):
    _seed(world, defs.DefScope("team", world.own.team), "child", CHILD)
    _seed(world, defs.INSTANCE, "child", CHILD)
    _seed(world, defs.DefScope("project", world.own.team, world.own.sibling), "child", CHILD)
    _seed(world, defs.DefScope("project", world.other.team, world.other.project), "child", CHILD)
    with world.engine.connect() as conn:
        resolver = calls.DbCallResolver(conn, world.own.project)
        with pytest.raises(calls.CallNotFound):
            resolver.resolve("child", None)
        assert calls.DbCallResolver(conn, world.own.team).resolve("child", None).text == CHILD
    admin = _client(world, "admin")
    created = admin.post("/api/workflows", json={"name": "parent", "scope_kind": "project",
                                                 "scope_id": world.own.project, "body": PARENT})
    assert created.status_code == 422 and _codes(created) == {"CALL_UNRESOLVED"}
    _seed(world, _project(world), "child", CHILD)
    created = admin.post("/api/workflows", json={"name": "parent", "scope_kind": "project",
                                                 "scope_id": world.own.project, "body": PARENT})
    assert created.status_code == 201, created.text


def test_saving_checks_the_callees_inputs_and_both_orders_of_a_cycle(world):
    child = _seed(world, _project(world), "child", CHILD)
    parent = _seed(world, _project(world), "parent", PARENT)
    admin = _client(world, "admin")
    wrong = PARENT.replace("topic:", "colour:")
    assert _codes(_save(admin, parent, wrong)) == {"CALL_UNKNOWN_INPUT"}
    back = CHILD + "  - id: again\n    call:\n      workflow: parent\n"
    assert "CALL_CYCLE" in _codes(_save(admin, child, back))
    second = _seed(world, _project(world), "second", "name: second\nstages:\n  - id: s\n    adapter: demo/post\n")
    _seed(world, _project(world), "first", _calling("first", "second"))
    assert "CALL_CYCLE" in _codes(_save(admin, second, _calling("second", "first")))
    validated = admin.post("/api/workflows/validate", json={"body": PARENT.replace("child", "missing"),
                                                            "scope_kind": "project", "scope_id": world.own.project})
    assert {e["code"] for e in validated.json()["validation"]["errors"]} == {"CALL_UNRESOLVED"}


def test_a_callee_with_callers_cannot_be_deleted_or_renamed(world):
    child = _seed(world, _project(world), "child", CHILD)
    _seed(world, _project(world), "parent", PARENT)
    admin = _client(world, "admin")
    deleted = admin.post(f"/api/workflows/{child}/delete", json={})
    assert deleted.status_code == 409 and deleted.json()["callers"] == ["parent"]
    renamed = _save(admin, child, CHILD.replace("name: child", "name: kid"))
    assert renamed.status_code == 409 and renamed.json()["callers"] == ["parent"]
    assert _save(admin, child, CHILD.replace("    adapter", "    fail_as_value: true\n    adapter")).status_code == 200


def test_the_call_graph_is_bounded_in_size_and_depth(world):
    scope = _project(world)
    for i in range(calls.MAX_CALLEES + 1):
        _seed(world, scope, f"leaf{i}", f"name: leaf{i}\nstages:\n  - id: s\n    adapter: demo/post\n")
    wide = "name: wide\nstages:\n" + "".join(
        f"  - id: c{i}\n    call:\n      workflow: leaf{i}\n" for i in range(calls.MAX_CALLEES + 1))
    for depth in range(calls.MAX_DEPTH + 1):
        _seed(world, scope, f"deep{depth}", _calling(f"deep{depth}", f"deep{depth + 1}") if depth < calls.MAX_DEPTH
              else f"name: deep{depth}\nstages:\n  - id: s\n    adapter: demo/post\n")
    admin = _client(world, "admin")
    for name, body in (("wide", wide), ("deep", _calling("deep", "deep0"))):
        created = admin.post("/api/workflows", json={"name": name, "scope_kind": "project",
                                                     "scope_id": world.own.project, "body": body})
        assert created.status_code == 422 and calls.TOO_LARGE in _codes(created), created.text
    ok = admin.post("/api/workflows", json={"name": "shallow", "scope_kind": "project",
                                            "scope_id": world.own.project, "body": _calling("shallow", "deep1")})
    assert ok.status_code == 201, ok.text


def _enqueue(world, workflow_id):
    with world.engine.begin() as conn:
        [matched] = webhooks.enqueue(conn, team_id=world.own.team, delivery_id="d", now=world.clock(), decisions=[
            {"matched": True, "shadow": False, "workflow_id": workflow_id, "project_id": world.own.project,
             "inputs": {}, "trigger_id": None}])
        return conn.execute(select(jobs).where(jobs.c.id == matched["job_id"])).one()


def _bump(world, workflow_id, body):
    with world.engine.begin() as conn:
        version = conn.execute(select(workflows.c.current_version).where(workflows.c.id == workflow_id)).scalar() + 1
        defs._add_version(conn, defs.WORKFLOW, workflow_id, version, body, "edit", None, world.clock())
        conn.execute(update(workflows).where(workflows.c.id == workflow_id).values(current_version=version))


def _bundle(world, job):
    with world.engine.begin() as conn:
        return worker.build_bundle(conn, world.keyring, job, now=world.clock(), lease=timedelta(minutes=2),
                                   events="unix:/x")


def test_enqueue_pins_callees_and_unions_their_requires(world, tmp_path):
    child = _seed(world, _project(world), "child", CHILD)
    parent = _seed(world, _project(world), "parent", PARENT + "requires: [llm]\n")
    job = _enqueue(world, parent)
    assert job.call_pins == {"child": 1} and job.requires == ["calls:v2", "git", "llm"]
    _bump(world, child, CHILD.replace("requires: [git]", "requires: [git]\ndescription: newer"))
    bundle = _bundle(world, job)
    assert set(bundle.calls) == {"manifest.json", "child@1.yaml"}
    assert json.loads(bundle.calls["manifest.json"]) == {"pins": {"child": 1}}
    assert bundle.calls["child@1.yaml"] == f"{STORED_WORKFLOW_HEADER}\n{CHILD}"
    assert bundle.payload()["bundle_version"] == BUNDLE_VERSION
    write_bundle(tmp_path, bundle.workflow, bundle.calls)
    assert BundleCallResolver(tmp_path / "calls").resolve("child", None).version == 1
    assert (tmp_path / "calls" / "child@1.yaml").stat().st_mode & 0o777 == 0o600


def test_a_job_queued_before_pins_resolves_its_callees_at_claim(world):
    _seed(world, _project(world), "child", CHILD)
    parent = _seed(world, _project(world), "parent", PARENT)
    job = _enqueue(world, parent)
    with world.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job.id).values(call_pins=None))
        job = conn.execute(select(jobs).where(jobs.c.id == job.id)).one()
    assert set(_bundle(world, job).calls) == {"manifest.json", "child@1.yaml"}


def _runner(world, capabilities):
    with world.engine.begin() as conn:
        record, _ = runners.create_key(conn, world.keyring, team_id=None, name=f"k-{new_id()[:8]}", actor_id=None,
                                       now=world.clock(), ip=None)
        key = conn.execute(select(runner_keys).where(runner_keys.c.id == record["id"])).one()
        return key, dispatch.hello(conn, key=key, body={"name": "r", "capabilities": capabilities},
                                   now=world.clock())["runner_id"]


def _claim(world, key, runner_id, version):
    bundle = dispatch.claim(world.engine, world.keyring, key=key, runner_id=runner_id, now=world.clock(),
                            lease=timedelta(minutes=2), events="unix:/x", bundle_version=version)
    return bundle and bundle.job_id


def test_only_a_runner_that_reads_roles_gets_a_job_whose_agent_names_one(world):
    body = "name: roled\nstages:\n  - id: a\n    agent:\n      model: review\n      role: security\n      prompt: p\n"
    job = _enqueue(world, _seed(world, _project(world), "roled", body))
    assert worker.ROLES_CAPABILITY in job.requires
    key, runner_id = _runner(world, ["git", "claude-code"])
    assert _claim(world, key, runner_id, worker.ROLES_BUNDLE_VERSION - 1) is None
    _claim(world, key, runner_id, worker.ROLES_BUNDLE_VERSION)
    with world.engine.connect() as conn:
        assert conn.execute(select(jobs.c.status).where(jobs.c.id == job.id)).scalar() != "queued"


def test_old_runners_never_get_a_job_that_calls(world):
    _seed(world, _project(world), "child", CHILD)
    pinned = _calling("pinned", "child", "      version: 1\n")
    explicit = _enqueue(world, _seed(world, _project(world), "pinned", pinned))
    assert explicit.call_pins == {} and worker.CALLS_CAPABILITY in explicit.requires
    world.clock.advance(seconds=1)
    legacy = _enqueue(world, _seed(world, _project(world), "parent", PARENT))
    with world.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == legacy.id).values(call_pins=None, requires=None))
    world.clock.advance(seconds=1)
    plain = _enqueue(world, world.own.wf["project"])
    key, runner_id = _runner(world, ["git"])
    assert _claim(world, key, runner_id, None) == plain.id
    assert _claim(world, key, runner_id, 1) is None
    assert {_claim(world, key, runner_id, 2), _claim(world, key, runner_id, 2)} == {explicit.id, legacy.id}
    with pytest.raises(dispatch.RunnerError, match=f"from 1 to {BUNDLE_VERSION}"):
        _claim(world, key, runner_id, BUNDLE_VERSION + 1)


def test_unresolved_calls_refuse_the_rule_and_never_queue_unpinned(world):
    parent = _seed(world, _project(world), "parent", PARENT)
    rule = SimpleNamespace(id=new_id(), workflow_id=parent, enabled=True, config={})
    with world.engine.begin() as conn:
        decision = webhooks._decide(conn, world.own.team, "github", rule, None, {})
        assert not decision["matched"] and "CALL_UNRESOLVED" in decision["reason"]
    with world.engine.begin() as conn:
        [entry] = webhooks.enqueue(conn, team_id=world.own.team, delivery_id="d", now=world.clock(), decisions=[
            {"matched": True, "shadow": False, "workflow_id": parent, "project_id": world.own.project,
             "inputs": {}, "trigger_id": None}])
        assert entry["job_id"] is None and "CALL_UNRESOLVED" in entry["error"]
        assert conn.execute(select(jobs)).all() == []


def test_settings_and_secrets_never_replace_the_hosts_path_home_or_packs(world, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/runner")
    monkeypatch.delenv("ARMATURE_PACKS_PATH", raising=False)
    planted = {"PATH": "/evil", "HOME": "/evil", "ARMATURE_PACKS_PATH": "/evil", "REGION": "x"}
    env = Runner(Client("http://127.0.0.1:9", "ark_k"), name="r", capabilities=[], slots=1, work_dir=tmp_path,
                 binary="armature").env(
        {"settings": planted, "secrets": {}, "events_url": "unix:/x", "run_token": "t"})
    assert (env["PATH"], env["HOME"], env["REGION"]) == ("/usr/bin", "/home/runner", "x")
    assert "ARMATURE_PACKS_PATH" not in env
    job = _enqueue(world, world.own.wf["project"])
    with world.engine.begin() as conn:
        for key in ("env.PATH", "env.HOME", "env.ARMATURE_PACKS_PATH"):
            conn.execute(insert(settings).values(team_id=None, key=key, value="/evil"))
    hub = SimpleNamespace(unix_endpoint="/x", engine=world.engine)
    embedded = worker.Worker(world.engine, world.keyring, ServerConfig(database_url="sqlite://", packs_dir="/srv/p"),
                             hub, clock=world.clock, work_dir=tmp_path / "work")
    with world.engine.begin() as conn:
        _, _, env = embedded._prepare(conn, job, "/bin/armature", tmp_path / "job")
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/runner"
    assert env["ARMATURE_PACKS_PATH"].split(":")[-1] == "/srv/p"


def test_the_runner_reads_only_the_call_bundle_layout(tmp_path):
    with pytest.raises(ValueError):
        write_bundle(tmp_path / "a", "name: x\n", {"child.yaml": "x"})
    with pytest.raises(ValueError):
        write_bundle(tmp_path / "b", "name: x\n", {"..@1.yaml": "x"})
    check_bundle_version(None)
    check_bundle_version(BUNDLE_VERSION)
    with pytest.raises(ValueError, match="Upgrade armature"):
        check_bundle_version(BUNDLE_VERSION + 1)


def test_share_copies_the_manifest_too(tmp_path, monkeypatch):
    import grp
    import os

    write_bundle(tmp_path / "w", "name: x\n", {"manifest.json": "{}", "child@1.yaml": "name: child\n"})
    monkeypatch.setattr(grp, "getgrnam", lambda name: type("G", (), {"gr_gid": os.getgid()})())
    run_dir = share(tmp_path / "w", "job", "me", base=tmp_path)
    assert sorted(p.name for p in (run_dir / "calls").iterdir()) == ["child@1.yaml", "manifest.json"]


def test_copy_to_project_copies_the_call_closure(world):
    scope = _project(world)
    grandchild = "name: grandchild\nstages:\n  - id: s\n    adapter: demo/post\n"
    _seed(world, scope, "grandchild", grandchild)
    _seed(world, scope, "child", CHILD + "  - id: deeper\n    call:\n      workflow: grandchild\n")
    parent = _seed(world, scope, "parent", PARENT)
    lead = _client(world, "team_lead")
    sibling = {"scope_kind": "project", "scope_id": world.own.sibling}
    copied = lead.post(f"/api/workflows/{parent}/fork", json={**sibling, "name": "ported"})
    assert copied.status_code == 201, copied.text
    with world.engine.connect() as conn:
        rows = {r.name: r.id for r in conn.execute(select(workflows).where(workflows.c.scope == world.own.sibling))}
        assert defs.body_of(conn, defs.WORKFLOW, rows["ported"], 1).startswith("name: ported\n")
    assert set(rows) == {"ported", "child", "grandchild"}
    again = lead.post(f"/api/workflows/{parent}/fork", json={**sibling, "name": "twice"})
    assert again.status_code == 409 and again.json()["conflicts"] == ["child", "grandchild"]
    team = lead.post(f"/api/workflows/{parent}/fork", json={"scope_kind": "team", "scope_id": world.own.team})
    assert team.status_code == 400


def test_project_aliases_win_over_the_team_and_never_cross_projects(world):
    lead = _client(world, "project_lead")
    made = lead.post(f"/api/projects/{world.own.project}/providers",
                     json={"kind": "anthropic", "aliases": {"review": "claude-project"}})
    assert made.status_code == 200, made.text
    assert made.json()["provider"]["project_id"] == world.own.project
    assert lead.post(f"/api/teams/{world.own.team}/providers", json={"kind": "anthropic"}).status_code == 403
    assert lead.post(f"/api/projects/{world.own.sibling}/providers", json={"kind": "anthropic"}).status_code == 403
    team_lead = _client(world, "team_lead")
    team = team_lead.post(f"/api/teams/{world.own.team}/providers",
                          json={"kind": "anthropic", "aliases": {"review": "claude-team", "lead": "claude-lead"}})
    assert team.status_code == 200, team.text
    with world.engine.connect() as conn:
        assert defs.resolve_alias(conn, "review", world.own.team, world.own.project)[1] == "claude-project"
        assert defs.resolve_alias(conn, "lead", world.own.team, world.own.project)[1] == "claude-lead"
        assert defs.resolve_alias(conn, "review", world.own.team, world.own.sibling)[1] == "claude-team"
        assert defs.resolve_alias(conn, "review", world.other.team, world.other.project) is None
        assert llm.resolve_provider(conn, world.own.team, "review", world.own.project)[1] == "claude-project"
        assert llm.resolve_provider(conn, world.own.team, "review", world.own.sibling)[1] == "claude-team"
        assert llm.team_aliases(conn, world.own.team, world.own.project) == ["lead", "review"]
    assert [p["kind"] for p in team_lead.get(f"/api/teams/{world.own.team}/providers").json()["providers"]] == [
        "anthropic"]
    project_provider = made.json()["provider"]["id"]
    assert lead.post(f"/api/providers/{team.json()['provider']['id']}", json={"aliases": {}}).status_code == 403
    assert lead.post(f"/api/providers/{project_provider}", json={"aliases": {"x": "m"}}).status_code == 200
    assert lead.post(f"/api/providers/{project_provider}/delete", json={}).status_code == 200


def test_a_project_lead_writes_only_their_projects_settings_and_project_env_wins(world):
    lead = _client(world, "project_lead")
    for key in ("env.PATH", "env.LD_PRELOAD", "env.GIT_CONFIG_COUNT", "env.HOME"):
        refused = lead.post("/api/settings", json={"scope": world.own.project, "key": key, "value": "/tmp/evil"})
        assert refused.status_code == 403, key
    admin = _client(world, "admin")
    assert admin.post("/api/settings", json={"scope": world.own.project, "key": "env.REGION",
                                             "value": "project"}).status_code == 200
    own = lead.post("/api/settings", json={"scope": world.own.project, "key": "review.mode", "value": "strict"})
    assert own.status_code == 200, own.text
    assert lead.post("/api/settings", json={"scope": world.own.project, "key": "display.show_cost",
                                            "value": True}).status_code == 400
    for scope in ("instance", world.own.team, world.own.sibling):
        assert lead.post("/api/settings", json={"scope": scope, "key": "review.mode", "value": "x"}).status_code == 403
    listed = lead.get("/api/settings", params={"scope": world.own.project}).json()["settings"]
    assert [(s["key"], s["value"]) for s in listed] == [("env.REGION", "project"), ("review.mode", "strict")]
    assert lead.get("/api/settings").status_code == 403
    with world.engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.own.team, key="env.REGION", value="team"))
        conn.execute(insert(settings).values(team_id=None, key="env.ONLY", value="instance"))
        assert worker.team_env(conn, world.own.team, world.own.project) == {"REGION": "project", "ONLY": "instance"}
        assert worker.team_env(conn, world.own.team, world.own.sibling) == {"REGION": "team", "ONLY": "instance"}
    assert lead.post("/api/settings/delete", json={"scope": world.own.project, "key": "env.REGION"}).status_code == 403
    assert lead.post("/api/settings/delete", json={"scope": world.own.project, "key": "review.mode"}).status_code == 200


def test_0015_backfills_provider_scope_and_downgrade_drops_project_rows(db_url):
    from alembic import command
    from sqlalchemy import text

    from armature.server.db import alembic_config, make_engine, migrate, new_id, utcnow
    migrate(db_url, "0014")
    team, project = new_id(), new_id()
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            at = {"at": "2026-10-30 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'own', :at)"), {"t": team, **at})
            conn.execute(text("INSERT INTO projects (id, team_id, name, created_at) VALUES (:p, :t, 'app', :at)"),
                         {"p": project, "t": team, **at})
            conn.execute(text("INSERT INTO providers (id, team_id, kind, created_at, updated_at) "
                              "VALUES (:i, :t, 'anthropic', :at, :at)"), {"i": new_id(), "t": team, **at})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            assert conn.execute(select(providers.c.scope)).scalar_one() == team
            conn.execute(insert(providers).values(id=new_id(), team_id=team, project_id=project, kind="anthropic",
                                                  created_at=utcnow(), updated_at=utcnow()))
            conn.execute(insert(settings).values(team_id=team, project_id=project, key="env.A", value="1"))
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0014")
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM providers")).scalar() == 1
            assert conn.execute(text("SELECT count(*) FROM settings")).scalar() == 0
    finally:
        engine.dispose()


def test_only_a_team_lead_hands_a_team_secret_to_a_project_provider(world, public_dns):
    lead = _client(world, "project_lead")
    path = f"/api/projects/{world.own.project}/providers"
    attacker = {"kind": "openai-compatible", "config": {"api_base": "https://attacker.example.com/v1"},
                "credential_secret": "TEAM_OPENAI_KEY"}
    refused = lead.post(path, json=attacker)
    assert refused.status_code == 403 and "team lead" in refused.json()["error"]
    made = _client(world, "team_lead").post(path, json={**attacker, "config": {"api_base": "https://llm.example/v1"}})
    assert made.status_code == 200, made.text
    provider = f"/api/providers/{made.json()['provider']['id']}"
    moved = lead.post(provider, json={"config": {"api_base": "https://attacker.example.com/v1"},
                                      "credential_secret": "TEAM_OPENAI_KEY"})
    assert moved.status_code == 403
    assert lead.post(provider, json={"aliases": {"review": "gpt"}}).status_code == 200
    assert lead.post(path, json={"kind": "anthropic"}).status_code == 200


def test_a_copy_that_names_a_later_callee_version_says_versions_reset(world):
    child = _seed(world, _project(world), "child", CHILD)
    _bump(world, child, CHILD.replace("requires: [git]", "requires: [git]\ndescription: second"))
    later = PARENT.replace("workflow: child", "workflow: child\n      version: 2")
    parent = _seed(world, _project(world), "parent", later)
    refused = _client(world, "admin").post(f"/api/workflows/{parent}/fork",
                                           json={"scope_kind": "project", "scope_id": world.own.sibling})
    assert refused.status_code == 422 and "version 1" in refused.json()["error"]
    assert "CALL_UNRESOLVED" in _codes(refused)


@pytest.mark.parametrize("key", ["ENV.LD_PRELOAD", "Env.LD_PRELOAD"])
def test_a_mixed_case_env_prefix_is_refused_for_every_role(world, key):
    body = {"scope": world.own.project, "key": key, "value": "/tmp/evil.so"}
    assert _client(world, "project_lead").post("/api/settings", json=body).status_code == 403
    admin = _client(world, "admin").post("/api/settings", json=body)
    assert admin.status_code == 422 and "lowercase" in admin.json()["error"]


def test_team_env_ignores_a_stray_mixed_case_row(world):
    with world.engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=None, key="ENV.LD_PRELOAD", value="/tmp/evil.so"))
        conn.execute(insert(settings).values(team_id=None, key="env.REGION", value="us-east-1"))
        assert worker.team_env(conn, world.own.team, world.own.project) == {"REGION": "us-east-1"}


AGENT_CHILD = """name: child
stages:
  - id: review
    agent:
      model: review
      prompt: Review.
      skills: [kotlin-review]
"""


def test_an_agent_step_only_in_a_callee_gets_its_skill_model_and_hosts(world):
    from armature.server import secrets as secret_store
    _seed(world, _project(world), "child", AGENT_CHILD)
    parent = _seed(world, _project(world), "parent", _calling("parent", "child"))
    with world.engine.begin() as conn:
        conn.execute(insert(providers).values(id=new_id(), team_id=world.own.team, kind="anthropic",
                                              config={"credential_secret": "AGENT_KEY"},
                                              aliases={"review": "claude-x"}, created_at=world.clock(),
                                              updated_at=world.clock()))
        secret_store.set_secret(conn, world.keyring, team_id=world.own.team, name="AGENT_KEY", value="k",
                                actor_id=None)
        _add_def(conn, defs.DOCUMENT, defs.DefScope("team", world.own.team), "kotlin-review", "skill body",
                 world.clock, doc_kind="skill")
    job = _enqueue(world, parent)
    assert {"claude-code", "git"} <= set(job.requires)
    bundle = _bundle(world, job)
    assert bundle.skills == {"kotlin-review": "skill body"}
    assert json.loads(bundle.secrets["ARMATURE_AGENT_MODELS"]) == {
        "review": {"model": "claude-x", "env": {"ANTHROPIC_API_KEY": "k"}}}
    assert bundle.secrets["ARMATURE_GIT_HOSTS"] == "github.com"

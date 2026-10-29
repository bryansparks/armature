"""Run actions: config, who may run one, how the host command runs, and what an operation records."""
import json
import sys
import textwrap
import threading
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select

from armature.server import actions
from armature.server import secrets as secret_store
from armature.server.config import ActionConfig, ServerConfig
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    operations,
    projects,
    run_steps,
    runner_keys,
    runs,
    teams,
)
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    sign_in_as,
)

FAKE = textwrap.dedent("""
    import json, os, sys, time
    control = json.load(open(sys.argv[1]))
    run_path = os.environ.get("ARMATURE_ACTION_RUN")
    with open(sys.argv[2], "w") as out:
        json.dump({"argv": sys.argv, "env": dict(os.environ), "run": json.load(open(run_path)),
                   "run_mode": oct(os.stat(run_path).st_mode & 0o777),
                   "dir_mode": oct(os.stat(os.path.dirname(run_path)).st_mode & 0o777)}, out)
    time.sleep(control.get("sleep", 0))
    sys.stderr.write(control.get("stderr", ""))
    sys.stdout.write(control.get("stdout", ""))
    sys.exit(control.get("exit", 0))
""")


HOST_KEY = "instance-runner-key"
TEAM_KEY = "team-runner-key"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text(FAKE)
    control = tmp_path / "control.json"
    record = tmp_path / "record.json"

    class Fake:
        command = (sys.executable, str(script), str(control), str(record))

        @staticmethod
        def set(**values):
            control.write_text(json.dumps(values))

        @staticmethod
        def seen():
            return json.loads(record.read_text())

        @staticmethod
        def ran():
            return record.exists()

    Fake.set(stdout=json.dumps({"ok": True, "message": "queued it"}) + "\n")
    return Fake


def _actions(fake):
    return [
        {"id": "rerun", "label": "Rerun", "workflows": ["code-review", "pr-fix"], "statuses": ["failed", "lost"],
         "confirm": "Queue it again?", "command": list(fake.command)},
        {"id": "cancel", "label": "Cancel", "workflows": ["code-review"], "statuses": ["running"],
         "command": list(fake.command)},
        {"id": "daily", "label": "Rerun daily", "workflows": ["repo-review"], "statuses": ["failed"],
         "admin_only": True, "command": list(fake.command)},
        {"id": "lead", "label": "Lead only", "workflows": ["code-review"], "min_role": "team_lead",
         "command": list(fake.command)},
    ]


@pytest.fixture
def world(engine, keyring, clock, fake):
    with engine.begin() as conn:
        team, other = new_id(), new_id()
        project = new_id()
        conn.execute(insert(teams).values(id=team, name="mobile", created_at=clock()))
        conn.execute(insert(teams).values(id=other, name="web", created_at=clock()))
        conn.execute(insert(projects).values(id=project, team_id=team, name="fs-eng/app", created_at=clock()))
        for key_id, key_team in ((HOST_KEY, None), (TEAM_KEY, team)):
            conn.execute(insert(runner_keys).values(id=key_id, team_id=key_team, name=key_id, key_hmac=key_id,
                                                    created_at=clock()))
    ids = {name: add_user(engine, keyring, clock, name, is_admin=name == "admin")[0]
           for name in ("admin", "contributor", "viewer", "outsider", "lead", "projectc")}
    add_membership(engine, clock, ids["contributor"], "team", team, "contributor")
    add_membership(engine, clock, ids["viewer"], "team", team, "viewer")
    add_membership(engine, clock, ids["outsider"], "team", other, "contributor")
    add_membership(engine, clock, ids["lead"], "team", team, "team_lead")
    add_membership(engine, clock, ids["projectc"], "project", project, "contributor")
    client = make_client(engine, keyring, clock, actions=_actions(fake))
    return {"team": team, "other": other, "project": project, "users": ids, "client": client}


# Actions run only on runs the host's own instance key registered, never a team key's.
def _run(engine, clock, team, workflow="code-review", status="failed", project=None, subject="fs-eng/app#12",
         trigger_ref=None, key=HOST_KEY):
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=team, project_id=project, workflow_name=workflow,
                                         status=status, started_at=clock(), subject=subject,
                                         trigger_ref=trigger_ref, summary={"title": "Review"},
                                         runner_key_id=key))
    return run_id


def _as(world, engine, keyring, clock, name, step_up=False):
    client = world["client"]
    client.cookies.clear()
    csrf = sign_in_as(client, engine, keyring, clock, world["users"][name], step_up=step_up)
    return client, {"Origin": "https://arm.test", "X-CSRF-Token": csrf}


def _post(world, engine, keyring, clock, name, run_id, action_id, step_up=False):
    client, headers = _as(world, engine, keyring, clock, name, step_up)
    return client.post(f"/api/runs/{run_id}/actions/{action_id}", headers=headers, json={})


# Config


def test_action_config_rejects_bad_entries():
    good = {"id": "rerun", "label": "Rerun", "workflows": ["x"], "command": ["/bin/true"]}
    assert ActionConfig.model_validate(good).min_role == "contributor"
    for bad in ({**good, "id": "Re run"}, {**good, "command": []}, {**good, "command": ["/bin/true", ""]},
                {**good, "workflows": []}, {**good, "min_role": "owner"}, {**good, "shell": True},
                {**good, "label": ""}):
        with pytest.raises(ValidationError):
            ActionConfig.model_validate(bad)
    with pytest.raises(ValidationError, match="own id"):
        ServerConfig(actions=[good, good])


def test_actions_and_status_come_only_from_the_file(tmp_path):
    from armature.server.config import load_config
    path = tmp_path / "server.yaml"
    path.write_text("actions:\n  - id: rerun\n    label: Rerun\n    workflows: [code-review]\n"
                    "    command: [/bin/true]\nstatus:\n  command: [/bin/echo, hi]\n")
    cfg = load_config(path, env={"ARMATURE_SERVER_ACTIONS": "[]", "ARMATURE_SERVER_STATUS": "x"})
    assert [a.id for a in cfg.actions] == ["rerun"]
    assert cfg.status.command == ["/bin/echo", "hi"] and cfg.status.interval_seconds == 30


# What is offered


def test_offered_matches_workflow_status_and_role(fake):
    cfg = ServerConfig(actions=_actions(fake))
    failed_review = {"workflow": "code-review", "status": "failed", "host_run": True}
    offered = {a["id"]: a for a in actions.offered(cfg.actions, failed_review, "contributor")}
    assert set(offered) == {"rerun", "cancel", "lead"}
    assert offered["rerun"]["enabled"] and offered["rerun"]["confirm"] == "Queue it again?"
    assert not offered["cancel"]["enabled"] and "state" in offered["cancel"]["reason"]
    assert not offered["lead"]["enabled"] and "role" in offered["lead"]["reason"]
    assert not any(a["enabled"] for a in actions.offered(cfg.actions, failed_review, "viewer"))
    daily = actions.offered(cfg.actions, {"workflow": "repo-review", "status": "failed", "host_run": True}, "team_lead")
    assert daily == [{"id": "daily", "label": "Rerun daily", "confirm": None, "enabled": False,
                      "reason": "Only an admin can do this."}]
    assert actions.offered(cfg.actions, {"workflow": "repo-review", "status": "failed", "host_run": True},
                           "admin")[0]["enabled"]
    forged = actions.offered(cfg.actions, {**failed_review, "host_run": False}, "admin")
    assert {(a["enabled"], a["reason"]) for a in forged} == {(False, actions.NOT_HOST_RUN)}


def test_get_actions_route(world, engine, keyring, clock):
    run_id = _run(engine, clock, world["team"])
    client, _ = _as(world, engine, keyring, clock, "contributor")
    body = client.get(f"/api/runs/{run_id}/actions").json()
    assert {a["id"]: a["enabled"] for a in body["actions"]} == {"rerun": True, "cancel": False, "lead": False}
    client, _ = _as(world, engine, keyring, clock, "outsider")
    assert client.get(f"/api/runs/{run_id}/actions").status_code == 404


# Who may run one


@pytest.mark.parametrize(("user", "action", "step_up", "expected"), [
    ("contributor", "rerun", False, 200),
    ("projectc", "rerun", False, 200),
    ("viewer", "rerun", False, 403),
    ("outsider", "rerun", False, 404),
    ("admin", "rerun", False, 200),
    ("contributor", "lead", False, 403),
    ("lead", "lead", False, 200),
    ("contributor", "cancel", False, 409),
    ("contributor", "nope", False, 404),
])
def test_rbac_matrix(world, engine, keyring, clock, fake, user, action, step_up, expected):
    run_id = _run(engine, clock, world["team"], project=world["project"])
    response = _post(world, engine, keyring, clock, user, run_id, action, step_up)
    assert response.status_code == expected, response.text
    assert fake.ran() == (expected == 200)


def test_admin_only_needs_admin_and_step_up(world, engine, keyring, clock, fake):
    run_id = _run(engine, clock, world["team"], workflow="repo-review")
    assert _post(world, engine, keyring, clock, "lead", run_id, "daily", step_up=True).status_code == 403
    refused = _post(world, engine, keyring, clock, "admin", run_id, "daily")
    assert refused.status_code == 403 and refused.json()["error"] == "step_up_required"
    assert not fake.ran()
    assert _post(world, engine, keyring, clock, "admin", run_id, "daily", step_up=True).status_code == 200


def test_mutation_checks_apply(world, engine, keyring, clock):
    run_id = _run(engine, clock, world["team"])
    client, headers = _as(world, engine, keyring, clock, "contributor")
    assert client.post(f"/api/runs/{run_id}/actions/rerun", json={}).status_code == 403
    assert client.post(f"/api/runs/{run_id}/actions/rerun", json={},
                       headers={**headers, "Origin": "https://evil.test"}).status_code == 403


# How the command runs


def test_run_fields_reach_the_command_only_in_the_json_file(world, engine, keyring, clock, fake):
    hostile = "fs-eng/app#12; rm -rf / $(touch pwned) `id`"
    run_id = _run(engine, clock, world["team"], subject=hostile[:191], trigger_ref={"repo": "fs-eng/app", "number": 12})
    with engine.begin() as conn:
        conn.execute(insert(run_steps).values(run_id=run_id, step_key="prepare", step_id="prepare",
                                              status="succeeded", output={"comment_id": 5, "scope": "pr"}))
    response = _post(world, engine, keyring, clock, "contributor", run_id, "rerun")
    assert response.status_code == 200
    seen = fake.seen()
    assert seen["argv"] == list(fake.command[1:])
    assert set(seen["env"]) <= {"PATH", "HOME", "LANG", "ARMATURE_ACTION_RUN", "__CF_USER_TEXT_ENCODING"}
    assert seen["run"]["subject"] == hostile
    assert seen["run"]["run_id"] == run_id and seen["run"]["workflow"] == "code-review"
    assert seen["run"]["status"] == "failed" and seen["run"]["actor_username"] == "contributor"
    assert seen["run"]["trigger_ref"] == {"repo": "fs-eng/app", "number": 12}
    assert seen["run"]["summary"] == {"title": "Review"}
    assert seen["run"]["prepare"] == {"comment_id": 5, "scope": "pr"}
    assert seen["run_mode"] == "0o600" and seen["dir_mode"] == "0o700"


@pytest.mark.parametrize(("control", "outcome", "message"), [
    ({"stdout": '{"ok": true, "message": "queued it"}\n'}, "ok", "queued it"),
    ({"stdout": "", "exit": 0}, "ok", "Done."),
    ({"stdout": '{"ok": false, "message": "Already retried."}\n', "exit": 3}, "refused", "Already retried."),
    ({"stderr": "refused: busy\n", "exit": 3}, "refused", "refused: busy"),
    ({"stderr": "boom\n", "exit": 1}, "failed", "boom"),
    ({"exit": 2}, "failed", "The command exited 2."),
])
def test_outcomes(world, engine, keyring, clock, fake, control, outcome, message):
    fake.set(**control)
    run_id = _run(engine, clock, world["team"])
    operation = _post(world, engine, keyring, clock, "contributor", run_id, "rerun").json()["operation"]
    shown = actions.FAILED_FOR_VIEWERS if outcome == "failed" else message
    assert (operation["outcome"], operation["message"]) == (outcome, shown)
    assert operation["exit_code"] == control.get("exit", 0)
    client, _ = _as(world, engine, keyring, clock, "admin")
    assert client.get(f"/api/operations/{operation['id']}").json()["operation"]["message"] == message
    assert [o["message"] for o in client.get(f"/api/operations?run={run_id}").json()["operations"]] == [message]
    client, _ = _as(world, engine, keyring, clock, "viewer")
    assert client.get(f"/api/operations/{operation['id']}").json()["operation"]["message"] == shown
    assert [o["message"] for o in client.get(f"/api/operations?run={run_id}").json()["operations"]] == [shown]
    with engine.connect() as conn:
        row = conn.execute(select(audit).where(audit.c.action == "operation.run")).one()
        active = conn.execute(select(operations.c.active_run, operations.c.active_action)).one()
    assert row.target_id == run_id and row.team_id == world["team"]
    assert row.detail["outcome"] == outcome and row.detail["operation_id"] == operation["id"]
    assert tuple(active) == (None, None)


def test_a_command_that_overruns_is_stopped(world, engine, keyring, clock, fake, monkeypatch):
    monkeypatch.setattr(actions, "ACTION_TIMEOUT", 0.5)
    fake.set(sleep=30)
    run_id = _run(engine, clock, world["team"])
    operation = _post(world, engine, keyring, clock, "admin", run_id, "rerun").json()["operation"]
    assert operation["outcome"] == "failed" and "did not finish" in operation["message"]


def test_a_missing_command_fails_cleanly(engine, keyring, clock, world):
    client = make_client(engine, keyring, clock, actions=[
        {"id": "rerun", "label": "Rerun", "workflows": ["code-review"], "command": ["/no/such/command"]}])
    world = {**world, "client": client}
    run_id = _run(engine, clock, world["team"])
    operation = _post(world, engine, keyring, clock, "admin", run_id, "rerun").json()["operation"]
    assert operation["outcome"] == "failed" and "could not start" in operation["message"]


def test_message_and_log_are_capped_and_redacted(world, engine, keyring, clock, fake):
    with engine.begin() as conn:
        secret_store.set_secret(conn, keyring, team_id=None, name="HOST_TOKEN", value="hunter2-secret", actor_id=None)
    long = "x" * 5000
    fake.set(stdout="noise ghp_abcdefghijklmnop hunter2-secret\n" * 3000
             + json.dumps({"ok": False, "message": f"hunter2-secret ghp_abc {long}"}) + "\n", exit=3)
    run_id = _run(engine, clock, world["team"])
    operation = _post(world, engine, keyring, clock, "contributor", run_id, "rerun").json()["operation"]
    assert operation["outcome"] == "refused"
    assert len(operation["message"].encode()) <= actions.MESSAGE_MAX
    assert "hunter2" not in operation["message"] and "ghp_abc" not in operation["message"]
    assert "log" not in operation
    client, _ = _as(world, engine, keyring, clock, "admin")
    full = client.get(f"/api/operations/{operation['id']}").json()["operation"]
    assert len(full["log"].encode()) <= actions.LOG_MAX
    assert "hunter2" not in full["log"] and "ghp_" not in full["log"] and "[redacted]" in full["log"]
    client, _ = _as(world, engine, keyring, clock, "contributor")
    assert "log" not in client.get(f"/api/operations/{operation['id']}").json()["operation"]


# One at a time


def _active(engine, clock, run_id, action_id, started=None):
    with engine.begin() as conn:
        conn.execute(insert(operations).values(id=new_id(), action_id=action_id, run_id=run_id,
                                               started_at=started or clock(), active_run=run_id,
                                               active_action=action_id))


def test_one_active_operation_per_run_and_per_action(world, engine, keyring, clock, fake):
    first, second = _run(engine, clock, world["team"]), _run(engine, clock, world["team"])
    _active(engine, clock, first, "lead")
    busy_run = _post(world, engine, keyring, clock, "contributor", first, "rerun")
    assert busy_run.status_code == 409 and "this run" in busy_run.json()["error"]
    _active(engine, clock, second, "rerun")
    third = _run(engine, clock, world["team"])
    busy_action = _post(world, engine, keyring, clock, "contributor", third, "rerun")
    assert busy_action.status_code == 409 and "already running" in busy_action.json()["error"]
    assert not fake.ran()


def test_a_stale_operation_stops_blocking(world, engine, keyring, clock, fake):
    run_id = _run(engine, clock, world["team"])
    _active(engine, clock, run_id, "rerun", started=clock() - actions.STALE_AFTER - timedelta(seconds=1))
    assert _post(world, engine, keyring, clock, "contributor", run_id, "rerun").status_code == 200
    with engine.connect() as conn:
        outcomes = sorted(conn.execute(select(operations.c.outcome)).scalars())
    assert outcomes == ["failed", "ok"]


def test_concurrent_presses_run_once(world, engine, keyring, clock, fake):
    fake.set(sleep=1, stdout='{"message": "done"}\n')
    run_id = _run(engine, clock, world["team"])
    client, headers = _as(world, engine, keyring, clock, "contributor")
    codes = []

    def press():
        codes.append(client.post(f"/api/runs/{run_id}/actions/rerun", headers=headers, json={}).status_code)

    threads = [threading.Thread(target=press) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(codes) == [200, 409, 409]


def test_recover_closes_what_a_dead_server_left(engine, clock, world):
    run_id = _run(engine, clock, world["team"])
    _active(engine, clock, run_id, "rerun")
    assert actions.recover(engine, clock()) == 1
    with engine.connect() as conn:
        row = conn.execute(select(operations)).one()
    assert row.outcome == "failed" and row.active_run is None and row.message == actions.INTERRUPTED


# Reading operations


def test_operations_list_is_scoped(world, engine, keyring, clock, fake):
    mine = _run(engine, clock, world["team"])
    theirs = _run(engine, clock, world["other"])
    assert _post(world, engine, keyring, clock, "contributor", mine, "rerun").status_code == 200
    assert _post(world, engine, keyring, clock, "outsider", theirs, "rerun").status_code == 200
    client, _ = _as(world, engine, keyring, clock, "viewer")
    assert [o["run_id"] for o in client.get("/api/operations").json()["operations"]] == [mine]
    assert [o["run_id"] for o in client.get(f"/api/operations?run={mine}").json()["operations"]] == [mine]
    assert client.get(f"/api/operations?run={theirs}").status_code == 404
    client, _ = _as(world, engine, keyring, clock, "admin")
    every = client.get("/api/operations?limit=10").json()["operations"]
    assert {o["run_id"] for o in every} == {mine, theirs}
    theirs_op = next(o for o in every if o["run_id"] == theirs)
    client, _ = _as(world, engine, keyring, clock, "viewer")
    assert client.get(f"/api/operations/{theirs_op['id']}").status_code == 404
    assert client.get("/api/operations/nope").status_code == 404


# Only the host's own runs


def test_a_team_key_run_is_offered_nothing_and_refused_even_for_an_admin(world, engine, keyring, clock, fake):
    forged = _run(engine, clock, world["team"], key=TEAM_KEY)
    unkeyed = _run(engine, clock, world["team"], key=None)
    for run_id in (forged, unkeyed):
        for user in ("contributor", "admin"):
            client, _ = _as(world, engine, keyring, clock, user)
            offered = client.get(f"/api/runs/{run_id}/actions").json()["actions"]
            assert offered and {(a["enabled"], a["reason"]) for a in offered} == {(False, actions.NOT_HOST_RUN)}
            response = _post(world, engine, keyring, clock, user, run_id, "rerun", step_up=True)
            assert (response.status_code, response.json()["error"]) == (409, actions.NOT_HOST_RUN)
    assert not fake.ran()
    with engine.connect() as conn:
        assert conn.execute(select(operations.c.id)).first() is None

"""Exporting definitions to files, by command and after each change."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import insert, select
from typer.testing import CliRunner

from armature.cli import app
from armature.packs import AdapterDef
from armature.server import connections, defs, rbac
from armature.server import secrets as secret_store
from armature.server.config import ExportConfig
from armature.server.db import new_id, utcnow
from armature.server.db.schema import audit, documents, projects, providers, teams, versions, workflows
from armature.server.export import ExportHook, ExportRefused, export_definitions
from armature.spec.loader import STORED_WORKFLOW_HEADER
from tests.server.app_support import Clock, add_user, make_client, mutation, sign_in_as

SECRET_VALUE = "export-test-secret-value"
WORKFLOW = "name: review\nstages:\n  - id: post\n    adapter: demo/post\n"


def _versioned(conn, table, object_kind, now, body, **values):
    object_id = new_id()
    conn.execute(insert(table).values(id=object_id, current_version=2, created_at=now, updated_at=now, **values))
    for version, text in ((1, "old body\n"), (2, body)):
        conn.execute(insert(versions).values(id=new_id(), object_kind=object_kind, object_id=object_id, version=version,
                                             body=text, author_id="t", created_at=now))
    return object_id


@pytest.fixture
def seeded(engine, keyring):
    now = utcnow()
    with engine.begin() as conn:
        team_id, other_id, project_id = new_id(), new_id(), new_id()
        conn.execute(insert(teams).values(id=team_id, name="Mobile", slug="mobile", created_at=now))
        conn.execute(insert(teams).values(id=other_id, name="Other", slug="other", created_at=now))
        conn.execute(insert(projects).values(id=project_id, team_id=team_id, name="android-tree",
                                             repo="fs-eng/android-tree", platform="android",
                                             enabled_workflows=["fix", "pr_review"], branch_filters=["dev"],
                                             created_at=now))
        _versioned(conn, workflows, "workflow", now, WORKFLOW, team_id=None, scope="*", name="review")
        _versioned(conn, workflows, "workflow", now, WORKFLOW, team_id=team_id, scope=team_id, name="review")
        _versioned(conn, workflows, "workflow", now, WORKFLOW, team_id=other_id, scope=other_id, name="private")
        conn.execute(insert(workflows).values(id=new_id(), team_id=None, scope="*", name="empty", current_version=0,
                                              created_at=now, updated_at=now))
        _versioned(conn, documents, "document", now, "---\nname: security\n---\n\nBe careful.\n", team_id=None,
                   scope="*", kind="role", key="security")
        _versioned(conn, documents, "document", now, "Shared.\n", team_id=None, scope="*", kind="prompt",
                   key="_shared")
        _versioned(conn, documents, "document", now, "Ship it.\n", team_id=team_id, project_id=project_id,
                   scope=project_id, kind="mission", key="goal")
        defs.put_setting(conn, team_id=None, key="ops.validate", value={"a": 1}, actor_id="t", now=now, ip=None)
        defs.put_setting(conn, team_id=team_id, key="teams.mobile.auto_fix", value=True, actor_id="t", now=now,
                         ip=None)
        conn.execute(insert(providers).values(id=new_id(), team_id=team_id, kind="bedrock",
                                              config={"region": "us-east-1", "credential_secret": "BEDROCK",
                                                      "credential_scope": "instance", "api_key": SECRET_VALUE},
                                              aliases={"lead": "us.anthropic.claude-opus-5-5"},
                                              created_at=now, updated_at=now))
        secret_store.set_secret(conn, keyring, team_id=None, name="BEDROCK", value=SECRET_VALUE, actor_id="t")
        connections.set_connection(conn, keyring, team_id=team_id, project_id=None, row=None,
                                   body={"kind": "linear", "name": "linear", "api_key": f"lin_api_{SECRET_VALUE}"},
                                   actor_id="t", now=now, ip=None)
    add_user(engine, keyring, Clock(now), "stephan", is_admin=True)
    return engine


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_export_writes_every_definition_and_no_secret(seeded, tmp_path):
    dest = tmp_path / "export"
    with seeded.connect() as conn:
        manifest = export_definitions(conn, dest)
    files = _tree(dest)
    assert set(files) == {
        "workflows/instance/review.yml", "workflows/team-mobile/review.yml", "workflows/team-other/private.yml",
        "roles/instance/security.md", "prompts/instance/_shared.md", "missions/project-mobile-android-tree/goal.md",
        "skills/instance/risk-rubric.md", "teams.json", "settings.json", "providers.json", "connections.json",
        "manifest.json"}
    assert files["workflows/instance/review.yml"].decode() == f"{STORED_WORKFLOW_HEADER}\n{WORKFLOW}"
    assert files["roles/instance/security.md"] == b"---\nname: security\n---\n\nBe careful.\n"
    assert json.loads(files["teams.json"])["teams"][0] == {
        "slug": "mobile", "name": "Mobile", "projects": [{"name": "android-tree", "repo": "fs-eng/android-tree",
                                                         "platform": "android",
                                                         "enabled_workflows": ["fix", "pr_review"],
                                                         "branch_filters": ["dev"], "environment_type": None}]}
    assert json.loads(files["settings.json"]) == {"instance": {"ops.validate": {"a": 1}},
                                                  "team-mobile": {"teams.mobile.auto_fix": True}}
    assert json.loads(files["providers.json"])["providers"] == [{
        "scope": "team-mobile", "kind": "bedrock", "aliases": {"lead": "us.anthropic.claude-opus-5-5"},
        "config": {"region": "us-east-1", "credential_secret": "BEDROCK", "credential_scope": "instance"}}]
    assert json.loads(files["connections.json"])["connections"] == [
        {"scope": "team-mobile", "kind": "linear", "name": "linear", "config": {}}]
    every = b"".join(files.values())
    assert SECRET_VALUE.encode() not in every and b"stephan" not in every and b"old body" not in every
    assert manifest["files"] == {path: hashlib.sha256(data).hexdigest()
                                 for path, data in files.items() if path != "manifest.json"}
    assert json.loads(files["manifest.json"]) == json.loads(json.dumps(manifest))
    review = next(o for o in manifest["objects"] if o["path"] == "workflows/instance/review.yml")
    assert review == {"path": "workflows/instance/review.yml", "kind": "workflow", "scope": "instance",
                      "name": "review", "version": 2}
    assert manifest["instance_id"] and manifest["exported_at"]


def test_team_export_keeps_the_instance_and_that_team(seeded, tmp_path):
    with seeded.connect() as conn:
        export_definitions(conn, tmp_path / "export", team="mobile")
        with pytest.raises(ValueError):
            export_definitions(conn, tmp_path / "none", team="missing")
    files = _tree(tmp_path / "export")
    assert "workflows/team-other/private.yml" not in files and "workflows/team-mobile/review.yml" in files
    assert [t["slug"] for t in json.loads(files["teams.json"])["teams"]] == ["mobile"]


def test_export_replaces_an_earlier_export_and_refuses_anything_else(seeded, tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    dest = root / "export"
    with seeded.connect() as conn:
        export_definitions(conn, dest)
        (dest / "roles" / "instance" / "stale.md").write_text("gone after the next export")
        export_definitions(conn, dest)
        assert not (dest / "roles" / "instance" / "stale.md").exists()
        assert sorted(p.name for p in root.iterdir()) == ["export"]
        unrelated = root / "home"
        unrelated.mkdir()
        (unrelated / "notes.txt").write_text("keep me")
        with pytest.raises(ExportRefused):
            export_definitions(conn, unrelated)
        assert (unrelated / "notes.txt").read_text() == "keep me"
        empty = root / "empty"
        empty.mkdir()
        export_definitions(conn, empty)
    assert (empty / "manifest.json").is_file()
    assert sorted(p.name for p in root.iterdir()) == ["empty", "export", "home"]


def test_export_command(seeded, tmp_path):
    config = tmp_path / "server.yaml"
    url = seeded.url.render_as_string(hide_password=False)
    config.write_text(f"database_url: {url}\nmaster_key: file:{tmp_path / 'key'}\n")
    result = CliRunner().invoke(app, ["server", "export", str(tmp_path / "out"), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "workflows" / "instance" / "review.yml").is_file()
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")
    refused = CliRunner().invoke(app, ["server", "export", str(tmp_path / "busy"), "--config", str(config)])
    assert refused.exit_code == 1 and "not an export" in refused.output


def _recorder(tmp_path: Path) -> list[str]:
    script = tmp_path / "record.py"
    script.write_text("import json, os, sys\n"
                      "with open(sys.argv[1], 'a') as out:\n"
                      "    out.write(json.dumps({'cwd': os.getcwd(), 'env': sorted(os.environ),\n"
                      "        'dir': os.environ.get('ARMATURE_EXPORT_DIR'), 'files': sorted(os.listdir())}) + '\\n')\n"
                      "fail = sys.argv[1] + '.fail'\n"
                      "sys.exit(int(open(fail).read()) if os.path.exists(fail) else 0)\n")
    return [sys.executable, str(script), str(tmp_path / "calls.jsonl")]


def _calls(tmp_path: Path) -> list[dict]:
    path = tmp_path / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _put(engine, key, value):
    with engine.begin() as conn:
        defs.put_setting(conn, team_id=None, key=key, value=value, actor_id="t", now=utcnow(), ip=None)


def test_hook_exports_after_a_commit_coalesces_and_runs_the_command(sqlite_engine, tmp_path):
    dest = tmp_path / "export"
    hook = ExportHook(sqlite_engine, ExportConfig(dir=str(dest), command=_recorder(tmp_path), debounce_seconds=1))
    hook.attach()
    try:
        _put(sqlite_engine, "a", 1)
        _put(sqlite_engine, "b", 2)
        with sqlite_engine.connect() as conn:
            conn.begin()
            defs.put_setting(conn, team_id=None, key="c", value=3, actor_id="t", now=utcnow(), ip=None)
            conn.rollback()
        assert hook.flush()
        calls = _calls(tmp_path)
        assert len(calls) == 1
        assert Path(calls[0]["cwd"]).resolve() == dest.resolve() and calls[0]["dir"] == str(dest)
        assert set(calls[0]["env"]) <= {"PATH", "HOME", "LANG", "ARMATURE_EXPORT_DIR", "__CF_USER_TEXT_ENCODING"}
        assert "manifest.json" in calls[0]["files"]
        assert json.loads((dest / "settings.json").read_text()) == {"instance": {"a": 1, "b": 2}}
        assert hook.status()["status"] == "ok" and hook.status()["last_run_at"] is not None
        with sqlite_engine.connect() as conn:
            rows = conn.execute(select(audit.c.detail).where(audit.c.action == "export.run")).scalars().all()
        assert rows == [{"status": "ok", "error": None, "summary": None, "returncode": 0, "command": True}]
        with sqlite_engine.begin() as conn:
            conn.execute(select(teams))
        assert hook.flush() and len(_calls(tmp_path)) == 1
    finally:
        hook.stop()
        hook.detach()


def test_hook_records_a_failing_or_hanging_command(sqlite_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_IN_SERVER_ENV", "x")
    (tmp_path / "calls.jsonl.fail").write_text("3")
    hook = ExportHook(sqlite_engine, ExportConfig(dir=str(tmp_path / "export"), command=_recorder(tmp_path)))
    assert hook.run_once() is False
    assert hook.status()["status"] == "failed" and hook.status()["error"].startswith("command exited 3")
    assert "SECRET_IN_SERVER_ENV" not in _calls(tmp_path)[0]["env"]

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    hook = ExportHook(sqlite_engine, ExportConfig(dir=str(tmp_path / "export"), command=["x"]), run=hang)
    assert hook.run_once() is False and "timed out" in hook.status()["error"]
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")
    hook = ExportHook(sqlite_engine, ExportConfig(dir=str(tmp_path / "busy")))
    assert hook.run_once() is False and "ExportRefused" in hook.status()["error"]

    def held(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 2, stdout='3 changed\n{"held": ["config/validate.json"]}\n',
                                           stderr="")

    hook = ExportHook(sqlite_engine, ExportConfig(dir=str(tmp_path / "export"), command=["x"]), run=held)
    assert hook.run_once() is True
    assert hook.status()["status"] == "warning" and hook.status()["error"] is None
    assert hook.status()["summary"] == '{"held": ["config/validate.json"]}'
    with sqlite_engine.connect() as conn:
        details = conn.execute(select(audit.c.detail).where(audit.c.action == "export.run")).scalars().all()
    assert [d["status"] for d in details] == ["failed", "failed", "failed", "warning"]
    assert details[-1]["summary"] == '{"held": ["config/validate.json"]}'


def test_stop_runs_an_export_still_due(sqlite_engine, tmp_path):
    hook = ExportHook(sqlite_engine, ExportConfig(dir=str(tmp_path / "export"), debounce_seconds=60))
    hook.attach()
    try:
        _put(sqlite_engine, "a", 1)
        assert hook.status()["pending"]
        hook.stop()
        assert (tmp_path / "export" / "manifest.json").is_file()
    finally:
        hook.detach()


def test_status_route_is_admin_only_and_follows_api_writes(sqlite_engine, keyring, tmp_path):
    clock = Clock(utcnow())
    dest = tmp_path / "export"
    client = make_client(sqlite_engine, keyring, clock,
                         export_on_change={"dir": str(dest), "command": _recorder(tmp_path), "debounce_seconds": 1})
    try:
        admin, _ = add_user(sqlite_engine, keyring, clock, "admin", is_admin=True)
        viewer, _ = add_user(sqlite_engine, keyring, clock, "viewer")
        sign_in_as(client, sqlite_engine, keyring, clock, viewer)
        assert client.get("/api/export/status").status_code == 403
        csrf = sign_in_as(client, sqlite_engine, keyring, clock, admin)
        assert client.get("/api/export/status").json()["status"] is None
        saved = client.post("/api/settings", json={"scope": "instance", "key": "ops.x", "value": 1},
                            headers=mutation(csrf))
        assert saved.status_code == 200, saved.text
        hook = client.app.state.export
        assert hook.flush()
        status = client.get("/api/export/status").json()
        assert status["status"] == "ok" and status["dir"] == str(dest)
        assert json.loads((dest / "settings.json").read_text()) == {"instance": {"ops.x": 1}}
    finally:
        client.app.state.export.stop()
        client.app.state.export.detach()


def test_status_route_without_config(sqlite_engine, keyring):
    clock = Clock(utcnow())
    client = make_client(sqlite_engine, keyring, clock)
    admin, _ = add_user(sqlite_engine, keyring, clock, "admin", is_admin=True)
    sign_in_as(client, sqlite_engine, keyring, clock, admin)
    assert client.get("/api/export/status").json() == {"configured": False}


def test_debounce_is_at_least_a_second():
    with pytest.raises(ValueError):
        ExportConfig(dir="x", debounce_seconds=0.5)


def test_a_failed_swap_puts_the_old_export_back(seeded, tmp_path, monkeypatch):
    dest = tmp_path / "out" / "export"
    with seeded.connect() as conn:
        export_definitions(conn, dest)
        before = _tree(dest)
        real = os.rename

        def failing(src, dst):
            if ".tmp-" in str(src):
                raise OSError("disk full")
            return real(src, dst)

        monkeypatch.setattr(os, "rename", failing)
        with pytest.raises(OSError):
            export_definitions(conn, dest)
    monkeypatch.undo()
    assert _tree(dest) == before
    assert sorted(p.name for p in dest.parent.iterdir()) == ["export"]


def test_leftovers_from_a_dead_export_are_cleared(seeded, tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    (root / "export.old-123").mkdir()
    (root / "export.tmp-999999").mkdir()
    (root / "export.tmp-notapid").mkdir()
    (root / "exports.old-1").mkdir()
    with seeded.connect() as conn:
        export_definitions(conn, root / "export")
    assert sorted(p.name for p in root.iterdir()) == ["export", "export.tmp-notapid", "exports.old-1"]


def test_the_server_exports_once_at_start(sqlite_engine, keyring, tmp_path):
    dest = tmp_path / "export"
    client = make_client(sqlite_engine, keyring, Clock(utcnow()),
                         export_on_change={"dir": str(dest), "debounce_seconds": 1})
    with client:
        assert client.app.state.export.flush()
        assert (dest / "manifest.json").is_file()


def test_a_renamed_workflow_leaves_no_old_file(seeded, tmp_path):
    dest = tmp_path / "export"
    with seeded.connect() as conn:
        export_definitions(conn, dest)
    assert (dest / "workflows" / "instance" / "review.yml").is_file()
    with seeded.begin() as conn:
        row = conn.execute(select(workflows).where(workflows.c.scope == "*", workflows.c.name == "review")).one()
        body = WORKFLOW.replace("name: review", "name: triage")
        defs.save(conn, defs.WORKFLOW, row, access=rbac.Access(is_admin=True), body=body, note=None,
                  base_version=row.current_version, actor_id="t", now=utcnow(), ip=None,
                  catalog={"demo/post": AdapterDef(cmd="post.sh")})
    with seeded.connect() as conn:
        export_definitions(conn, dest)
    assert not (dest / "workflows" / "instance" / "review.yml").exists()
    assert (dest / "workflows" / "instance" / "triage.yml").read_text().endswith(body)

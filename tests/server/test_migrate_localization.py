"""localization imported from ops: run by hand on an app's pull request, it translates with ops' instructions and,
in ops' default draft mode, commits without pushing."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select

from armature.server.db import new_id
from armature.server.db.schema import projects, teams, triggers, workflows
from armature.server.migrate_ops import MigrateError, import_ops, localization
from tests.server import ops_harness

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
GOLDENS = {path.stem: ops_harness.load(path) for path in ops_harness.fixtures("localization")}


@pytest.fixture
def fs(sqlite_engine):
    team = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="FamilySearch", slug="familysearch", created_at=NOW))
        for repo in (*localization.APPS, "fs-eng/mobile-api-nodejs"):
            conn.execute(insert(projects).values(id=new_id(), team_id=team, name=repo.split("/")[1], repo=repo,
                                                 created_at=NOW))
        homes = dict(conn.execute(select(projects.c.repo, projects.c.id)).all())
        for repo in localization.APPS:
            import_ops(conn, "localization", team_id=team, project_id=homes[repo], now=NOW)
        with pytest.raises(MigrateError, match="only the apps"):
            import_ops(conn, "localization", team_id=team, project_id=homes["fs-eng/mobile-api-nodejs"], now=NOW)
    return team, homes


def _run(engine, homes, golden, tmp_path, monkeypatch, mode="draft"):
    prepare = golden["ops"]["outputs"]["prepare"]
    repo = prepare["repo"]
    with engine.connect() as conn:
        workflow = conn.execute(select(workflows.c.id).where(workflows.c.project_id == homes[repo],
                                                             workflows.c.name == "localization")).scalar_one()
    written = [{"file": w["file"], "key": w["key"]} for t in golden["ops"]["outputs"]["translate"]
               for w in (t.get("written") or [])]
    agents = {"translate": {"written": written, "skipped": [], "commit": "c" * 40, "worktree": "/work/l10n"}}
    inputs = {"repo": f"https://github.com/{repo}.git", "slug": repo, "number": prepare["number"],
              "ref": prepare["branch"], "base": prepare["base_ref"], "mode": mode}
    return ops_harness.run_stored(engine, workflow, inputs, tmp_path, monkeypatch, agents)


def test_it_runs_only_by_hand_as_in_ops(sqlite_engine, fs):
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(triggers)).all() == []


@pytest.mark.parametrize("name", ["android-new-strings", "ios-reminder-strings"])
def test_a_draft_translates_with_ops_instructions_and_pushes_nothing(sqlite_engine, fs, tmp_path, monkeypatch,
                                                                       name):
    golden = GOLDENS[name]
    assert golden["ops"]["outputs"]["publish"]["mode"] == "draft"
    replay = _run(sqlite_engine, fs[1], golden, tmp_path, monkeypatch)
    [session] = replay.sessions
    assert session["access"] == "write" and session["model"] == "translate"
    ops_prompt = next(s for s in golden["ops"]["sessions"] if s["kind"] == "claude")["args"][1]
    rules = localization.TRANSLATE[localization.TRANSLATE.index("Keep exactly:"):
                                   localization.TRANSLATE.index("When you are done")]
    assert rules in session["prompt"] and rules in ops_prompt
    assert replay.calls == []


def test_preview_branch_pushes_the_commit_to_ops_branch(sqlite_engine, fs, tmp_path, monkeypatch):
    golden = GOLDENS["android-new-strings"]
    monkeypatch.setattr(ops_harness, "_tool", lambda vendor, call, out: {"pushed": True})
    replay = _run(sqlite_engine, fs[1], golden, tmp_path, monkeypatch, mode="preview_branch")
    [push] = replay.calls
    assert push["tool"] == "github.push" and push["params"]["branch"] == "armature/l10n/pr-6214"


def test_todo_nothing_to_translate_still_runs_one_session_until_detect_is_a_pack_step(sqlite_engine, fs, tmp_path,
                                                                                       monkeypatch):
    golden = GOLDENS["nothing-to-translate"]
    assert not [s for s in golden["ops"]["sessions"] if s["kind"] == "claude"]
    assert len(_run(sqlite_engine, fs[1], golden, tmp_path, monkeypatch).sessions) == 1

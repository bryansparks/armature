"""repo-review imported from ops: a disabled daily schedule per project, the same specialist panel as ops' roles.json,
and a team lead and digest that read what ops' did."""
from __future__ import annotations

import ast
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select

from armature.server.db import new_id
from armature.server.db.schema import projects, teams, triggers, workflows
from armature.server.migrate_ops import import_ops, repo_review
from tests.server import ops_harness

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
GOLDENS = {path.stem: ops_harness.load(path) for path in ops_harness.fixtures("repo-review")}


@pytest.fixture
def fs(sqlite_engine):
    team = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="FamilySearch", slug="familysearch", created_at=NOW))
        for repo in repo_review.PLATFORMS:
            project = new_id()
            conn.execute(insert(projects).values(id=project, team_id=team, name=repo.split("/")[1], repo=repo,
                                                 created_at=NOW))
            import_ops(conn, "repo-review", team_id=team, project_id=project, now=NOW)
        return {repo: (project, workflow) for repo, project, workflow in conn.execute(
            select(projects.c.repo, projects.c.id, workflows.c.id).join(workflows,
                                                                        workflows.c.project_id == projects.c.id))}


def _ran(golden: dict) -> list[str]:
    return sorted(r["role"] for r in golden["ops"]["outputs"].get("reviews", []) if r["repo"].endswith("android-tree"))


def test_each_project_gets_a_disabled_daily_schedule_at_ops_time(sqlite_engine, fs):
    with sqlite_engine.connect() as conn:
        rows = conn.execute(select(triggers)).all()
    assert len(rows) == 3 and {r.kind for r in rows} == {"schedule"} and not any(r.enabled for r in rows)
    assert {r.config["cron"] for r in rows} == {"30 6 * * *"}
    android = next(r for r in rows if r.config["project_id"] == fs["fs-eng/android-tree"][0])
    assert android.config["inputs"] == {"repo": "https://github.com/fs-eng/android-tree.git", "ref": "dev"}


def test_the_panel_is_ops_roles_json_panel_per_platform():
    assert sorted(repo_review.panel("fs-eng/android-tree")) == _ran(GOLDENS["one-repo-changed"])
    assert "beauty" not in repo_review.panel("fs-eng/mobile-api-nodejs")


def test_a_recorded_day_feeds_the_lead_and_digest_what_ops_did(sqlite_engine, fs, tmp_path, monkeypatch):
    golden = GOLDENS["one-repo-changed"]
    reviews = {r["role"]: r["review"] for r in golden["ops"]["outputs"]["reviews"]}
    agents = {role: {"content": text} for role, text in reviews.items()}
    agents.update(team_lead=golden["stubs"]["team_lead"], digest=golden["stubs"]["digest"])
    workflow = fs["fs-eng/android-tree"][1]
    replay = ops_harness.run_stored(sqlite_engine, workflow, {"repo": "https://github.com/fs-eng/android-tree.git",
                                                              "ref": "dev"}, tmp_path, monkeypatch, agents)
    sessions = {s["stage"]: s for s in replay.sessions}
    assert sorted(set(sessions) - {"team_lead", "digest"}) == _ran(golden)
    assert all(sessions[r]["role"] == r and sessions[r]["access"] == "read" for r in reviews)
    assert sessions["team_lead"]["context_files"] == reviews
    assert ast.literal_eval(sessions["digest"]["context_files"]["team_lead"]) == golden["stubs"]["team_lead"]
    ops_lead = next(s for s in golden["ops"]["sessions"] if s.get("stage") == "team_lead")["prompt"]
    assert ops_lead.split("Reviews:", 1)[0].strip() in sessions["team_lead"]["prompt"]
    assert replay.calls == [] and replay.effects == []


def test_todo_a_quiet_day_still_runs_the_panel_until_marks_exist(sqlite_engine, fs, tmp_path, monkeypatch):
    golden = GOLDENS["nothing-landed"]
    assert not [s for s in golden["ops"]["sessions"] if s["kind"] == "claude"]
    agents = {role: {"content": "No commits landed in the last 24 hours."}
              for role in repo_review.panel("fs-eng/android-tree")}
    agents.update(team_lead=golden["stubs"]["team_lead"], digest=golden["stubs"]["digest"])
    replay = ops_harness.run_stored(sqlite_engine, fs["fs-eng/android-tree"][1],
                                    {"repo": "https://github.com/fs-eng/android-tree.git", "ref": "dev"}, tmp_path,
                                    monkeypatch, agents)
    assert len(replay.sessions) == len(repo_review.panel("fs-eng/android-tree")) + 2

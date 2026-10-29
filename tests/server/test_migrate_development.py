"""development imported from ops: each project polls ops' JQL for its own components, asks before it starts, and an
already-fixed issue ends with no branch and no pull request, as it did in ops."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, update

from armature.server.db import new_id
from armature.server.db.schema import projects, teams, triggers, workflows
from armature.server.migrate_ops import development, import_ops
from tests.server import ops_harness
from tests.server.test_migrate_ops import jira_connection

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
GOLDENS = {path.stem: ops_harness.load(path) for path in ops_harness.fixtures("development")}
OPS_JQL = "assignee = currentUser() AND sprint in openSprints() AND statusCategory != Done ORDER BY created ASC"


@pytest.fixture
def fs(sqlite_engine, keyring):
    team = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="FamilySearch", slug="familysearch", created_at=NOW))
        for repo in development.COMPONENTS:
            conn.execute(insert(projects).values(id=new_id(), team_id=team, name=repo.split("/")[1], repo=repo,
                                                 created_at=NOW))
    connection = jira_connection(sqlite_engine, keyring, team)
    with sqlite_engine.begin() as conn:
        homes = dict(conn.execute(select(projects.c.repo, projects.c.id)).all())
        for home in homes.values():
            import_ops(conn, "development", team_id=team, project_id=home, now=NOW)
    return team, homes, connection


def _polls(engine):
    with engine.connect() as conn:
        return {r.config["project_id"]: r for r in conn.execute(select(triggers).where(triggers.c.kind == "jira_poll"))}


def test_each_project_polls_ops_jql_for_its_components_disabled_and_in_shadow(sqlite_engine, fs):
    _, homes, connection = fs
    polls = _polls(sqlite_engine)
    android = polls[homes["fs-eng/android-tree"]]
    assert android.config["jql"] == OPS_JQL.replace(" ORDER BY", ' AND component in ("Android") ORDER BY')
    assert android.config["connection_id"] == connection and android.config["shadow"] is True
    assert android.config["inputs"] == {"issue": "$.key", "slug": "fs-eng/android-tree",
                                        "repo": "https://github.com/fs-eng/android-tree.git", "ref": "dev"}
    assert not any(p.enabled for p in polls.values()) and len(polls) == 3


def test_a_reimport_keeps_a_poll_taken_out_of_shadow(sqlite_engine, fs):
    team, homes, _ = fs
    home = homes["fs-eng/mobile-ios"]
    poll = _polls(sqlite_engine)[home]
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == poll.id).values(config={**poll.config, "shadow": False},
                                                                             enabled=True))
        changes, _ = import_ops(conn, "development", team_id=team, project_id=home, now=NOW)
    assert [c.action for c in changes if c.what == "trigger"] == ["unchanged"]
    assert _polls(sqlite_engine)[home].config["shadow"] is False


def test_the_component_ops_routed_by_picks_the_project_and_a_componentless_issue_is_todo():
    android = GOLDENS["android-already-fixed"]["ops"]["outputs"]["prepare"]["components"]
    assert [repo for repo, names in development.COMPONENTS.items() if set(android) & set(names)] == \
        ["fs-eng/android-tree"]
    # TODO: ops asked a model to pick a platform for an issue with no component. No project's poll matches one.
    assert GOLDENS["platform-unclear"]["ops"]["outputs"]["prepare"]["components"] == []


def _workflow(engine, homes, repo):
    with engine.connect() as conn:
        return conn.execute(select(workflows.c.id).where(workflows.c.project_id == homes[repo],
                                                         workflows.c.name == "development")).scalar_one()


def _tools(monkeypatch, issue, level="low"):
    asked = []

    def answer(vendor, call, out):
        asked.append(call["tool"])
        return {"jira.get-issue": issue, "github.push": {"pushed": True, "commit": "c" * 40},
                "review.risk": {"level": level, "head": "c" * 40},
                "github.open-pr": {"number": 9231},
                "review.marked": {"valid": True, "head": "c" * 40}}[call["tool"]]

    monkeypatch.setattr(ops_harness, "_tool", answer)
    return asked


def _issue(golden):
    prepare = golden["ops"]["outputs"]["prepare"]
    return {"issue": prepare["issue"], "found": True, "summary": prepare["summary"],
            "description": prepare["description"], "components": prepare["components"]}


INPUTS = {"repo": "https://github.com/fs-eng/android-tree.git", "slug": "fs-eng/android-tree", "ref": "dev"}


def test_an_already_fixed_issue_asks_first_reads_the_issue_and_opens_nothing(sqlite_engine, fs, tmp_path,
                                                                              monkeypatch):
    _, homes, _ = fs
    golden = GOLDENS["android-already-fixed"]
    ops_implement = golden["ops"]["outputs"]["implement"]
    agents = {"implement": {k: ops_implement[k] for k in ("outcome", "summary", "verification", "pr_title",
                                                         "pr_body", "follow_ups")}}
    asked = _tools(monkeypatch, _issue(golden))
    replay = ops_harness.run_stored(sqlite_engine, _workflow(sqlite_engine, homes, "fs-eng/android-tree"),
                                    {"issue": "MEMTEMDIS-4630", **INPUTS}, tmp_path, monkeypatch, agents)
    assert replay.gates == ["approve"] and [s["stage"] for s in replay.sessions] == ["implement"]
    [session] = replay.sessions
    assert session["access"] == "write" and session["context_files"]["description"] == _issue(golden)["description"]
    assert asked == ["jira.get-issue"] and golden["ops"]["outputs"]["publish"]["pr_url"] == ""
    ops_prompt = [s for s in golden["ops"]["sessions"] if s["kind"] == "claude"][-1]["args"][1]
    first = development.WORK.split("\n\n", 1)[0].replace("approved this run in Slack", "approved this run")
    assert first in session["prompt"] and development.WORK.split("\n\n", 1)[0] in ops_prompt


IMPLEMENTED = {"outcome": "implemented", "summary": "Cleared the cache on sign-out.", "verification": "tests pass",
               "pr_title": "[MEMTEMDIS-4512] Clear the memories cache on sign-out", "pr_body": "Clears it.",
               "follow_ups": [], "commit": "c" * 40, "worktree": "/work/tree"}


def test_the_gates_pause_the_job_itself_and_high_risk_holds_the_pull_request(sqlite_engine, fs, tmp_path,
                                                                              monkeypatch):
    _, homes, _ = fs
    workflow = _workflow(sqlite_engine, homes, "fs-eng/android-tree")
    paused = ops_harness.run_stored(sqlite_engine, workflow, {"issue": "MEMTEMDIS-4512", **INPUTS}, tmp_path / "a",
                                    monkeypatch, {}, pause=True)
    assert paused.paused == "approve" and paused.sessions == []
    asked = _tools(monkeypatch, _issue(GOLDENS["android-already-fixed"]), level="high")
    agents = {"implement": IMPLEMENTED, "rate": {"level": "high", "reasons": ["auth"], "commit": "c" * 40},
              "gate:approval": False}
    held = ops_harness.run_stored(sqlite_engine, workflow, {"issue": "MEMTEMDIS-4512", **INPUTS}, tmp_path / "b",
                                  monkeypatch, agents)
    assert held.gates == ["approve", "approval"] and asked == ["jira.get-issue", "github.push", "review.risk"]
    assert next(s for s in held.sessions if s["stage"] == "rate")["access"] == "read"


def test_a_low_risk_branch_opens_its_pull_request_and_reviews_it(sqlite_engine, fs, tmp_path, monkeypatch):
    _, homes, _ = fs
    asked = _tools(monkeypatch, _issue(GOLDENS["android-already-fixed"]))
    agents = {"implement": IMPLEMENTED, "rate": {"level": "low", "reasons": [], "commit": "c" * 40}}
    replay = ops_harness.run_stored(sqlite_engine, _workflow(sqlite_engine, homes, "fs-eng/android-tree"),
                                    {"issue": "MEMTEMDIS-4512", **INPUTS}, tmp_path, monkeypatch, agents)
    assert replay.gates == ["approve"] and asked[:4] == ["jira.get-issue", "github.push", "review.risk",
                                                          "github.open-pr"]


def test_a_declined_start_does_nothing(sqlite_engine, fs, tmp_path, monkeypatch):
    _, homes, _ = fs
    replay = ops_harness.run_stored(sqlite_engine, _workflow(sqlite_engine, homes, "fs-eng/mobile-ios"),
                                    {"issue": "MEMTEMDIS-4700", "repo": "https://github.com/fs-eng/mobile-ios.git",
                                     "slug": "fs-eng/mobile-ios", "ref": "master"}, tmp_path, monkeypatch,
                                    {"gate:approve": False})
    assert replay.sessions == [] and replay.calls == []

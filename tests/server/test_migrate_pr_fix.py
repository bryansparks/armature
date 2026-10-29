"""pr-fix imported from ops: a /armature fix reply queues S14b's auto_fix review once per pull request, and what it
can't do yet is spelled out against the recorded goldens."""
from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from ruamel.yaml import YAML
from sqlalchemy import insert, select

from armature.server.db import new_id
from armature.server.db.schema import projects, teams, workflows
from armature.server.migrate_ops import import_ops, pr_fix
from tests.server import ops_harness

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
REPOS = ("fs-eng/mobile-ios", "fs-eng/android-tree", "fs-eng/mobile-api-nodejs")
GOLDENS = {path.stem: ops_harness.load(path) for path in ops_harness.fixtures("pr-fix")}


@pytest.fixture
def fs(sqlite_engine):
    team = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="FamilySearch", slug="familysearch", created_at=NOW))
        for repo in REPOS:
            project = new_id()
            conn.execute(insert(projects).values(id=project, team_id=team, name=repo.split("/")[1], repo=repo,
                                                 created_at=NOW))
            import_ops(conn, "pr-fix", team_id=team, project_id=project, now=NOW)
        homes = dict(conn.execute(select(projects.c.repo, projects.c.id)).all())
    return team, homes


def _job(golden):
    return next(v for k, v in golden["files"].items() if "queue/job" in k)


def _reply(golden, event="pull_request_review_comment"):
    """ops' fix request as a reply in a review thread, the one form the rule takes today."""
    job = _job(golden)
    payload = {"action": "created", "comment": {"id": job["comment_id"], "body": "/armature fix please",
                                                "user": {"login": job["requester"]}},
               "pull_request": {"number": job["number"], "head": {"sha": "b" * 40, "ref": "MEMTEMDIS-4588-fix"},
                                "base": {"ref": "master"}},
               "repository": {"full_name": job["repo"], "clone_url": f"https://github.com/{job['repo']}.git"},
               "sender": {"login": job["requester"], "type": "User"}}
    return {"source": "github", "headers": {"x-github-event": event}, "payload": payload}


def test_the_fix_is_reviewed_on_a_new_branch_before_the_pull_request_moves(sqlite_engine, fs):
    data = YAML(typ="safe").load(pr_fix.body())
    ids = [s["id"] for s in data["stages"]]
    assert ids == ["permission", "refused", "first", "pushable", "fix", "preview", "check", "push", "fixed", "second"]
    stages = {s["id"]: s for s in data["stages"]}
    assert "number" not in stages["preview"]["with"] and stages["preview"]["with"]["branch"].startswith("armature/fix-")
    assert stages["check"]["call"] == {"workflow": "review-branch"} and stages["push"]["with"]["number"]
    branch = YAML(typ="safe").load(pr_fix.review_branch())
    assert {"marked", "decisions", "post", "mark"}.isdisjoint(s["id"] for s in branch["stages"])
    risk = next(s for s in branch["stages"] if s["id"] == "risk")
    assert "number" not in risk["with"] and risk["with"]["branch"] == "{{ ref }}"
    with sqlite_engine.connect() as conn:
        names = sorted(r.name for r in conn.execute(select(workflows).where(workflows.c.project_id == fs[1][REPOS[0]])))
    assert names == ["pr-fix", "review-branch", "review-pass"]


def test_a_fix_request_queues_one_fix_in_its_project_one_at_a_time(sqlite_engine, fs):
    team, homes = fs
    [decision] = ops_harness._decisions(sqlite_engine, team, _reply(GOLDENS["pushed-after-review"]))
    assert decision["project_id"] == homes["fs-eng/mobile-ios"] and decision["concurrency"] == "queue"
    assert decision["key"] == "fs-eng/mobile-ios#9214"
    assert decision["inputs"]["ref"] == "b" * 40 and decision["inputs"]["branch"] == "MEMTEMDIS-4588-fix"


@pytest.mark.parametrize("change", [{"comment": {"body": "looks good"}}, {"action": "edited"},
                                    {"sender": {"login": "armature-bot[bot]", "type": "Bot"}}],
                         ids=["no-command", "edit", "bot"])
def test_other_comments_match_no_rule(sqlite_engine, fs, change):
    delivery = _reply(GOLDENS["pushed-after-review"])
    delivery["payload"].update(copy.deepcopy(change))
    assert ops_harness._decisions(sqlite_engine, fs[0], delivery) == []


def test_todo_the_pull_request_comment_form_ops_recorded_has_no_rule_yet(sqlite_engine, fs):
    golden = GOLDENS["pushed-after-review"]
    assert _job(golden)["scope"] == "pr" and golden["ops"]["outputs"]["push"]["outcome"] == "pushed"
    delivery = _reply(golden, event="issue_comment")
    assert ops_harness._decisions(sqlite_engine, fs[0], delivery) == []


def _fix(engine, fs, golden, tmp_path, monkeypatch, level):
    team = fs[0]
    [decision] = ops_harness._decisions(engine, team, _reply(golden))
    asked = []

    def answer(vendor, call, out):
        asked.append((call["tool"], call["params"]))
        if call["tool"] == "github.permission":
            return {"permission": level, "allowed": level in ("write", "admin")}
        return {"comment_id": 1}

    monkeypatch.setattr(ops_harness, "_tool", answer)
    with engine.connect() as conn:
        workflow = conn.execute(select(workflows.c.id).where(workflows.c.project_id == decision["project_id"],
                                                             workflows.c.name == "pr-fix")).scalar_one()
    replay = ops_harness.run_stored(engine, workflow, decision["inputs"], tmp_path, monkeypatch, {})
    return asked, replay


def test_a_requester_without_write_access_is_refused_with_a_reply_as_ops_declined(sqlite_engine, fs, tmp_path,
                                                                                    monkeypatch):
    golden = GOLDENS["no-write-access"]
    assert golden["ops"]["outputs"]["push"]["outcome"] == "declined"
    asked, replay = _fix(sqlite_engine, fs, golden, tmp_path, monkeypatch, "read")
    assert asked[0] == ("github.permission", {"repo": "fs-eng/android-tree", "user": "mrasmussen-contractor"})
    assert [t for t, _ in asked] == ["github.permission", "github.pr-comment"] and replay.sessions == []
    assert "write access" in asked[1][1]["body"]


FIX = {"fixed": [{"claim": "cache leaks", "path": "a.swift", "reason": "cleared"}], "built": True,
       "commit": "d" * 40, "worktree": "/work/fix"}
COMMENTS = [{"path": "a.swift", "line": 3, "severity": "high", "blocking": True, "body": "It leaks.",
             "claim": "cache leaks"}]


def _flow(engine, fs, tmp_path, monkeypatch, verdicts, moved=False):
    team = fs[0]
    [decision] = ops_harness._decisions(engine, team, _reply(GOLDENS["pushed-after-review"]))
    pushes = []

    def answer(vendor, call, out):
        tool, params = call["tool"], call["params"]
        if tool == "github.push":
            pushes.append(params)
            return {"pushed": True, "commit": params["commit"], "branch": params["branch"]}
        return {"github.permission": {"allowed": True, "permission": "write"},
                "review.marked": {"valid": False, "head": "b" * 40},
                "review.risk": {"level": "low", "panel": ["correctness"],
                                "head": "e" * 40 if moved and params.get("branch") else params.get("expect_head")},
                "review.pushable": {"pushable": True}}.get(tool, {"posted": True})

    monkeypatch.setattr(ops_harness, "_tool", answer)
    leads = [{"verdict": v, "summary": "s", "decisions": [], "comments": [] if v == "approve" else COMMENTS}
             for v in verdicts]
    agents = {"rate": {"level": "low", "reasons": [], "commit": "d" * 40}, "correctness": {"findings": []},
              "team_lead": leads, "fix": FIX}
    with engine.connect() as conn:
        workflow = conn.execute(select(workflows.c.id).where(workflows.c.project_id == decision["project_id"],
                                                             workflows.c.name == "pr-fix")).scalar_one()
    ops_harness.run_stored(engine, workflow, decision["inputs"], tmp_path, monkeypatch, agents)
    return pushes


def test_an_unapproved_fix_never_moves_the_pull_request_head(sqlite_engine, fs, tmp_path, monkeypatch):
    golden = GOLDENS["review-not-approved"]
    assert golden["ops"]["outputs"]["push"]["outcome"] == "review-blocked"
    pushes = _flow(sqlite_engine, fs, tmp_path, monkeypatch, ["comment", "comment"])
    assert [p.get("number") for p in pushes] == [None]
    assert pushes[0]["branch"] == f"armature/fix-9214-{'d' * 40}" and pushes[0]["commit"] == "d" * 40


def test_a_fix_whose_branch_moved_before_the_push_never_reaches_the_pull_request(sqlite_engine, fs, tmp_path,
                                                                                  monkeypatch):
    pushes = _flow(sqlite_engine, fs, tmp_path, monkeypatch, ["comment", "approve"], moved=True)
    assert [p.get("number") for p in pushes] == [None]


def test_an_approved_fix_pushes_the_reviewed_commit_to_the_pull_request(sqlite_engine, fs, tmp_path, monkeypatch):
    assert GOLDENS["pushed-after-review"]["ops"]["outputs"]["push"]["outcome"] == "pushed"
    pushes = _flow(sqlite_engine, fs, tmp_path, monkeypatch, ["comment", "approve", "approve"])
    assert [(p.get("number"), p["commit"]) for p in pushes] == [(None, "d" * 40), (9214, "d" * 40)]
    assert pushes[1]["branch"] == "MEMTEMDIS-4588-fix"

"""ios-port imported from ops: the android-tree copy takes the iOS pull requests ops ported, and a port that already
matches pushes nothing, as in ops."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ruamel.yaml import YAML
from sqlalchemy import insert, select

from armature.server.db import new_id
from armature.server.db.schema import projects, teams, workflows
from armature.server.migrate_ops import MigrateError, import_ops, ios_port
from tests.server import ops_harness

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
GOLDENS = {path.stem: ops_harness.load(path) for path in ops_harness.fixtures("ios-port")}


@pytest.fixture
def fs(sqlite_engine):
    team = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="FamilySearch", slug="familysearch", created_at=NOW))
        for repo in (ios_port.SOURCE, ios_port.TARGET):
            conn.execute(insert(projects).values(id=new_id(), team_id=team, name=repo.split("/")[1], repo=repo,
                                                 created_at=NOW))
        homes = dict(conn.execute(select(projects.c.repo, projects.c.id)).all())
        import_ops(conn, "ios-port", team_id=team, project_id=homes[ios_port.TARGET], now=NOW)
        with pytest.raises(MigrateError, match="only into that project"):
            import_ops(conn, "ios-port", team_id=team, project_id=homes[ios_port.SOURCE], now=NOW)
    return team, homes


def _delivery(golden, **pr):
    source = golden["ops"]["outputs"]["prepare"].get("source") or {}
    number = source.get("number") or 8862
    login = pr.pop("login", "RyanPlumb")
    payload = {"action": pr.pop("action", "opened"), "number": number,
               "pull_request": {"number": number, "draft": False, "title": "Tighten the reservation card",
                                "user": {"login": login}, **pr},
               "repository": {"full_name": ios_port.SOURCE}, "sender": {"login": login, "type": "User"}}
    return {"source": "github", "headers": {"x-github-event": "pull_request"}, "payload": payload}


def test_a_listed_designers_pull_request_ports_into_android_tree(sqlite_engine, fs):
    team, homes = fs
    [decision] = ops_harness._decisions(sqlite_engine, team, _delivery(GOLDENS["ported-draft-pr"]))
    assert decision["project_id"] == homes[ios_port.TARGET] and decision["key"] == "fs-eng/mobile-ios#8841"
    assert decision["inputs"] == {"source_number": 8841, "repo": "https://github.com/fs-eng/android-tree.git",
                                  "slug": ios_port.TARGET, "ref": "dev"}


@pytest.mark.parametrize("change", [{"login": "kjensen-fs"}, {"draft": True}, {"action": "synchronize"}],
                         ids=["other-author", "draft", "push"])
def test_what_ops_did_not_port_matches_no_rule(sqlite_engine, fs, change):
    golden = GOLDENS["author-not-ported"]
    assert golden["ops"]["outputs"]["publish"]["outcome"] == "declined"
    assert ops_harness._decisions(sqlite_engine, fs[0], _delivery(golden, **change)) == []


def test_todo_a_merged_pull_request_ops_ported_matches_no_rule_yet(sqlite_engine, fs):
    golden = GOLDENS["already-matches"]
    assert ops_harness._decisions(sqlite_engine, fs[0], _delivery(golden, action="closed", merged=True)) == []


PATCH = "@@ -12,7 +12,7 @@ struct ReservationCardView: View {\n-        .padding(16)\n+        .padding(12)"


def test_the_port_reads_the_ios_diff_as_data_and_pushes_nothing_when_android_matches(sqlite_engine, fs, tmp_path,
                                                                                       monkeypatch):
    golden = GOLDENS["already-matches"]
    ops_implement = golden["ops"]["outputs"]["implement"]
    agents = {"implement": {k: ops_implement.get(k, "") for k in ("outcome", "summary", "verification", "pr_title",
                                                                 "pr_body")}}
    asked = []

    def answer(vendor, call, out):
        asked.append((call["tool"], call["params"]))
        return {"title": "Ordinance pill accent", "head": "f" * 40, "truncated": False,
                "files": [{"filename": "shared/TempleUI/ReservationCardView.swift", "status": "modified",
                           "additions": 1, "deletions": 1, "patch": PATCH}]}

    monkeypatch.setattr(ops_harness, "_tool", answer)
    with sqlite_engine.connect() as conn:
        workflow = conn.execute(select(workflows.c.id).where(workflows.c.name == "ios-port")).scalar_one()
    inputs = {"source_number": 8857, "slug": ios_port.TARGET, "repo": "https://github.com/fs-eng/android-tree.git",
              "ref": "dev"}
    replay = ops_harness.run_stored(sqlite_engine, workflow, inputs, tmp_path, monkeypatch, agents)
    assert asked == [("github.pr-diff", {"repo": ios_port.SOURCE, "number": 8857})]
    [session] = replay.sessions
    assert session["access"] == "write" and session["context_files"]["title"] == "Ordinance pill accent"
    assert ".padding(12)" in session["context_files"]["diff"] and ".padding(12)" not in session["prompt"]
    assert golden["ops"]["outputs"]["publish"]["outcome"] == "already_fixed" and replay.gates == []
    ops_prompt = next(s for s in golden["ops"]["sessions"] if s["kind"] == "claude")["args"][1]
    first = ios_port.PORT.split("\n\n", 1)[0]
    assert first in session["prompt"] and first in ops_prompt


def test_a_high_risk_port_waits_at_the_gate_in_its_own_job():
    data = YAML(typ="safe").load(ios_port.body())
    ids = [stage["id"] for stage in data["stages"]]
    assert ids == ["source", "implement", "push", "rate", "risk", "approval", "open", "review"]
    gate = data["stages"][ids.index("approval")]
    assert gate["gate"] == "human" and [s["id"] for s in data["stages"] if "call" in s] == ["review"]

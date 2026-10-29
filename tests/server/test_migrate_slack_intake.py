"""slack-intake imported from ops: its conversation rule takes what ops' gateway took, and a recorded message makes
the Jira comment and thread reply ops made."""
from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert

from armature.server import defs
from armature.server.db import new_id
from armature.server.db.schema import projects, teams
from armature.server.migrate_ops import import_ops
from tests.server import ops_harness

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
GOLDENS = {path.stem: ops_harness.load(path) for path in ops_harness.fixtures("slack-intake")}


@pytest.fixture
def fs(sqlite_engine):
    team, project = new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="FamilySearch", slug="familysearch", created_at=NOW))
        conn.execute(insert(projects).values(id=project, team_id=team, name="mobile-ios", repo="fs-eng/mobile-ios",
                                             created_at=NOW))
        for key, value in (("slack.intake_channel", "C07MOBINTAKE"), ("jira.project", "MEMTEMDIS"),
                           ("jira.site_url", "https://familysearch.atlassian.net")):
            defs.put_setting(conn, team_id=team, key=key, value=value, actor_id=None, now=NOW, ip=None)
        import_ops(conn, "slack-intake", team_id=team, project_id=project, now=NOW)
    return team


def _replay(engine, team, name, tmp_path, monkeypatch):
    golden = GOLDENS[name]
    return golden, ops_harness.replay(engine, team, golden, tmp_path, monkeypatch,
                                      agents={"classify": golden["stubs"]["classify"]})


def test_a_comment_makes_ops_jira_comment_and_thread_reply(sqlite_engine, fs, tmp_path, monkeypatch):
    golden, replay = _replay(sqlite_engine, fs, "comment-on-existing", tmp_path, monkeypatch)
    [jira, slack] = golden["ops"]["effects"]
    assert replay.effects == ops_harness.expected_effects({"ops": {"effects": [jira]}})
    assert replay.replies == [slack["text"]] and slack["thread_ts"] == golden["payload"]["event"]["ts"]


@pytest.mark.parametrize("name", ["ignore-question", "low-confidence-create"])
def test_what_ops_left_alone_stays_alone(sqlite_engine, fs, tmp_path, monkeypatch, name):
    golden, replay = _replay(sqlite_engine, fs, name, tmp_path, monkeypatch)
    assert golden["ops"]["effects"] == [] and replay.effects == [] and replay.replies == []
    assert [s["stage"] for s in replay.sessions] == ["classify"]


def test_a_create_makes_ops_issue_and_replies_with_its_link(sqlite_engine, fs, tmp_path, monkeypatch):
    golden, replay = _replay(sqlite_engine, fs, "create-defect", tmp_path, monkeypatch)
    [jira, slack] = golden["ops"]["effects"]
    assert replay.effects == ops_harness.expected_effects({"ops": {"effects": [jira]}})
    # The fake Jira names its new issue MOB-99. Ops' fake named it MEMTEMDIS-4801.
    assert slack["text"].endswith("/browse/MEMTEMDIS-4801") and replay.replies == [f"{ops_harness.SITE}/browse/MOB-99"]


def test_classify_gets_ops_instructions_and_never_the_message_itself(sqlite_engine, fs, tmp_path, monkeypatch):
    golden, replay = _replay(sqlite_engine, fs, "comment-on-existing", tmp_path, monkeypatch)
    ops_prompt = golden["ops"]["sessions"][0]["prompt"]
    [session] = replay.sessions
    assert ops_prompt.split("Message from", 1)[0].strip() in session["prompt"]
    assert golden["payload"]["event"]["text"] not in session["prompt"]


@pytest.mark.parametrize("change", [{"bot_id": "B07ARMATURE"}, {"subtype": "message_changed"},
                                    {"channel": "C01GENERAL"}], ids=["bot", "edit", "other-channel"])
def test_what_ops_gateway_dropped_matches_no_rule(sqlite_engine, fs, change):
    delivery = copy.deepcopy(GOLDENS["comment-on-existing"])
    delivery["payload"]["event"].update(change)
    assert ops_harness._decisions(sqlite_engine, fs, delivery) == []

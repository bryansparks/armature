"""The database-only review tools a pack calls after making its own GitHub calls, through the broker."""
from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from armature.server import connections
from armature.server.db import new_id
from armature.server.db.schema import review_marks, runs, settings
from armature.server.tools import NONE, marks, registry
from armature.server.vendors import github as github_vendor
from tests.server.tool_support import (  # noqa: F401
    HEAD,
    NUMBER,
    REPO,
    app,
    calls,
    clock,
    dns,
    pem,
    start_run,
    tool,
    vendor,
    world,
)

NEW = "b" * 40
PR = {"repo": REPO, "number": NUMBER}


@pytest.fixture
def run(sqlite_engine, keyring, clock, world):
    return start_run(sqlite_engine, keyring, clock, world, risk=None)


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _risk(app, run, level="low", head=HEAD, **params):
    params.setdefault("total", len(set(params.get("paths") or [])))
    return _ok(tool(app, run, "record_risk", "review.record-risk", {**PR, "head": head, "level": level, **params}))


def _post(app, run, head=HEAD, posted=True, approved=True, **params):
    return _ok(tool(app, run, "record_post", "review.record-post",
                    {**PR, "head": head, "posted": posted, "approved": approved, **params}))


def _mark(app, run, head=HEAD, status_id=900):
    return _ok(tool(app, run, "record_mark", "review.record-mark", {**PR, "head": head, "status_id": status_id}))


def _is_marked(app, run, head=HEAD):
    return _ok(tool(app, run, "is_marked", "review.is-marked", {**PR, "head": head}))


def _row(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(runs).where(runs.c.id == run_id)).first()


def _marks(engine):
    with engine.connect() as conn:
        return conn.execute(select(review_marks)).all()


@pytest.fixture
def boom(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a database-only tool touched a connection")
    monkeypatch.setattr(connections, "resolve", refuse)
    monkeypatch.setattr(connections, "credentials", refuse)
    monkeypatch.setattr(github_vendor, "mint", refuse)


def test_the_database_only_tools_resolve_no_connection(app, sqlite_engine, run, vendor, boom):
    assert {name for name, spec in registry().items() if spec.kind == NONE} == {
        "review.decisions", "review.record-risk", "review.record-post", "review.record-mark", "review.is-marked"}
    _risk(app, run)
    _post(app, run)
    assert _mark(app, run)["marked"] is True
    assert _is_marked(app, run)["valid"] is True
    decided = tool(app, run, "decisions", "review.decisions",
                   {**PR, "decisions": [{"claim": "x", "decision": "drop", "reason": "y"}]})
    assert _ok(decided) == {"recorded": 1}
    assert vendor.requests == []


def test_a_repository_outside_the_jobs_project_is_refused(app, run, boom):
    response = tool(app, run, "is_marked", "review.is-marked", {"repo": "acme/other", "number": 1, "head": HEAD})
    assert response.status_code == 403


# review.record-risk

def test_record_risk_records_the_level_head_and_low_risk_panel(app, sqlite_engine, run):
    out = _risk(app, run, reasons=["copy change"], paths=["src/a.swift"])
    assert (out["level"], out["floor"], out["model"], out["head"]) == ("low", "low", "low", HEAD)
    assert out["panel"] == ["correctness", "security", "conventions", "simplicity"]
    assert out["reasons"] == [{"source": "model", "text": "copy change"}]
    stored = _row(sqlite_engine, run.id)
    assert (stored.risk, stored.head_sha) == ("low", HEAD)


def test_record_risk_applies_the_servers_floors_to_the_reported_paths(app, sqlite_engine, keyring, clock, world,
                                                                     run):
    out = _risk(app, run, paths=["db/migrations/0001.sql"])
    assert (out["level"], out["floor"]) == ("high", "high") and len(out["panel"]) == 9
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="risk.floors", value={"medium": ["docs/**"]}))
    other = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, other, paths=["docs/a.md"])["level"] == "medium"
    third = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, third, paths=["src/net/Client.swift"], added=["src/net/Client.swift"])["level"] == "high"
    fourth = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, fourth, total=3001)["level"] == "high"


def test_a_listing_cut_short_is_high(app, sqlite_engine, keyring, clock, world, run):
    out = _risk(app, run, paths=["src/a.swift", "src/a.swift"], total=2)
    assert out["level"] == "high" and out["reasons"][0]["text"] == "GitHub listed fewer files than the diff changes"
    other = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, other, paths=["src/a.swift"], truncated=True)["level"] == "high"
    third = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, third, paths=["src/a.swift", "src/b.swift"], total=2)["level"] == "low"


def test_record_risk_never_lowers_the_runs_level(app, sqlite_engine, run):
    _risk(app, run, level="high")
    assert _risk(app, run, level="low")["level"] == "high"
    assert _row(sqlite_engine, run.id).risk == "high"


def test_record_risk_refuses_a_bad_level_or_head(app, run):
    assert tool(app, run, "record_risk", "review.record-risk",
                {**PR, "head": HEAD, "level": "none", "total": 0}).status_code == 400
    assert tool(app, run, "record_risk", "review.record-risk",
                {**PR, "head": "abc", "level": "low", "total": 0}).status_code == 400


def test_record_risk_requires_the_diffs_file_count(app, sqlite_engine, run):
    missing = tool(app, run, "record_risk", "review.record-risk", {**PR, "head": HEAD, "level": "low"})
    assert missing.status_code == 400 and "total" in missing.json()["error"]
    assert _row(sqlite_engine, run.id).risk is None


# review.record-post and review.record-mark

def test_record_mark_refuses_without_an_approving_post_of_the_runs_head(app, sqlite_engine, run):
    assert _mark(app, run) == {"marked": False, "reason": "the run has no reviewed head"}
    _risk(app, run)
    assert _mark(app, run)["reason"] == "this run posted no approving review of its head"
    _post(app, run, approved=False)
    assert _mark(app, run)["marked"] is False
    _post(app, run, head=NEW)
    assert _mark(app, run)["marked"] is False
    _post(app, run, posted=False)
    assert _mark(app, run)["marked"] is False
    assert _marks(sqlite_engine) == []


def test_record_mark_refuses_a_head_other_than_the_one_reviewed(app, sqlite_engine, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    out = _mark(app, run, head=NEW)
    assert out["marked"] is False and "not bbbbbbbbbbbb" in out["reason"]
    assert _marks(sqlite_engine) == []


def test_a_skipped_post_takes_a_null_review_id(app, sqlite_engine, run):
    assert _post(app, run, posted=False, approved=False, review_id=None)["posted"] is False
    assert tool(app, run, "record_post", "review.record-post", {**PR, "head": HEAD, "posted": True,
                                                                "approved": True, "review_id": 0}).status_code == 400


def test_a_non_approving_post_leaves_the_run_recommended(app, sqlite_engine, run):
    _risk(app, run)
    _post(app, run, approved=False, review_id=55)
    assert _row(sqlite_engine, run.id).outcome == "recommended"
    [row] = [c for c in calls(sqlite_engine) if c.tool == "review.record-post"]
    assert (row.outcome, row.target) == ("ok", f"{REPO.lower()}#{NUMBER}")
    assert row.detail == {"head": HEAD, "number": NUMBER, "posted": True, "approved": False, "review_id": 55}


def test_an_approved_head_gets_the_row_the_server_trusts(app, sqlite_engine, world, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    assert _mark(app, run) == {"marked": True, "head": HEAD, "check_run_id": 900, "outcome": "approved"}
    [mark] = _marks(sqlite_engine)
    assert (mark.project_id, mark.head_sha, mark.run_id, mark.check_run_id, mark.app_id, mark.conclusion) == (
        world.project, HEAD, run.id, 900, None, "success")
    stored = _row(sqlite_engine, run.id)
    assert (stored.outcome, stored.outcome_source) == ("approved", "server")


def test_only_a_newer_status_id_replaces_the_mark(app, sqlite_engine, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    _mark(app, run, status_id=900)
    assert _mark(app, run, status_id=800)["check_run_id"] == 900
    assert [m.check_run_id for m in _marks(sqlite_engine)] == [900]
    assert _mark(app, run, status_id=901)["check_run_id"] == 901
    assert [m.check_run_id for m in _marks(sqlite_engine)] == [901]


def test_record_mark_never_overwrites_an_app_check_runs_mark(app, sqlite_engine, world, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(review_marks).values(id=new_id(), team_id=world.team, project_id=world.project,
                                                 repo=REPO.lower(), pr_number=NUMBER, head_sha=HEAD, run_id=run.id,
                                                 conclusion="success", check_run_id=5, app_id=4242,
                                                 created_at=clock()))
    assert _mark(app, run, status_id=900)["check_run_id"] == 5
    assert [(m.check_run_id, m.app_id) for m in _marks(sqlite_engine)] == [(5, 4242)]


def test_a_mark_another_run_inserted_first_is_updated_cleanly(app, sqlite_engine, world, run, clock, monkeypatch):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(review_marks).values(id=new_id(), team_id=world.team, project_id=world.project,
                                                 repo=REPO.lower(), pr_number=NUMBER, head_sha=HEAD, run_id=run.id,
                                                 conclusion="success", check_run_id=5, created_at=clock()))
    real, seen = marks.mark_of, []

    def stale(conn, project_id, head):
        seen.append(head)
        return None if len(seen) == 1 else real(conn, project_id, head)
    monkeypatch.setattr(marks, "mark_of", stale)
    assert _mark(app, run)["marked"] is True
    assert [m.check_run_id for m in _marks(sqlite_engine)] == [900]


def test_review_mark_ignores_a_recorded_post(app, sqlite_engine, vendor, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    out = _ok(tool(app, run, "mark", "review.mark", PR))
    assert out["marked"] is False and vendor.check_runs == []


def test_record_mark_ignores_a_live_post(app, sqlite_engine, vendor, run, clock):
    _risk(app, run)
    live = tool(app, run, "review", "github.post-review", {**PR, "head": HEAD, "verdict": "approve", "summary": "s",
                                                           "comments": [], "decisions": []})
    assert live.status_code == 200, live.text
    clock.advance(seconds=1)
    assert _mark(app, run)["reason"] == "this run posted no approving review of its head"
    assert _marks(sqlite_engine) == []


def test_a_record_risk_counts_as_the_re_review_after_a_push(app, sqlite_engine, world, run, clock):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="github.push_authors", value=["*"]))
    _risk(app, run)
    pushed = tool(app, run, "push", "github.push", {**PR, "branch": "feature", "commit": NEW})
    assert pushed.json()["approved"] is True
    clock.advance(seconds=1)
    with sqlite_engine.connect() as conn:
        assert marks.push_refusal(conn, run.id, to_pr=True) == "the code this run last pushed was never re-reviewed"
    _risk(app, run, head=NEW)
    with sqlite_engine.connect() as conn:
        assert marks.push_refusal(conn, run.id, to_pr=True) is None


# review.is-marked

def test_is_marked_is_true_only_after_record_mark_and_settles_noop(app, sqlite_engine, run, clock):
    assert _is_marked(app, run) == {"valid": False, "head": HEAD}
    assert _row(sqlite_engine, run.id).outcome is None
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    _mark(app, run)
    assert _is_marked(app, run, head=NEW)["valid"] is False
    assert _is_marked(app, run) == {"valid": True, "head": HEAD}
    stored = _row(sqlite_engine, run.id)
    assert (stored.outcome, stored.outcome_source) == ("noop", "server")


def test_a_dry_run_records_a_mark_without_writing_one(app, sqlite_engine, keyring, clock, world):
    run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True}, risk=None)
    _risk(app, run)
    _post(app, run)
    assert _mark(app, run)["dry_run"] is True
    assert _marks(sqlite_engine) == [] and _is_marked(app, run)["valid"] is False
    assert _row(sqlite_engine, run.id).risk == "low"

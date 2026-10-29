"""review.risk, review.marked, and review.mark through the broker, against a fake GitHub."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from armature.server import risk
from armature.server.db import new_id
from armature.server.db.schema import findings, review_marks, runs, settings
from tests.server.tool_support import (  # noqa: F401
    APP_ID,
    GH_LOGIN,
    HEAD,
    NUMBER,
    REPO,
    app,
    calls,
    clock,
    dns,
    gh_cli,
    pem,
    start_run,
    tool,
    vendor,
    world,
)

NEW = "b" * 40


@pytest.fixture
def run(sqlite_engine, keyring, clock, world):
    return start_run(sqlite_engine, keyring, clock, world, risk=None)


def _risk(app, run, level="low", **params):
    response = tool(app, run, "risk", "review.risk", {"repo": REPO, "number": NUMBER, "level": level,
                                                      "expect_head": HEAD, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _row(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(runs).where(runs.c.id == run_id)).first()


def _files(vendor, *paths, status="modified"):
    vendor.files = [{"filename": p, "status": status} for p in paths]
    vendor.pr["changed_files"] = len(paths)


# review.risk

def test_low_risk_runs_the_low_risk_panel_and_records_the_head(app, sqlite_engine, run):
    out = _risk(app, run, reasons=["copy change"])
    assert (out["level"], out["floor"], out["model"], out["head"]) == ("low", "low", "low", HEAD)
    assert out["panel"] == list(risk.DEFAULT_LOW_RISK_PANEL)
    assert sorted(out["panel"] + out["skipped"]) == sorted(risk.SPECIALISTS)
    stored = _row(sqlite_engine, run.id)
    assert (stored.risk, stored.head_sha) == ("low", HEAD)
    assert stored.risk_reasons == [{"source": "model", "text": "copy change"}]


def test_medium_and_high_run_all_nine(app, run):
    assert _risk(app, run, "medium")["panel"] == list(risk.SPECIALISTS)
    assert _risk(app, run, "high")["skipped"] == []


def test_the_low_risk_panel_is_a_setting(app, sqlite_engine, world, run):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key=risk.PANEL_KEY, value=["security"]))
    assert _risk(app, run)["panel"] == ["security"]


def test_a_floor_beats_the_models_level(app, sqlite_engine, vendor, run):
    _files(vendor, "db/migrations/0002.sql", "README.md")
    out = _risk(app, run, "low")
    assert (out["level"], out["floor"]) == ("high", "high")
    assert {"source": "floor", "text": "migrations and schema: db/migrations/0002.sql"} in out["reasons"]
    assert _row(sqlite_engine, run.id).risk == "high"


def test_a_later_call_never_lowers_the_runs_risk(app, sqlite_engine, vendor, run):
    assert _risk(app, run, "high")["level"] == "high"
    out = _risk(app, run, "low")
    assert out["level"] == "high" and out["panel"] == list(risk.SPECIALISTS)
    assert _row(sqlite_engine, run.id).risk == "high"


def test_an_added_network_client_is_high_and_a_changed_one_is_not(app, vendor, run, sqlite_engine, keyring, clock,
                                                                   world):
    _files(vendor, "Sources/API/UserClient.swift")
    assert _risk(app, run)["level"] == "low"
    _files(vendor, "Sources/API/UserClient.swift", status="added")
    out = _risk(app, start_run(sqlite_engine, keyring, clock, world, risk=None))
    assert out["level"] == "high"
    assert out["reasons"][0] == {"source": "floor", "text": "new network endpoint or client: "
                                                         "Sources/API/UserClient.swift"}


def test_a_rename_counts_its_old_path(app, vendor, run):
    vendor.files = [{"filename": "docs/old.md", "previous_filename": "auth/session.py", "status": "renamed"}]
    assert _risk(app, run)["level"] == "high"


def test_over_3000_files_is_high_and_so_is_a_short_listing(app, vendor, run, sqlite_engine, keyring, clock, world):
    vendor.pr["changed_files"] = 3001
    out = _risk(app, run)
    assert out["level"] == "high" and "3001 files changed" in out["reasons"][-1]["text"]
    vendor.pr["changed_files"] = 5
    out = _risk(app, start_run(sqlite_engine, keyring, clock, world, risk=None))
    assert out["level"] == "high" and out["reasons"][0]["text"] == "GitHub listed fewer files than the diff changes"


def test_every_page_of_files_is_read(app, vendor, run):
    _files(vendor, *[f"src/f{n}.py" for n in range(150)], "deploy/prod.yml")
    assert _risk(app, run)["level"] == "high"
    pages = [r for r in vendor.requests if r.url.path.endswith(f"/pulls/{NUMBER}/files")]
    assert [vendor.query(r)["page"] for r in pages] == ["1", "2"]


def test_a_branch_is_rated_against_its_base_before_any_pull_request(app, sqlite_engine, vendor, run):
    vendor.branches["work"] = NEW
    _files(vendor, "infra/main.tf")
    out = tool(app, run, "risk", "review.risk", {"repo": REPO, "branch": "work", "base": "main",
                                                 "level": "low", "expect_head": NEW}).json()
    assert (out["level"], out["head"]) == ("high", NEW)
    assert any(r.url.path == f"/repos/{REPO}/compare/main...{NEW}" for r in vendor.requests)
    assert _row(sqlite_engine, run.id).head_sha == NEW
    missing = tool(app, run, "risk", "review.risk", {"repo": REPO, "level": "low", "expect_head": NEW})
    assert missing.status_code == 400 and "number, or branch and base" in missing.json()["error"]


def test_a_model_level_outside_the_enum_is_refused(app, run):
    assert tool(app, run, "risk", "review.risk", {"repo": REPO, "number": NUMBER, "level": "none",
                                                  "expect_head": HEAD}).status_code == 400
    assert tool(app, run, "risk", "review.risk", {"repo": REPO, "number": NUMBER, "level": "low"}).status_code == 400


def test_a_head_other_than_the_reviewed_one_is_refused_and_never_recorded(app, sqlite_engine, vendor, run):
    vendor.pr["head"]["sha"] = NEW
    response = tool(app, run, "risk", "review.risk", {"repo": REPO, "number": NUMBER, "level": "low",
                                                      "expect_head": HEAD})
    assert response.status_code == 409 and "not the reviewed" in response.json()["error"]
    stored = _row(sqlite_engine, run.id)
    assert (stored.risk, stored.head_sha) == (None, None)


# review.marked

def _marked(app, run):
    response = tool(app, run, "marked", "review.marked", {"repo": REPO, "number": NUMBER})
    assert response.status_code == 200, response.text
    return response.json()


def _mark_row(engine, world, head=HEAD, check_run_id=None):
    with engine.begin() as conn:
        run_id = conn.execute(select(runs.c.id)).scalars().first()
        conn.execute(insert(review_marks).values(id=new_id(), team_id=world.team, project_id=world.project,
                                                 repo=REPO.lower(), pr_number=NUMBER, head_sha=head, run_id=run_id,
                                                 conclusion="success", created_at=run_created(conn, run_id),
                                                 check_run_id=check_run_id))


def run_created(conn, run_id):
    return conn.execute(select(runs.c.started_at).where(runs.c.id == run_id)).scalar()


def _check(vendor, app_id=APP_ID, head=HEAD, conclusion="success"):
    vendor.check_runs.append({"id": 900, "head_sha": head, "name": "Armature review", "status": "completed",
                              "conclusion": conclusion, "app": {"slug": "armature-bot", "id": app_id}})


def test_an_unmarked_head_is_not_valid(app, sqlite_engine, run):
    assert _marked(app, run) == {"valid": False, "head": HEAD, "source": None}
    stored = _row(sqlite_engine, run.id)
    assert (stored.head_sha, stored.outcome) == (None, None)


def test_a_marked_head_is_valid_and_the_run_is_a_noop(app, sqlite_engine, world, run):
    _mark_row(sqlite_engine, world)
    assert _marked(app, run) == {"valid": True, "head": HEAD, "source": "database"}
    stored = _row(sqlite_engine, run.id)
    assert (stored.outcome, stored.outcome_source) == ("noop", "server")


def test_a_mark_on_an_older_head_does_not_count(app, sqlite_engine, world, run):
    _mark_row(sqlite_engine, world, head=NEW)
    assert _marked(app, run)["valid"] is False


def test_this_apps_own_check_counts_when_review_mark_made_it(app, sqlite_engine, world, vendor, run):
    _check(vendor)
    assert _marked(app, run)["valid"] is False
    _mark_row(sqlite_engine, world, head=NEW, check_run_id=900)
    assert _marked(app, run)["source"] == "check"


@pytest.mark.parametrize("change", [{"app_id": 99}, {"head": NEW}, {"conclusion": "failure"}])
def test_another_apps_check_or_a_failed_or_other_heads_check_is_forged(app, vendor, run, change):
    _check(vendor, **change)
    assert _marked(app, run)["valid"] is False


def _status(vendor, login=GH_LOGIN, state="success", head=HEAD, context="Armature review"):
    vendor.statuses.append({"id": vendor._id(), "sha": head, "context": context, "state": state,
                            "creator": {"login": login}, "created_at": f"2026-10-29T00:{len(vendor.statuses):02d}:00Z"})
    return vendor.statuses[-1]["id"]


def test_a_gh_cli_status_counts_only_from_its_own_login_when_review_mark_made_it(app, sqlite_engine, world, vendor,
                                                                                    run, gh_cli):
    ours = _status(vendor)
    assert _marked(app, run)["valid"] is False
    _mark_row(sqlite_engine, world, head=NEW, check_run_id=ours)
    assert _marked(app, run)["source"] == "check"
    _status(vendor, login="someone-else", state="failure")
    assert _marked(app, run)["source"] == "check"
    _status(vendor, state="failure")
    assert _marked(app, run)["valid"] is False


def test_a_status_another_login_set_is_forged_even_with_a_recorded_id(app, sqlite_engine, world, vendor, run,
                                                                       gh_cli):
    forged = _status(vendor, login="intruder")
    _mark_row(sqlite_engine, world, head=NEW, check_run_id=forged)
    assert _marked(app, run)["valid"] is False


# review.mark

def _post(app, run, verdict="approve", head=HEAD, comments=()):
    response = tool(app, run, "review", "github.post-review", {
        "repo": REPO, "number": NUMBER, "head": head, "verdict": verdict, "summary": "s", "comments": list(comments),
        "decisions": []})
    assert response.status_code == 200, response.text
    return response.json()


def _mark(app, run):
    response = tool(app, run, "mark", "review.mark", {"repo": REPO, "number": NUMBER})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_gh_cli_mark_is_a_status_from_its_login_that_reads_back(app, sqlite_engine, vendor, world, run, clock,
                                                                   gh_cli):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    out = _mark(app, run)
    assert out["marked"] is True and vendor.check_runs == []
    [status] = vendor.statuses
    assert (status["sha"], status["context"], status["state"], status["creator"]["login"]) == (
        HEAD, "Armature review", "success", GH_LOGIN)
    with sqlite_engine.connect() as conn:
        mark = conn.execute(select(review_marks)).one()
    assert (mark.check_run_id, mark.app_id) == (status["id"], None)
    with sqlite_engine.begin() as conn:
        conn.execute(update(review_marks).values(head_sha=NEW))
    assert _marked(app, run)["source"] == "check"


def test_an_approved_head_is_marked_with_a_check_and_a_row(app, sqlite_engine, vendor, world, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    out = _mark(app, run)
    assert out["marked"] is True and out["head"] == HEAD and out["outcome"] == "approved"
    [check] = vendor.check_runs
    assert (check["head_sha"], check["name"], check["conclusion"]) == (HEAD, "Armature review", "success")
    with sqlite_engine.connect() as conn:
        mark = conn.execute(select(review_marks)).one()
    assert (mark.project_id, mark.head_sha, mark.run_id, mark.app_id) == (world.project, HEAD, run.id, APP_ID)
    assert mark.check_run_id == check["id"]
    stored = _row(sqlite_engine, run.id)
    assert (stored.outcome, stored.outcome_source) == ("approved", "server")
    assert _mark(app, run)["marked"] is True
    with sqlite_engine.connect() as conn:
        assert len(conn.execute(select(review_marks)).all()) == 1


def test_a_run_that_posted_no_review_marks_nothing(app, vendor, run):
    assert _mark(app, run) == {"marked": False, "reason": "the run has no reviewed head"}
    _risk(app, run)
    assert _mark(app, run)["reason"] == "this run posted no approving review of its head"
    assert vendor.check_runs == []


def test_a_review_that_recommends_changes_is_not_marked(app, sqlite_engine, vendor, run):
    _risk(app, run)
    comment = {"path": "src/a.swift", "line": 3, "severity": "high", "blocking": True, "body": "Wrong.",
               "claim": "wrong"}
    _post(app, run, verdict="comment", comments=[comment])
    assert _row(sqlite_engine, run.id).outcome == "recommended"
    assert _mark(app, run)["marked"] is False and vendor.check_runs == []


def test_a_head_that_moved_after_the_review_is_not_marked(app, vendor, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    vendor.pr["head"]["sha"] = NEW
    _risk(app, run, expect_head=NEW)
    out = _mark(app, run)
    assert out == {"marked": False, "head": NEW, "reason": "this run posted no approving review of its head"}


def test_an_approval_of_another_head_than_the_runs_is_not_marked(app, run):
    _risk(app, run)
    _post(app, run, head=NEW)
    assert _mark(app, run)["marked"] is False


def test_a_run_that_pushed_its_fix_ends_pushed(app, sqlite_engine, world, run, clock):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="github.push_authors", value=["*"]))
    _risk(app, run)
    assert tool(app, run, "push", "github.push", {"repo": REPO, "number": NUMBER, "branch": "feature",
                                                  "commit": NEW}).json()["approved"] is True
    clock.advance(seconds=1)
    _post(app, run)
    clock.advance(seconds=1)
    assert _mark(app, run)["outcome"] == "pushed"


def test_review_tools_tell_the_live_hub_the_run_changed(sqlite_engine, keyring, clock, world, vendor, run):
    import httpx

    from armature.server import broker
    from armature.server.broker import Grant, encode
    heard = []
    token = encode(keyring, Grant(run.id, "risk", ("review.risk",), clock() + broker.GRANT_MAX))
    broker.call_tool(sqlite_engine, keyring, public_url="https://arm.test", token=token, tool="review.risk",
                     params={"repo": REPO, "number": NUMBER, "level": "low", "expect_head": HEAD}, now=clock(),
                     ip=None,
                     transport=httpx.MockTransport(vendor), hub=SimpleNamespace(run_changed=heard.append))
    assert heard == [run.id]


def test_a_dry_run_rates_its_own_run_from_githubs_files(app, sqlite_engine, keyring, clock, world, vendor):
    run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True}, risk=None)
    out = _risk(app, run)
    assert "dry_run" not in out and out["level"] == "low" and out["head"] == HEAD and out["panel"]
    assert _row(sqlite_engine, run.id).risk == "low" and vendor.writes() == []
    assert [c.mode for c in calls(sqlite_engine)] == ["live"]


def test_the_runs_risk_reads_high_once_its_row_is_gone(sqlite_engine, run):
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run.id).values(risk=None))
        assert risk.recorded(conn, run.id) == "high"


# review.decisions

DECISION = {"claim": "the handle leaks", "raised_by": ["correctness", "performance"], "decision": "block",
            "reason": "the code shows it", "finding_id": "0123456789ab", "path": "src/a.swift", "line": 3,
            "token": "dropped"}


def _rows(engine):
    with engine.connect() as conn:
        return conn.execute(select(findings).order_by(findings.c.created_at)).all()


def test_the_leads_decisions_become_the_steps_findings(app, sqlite_engine, world, run):
    response = tool(app, run, "decisions", "review.decisions", {"repo": REPO, "number": NUMBER,
                                                                "decisions": [DECISION]})
    assert response.json() == {"recorded": 1}
    [row] = _rows(sqlite_engine)
    assert (row.run_id, row.project_id, row.step_id, row.finding_key) == (run.id, world.project, "decisions",
                                                                          "0123456789ab")
    assert (row.decision, row.reason, row.raised_by, row.body, row.line) == (
        "block", "the code shows it", ["correctness", "performance"], "the handle leaks", 3)


def _decide(app, run, decisions=(DECISION,), **params):
    return tool(app, run, "decisions", "review.decisions", {"repo": REPO, "decisions": list(decisions), **params})


def test_fixed_decisions_need_this_runs_approved_push_and_carry_its_commit(app, sqlite_engine, world, run, clock):
    _decide(app, run)
    refused = _decide(app, run, fixed=True)
    assert refused.status_code == 409 and "push was approved" in refused.json()["error"]
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="github.push_authors", value=["*"]))
    _risk(app, run)
    pushed = tool(app, run, "push", "github.push", {"repo": REPO, "number": NUMBER, "branch": "feature",
                                                    "commit": NEW}).json()
    assert pushed["approved"] is True
    clock.advance(seconds=1)
    assert _decide(app, run, fixed=True).json() == {"recorded": 1}
    fixed = [r for r in _rows(sqlite_engine) if r.decision == "fixed"]
    assert [(r.finding_key, r.fix_sha) for r in fixed] == [("0123456789ab", NEW)]
    assert _decide(app, run, fixed=True, fix_sha=HEAD).status_code == 400


def test_fixed_refuses_a_finding_this_run_never_raised(app, sqlite_engine, world, run):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="github.push_authors", value=["*"]))
    _risk(app, run)
    tool(app, run, "push", "github.push", {"repo": REPO, "number": NUMBER, "branch": "feature", "commit": NEW})
    refused = _decide(app, run, [{**DECISION, "finding_id": "fedcba987654"}], fixed=True)
    assert refused.status_code == 409 and "never raised fedcba987654" in refused.json()["error"]
    assert _rows(sqlite_engine) == []


def test_a_decision_without_an_id_takes_the_key_post_review_gives_it(app, sqlite_engine, run):
    from armature.server.tools.review import finding_key
    given = {k: v for k, v in DECISION.items() if k != "finding_id"}
    _decide(app, run, [given])
    assert [r.finding_key for r in _rows(sqlite_engine)] == [finding_key(given)]


# review.pushable and the open-pr hold

def _pushable(app, run):
    return tool(app, run, "pushable", "review.pushable", {"repo": REPO, "number": NUMBER,
                                                          "branch": "feature"}).json()


def test_pushable_asks_the_push_gate_and_pushes_nothing(app, sqlite_engine, world, run, vendor):
    _risk(app, run)
    assert _pushable(app, run) == {"pushable": False, "reason": "no push author allowlist is set"}
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="github.push_authors", value=["*"]))
    assert _pushable(app, run) == {"pushable": True, "reason": "*"}
    vendor.pr["head"]["repo"]["full_name"] = "mallory/App"
    assert _pushable(app, run)["reason"] == "the pull request comes from a fork"
    assert not [c for c in calls(sqlite_engine) if c.tool == "github.push"]


def test_a_run_is_not_pushable_again_until_its_push_is_re_reviewed(app, sqlite_engine, world, run, clock):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="github.push_authors", value=["*"]))
    _risk(app, run)
    clock.advance(seconds=1)
    assert tool(app, run, "push", "github.push", {"repo": REPO, "number": NUMBER, "branch": "feature",
                                                  "commit": NEW}).json()["approved"] is True
    clock.advance(seconds=1)
    assert _pushable(app, run) == {"pushable": False,
                                   "reason": "the code this run last pushed was never re-reviewed"}
    _risk(app, run)
    clock.advance(seconds=1)
    assert _pushable(app, run)["pushable"] is True


def test_a_high_or_unrated_run_is_not_pushable(app, run):
    assert _pushable(app, run) == {"pushable": False, "reason": "the run has no recorded risk"}


OPEN = {"repo": REPO, "head": "feature", "base": "main", "title": "Add it"}


@pytest.mark.parametrize("level, opens", [("low", True), ("medium", True), ("high", False), (None, False)])
def test_open_pr_needs_low_or_medium_risk(app, sqlite_engine, run, level, opens):
    if level:
        _risk(app, run, level)
    response = tool(app, run, "open", "github.open-pr", OPEN)
    assert (response.status_code == 200) is opens
    if not opens:
        assert response.status_code == 403 and "person's approval" in response.json()["error"]
        assert [(c.tool, c.outcome) for c in calls(sqlite_engine)][-1] == ("github.open-pr", "refused")


def _gate(engine, world, run, clock, **values):
    from armature.server.db.schema import gates
    with engine.begin() as conn:
        conn.execute(insert(gates).values(**{"id": new_id(), "run_id": run.id, "team_id": world.team,
                                             "step_id": "approval", "step_key": "approval", "nonce": "n" * 64,
                                             "created_at": clock(), "expires_at": clock(), **values}))


def test_an_approved_gate_lets_a_high_risk_run_open_its_pull_request(app, sqlite_engine, world, run, clock):
    _risk(app, run, "high")
    clock.advance(seconds=1)
    _gate(sqlite_engine, world, run, clock, state="rejected", decided_at=clock())
    assert tool(app, run, "open", "github.open-pr", OPEN).status_code == 403
    _gate(sqlite_engine, world, run, clock, step_key="again", state="approved", decided_at=clock())
    assert tool(app, run, "open", "github.open-pr", OPEN).status_code == 200


@pytest.mark.parametrize("min_role, decided", [("contributor", 1), ("project_lead", -1)])
def test_a_gate_counts_only_from_a_project_lead_after_the_rating(app, sqlite_engine, world, run, clock, min_role,
                                                                 decided):
    clock.advance(seconds=5)
    _risk(app, run, "high")
    _gate(sqlite_engine, world, run, clock, state="approved", min_role=min_role,
          decided_at=clock() + timedelta(seconds=decided))
    assert tool(app, run, "open", "github.open-pr", OPEN).status_code == 403


def test_open_pr_refuses_a_branch_other_than_the_rated_head(app, sqlite_engine, run, vendor):
    _risk(app, run, "low")
    vendor.branches["other"] = NEW
    response = tool(app, run, "open", "github.open-pr", {**OPEN, "head": "other"})
    assert response.status_code == 409 and "this run's review rated" in response.json()["error"]
    assert vendor.writes("POST") == []


def test_a_live_hub_takes_the_decisions_as_events(sqlite_engine, keyring, clock, world, vendor, run):
    import httpx

    from armature.server import broker
    heard = []
    hub = SimpleNamespace(run_changed=lambda run_id: None,
                          server_findings=lambda run_id, step, payloads: heard.append((run_id, step, payloads)))
    token = broker.encode(keyring, broker.Grant(run.id, "decisions", ("review.decisions",),
                                                clock() + broker.GRANT_MAX))
    out = broker.call_tool(sqlite_engine, keyring, public_url="https://arm.test", token=token,
                           tool="review.decisions", params={"repo": REPO, "decisions": [DECISION]}, now=clock(),
                           ip=None, transport=httpx.MockTransport(vendor), hub=hub)
    assert out == {"recorded": 1} and _rows(sqlite_engine) == []
    [(run_id, step, [payload])] = heard
    assert (run_id, step, payload["finding_key"], payload["decision"]) == (run.id, "decisions", "0123456789ab",
                                                                            "block")
    assert "token" not in payload and "finding_id" not in payload


# review.context


def test_context_lists_each_files_commentable_ranges_and_the_jira_keys_once():
    from armature.server.tools import context
    patch = "@@ -1,3 +1,3 @@\n line 1\n-old\n+new\n line 3\n@@ -10,0 +11,2 @@\n+a\n+b"
    assert context.files_of([{"filename": "a.swift", "status": "modified", "patch": patch},
                             {"filename": "gone.swift", "status": "removed"}]) == [
        {"path": "a.swift", "status": "modified", "lines": [[1, 3], [11, 12]]},
        {"path": "gone.swift", "status": "deleted", "lines": []}]
    assert context.jira_keys("MOB-1 and MOB-1", "MOB-2-x MOB-3 MOB-4") == ["MOB-1", "MOB-2", "MOB-3"]
    assert context.jira_keys("UTF-8 fix for MOB-7", "feature/ABC-2-MOB-9", project="MOB") == ["MOB-7", "MOB-9"]


def test_context_keeps_only_the_jira_projects_keys_once_it_is_set(app, sqlite_engine, world, vendor, run):
    vendor.pr.update(title="MOB-12 and UTF-8 cleanup")

    def keys():
        response = tool(app, run, "context", "review.context", {"repo": REPO, "number": NUMBER})
        assert response.status_code == 200, response.text
        return response.json()["jira_keys"]

    assert keys() == ["MOB-12", "UTF-8"]
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="jira.project", value="MOB"))
    assert keys() == ["MOB-12"]


def test_context_keeps_the_newest_comments_past_the_cap(monkeypatch):
    from armature.server.tools import context
    monkeypatch.setattr(context, "COMMENT_CAP", 2)
    comments = [{"id": n, "user": {"login": "ana"}, "created_at": f"2026-10-2{n}T00:00:00Z", "body": f"note {n}"}
                for n in range(1, 4)]
    text = context.discussion_md(REPO, NUMBER, comments, [], [], lambda item: False)
    assert "note 1" not in text and "note 2" in text and "note 3" in text
    assert "1 older comments were left out" in text

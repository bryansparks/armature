"""code-review imported from ops: its rule decides as ops' gateway did, its sessions get ops' role instructions, and a
recorded review posts what ops posted."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, insert, select, update

from armature.nodes.agent import _render as render_context
from armature.server import defs, risk, starters, webhooks
from armature.server.db import new_id
from armature.server.db.schema import jobs, projects, settings, teams, workflows
from armature.server.db.schema import webhooks as webhooks_table
from armature.server.migrate_ops import code_review, import_ops
from tests.server import ops_harness
from tests.server.test_review_flow import INPUTS, flow, run_flow  # noqa: F401
from tests.server.tool_support import (  # noqa: F401
    BOT,
    HEAD,
    NUMBER,
    REPO,
    app,
    calls,
    clock,
    dns,
    marker,
    pem,
    start_run,
    vendor,
    world,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
GOLDENS = {path.stem: ops_harness.load(path) for path in ops_harness.fixtures("code-review")}
FRONT = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def _fs(conn) -> str:
    team = new_id()
    conn.execute(insert(teams).values(id=team, name="FamilySearch", slug="familysearch", created_at=NOW))
    for repo in code_review.REVIEW_BRANCHES:
        conn.execute(insert(projects).values(id=new_id(), team_id=team, name=repo.split("/")[1], repo=repo,
                                             created_at=NOW))
    return team


@pytest.fixture
def fs(sqlite_engine):
    with sqlite_engine.begin() as conn:
        team = _fs(conn)
        homes = dict(conn.execute(select(projects.c.repo, projects.c.id)).all())
        for home in homes.values():
            import_ops(conn, "code-review", team_id=team, project_id=home, now=NOW)
    return team, homes


def _delivery(golden: dict, **change) -> dict:
    """The pull_request event ops' gateway turned into the golden's job."""
    job = golden["files"]["state/pr-queue/job.json"]
    pr = {"number": job["number"], "draft": False, "head": {"sha": "a" * 40, "ref": "MEMTEMDIS-4512-memories-cache"},
          "base": {"ref": "dev"}, "user": {"login": "jstutz4"}}
    payload = {"action": "opened", "number": job["number"], "pull_request": pr,
               "repository": {"full_name": job["repo"], "clone_url": f"https://github.com/{job['repo']}.git"},
               "sender": {"login": "jstutz4", "type": "User"}}
    for path, value in change.items():
        *parents, leaf = path.split(".")
        target = payload
        for part in parents:
            target = target[part]
        target[leaf] = value
    return {"source": "github", "headers": {"x-github-event": "pull_request"}, "payload": payload}


def _matched(engine, team, delivery):
    return ops_harness._decisions(engine, team, delivery)


def test_the_import_copies_s14bs_starters_and_seeds_their_roles(sqlite_engine, fs):
    team, homes = fs
    with sqlite_engine.connect() as conn:
        for home in homes.values():
            for name in ("code-review", "review-pass"):
                row = conn.execute(select(workflows).where(workflows.c.scope == home, workflows.c.name == name)).one()
                assert defs.body_of(conn, defs.WORKFLOW, row.id, 1) == starters.body(name)
        assert sorted(defs.resolve_document(conn, "role", k, team, None).key for k in starters.ROLES) == \
            sorted(starters.ROLES)
        hook = conn.execute(select(webhooks_table)).one()
        rules = webhooks.rules_of(conn, hook.id)
    assert len(rules) == 6 and len({r.config["order"] for r in rules}) == 6
    assert sorted(bool(r.config.get("cancel_only")) for r in rules) == [False] * 3 + [True] * 3


def test_the_push_gate_gets_ops_author_allowlist_per_project(sqlite_engine, fs):
    _, homes = fs
    with sqlite_engine.connect() as conn:
        found = {(r.scope, r.key): r.value for r in conn.execute(select(settings))}
    assert found[(homes["fs-eng/android-tree"], "github.push_authors")] == ["team:android-temple", "moretanjames"]
    assert found[(homes["fs-eng/mobile-ios"], "github.push_authors")][0] == "team:ios-temple"
    assert (homes["fs-eng/mobile-api-nodejs"], "github.push_authors") not in found


def test_closing_a_pull_request_unmerged_cancels_its_review_and_queues_nothing(sqlite_engine, fs):
    team = fs[0]
    opened = _matched(sqlite_engine, team, _delivery(GOLDENS["blocking-finding"]))
    closed = _delivery(GOLDENS["blocking-finding"], action="closed", **{"pull_request.merged": False})
    [decision] = _matched(sqlite_engine, team, closed)
    assert decision["key"] == "fs-eng/android-tree#4821" and decision["cancel_only"] is True

    def close(conn):
        before = conn.execute(select(func.count()).select_from(jobs)).scalar_one()
        [matched] = webhooks.enqueue(conn, team_id=team, delivery_id=new_id(),
                                     decisions=[{**decision, "shadow": False, "dry": False}], now=NOW)
        assert matched["job_id"] is None and matched["cancelled"] == 1
        assert conn.execute(select(func.count()).select_from(jobs)).scalar_one() == before

    with sqlite_engine.begin() as conn:
        [queued] = webhooks.enqueue(conn, team_id=team, delivery_id=new_id(),
                                    decisions=[{**opened[0], "shadow": False}], now=NOW)
        [dry] = webhooks.enqueue(conn, team_id=team, delivery_id=new_id(),
                                 decisions=[{**decision, "shadow": False, "dry": True}], now=NOW)
        assert dry["cancelled"] == 0
        assert conn.execute(select(jobs.c.status).where(jobs.c.id == queued["job_id"])).scalar_one() == "queued"
        close(conn)
        assert conn.execute(select(jobs.c.status).where(jobs.c.id == queued["job_id"])).scalar_one() == "superseded"
        [running] = webhooks.enqueue(conn, team_id=team, delivery_id=new_id(),
                                     decisions=[{**opened[0], "shadow": False}], now=NOW)
        conn.execute(update(jobs).where(jobs.c.id == running["job_id"]).values(status="running"))
        close(conn)
        row = conn.execute(select(jobs).where(jobs.c.id == running["job_id"])).one()
    assert row.status == "running" and row.cancel_requested_at is not None
    merged = _delivery(GOLDENS["blocking-finding"], action="closed", **{"pull_request.merged": True})
    assert _matched(sqlite_engine, team, merged) == []


def test_a_review_runs_once_in_the_pull_requests_project_and_supersedes_by_pull_request(sqlite_engine, fs):
    team, homes = fs
    [decision] = _matched(sqlite_engine, team, _delivery(GOLDENS["blocking-finding"]))
    assert decision["project_id"] == homes["fs-eng/android-tree"]
    assert decision["key"] == "fs-eng/android-tree#4821" and decision["concurrency"] == "supersede"
    assert decision["inputs"] == {"repo": "https://github.com/fs-eng/android-tree.git", "slug": "fs-eng/android-tree",
                                  "number": 4821, "ref": "a" * 40, "branch": "MEMTEMDIS-4512-memories-cache"}


@pytest.mark.parametrize("change", [{"action": "edited"}, {"pull_request.draft": True},
                                    {"pull_request.base.ref": "feature/x"},
                                    {"sender": {"login": "dependabot[bot]", "type": "Bot"}}],
                         ids=["edited", "draft", "other-base", "bot"])
def test_what_ops_skipped_matches_no_rule(sqlite_engine, fs, change):
    assert _matched(sqlite_engine, fs[0], _delivery(GOLDENS["blocking-finding"], **change)) == []


def test_todo_the_author_allowlist_has_no_rule_condition_yet(sqlite_engine, fs):
    # ops declined this author (pr-authors.json). Until a rule can check team membership, the platform reviews it.
    golden = GOLDENS["author-not-allowed"]
    assert golden["ops"]["outputs"]["post"]["posted"] is False
    assert len(_matched(sqlite_engine, fs[0], _delivery(golden, **{"pull_request.user.login": "contractor-dev"}))) == 1


def test_each_ops_specialist_session_ran_with_its_starter_roles_instructions():
    sessions = [s for s in GOLDENS["blocking-finding"]["ops"]["sessions"]
                if s["kind"] == "claude" and "--json-schema" not in s["args"]]
    found = set()
    for session in sessions:
        prompt = session["args"][1]
        matches = [k for k in starters.ROLES if FRONT.sub("", starters.role(k)).strip() in prompt]
        assert len(matches) == 1, prompt[:200]
        found |= set(matches)
    assert found == set(starters.ROLES) - {"team_lead"}


def _lead(golden: dict) -> dict:
    return next(f["result"]["structured_output"] for f in golden["fakes"]["claude"] if f["match"] == "--json-schema")


def _ops_review(golden: dict) -> dict:
    post = next(e for e in golden["ops"]["effects"] if e.get("stdin") and "/reviews" in " ".join(e["args"]))
    return json.loads(post["stdin"])


def _files(golden: dict) -> list:
    return next(f["stdout"] for f in golden["fakes"]["gh"] if "/files --paginate" in f["match"])[0]


@pytest.mark.parametrize("name", ["blocking-finding", "approve-clean"])
async def test_a_recorded_review_posts_what_ops_posted(flow, app, vendor, tmp_path, name):  # noqa: F811
    golden = GOLDENS[name]
    flow.level = "high"
    played = flow.agent

    async def agent(node, context):
        if node._stage.id == "team_lead":
            return _lead(golden)
        return await played(node, context)

    flow.agent = agent
    files = _files(golden)

    def vendors(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/repos/{REPO}/pulls/{NUMBER}/files":
            return httpx.Response(200, json=files)
        return vendor(request)

    app.app.state.vendor_transport = httpx.MockTransport(vendors)
    await run_flow(tmp_path, "code-review", INPUTS)
    assert sorted(flow.specialists[0]) == sorted(set(starters.ROLES) - {"team_lead"})
    [posted] = [vendor.body(r) for r in vendor.writes("POST") if r.url.path.endswith(f"/pulls/{NUMBER}/reviews")]
    ops = _ops_review(golden)
    assert posted["event"] == ops["event"]

    def anchors(review):
        return [(c["path"], c["line"], c.get("side"), c["body"]) for c in review.get("comments", [])]

    assert anchors(posted) == anchors(ops)


async def test_a_dry_run_reads_github_runs_the_panel_and_records_every_write(flow, sqlite_engine, keyring, clock,  # noqa: F811
                                                                             world, vendor, tmp_path):
    flow.run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True}, risk=None)
    vendor.pr.update(title="MOB-12 Fix the cache leak", body="Clears the cache on sign-out.")
    vendor.comments = [{"id": 31, "user": {"login": BOT}, "path": "src/a.swift", "line": 3,
                        "created_at": "2026-10-27T10:00:00Z", "body": "It leaks.\n\n" + marker("0123456789ab")},
                       {"id": 32, "in_reply_to_id": 31, "user": {"login": "jstutz4"}, "author_association": "MEMBER",
                        "created_at": "2026-10-27T11:00:00Z", "body": "Won't fix: the cache is per session."}]
    vendor.issue_comments = [{"id": 41, "user": {"login": "ana"}, "created_at": "2026-10-27T12:00:00Z",
                              "body": "Looks good to me."}]
    seen = {}
    played = flow.agent

    async def agent(node, context):
        seen[node._stage.id] = {name: render_context(text, context)
                                for name, text in node._stage.agent.context_files.items()}
        return await played(node, context)

    flow.agent = agent
    result = await run_flow(tmp_path, "code-review", INPUTS)
    assert sorted(flow.specialists[0]) == sorted(risk.DEFAULT_LOW_RISK_PANEL)
    assert result["first"]["risk"] == "low" and result["first"]["head"] == HEAD
    assert result["first"]["verdict"] == "approve"
    assert vendor.writes() == [] and vendor.check_runs == []
    assert sorted((c.tool, c.mode) for c in calls(sqlite_engine)) == [
        ("github.post-review", "dry"), ("jira.get-issue", "live"), ("review.context", "live"),
        ("review.decisions", "dry"), ("review.mark", "dry"), ("review.marked", "live"), ("review.risk", "live")]

    specialist, lead = seen["correctness"], seen["team_lead"]
    assert set(specialist) == {"pr", "files.json", "jira"}
    assert set(lead) == {"reviews", "verify.json", "pr", "files.json", "findings", "discussion", "jira"}
    assert "# MOB-12 Fix the cache leak" in lead["pr"] and "Clears the cache on sign-out." in lead["pr"]
    assert json.loads(lead["files.json"]) == [{"path": "src/a.swift", "status": "modified", "lines": [[1, 12]]}]
    assert "## Finding 0123456789ab" in lead["findings"] and "`src/a.swift:3`" in lead["findings"]
    assert "Won't fix: the cache is per session." in lead["discussion"] and "[armature]" in lead["discussion"]
    assert "Looks good to me." in lead["discussion"]
    assert json.loads(lead["jira"])["issue"] == "MOB-12" and specialist["jira"] == lead["jira"]
    assert json.loads(lead["verify.json"])["ran"] is False
    assert json.loads(lead["reviews"]) == {role: {"findings": []} if role in risk.DEFAULT_LOW_RISK_PANEL
                                           else {"_skipped": True} for role in risk.SPECIALISTS}


async def test_a_dry_auto_fix_run_says_it_would_push_and_never_opens_a_write_session(flow, sqlite_engine, keyring,  # noqa: F811
                                                                                     clock, world, vendor, tmp_path):
    flow.run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True}, risk=None)
    flow.verdicts[1] = "comment"
    result = await run_flow(tmp_path, "code-review", {**INPUTS, "auto_fix": True})
    assert result["pushable"] == {"pushable": True, "reason": result["pushable"]["reason"], "dry_run": True}
    assert result["fix"] == result["push"] == result["second"] == {"_skipped": True}
    assert "fix" not in flow.sessions and "github.push" not in flow.tools and vendor.writes() == []
    [row] = [c for c in calls(sqlite_engine) if c.tool == "review.pushable"]
    assert row.detail["would_push"] is True

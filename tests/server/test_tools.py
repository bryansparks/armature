"""The tool broker: per-step grants, built-in tool calls, dry runs, the push gate, and git credentials."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, select, update

from armature.server import broker, sanitize, writer
from armature.server.ingest import REDACTED
from armature.server.db import new_id
from armature.server.db.schema import audit, connections, jobs, projects, runs, settings
from armature.server.tools.jira import adf
from tests.server.tool_support import (  # noqa: F401
    AUTHOR,
    BOT,
    BOT_TOKEN,
    GH_TOKEN,
    HEAD,
    NUMBER,
    REPO,
    app,
    ask_grant,
    calls,
    clock,
    dns,
    fresh_members,
    gh_cli,
    grant,
    on_runner,
    pem,
    start_run,
    tool,
    use,
    vendor,
    world,
)


FIX = "f" * 40
PUSH = {"repo": REPO, "number": NUMBER, "branch": "feature", "commit": FIX}


@pytest.fixture
def run(sqlite_engine, keyring, clock, world):
    return start_run(sqlite_engine, keyring, clock, world)


def _setting(engine, key, value, team_id=None, project_id=None):
    with engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=team_id, project_id=project_id, key=key, value=value))


def _end(engine, run_id, clock):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(status="succeeded", finished_at=clock()))


SLACK = {"channel": "C0REVIEW", "text": "Review done"}


# Grants.

def test_a_grant_covers_only_the_steps_own_builtin_tool(app, run):
    response = ask_grant(app, run, "post")
    assert response.status_code == 200 and response.json()["tools"] == ["slack.post"]
    assert response.headers["cache-control"] == "no-store"
    token = response.json()["token"]
    assert token.startswith("atg_")
    assert use(app, token, "slack.update", {"channel": "C0REVIEW", "ts": "1.2", "text": "x"}).status_code == 403
    assert use(app, token, "jira.comment", {"issue": "MOB-1", "body": "x"}).status_code == 403


def test_a_step_without_a_builtin_tool_gets_no_grant(app, run):
    for step in ("plain", "missing"):
        response = ask_grant(app, run, step)
        assert response.status_code == 403 and "uses no built-in tool" in response.json()["error"]


def test_a_workflow_adapter_under_a_builtin_name_gets_no_grant(app, sqlite_engine, keyring, clock, world):
    from armature.server import defs
    spoof = ("name: tools\nadapters:\n  slack/post: {name: slack/post, type: script, cmd: 'env'}\n"
             "stages:\n  - id: post\n    adapter: slack/post\n")
    with sqlite_engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world.workflow, 2, spoof, "spoof", None, clock())
    run = start_run(sqlite_engine, keyring, clock, world, version=2)
    response = ask_grant(app, run, "post")
    assert response.status_code == 403


def test_grants_need_a_live_job_run_token(app, sqlite_engine, keyring, clock, world, run):
    bad = app.post("/api/tools/grants", json={"step": "post"}, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401 and bad.headers["www-authenticate"] == "Bearer"
    from armature.server import runners
    with sqlite_engine.begin() as conn:
        row, token = runners.open_run(conn, keyring, team_id=world.team, project_id=None, workflow_id=None,
                                      workflow_name="adhoc", now=clock())
    adhoc = ask_grant(app, SimpleNamespace(id=row["id"], token=token, key=runners.engine_key(keyring, row["id"]),
                                           clock=clock), "post")
    assert adhoc.status_code == 403 and "job" in adhoc.json()["error"]


def test_a_script_step_with_only_the_event_token_gets_no_grant(app, keyring, clock, run, vendor):
    """A step holds the run token but never the engine key, so it can't mint a grant for push or anything else."""
    for step in ("push", "post"):
        unsigned = ask_grant(app, run, step, signed=False)
        assert unsigned.status_code == 403 and "Only the run's engine" in unsigned.json()["error"]
    assert ask_grant(app, run, "push", key="0" * 64).status_code == 403
    assert ask_grant(app, run, "push", key=run.token).status_code == 403
    assert ask_grant(app, run, "post", sig="x" * 64).status_code == 403
    assert ask_grant(app, run, "post", kind="run_suspend").status_code == 403


def test_a_stale_or_mismatched_grant_request_is_refused(app, clock, run):
    now = int(clock().timestamp())
    assert ask_grant(app, run, "post", ts=now - 61).json()["error"] == "The grant request is stale."
    assert ask_grant(app, run, "post", ts=now + 61).status_code == 403
    assert ask_grant(app, run, "post", ts=now - 59).status_code == 200
    assert ask_grant(app, run, "post", tools=["github.push"]).status_code == 403
    other = SimpleNamespace(**{**vars(run), "id": "someone-else"})
    assert ask_grant(app, other, "post", key=run.key).status_code == 403


def test_a_grant_expires_and_dies_with_its_run(app, sqlite_engine, keyring, clock, world, vendor):
    run = start_run(sqlite_engine, keyring, clock, world)
    token = grant(app, run, "post")
    clock.advance(seconds=60 + 61)
    assert use(app, token, "slack.post", SLACK).status_code == 401
    token = grant(app, run, "post")
    _end(sqlite_engine, run.id, clock)
    assert use(app, token, "slack.post", SLACK).status_code == 401
    assert vendor.requests == []


def test_a_forged_or_altered_grant_is_refused(app, keyring, clock, run):
    token = grant(app, run, "post")
    body, mac = token[len("atg_"):].split(".")
    other = broker.encode(keyring, broker.Grant(run.id, "jira", ("jira.comment",), clock() + timedelta(minutes=5)))
    for forged in (f"atg_{other.split('.')[0][4:]}.{mac}", token + "x", "atg_e30.AAAA", None):
        assert broker.decode(keyring, forged, clock()) is None
    assert broker.decode(keyring, token, clock()).tools == ("slack.post",)


# Calls.

def test_slack_post_escapes_the_text_and_keeps_the_token_on_the_server(app, sqlite_engine, run, vendor):
    response = tool(app, run, "post", "slack.post",
                    {"channel": "C0REVIEW", "text": "<!channel> & <https://evil|click> <@U123>",
                     "thread_ts": "1700000000.000100"})
    assert response.status_code == 200
    assert response.json() == {"posted": True, "channel": "C0REVIEW", "ts": "1700000000.000100"}
    [request] = vendor.requests
    sent = vendor.body(request)
    assert sent["text"] == "&lt;!channel&gt; &amp; &lt;https://evil|click&gt; &lt;@U123&gt;"
    assert sent["unfurl_links"] is False and sent["unfurl_media"] is False
    assert sent["thread_ts"] == "1700000000.000100"
    assert request.headers["authorization"] == f"Bearer {BOT_TOKEN}"
    assert BOT_TOKEN not in response.text


def test_slack_post_unfurls_links_only_when_asked(app, run, vendor):
    assert tool(app, run, "post", "slack.post", {"channel": "C0REVIEW", "text": "https://x.test", "unfurl": True}
                ).status_code == 200
    sent = vendor.body(vendor.requests[-1])
    assert sent["unfurl_links"] is True and sent["unfurl_media"] is True
    assert tool(app, run, "post", "slack.post", {"channel": "C0REVIEW", "text": "hi", "unfurl": "yes"}
                ).status_code == 400


def test_every_call_is_recorded_and_audited(app, sqlite_engine, run, vendor):
    tool(app, run, "post", "slack.post", SLACK)
    vendor.slack = {"ok": False, "error": "channel_not_found"}
    failed = tool(app, run, "post", "slack.post", SLACK)
    assert failed.status_code == 502 and "channel_not_found" in failed.json()["error"]
    rows = calls(sqlite_engine)
    assert [(r.tool, r.mode, r.outcome, r.step_id, r.target) for r in rows] == [
        ("slack.post", "live", "ok", "post", "C0REVIEW"), ("slack.post", "live", "failed", "post", "C0REVIEW")]
    with sqlite_engine.connect() as conn:
        actions = conn.execute(select(audit.c.action, audit.c.detail).where(
            audit.c.action.in_(("tool.grant", "tool.call")))).all()
    assert [a.action for a in actions] == ["tool.grant", "tool.call", "tool.grant", "tool.call"]
    assert actions[1].detail == {"tool": "slack.post", "step": "post", "mode": "live", "outcome": "ok",
                                 "target": "C0REVIEW"}


def test_params_are_checked_against_the_adapter_schema(app, run, vendor):
    response = tool(app, run, "post", "slack.post", {"channel": "C0REVIEW", "text": "x", "as_user": True})
    assert response.status_code == 400 and "as_user" in response.json()["error"]
    assert vendor.requests == []


def test_an_unverified_connection_or_another_teams_repo_is_refused(app, sqlite_engine, world, run, vendor):
    with sqlite_engine.begin() as conn:
        conn.execute(update(connections).where(connections.c.id == world.slack).values(state="unverified"))
    response = tool(app, run, "post", "slack.post", SLACK)
    assert response.status_code == 409 and "Verify it first" in response.json()["error"]
    other = tool(app, run, "comment", "github.pr-comment", {"repo": "Evil/Repo", "number": 1, "body": "hi"})
    assert other.status_code == 403 and vendor.requests == []


@pytest.mark.parametrize("how", ["trigger", "setting"])
def test_a_dry_run_records_the_call_redacted_and_calls_no_vendor(app, sqlite_engine, keyring, clock, world,
                                                                vendor, how):
    if how == "setting":
        _setting(sqlite_engine, "tools.dry_run", True, team_id=world.team)
    run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True} if how == "trigger" else {})
    text = "token ghp_" + "A" * 36 + " " + "x" * 3000
    response = tool(app, run, "post", "slack.post", {"channel": "C0REVIEW", "text": text})
    assert response.status_code == 200 and response.json()["dry_run"] is True
    assert vendor.requests == []
    [row] = calls(sqlite_engine)
    assert row.mode == "dry" and row.outcome == "ok"
    shown = row.detail["params"]["text"]
    assert "ghp_" not in shown and REDACTED in shown and len(shown) < 2100


@pytest.mark.parametrize("level,allowed", [("write", True), ("admin", True), ("read", False), (None, False)])
def test_permission_allows_only_write_or_admin(app, run, vendor, level, allowed):
    def answer(request):
        if request.url.path == f"/repos/{REPO}/collaborators/ana/permission":
            return httpx.Response(404) if level is None else httpx.Response(200, json={"permission": level})
        return vendor(request)

    app.app.state.vendor_transport = httpx.MockTransport(answer)
    out = tool(app, run, "permission", "github.permission", {"repo": REPO, "user": "ana"}).json()
    assert out == {"repo": REPO, "user": "ana", "permission": level or "none", "allowed": allowed}


def _diff_vendor(app, vendor, repo, number, patches):
    def answer(request):
        if request.url.path == f"/repos/{repo}/pulls/{number}":
            return httpx.Response(200, json={"title": "Tighten the card", "head": {"sha": "f" * 40}})
        if request.url.path == f"/repos/{repo}/pulls/{number}/files":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=[{"filename": f"f{i}.swift", "status": "modified", "additions": 1,
                                              "deletions": 0, "patch": p} for i, p in enumerate(patches)]
                                  if page == 1 else [])
        return vendor(request)

    app.app.state.vendor_transport = httpx.MockTransport(answer)


def test_pr_diff_reads_another_project_of_the_same_team_only(app, sqlite_engine, clock, run, vendor, world):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(projects).values(id=new_id(), team_id=world.team, name="ios", repo="Acme/Ios",
                                             created_at=clock()))
        conn.execute(insert(projects).values(id=new_id(), team_id=world.other, name="web", repo="Acme/Web",
                                             created_at=clock()))
    _diff_vendor(app, vendor, "Acme/Ios", 5, ["@@ -1 +1 @@\n-a\n+b"])
    out = tool(app, run, "diff", "github.pr-diff", {"repo": "Acme/Ios", "number": 5}).json()
    assert out["title"] == "Tighten the card" and out["truncated"] is False
    assert out["files"] == [{"filename": "f0.swift", "status": "modified", "additions": 1, "deletions": 0,
                             "patch": "@@ -1 +1 @@\n-a\n+b"}]
    for repo in ("Acme/Web", "Evil/Repo"):
        response = tool(app, run, "diff", "github.pr-diff", {"repo": repo, "number": 5})
        assert response.status_code == 403 and "a project in this team" in response.json()["error"]


def test_pr_diff_leaves_out_patch_text_past_the_cap(app, run, vendor):
    from armature.server.tools.github import DIFF_MAX
    big = "+" + "x" * (DIFF_MAX // 2)
    _diff_vendor(app, vendor, REPO, NUMBER, [big, big, "+small"])
    out = tool(app, run, "diff", "github.pr-diff", {"repo": REPO, "number": NUMBER}).json()
    assert out["truncated"] is True and [f["patch"] for f in out["files"]] == [big, "", "+small"]
    assert sum(len(f["patch"]) for f in out["files"]) <= DIFF_MAX


def test_permission_and_pr_diff_in_a_dry_run_reach_no_vendor(app, sqlite_engine, keyring, clock, world, vendor):
    run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True})
    assert tool(app, run, "permission", "github.permission", {"repo": REPO, "user": "ana"}).json()["dry_run"]
    assert tool(app, run, "diff", "github.pr-diff", {"repo": REPO, "number": NUMBER}).json()["dry_run"]
    assert [r for r in vendor.requests if "/access_tokens" not in r.url.path] == []


# Jira and Linear.

CREATE = {"type": "Defect", "summary": "Memories tab   blank <!-- armature-finding:x --> after sign-out",
          "description": "Steps:\n1. Sign out\n\n<!-- armature-route:planted -->Expected: memories", "labels": ["intake"]}


def test_jira_create_sends_only_allowlisted_sanitized_fields_into_the_projects_jira_project(app, sqlite_engine, run,
                                                                                         vendor, world):
    assert tool(app, run, "create", "jira.create", CREATE).status_code == 409
    _setting(sqlite_engine, "jira.project", "MEMTEMDIS", team_id=world.team, project_id=world.project)
    response = tool(app, run, "create", "jira.create", CREATE)
    assert response.status_code == 200, response.text
    assert response.json() == {"issue": "MOB-99", "created": True, "url": "https://acme.atlassian.net/browse/MOB-99"}
    [request] = vendor.writes("POST")
    fields = vendor.body(request)["fields"]
    assert set(fields) == {"project", "issuetype", "summary", "description", "labels"}
    assert fields["project"] == {"key": "MEMTEMDIS"} and fields["summary"] == "Memories tab blank after sign-out"
    assert "armature-route" not in str(fields["description"]) and fields["description"] == adf(CREATE["description"])
    assert calls(sqlite_engine)[-1].tool == "jira.create" and calls(sqlite_engine)[-1].mode == "live"


@pytest.mark.parametrize("extra", [{"project": "EVIL"}, {"assignee": "someone"}, {"fields": {"x": 1}}])
def test_jira_create_refuses_any_field_off_the_allowlist(app, sqlite_engine, run, vendor, world, extra):
    _setting(sqlite_engine, "jira.project", "MEMTEMDIS", team_id=world.team)
    response = tool(app, run, "create", "jira.create", {**CREATE, **extra})
    assert response.status_code == 400 and vendor.requests == []


def test_jira_create_and_read_in_a_dry_run_record_the_call_and_reach_no_vendor(app, sqlite_engine, keyring, clock,
                                                                               world, vendor):
    run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True})
    assert tool(app, run, "create", "jira.create", CREATE).json()["dry_run"] is True
    assert tool(app, run, "read", "jira.get-issue", {"issue": "MOB-12"}).json()["dry_run"] is True
    assert vendor.requests == []
    assert [(c.tool, c.mode) for c in calls(sqlite_engine)] == [("jira.create", "dry"), ("jira.get-issue", "dry")]


def test_jira_get_issue_returns_plain_text_and_hides_an_unreadable_issue(app, run, vendor):
    vendor.jira_issue = {"fields": {
        "summary": "Blank tab", "status": {"name": "In Progress"}, "issuetype": {"name": "Defect"},
        "components": [{"name": "Android"}], "labels": ["intake"],
        "description": adf("Steps\n1. Sign out\n\nExpected: memories"),
        "comment": {"comments": [{"author": {"displayName": "Ana"}, "body": adf("Also on iPad")}]}}}
    out = tool(app, run, "read", "jira.get-issue", {"issue": "MOB-12"}).json()
    assert out == {"issue": "MOB-12", "found": True, "summary": "Blank tab",
                   "description": "Steps\n1. Sign out\n\nExpected: memories", "status": "In Progress",
                   "type": "Defect", "components": ["Android"], "labels": ["intake"],
                   "comments": [{"author": "Ana", "body": "Also on iPad"}],
                   "url": "https://acme.atlassian.net/browse/MOB-12"}
    vendor.jira_issue = None
    assert tool(app, run, "read", "jira.get-issue", {"issue": "MOB-12"}).json() == {"issue": "MOB-12",
                                                                                 "found": False}


def test_jira_comment_goes_out_as_plain_adf(app, run, vendor):
    response = tool(app, run, "jira", "jira.comment", {"issue": "MOB-12", "body": "One *bold*\n\nTwo"})
    assert response.status_code == 200 and response.json()["commented"] is True
    [request] = vendor.requests
    assert request.url.path == "/rest/api/3/issue/MOB-12/comment"
    assert vendor.body(request)["body"] == adf("One *bold*\n\nTwo")
    assert [p["content"][0]["text"] for p in adf("One\n\nTwo")["content"]] == ["One", "Two"]


def test_adf_keeps_each_line_apart_with_hard_breaks():
    [first, second] = adf("Title\r\nhttps://x.test/1\n\n\nDone\n")["content"]
    assert first["content"] == [{"type": "text", "text": "Title"}, {"type": "hardBreak"},
                                {"type": "text", "text": "https://x.test/1"}]
    assert second["content"] == [{"type": "text", "text": "Done"}]
    assert adf("")["content"] == [{"type": "paragraph", "content": [{"type": "text", "text": "(empty)"}]}]


@pytest.mark.parametrize("params,status,reason", [
    ({"key_from": ["no key here"]}, None, "no issue key found"),
    ({"issue": "OPS-1", "projects": ["MOB"]}, None, "project not in projects"),
    ({"issue": "MOB-1", "from": "In Review"}, "In Progress", "source status is In Progress, not In Review"),
    ({"issue": "MOB-1", "to": "in progress"}, "In Progress", "already in target status"),
    ({"issue": "MOB-1", "to": "Done"}, "In Progress", "transition unavailable"),
])
def test_jira_transition_guards_are_data(app, run, vendor, params, status, reason):
    out = tool(app, run, "move", "jira.transition", {"to": "In Test", **params}).json()
    assert out["transitioned"] is False and out["reason"] == reason
    assert not [r for r in vendor.requests if r.method == "POST"]


def test_jira_transition_moves_a_defect_with_its_resolution_and_comments(app, run, vendor):
    vendor.jira_issue["fields"]["issuetype"]["name"] = "Defect"
    vendor.jira_transitions = [{"id": "41", "name": "Resolve Issue", "to": {"name": "Resolved"}}]
    out = tool(app, run, "move", "jira.transition",
               {"key_from": ["feature/MOB-77-thing"], "to": "In Test", "from": "In Progress",
                "defect_to": "Resolved", "defect_resolution": "Done", "comment": "Merged."}).json()
    assert out == {"issue": "MOB-77", "from": "In Progress", "to": "Resolved", "issue_type": "Defect",
                   "transitioned": True, "url": "https://acme.atlassian.net/browse/MOB-77"}
    move, note = [r for r in vendor.requests if r.method == "POST"]
    assert vendor.body(move) == {"transition": {"id": "41"}, "fields": {"resolution": {"name": "Done"}}}
    assert note.url.path.endswith("/MOB-77/comment")


def test_linear_comment_and_state(app, run, vendor):
    assert tool(app, run, "linear", "linear.comment", {"issue": "ENG-4", "body": "@all see"}).json() == {
        "issue": "ENG-4", "commented": True}
    sent = vendor.body(vendor.requests[-1])
    assert sent["variables"]["input"] == {"issueId": "uuid-1", "body": "@\u200ball see"}
    assert vendor.requests[-1].headers["authorization"] == "lin_api_0123456789abcdefghij"
    assert tool(app, run, "state", "linear.transition", {"issue": "ENG-4", "state": "in review"}).json()["moved"]
    unknown = tool(app, run, "state", "linear.transition", {"issue": "ENG-4", "state": "Shipped"}).json()
    assert unknown == {"issue": "ENG-4", "moved": False, "reason": "the team has no state named Shipped"}


# GitHub.

def test_check_run_is_created_then_updated_with_a_link_to_the_run(app, run, vendor):
    first = tool(app, run, "check", "github.check-run", {"repo": REPO, "head": HEAD, "name": "Build",
                                                          "status": "in_progress"})
    assert first.json()["created"] is True
    second = tool(app, run, "check", "github.check-run",
                  {"repo": REPO, "head": HEAD, "name": "Build", "conclusion": "success", "title": "Approved",
                   "summary": "@x ok"})
    assert second.json()["created"] is False and second.json()["status"] == "completed"
    post, patch = vendor.writes()
    assert vendor.body(post)["head_sha"] == HEAD
    assert vendor.body(post)["details_url"] == f"https://arm.test/#/run/{run.id}"
    assert patch.method == "PATCH" and vendor.body(patch)["output"]["summary"] == "@\u200bx ok"
    minted = [r for r in vendor.requests if r.url.path.endswith("/access_tokens")]
    assert vendor.body(minted[0]) == {"repositories": ["App"], "permissions": {"checks": "write"}}


@pytest.mark.parametrize("name", [None, "Armature review", " armature REVIEW "])
def test_no_step_can_write_the_reviewed_mark_through_check_run(app, run, vendor, name):
    params = {"repo": REPO, "head": HEAD, "conclusion": "success", **({"name": name} if name else {})}
    response = tool(app, run, "check", "github.check-run", params)
    assert response.status_code in (400, 403) and vendor.check_runs == []


def test_a_marked_comment_is_posted_once_then_edited(app, run, vendor):
    """ops test_pr_status: queued, then start, edits the same comment."""
    first = tool(app, run, "comment", "github.pr-comment",
                 {"repo": REPO, "number": NUMBER, "marker": "status", "body": "Armature queued a review."})
    second = tool(app, run, "comment", "github.pr-comment",
                  {"repo": REPO, "number": NUMBER, "marker": "status", "body": "Armature is reviewing."})
    assert first.json()["updated"] is False and second.json()["updated"] is True
    assert first.json()["comment_id"] == second.json()["comment_id"]
    post, patch = vendor.writes()
    assert post.method == "POST" and patch.url.path.endswith(f"/issues/comments/{first.json()['comment_id']}")
    assert vendor.issue_comments[0]["body"] == "<!-- armature-status -->\nArmature is reviewing."


def test_another_identitys_marked_comment_is_left_alone(app, run, vendor):
    """ops test_pr_status: an app identity posts its own comment over the owner's."""
    vendor.issue_comments = [{"id": 1, "user": {"login": "stephanheilner"}, "body": "<!-- armature-status -->\nold"}]
    out = tool(app, run, "comment", "github.pr-comment",
               {"repo": REPO, "number": NUMBER, "marker": "status", "body": "new"}).json()
    assert out["comment_id"] != 1 and vendor.issue_comments[0]["body"] == "<!-- armature-status -->\nold"


def test_a_deleted_marked_comment_is_posted_again(app, run, vendor):
    vendor.issue_comments = [{"id": 9, "user": {"login": BOT}, "body": "<!-- armature-status -->\nold"}]

    def gone(request):
        if request.method == "PATCH":
            vendor.issue_comments.clear()
        return vendor(request)
    app.app.state.vendor_transport = httpx.MockTransport(gone)
    out = tool(app, run, "comment", "github.pr-comment",
               {"repo": REPO, "number": NUMBER, "marker": "status", "body": "new"}).json()
    assert out["updated"] is False and out["comment_id"] != 9
    assert [r.method for r in vendor.writes()] == ["PATCH", "POST"]


def test_a_reply_goes_only_in_the_apps_own_thread(app, run, vendor):
    vendor.comments = [{"id": 101, "in_reply_to_id": None, "user": {"login": BOT}, "body": "ours"},
                       {"id": 201, "in_reply_to_id": None, "user": {"login": "mallory"}, "body": "theirs"}]
    ok = tool(app, run, "comment", "github.pr-comment",
              {"repo": REPO, "number": NUMBER, "body": "Fixed in abc.", "in_reply_to": 101})
    refused = tool(app, run, "comment", "github.pr-comment",
                   {"repo": REPO, "number": NUMBER, "body": "x", "in_reply_to": 201})
    assert ok.status_code == 200 and refused.status_code == 403


def test_open_pr_neuters_its_text_and_requests_reviewers(app, sqlite_engine, run, vendor):
    vendor.branches["fix-followup-1"] = HEAD
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run.id).values(head_sha=HEAD))
    out = tool(app, run, "open", "github.open-pr",
               {"repo": REPO, "head": "fix-followup-1", "base": "main", "title": "Follow\nup",
                "body": "cc @someone", "reviewers": [AUTHOR]}).json()
    assert out["number"] == 12 and out["requested_reviewers"] == [AUTHOR]
    opened, asked = vendor.writes()
    assert vendor.body(opened)["title"] == "Follow up" and vendor.body(opened)["body"] == "cc @\u200bsomeone"
    assert vendor.body(asked) == {"reviewers": [AUTHOR]}


# Push gate and git credentials.

def _push(app, run, **params):
    return tool(app, run, "push", "github.push", {"repo": REPO, "number": NUMBER, "branch": "feature",
                                                  "commit": FIX, **params})


def test_a_push_is_approved_for_an_allowed_author_on_the_pr_branch(app, sqlite_engine, world, run, vendor):
    _setting(sqlite_engine, "github.push_authors", [AUTHOR], team_id=world.team)
    out = _push(app, run).json()
    assert out["approved"] is True and out["rule"] == f"login:{AUTHOR}" and out["head"] == HEAD
    assert out["commit"] == FIX
    assert [(r.tool, r.outcome, r.target, r.detail["commit"]) for r in calls(sqlite_engine)] == [
        ("github.push", "approved", f"{REPO.lower()}#{NUMBER}", FIX)]
    assert _push(app, run, commit="not-a-sha").status_code == 400


def test_push_admission_locks_the_repos_project_row(app, sqlite_engine, world, run, vendor):
    from sqlalchemy import event
    from armature.server.db.schema import projects
    _setting(sqlite_engine, "github.push_authors", [AUTHOR], team_id=world.team)
    locked = []

    def seen(conn, clause, multiparams, params, execution_options):
        if getattr(clause, "_for_update_arg", None) is not None:
            locked.append((clause.get_final_froms()[0].name, clause.compile().params))
    event.listen(sqlite_engine, "before_execute", seen)
    try:
        assert _push(app, run).json()["approved"] is True
    finally:
        event.remove(sqlite_engine, "before_execute", seen)
    assert locked == [(projects.name, {"id_1": world.project})]


@pytest.mark.parametrize("dry", [False, True])
def test_a_tool_step_inside_a_called_workflow_gets_its_grant(app, sqlite_engine, keyring, clock, world, vendor, dry):
    from armature.server import defs
    from armature.server.db import new_id
    callee = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(defs.WORKFLOW.table).values(id=callee, name="reviewer", current_version=1,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", world.team).columns()))
        defs._add_version(conn, defs.WORKFLOW, callee, 1,
                          "name: reviewer\nstages:\n  - id: publish\n    adapter: slack/post\n", "seed", None,
                          clock())
        defs._add_version(conn, defs.WORKFLOW, world.workflow, 2,
                          "name: tools\nstages:\n  - id: review\n    call: {workflow: reviewer}\n", "call", None,
                          clock())
    run = start_run(sqlite_engine, keyring, clock, world, version=2, trigger_ref={"dry_run": True} if dry else {})
    response = ask_grant(app, run, "publish", tools=["slack.post"])
    assert response.status_code == 200 and response.json()["tools"] == ["slack.post"]
    out = use(app, response.json()["token"], "slack.post", SLACK).json()
    assert ask_grant(app, run, "review", tools=[]).status_code == 403
    if not dry:
        assert out["posted"] is True
        return
    assert out == {"dry_run": True, "tool": "slack.post", "params": SLACK} and vendor.requests == []
    [row] = calls(sqlite_engine)
    assert (row.run_id, row.step_id, row.mode, row.outcome) == (run.id, "publish", "dry", "ok")


@pytest.mark.parametrize("change,reason", [
    (lambda v: v.pr.update(state="closed"), "the pull request is not open"),
    (lambda v: v.pr["head"]["repo"].update(full_name="mallory/App"), "the pull request comes from a fork"),
    (lambda v: v.pr["head"].update(ref="main"), "the branch is not the pull request's head branch"),
    (lambda v: v.pr["user"].update(login="stranger"), "stranger is not on the push allowlist"),
])
def test_a_push_is_refused_from_githubs_answers(app, sqlite_engine, world, run, vendor, change, reason):
    _setting(sqlite_engine, "github.push_authors", [AUTHOR], team_id=world.team)
    change(vendor)
    out = _push(app, run).json()
    assert out == {"approved": False, "repo": REPO, "number": NUMBER, "branch": "feature", "reason": reason}
    assert calls(sqlite_engine)[0].outcome == "refused"


def test_push_allowlist_teams_codeowner_requests_and_excludes(app, sqlite_engine, keyring, clock, world, run,
                                                               vendor):
    _setting(sqlite_engine, "github.push_authors", ["team:mobile", "team:ios"], team_id=world.team)
    _setting(sqlite_engine, "github.push_exclude", [AUTHOR], team_id=world.team)
    vendor.members.add(("Acme", "mobile", AUTHOR))
    assert _push(app, run).json()["reason"] == f"{AUTHOR} is on the push exclude list"
    vendor.pr["requested_teams"] = [{"slug": "ios"}]
    assert _push(app, run).json()["rule"] == "codeowner-request:ios"
    vendor.pr["requested_teams"] = []
    with sqlite_engine.begin() as conn:
        conn.execute(settings.delete().where(settings.c.key == "github.push_exclude"))
    assert _push(app, start_run(sqlite_engine, keyring, clock, world)).json()["rule"] == "team:mobile"
    minted = [r for r in vendor.requests if r.url.path.endswith("/access_tokens")]
    assert vendor.body(minted[-1])["permissions"] == {"pull_requests": "read", "members": "read"}


def _member_asks(vendor):
    return [r for r in vendor.requests if "/memberships/" in r.url.path]


def test_an_org_team_entry_admits_a_member_and_remembers_for_ten_minutes(app, sqlite_engine, keyring, clock, world,
                                                                         run, vendor):
    _setting(sqlite_engine, "github.push_authors", ["team:Platform/core"], team_id=world.team)
    vendor.members.add(("Platform", "core", AUTHOR))
    assert _push(app, run).json()["rule"] == "team:Platform/core"
    [asked] = _member_asks(vendor)
    assert asked.url.path == f"/orgs/Platform/teams/core/memberships/{AUTHOR}"
    assert asked.headers["authorization"].startswith("Bearer ghs_")
    again = start_run(sqlite_engine, keyring, clock, world)
    assert _push(app, again).json()["approved"] is True and len(_member_asks(vendor)) == 1
    clock.advance(minutes=11)
    later = start_run(sqlite_engine, keyring, clock, world)
    assert _push(app, later).json()["approved"] is True and len(_member_asks(vendor)) == 2


def _branch_push(app, run, branch="armature/mob-1", commit=FIX):
    return tool(app, run, "push", "github.push", {"repo": REPO, "branch": branch, "commit": commit})


def _rerate(app, run):
    response = tool(app, run, "risk", "review.risk", {"repo": REPO, "number": NUMBER, "level": "low",
                                                      "expect_head": HEAD})
    assert response.status_code == 200, response.text


def test_development_pushes_its_branch_then_a_reviewed_fix_and_no_unreviewed_third(app, sqlite_engine, keyring,
                                                                                  clock, world, vendor):
    _setting(sqlite_engine, "github.push_authors", ["*"], team_id=world.team)
    run = start_run(sqlite_engine, keyring, clock, world, risk=None)
    branch = _branch_push(app, run).json()
    assert (branch["approved"], branch["rule"], branch["commit"]) == (True, "new branch", FIX)
    clock.advance(seconds=1)
    assert _push(app, run).json()["reason"] == "the run has no recorded risk"
    _rerate(app, run)
    clock.advance(seconds=1)
    assert _push(app, run, commit="e" * 40).json()["approved"] is True
    clock.advance(seconds=1)
    third = _push(app, run, commit="d" * 40).json()
    assert third["approved"] is False and third["reason"] == "the code this run last pushed was never re-reviewed"
    assert [(r.tool, r.outcome, r.target) for r in calls(sqlite_engine) if r.tool == "github.push"] == [
        ("github.push", "approved", f"{REPO.lower()}:armature/mob-1"),
        ("github.push", "refused", f"{REPO.lower()}#{NUMBER}"),
        ("github.push", "approved", f"{REPO.lower()}#{NUMBER}"),
        ("github.push", "refused", f"{REPO.lower()}#{NUMBER}")]


def test_a_run_pushes_at_most_three_times(app, sqlite_engine, keyring, clock, world, vendor):
    _setting(sqlite_engine, "github.push_authors", ["*"], team_id=world.team)
    run = start_run(sqlite_engine, keyring, clock, world)
    for n in range(3):
        _rerate(app, run)
        clock.advance(seconds=1)
        assert _push(app, run, commit=f"{n}" * 40).json()["approved"] is True
        clock.advance(seconds=1)
    _rerate(app, run)
    clock.advance(seconds=1)
    assert _push(app, run).json()["reason"] == "this run reached its cap of 3 pushes"
    assert _branch_push(app, run).json()["reason"] == "this run reached its cap of 3 pushes"


@pytest.mark.parametrize("branch, setup, reason", [
    ("feature/x", None, "a push without a pull request goes only to a new armature/ branch"),
    ("armature/old", lambda v: v.branches.update({"armature/old": HEAD}), "the branch already exists"),
    ("armature/pr", lambda v: v.branch_prs.add("armature/pr"), "a pull request already uses the branch"),
])
def test_a_new_branch_push_needs_a_new_armature_branch_without_a_pull_request(app, sqlite_engine, world, run,
                                                                              vendor, branch, setup, reason):
    if setup:
        setup(vendor)
    assert _branch_push(app, run, branch).json()["reason"] == reason


def test_a_new_branch_push_earns_a_write_token_for_its_repo(app, sqlite_engine, world, run, vendor):
    assert _branch_push(app, run).json()["approved"] is True
    token = grant(app, run, "push")
    response = app.post("/api/tools/git-credential", json={"access": "write", "host": "github.com",
                                                           "path": f"{REPO}.git"},
                        headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text


def test_an_org_team_entry_refuses_a_non_member(app, sqlite_engine, world, run, vendor):
    _setting(sqlite_engine, "github.push_authors", ["team:Platform/core"], team_id=world.team)
    out = _push(app, run).json()
    assert out["approved"] is False and out["reason"] == f"{AUTHOR} is not on the push allowlist"


def test_a_membership_api_error_fails_closed_and_is_not_remembered(app, sqlite_engine, world, run, vendor):
    _setting(sqlite_engine, "github.push_authors", ["team:Platform/core"], team_id=world.team)
    vendor.members.add(("Platform", "core", AUTHOR))
    vendor.fail = (f"/memberships/{AUTHOR}",)
    out = _push(app, run).json()
    assert out["approved"] is False and out["reason"] == f"{AUTHOR} is not on the push allowlist"
    vendor.fail = ()
    assert _push(app, run).json()["approved"] is True and len(_member_asks(vendor)) == 2


def test_a_push_is_refused_without_an_allowlist_or_over_the_cap(app, sqlite_engine, keyring, clock, world,
                                                                 vendor):
    run = start_run(sqlite_engine, keyring, clock, world)
    assert _push(app, run).json()["reason"] == "no push author allowlist is set"
    _setting(sqlite_engine, "github.push_authors", ["*"], team_id=world.team)
    _setting(sqlite_engine, "github.push_daily_cap", 2, team_id=world.team)
    pushes = [_push(app, start_run(sqlite_engine, keyring, clock, world)).json()["approved"] for _ in range(3)]
    assert pushes == [True, True, False]
    assert _push(app, start_run(sqlite_engine, keyring, clock, world)).json()["reason"] == \
        "the push cap of 2 a day is reached"
    assert [r.outcome for r in calls(sqlite_engine)][-4:] == ["approved", "approved", "refused", "refused"]
    clock.advance(days=1, seconds=1)
    assert _push(app, start_run(sqlite_engine, keyring, clock, world)).json()["approved"] is True


def test_a_pull_request_takes_at_most_six_pushes_a_day_across_runs(app, sqlite_engine, keyring, clock, world,
                                                                    vendor):
    _setting(sqlite_engine, "github.push_authors", ["*"], team_id=world.team)
    _setting(sqlite_engine, "github.push_daily_cap", 100, team_id=world.team)
    pushes = [_push(app, start_run(sqlite_engine, keyring, clock, world)).json() for _ in range(7)]
    assert [p["approved"] for p in pushes] == [True] * 6 + [False]
    assert pushes[-1]["reason"] == "the pull request reached its cap of 6 pushes a day"
    assert _branch_push(app, start_run(sqlite_engine, keyring, clock, world)).json()["approved"] is True
    clock.advance(days=1, seconds=1)
    assert _push(app, start_run(sqlite_engine, keyring, clock, world)).json()["approved"] is True


@pytest.mark.parametrize("level,approved", [("low", True), ("medium", True), ("high", False), (None, False)])
def test_a_push_needs_the_runs_recorded_risk_to_be_low_or_medium(app, sqlite_engine, keyring, clock, world,
                                                                 vendor, level, approved):
    _setting(sqlite_engine, "github.push_authors", ["*"], team_id=world.team)
    run = start_run(sqlite_engine, keyring, clock, world, risk=level, inputs={"risk": "low"})
    out = _push(app, run).json()
    assert out["approved"] is approved
    if not approved:
        assert out["reason"] == ("the run's risk is high" if level else "the run has no recorded risk")
        assert not [r for r in vendor.requests if r.url.path.endswith(f"/pulls/{NUMBER}")]


def test_a_projects_job_reaches_only_its_own_repo(app, sqlite_engine, keyring, clock, world, vendor):
    from armature.server.db import new_id
    from armature.server.db.schema import projects
    other = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(projects).values(id=other, team_id=world.team, name="web", repo="Acme/Web",
                                             created_at=clock()))
    run = start_run(sqlite_engine, keyring, clock, world, project=other)
    refused = tool(app, run, "comment", "github.pr-comment", {"repo": REPO, "number": NUMBER, "body": "hi"})
    assert refused.status_code == 403 and "this job's project" in refused.json()["error"]
    assert vendor.requests == []


def test_push_settings_come_from_the_repos_project(app, sqlite_engine, keyring, clock, world, vendor):
    _setting(sqlite_engine, "github.push_authors", ["nobody"], team_id=world.team)
    _setting(sqlite_engine, "github.push_authors", [AUTHOR], team_id=world.team, project_id=world.project)
    teamwide = start_run(sqlite_engine, keyring, clock, world, project=None)
    assert _push(app, teamwide).json()["rule"] == f"login:{AUTHOR}"


def test_without_the_members_permission_team_entries_match_nothing(app, sqlite_engine, world, run, vendor):
    _setting(sqlite_engine, "github.push_authors", ["team:mobile", AUTHOR], team_id=world.team)
    vendor.members.add(("Acme", "mobile", AUTHOR))
    real = app.app.state.vendor_transport

    def no_members(request):
        if request.url.path.endswith("/access_tokens") and "members" in vendor.body(request)["permissions"]:
            vendor.requests.append(request)
            return httpx.Response(422, json={"message": "permissions not granted"})
        return real.handle_request(request)
    app.app.state.vendor_transport = httpx.MockTransport(no_members)
    assert _push(app, run).json()["rule"] == f"login:{AUTHOR}"
    minted = [vendor.body(r)["permissions"] for r in vendor.requests if r.url.path.endswith("/access_tokens")]
    assert minted == [{"pull_requests": "read", "members": "read"}, {"pull_requests": "read"}]
    assert not [r for r in vendor.requests if "/memberships/" in r.url.path]
    _setting(sqlite_engine, "github.push_authors", ["team:mobile"], project_id=world.project, team_id=world.team)
    assert _push(app, run).json()["reason"] == f"{AUTHOR} is not on the push allowlist"


def test_a_workflow_with_an_agent_stage_gets_write_tools_only_with_an_agent_user(app, sqlite_engine, keyring,
                                                                                clock, world):
    from armature.server import defs
    body = ("name: tools\nstages:\n  - id: fix\n    agent: {model: sonnet, prompt: Fix it., access: write}\n"
            "  - id: push\n    adapter: github/push\n  - id: checkout\n    adapter: github/checkout\n")
    with sqlite_engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world.workflow, 2, body, "agent", None, clock())
    run = start_run(sqlite_engine, keyring, clock, world, version=2)

    def ask(step, **extra):
        return ask_grant(app, run, step, **extra)
    refused = ask("push")
    assert refused.status_code == 403 and "separate agent user" in refused.json()["error"]
    assert ask("push", agent_user="yes").status_code == 403
    assert ask("push", agent_user=True).status_code == 200
    assert ask("checkout").status_code == 200


def test_the_runner_holding_the_job_must_have_registered_same_user_for_a_push(app, sqlite_engine, keyring, clock,
                                                                             world):
    from armature.server import defs
    from armature.server.db.schema import runner_keys, runners
    body = ("name: tools\nstages:\n  - id: fix\n    agent: {model: sonnet, prompt: Fix it., access: write}\n"
            "  - id: push\n    adapter: github/push\n")
    with sqlite_engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world.workflow, 2, body, "agent", None, clock())
        conn.execute(insert(runner_keys).values(id="k0", team_id=None, name="host", key_hmac="h0",
                                                created_at=clock()))
        conn.execute(insert(runner_keys).values(id="k1", team_id=world.team, scope=world.team, name="mac",
                                                key_hmac="h1", created_at=clock()))
        for runner_id, key, platform in (("plain", "k0", {"kind": "local"}),
                                         ("mine", "k0", {"kind": "local", "same_user": True}),
                                         ("team", "k1", {"kind": "local", "same_user": True})):
            conn.execute(insert(runners).values(id=runner_id, runner_key_id=key, name=runner_id, platform=platform,
                                                created_at=clock(), last_seen_at=clock()))
    run = start_run(sqlite_engine, keyring, clock, world, version=2)

    def held_by(runner_id, status="running"):
        with sqlite_engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == run.job).values(runner_id=runner_id, status=status))
    assert ask_grant(app, run, "push", same_user=True).status_code == 403
    held_by("plain")
    assert ask_grant(app, run, "push").status_code == 403
    assert ask_grant(app, run, "push", same_user=True).status_code == 403
    held_by("team")
    assert ask_grant(app, run, "push").status_code == 403
    held_by("mine")
    assert ask_grant(app, run, "push").status_code == 200
    held_by("mine", status="lost")
    assert ask_grant(app, run, "push").status_code == 403


def _credential(app, token, access, path=f"/{REPO}.git", host="github.com"):
    return app.post("/api/tools/git-credential", json={"access": access, "host": host, "path": path},
                    headers={"Authorization": f"Bearer {token}"})


def test_checkout_gets_a_fresh_read_token_and_nothing_more(app, sqlite_engine, run, vendor):
    token = grant(app, run, "checkout")
    first = _credential(app, token, "read")
    second = _credential(app, token, "read")
    assert first.status_code == 200 and first.json()["username"] == "x-access-token"
    assert first.json()["password"] != second.json()["password"]
    minted = [vendor.body(r) for r in vendor.requests if r.url.path.endswith("/access_tokens")]
    assert minted[0] == {"repositories": ["App"], "permissions": {"contents": "read"}}
    assert _credential(app, token, "write").status_code == 403
    assert _credential(app, token, "read", host="gitlab.com").status_code == 403
    assert _credential(app, token, "read", path="/Evil/Repo.git").status_code == 403
    assert use(app, token, "github.checkout", {"repo": REPO}).status_code == 404
    rows = calls(sqlite_engine)
    assert [(r.tool, r.target, r.detail) for r in rows] == [
        ("github.git-credential", "acme/app", {"access": "read"})] * 2
    assert all(first.json()["password"] not in str(r.detail) for r in rows)


def test_a_write_token_needs_this_steps_approved_push(app, sqlite_engine, world, run, vendor):
    token = grant(app, run, "push")
    assert _credential(app, token, "write").status_code == 403
    _setting(sqlite_engine, "github.push_authors", ["*"], team_id=world.team)
    assert use(app, token, "github.push", PUSH).json()["approved"]
    written = _credential(app, token, "write")
    assert written.status_code == 200
    minted = [vendor.body(r) for r in vendor.requests if r.url.path.endswith("/access_tokens")]
    assert minted[-1] == {"repositories": ["App"], "permissions": {"contents": "write"}}


def test_a_dry_run_never_gets_a_write_token(app, sqlite_engine, keyring, clock, world, vendor):
    _setting(sqlite_engine, "github.push_authors", ["*"], team_id=world.team)
    run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True})
    token = grant(app, run, "push")
    assert use(app, token, "github.push", PUSH).json()["dry_run"]
    assert _credential(app, token, "write").status_code == 403
    assert _credential(app, token, "read").status_code == 200


def test_the_runs_end_revokes_its_git_tokens(app, sqlite_engine, clock, run, vendor):
    token = grant(app, run, "checkout")
    secret = _credential(app, token, "read").json()["password"]
    thread = broker.run_ended(run.id)
    thread.join(5)
    [revoked] = [r for r in vendor.requests if r.url.path == "/installation/token"]
    assert revoked.method == "DELETE" and revoked.headers["authorization"] == f"Bearer {secret}"
    assert broker.run_ended(run.id) is None


def test_a_gh_cli_connection_uses_the_gh_token_and_never_revokes_it(app, sqlite_engine, keyring, clock, world, run,
                                                                     vendor, gh_cli):
    out = tool(app, run, "permission", "github.permission", {"repo": REPO, "user": "ana"})
    assert out.status_code == 200, out.text
    assert vendor.requests[-1].headers["authorization"] == f"Bearer {GH_TOKEN}"
    on_runner(app, sqlite_engine, keyring, clock, world, run, {"kind": "local", "same_user": True})
    token = grant(app, run, "checkout")
    assert _credential(app, token, "read").json()["password"] == GH_TOKEN
    assert gh_cli == ["github.com"] and vendor.minted == 0
    assert broker.run_ended(run.id) is None
    assert not [r for r in vendor.requests if r.url.path == "/installation/token"]


def test_a_gh_cli_check_run_is_a_commit_status(app, run, vendor, gh_cli):
    out = tool(app, run, "check", "github.check-run", {"repo": REPO, "head": HEAD, "name": "lint",
                                                       "title": "Lint passed", "conclusion": "success"})
    assert out.status_code == 200, out.text
    [status] = vendor.statuses
    assert (status["sha"], status["context"], status["state"], status["description"]) == (
        HEAD, "lint", "success", "Lint passed")
    assert out.json()["check_run_id"] == status["id"] and vendor.check_runs == []
    tool(app, run, "check", "github.check-run", {"repo": REPO, "head": HEAD, "name": "lint", "status": "in_progress"})
    tool(app, run, "check", "github.check-run", {"repo": REPO, "head": HEAD, "name": "lint", "conclusion": "neutral"})
    assert [s["state"] for s in vendor.statuses] == ["success", "pending", "failure"]


@pytest.mark.parametrize("platform, team_key", [(None, False), ({"kind": "local"}, False),
                                                ({"kind": "local", "same_user": "true"}, False),
                                                ({"kind": "local", "same_user": True}, True)])
def test_a_gh_cli_token_never_goes_to_another_users_runner(app, sqlite_engine, keyring, clock, world, run, vendor,
                                                           gh_cli, platform, team_key):
    on_runner(app, sqlite_engine, keyring, clock, world, run, platform, team_key=team_key)
    refused = _credential(app, grant(app, run, "checkout"), "read")
    assert refused.status_code == 403 and "own OS user" in refused.json()["error"]
    assert gh_cli == []


def test_the_push_gate_and_a_write_token_work_on_a_gh_cli_connection(app, sqlite_engine, keyring, clock, world, run,
                                                                      vendor, gh_cli):
    on_runner(app, sqlite_engine, keyring, clock, world, run, {"kind": "local", "same_user": True})
    _setting(sqlite_engine, "github.push_authors", ["*"], team_id=world.team)
    token = grant(app, run, "push")
    assert use(app, token, "github.push", PUSH).json()["approved"]
    assert _credential(app, token, "write").json()["password"] == GH_TOKEN
    assert vendor.minted == 0


def test_the_writer_ends_a_runs_grants_and_tokens(sqlite_engine, clock, run, monkeypatch):
    ended = []
    monkeypatch.setattr(broker, "run_ended", ended.append)
    with sqlite_engine.begin() as conn:
        writer._apply_run(conn, run.id, writer.RunChange(end=("succeeded", clock(), None)))
    assert ended == [run.id]


def test_a_lost_run_loses_its_grants_and_tokens(app, sqlite_engine, clock, run, vendor, monkeypatch):
    token = grant(app, run, "post")
    ended = []
    monkeypatch.setattr(broker, "run_ended", ended.append)
    clock.advance(minutes=1)
    with sqlite_engine.begin() as conn:
        assert writer.sweep_lost(conn, clock(), timedelta(seconds=30)) == [run.id]
    assert ended == [run.id]
    assert use(app, token, "slack.post", SLACK).status_code == 401 and vendor.requests == []


# Sanitizer.

@pytest.mark.parametrize("text", [
    "```\n@team <img src=x onerror=1>",
    "~~~\n@team <img src=x onerror=1>\n~~~",
    "`` ` `` @team <img src=x onerror=1> `",
])
def test_markdown_neuters_mentions_and_html_inside_code_too(text):
    """The reviewer's bypasses: an unclosed fence, a tilde fence, and a lopsided code span."""
    out = sanitize.markdown(text)
    assert "@team" not in out and "<img" not in out and "@\u200bteam &lt;img" in out


def test_markdown_strips_markers():
    assert sanitize.markdown("Hi <!-- armature-status --> @x") == "Hi  @\u200bx"


def test_a_fence_outlasts_every_backtick_run():
    assert sanitize.fence("plain") == "```"
    assert sanitize.fence("a ```` b ` c") == "`````"
    assert sanitize.markdown("x" * 70_000).endswith("(truncated)") and len(sanitize.markdown("x" * 70_000)) == 60_000


def test_slack_text_cannot_link_or_mention():
    assert sanitize.slack("<!here> <@U1> <http://x|y> a&b") == "&lt;!here&gt; &lt;@U1&gt; &lt;http://x|y&gt; a&amp;b"

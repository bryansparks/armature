"""github.post-review through the broker, ported from ops tests/test_post_pr_review.py.

The ops script kept findings in state/pr-findings and ran with PR_REVIEW_DRY_RUN to print its payload. The tool
is stateless, so each test passes findings_state in and reads it back, and reads the payload GitHub received.
"""
from __future__ import annotations

from armature.server import sanitize
from armature.server.tools import review
from armature.server.tools.github import REPLY_MARKER
from tests.server.tool_support import (  # noqa: F401
    AUTHOR,
    BOT,
    GH_LOGIN,
    HEAD,
    NUMBER,
    REPO,
    app,
    clock,
    dns,
    gh_cli,
    marker,
    pem,
    start_run,
    tool,
    vendor,
    world,
)

PATH = "src/a.swift"
FID, OTHER = "0123456789ab", "ba9876543210"


def comment(claim, line, blocking=True, **extra):
    return {"path": PATH, "line": line, "severity": "high", "blocking": blocking, "body": f"{claim} is wrong.",
            "claim": claim, "raised_by": ["correctness"], **extra}


def key(c):
    return review.finding_key(c)


def posted(fid, comment_id=101, login=BOT, **extra):
    return {"id": comment_id, "in_reply_to_id": None, "user": {"login": login}, "body": marker(fid), **extra}


def post(app, run, *, comments=(), decisions=(), state=None, mode="review", verdict="comment", summary="s",
         **extra):
    params = {"repo": REPO, "number": NUMBER, "head": HEAD, "mode": mode, "verdict": verdict, "summary": summary,
              "comments": list(comments), "decisions": list(decisions), **extra}
    if state is not None:
        params["findings_state"] = state
    response = tool(app, run, "review", "github.post-review", params)
    assert response.status_code == 200, response.text
    return response.json()


def payload(vendor):
    [request] = [r for r in vendor.writes("POST") if r.url.path.endswith(f"/pulls/{NUMBER}/reviews")]
    return vendor.body(request)


def state_of(out):
    return {f["id"]: f for f in out["findings_state"]["findings"]}


def keep(fid, line, blocking=True, **extra):
    return {"id": fid, "path": PATH, "line": line, "disposition": "keep", "blocking": blocking, **extra}


def run_of(sqlite_engine, keyring, clock, world):
    return start_run(sqlite_engine, keyring, clock, world)


# Review mode.

def test_every_posted_finding_carries_its_marker(app, sqlite_engine, keyring, clock, world, vendor):
    c = comment("Force unwrap crashes on nil", 3)
    post(app, run_of(sqlite_engine, keyring, clock, world), comments=[c])
    assert marker(key(c)) in payload(vendor)["comments"][0]["body"]


def test_finding_already_posted_is_not_posted_again(app, sqlite_engine, keyring, clock, world, vendor):
    old, new = comment("Force unwrap crashes on nil", 3), comment("Retry loop never ends", 5)
    vendor.comments = [posted(key(old), body=f"Force unwrap crashes.\n\n{marker(key(old))}")]
    out = post(app, run_of(sqlite_engine, keyring, clock, world), comments=[old, new])
    bodies = [c["body"] for c in payload(vendor)["comments"]]
    assert len(bodies) == 1 and marker(key(new)) in bodies[0]
    assert out["repeated_findings"] == 1
    assert state_of(out)[key(old)]["comment_id"] == 101


def test_lead_supplied_id_dedups_a_reworded_claim(app, sqlite_engine, keyring, clock, world, vendor):
    vendor.comments = [posted(FID)]
    reworded = comment("The force unwrap on line 3 crashes when nil", 3, finding_id=FID)
    post(app, run_of(sqlite_engine, keyring, clock, world), comments=[reworded],
         state={"findings": [keep(FID, 3)]})
    assert "comments" not in payload(vendor)


def test_marker_from_someone_else_does_not_count(app, sqlite_engine, keyring, clock, world, vendor):
    c = comment("Force unwrap crashes on nil", 3)
    vendor.comments = [posted(key(c), login="mallory")]
    post(app, run_of(sqlite_engine, keyring, clock, world), comments=[c])
    assert len(payload(vendor)["comments"]) == 1


def test_finding_posted_again_when_its_code_changed(app, sqlite_engine, keyring, clock, world, vendor):
    c = comment("Force unwrap crashes on nil", 3, code_hash="fresh")
    vendor.comments = [posted(key(c))]
    post(app, run_of(sqlite_engine, keyring, clock, world), comments=[c],
         state={"findings": [keep(key(c), 3, code_hash="stale")]})
    assert len(payload(vendor)["comments"]) == 1


def test_planted_marker_is_stripped(app, sqlite_engine, keyring, clock, world, vendor):
    c = comment("Force unwrap crashes on nil", 3, fix="Guard it. <!-- armature-finding:ffffffffffff -->")
    c["body"] += " <!--armature-finding:eeeeeeeeeeee-->"
    post(app, run_of(sqlite_engine, keyring, clock, world), comments=[c],
         summary="s <!-- armature-finding:dddddddddddd -->")
    sent = payload(vendor)
    assert review.FINDING_MARKER.findall(sent["comments"][0]["body"]) == [key(c)]
    assert "armature-finding" not in sent["body"]


def test_finding_key_ignores_roles():
    assert key(comment("Crash", 3, raised_by=["a"])) == key(comment("crash!", 3, raised_by=["b", "c"]))


def test_unmentioned_open_finding_is_resolved(app, sqlite_engine, keyring, clock, world, vendor):
    decisions = [{"claim": "c", "raised_by": ["x"], "decision": "suggest", "reason": "r", "disposition": "keep",
                  "finding_id": OTHER}]
    out = post(app, run_of(sqlite_engine, keyring, clock, world), verdict="approve", summary="",
               decisions=decisions, state={"findings": [keep(FID, 3), keep(OTHER, 5)]})
    assert {fid: f["disposition"] for fid, f in state_of(out).items()} == {FID: "resolved", OTHER: "keep"}


def test_merged_review_posts_repeated_findings(app, sqlite_engine, keyring, clock, world, vendor):
    c = comment("Force unwrap crashes on nil", 3)
    vendor.comments = [posted(key(c))]
    vendor.pr["merged"] = True
    out = post(app, run_of(sqlite_engine, keyring, clock, world), comments=[c])
    assert len(payload(vendor)["comments"]) == 1 and out["followup"] is True
    assert payload(vendor)["event"] == "COMMENT"


def test_merged_with_nothing_blocking_posts_nothing(app, sqlite_engine, keyring, clock, world, vendor):
    vendor.pr["merged"] = True
    out = post(app, run_of(sqlite_engine, keyring, clock, world), verdict="approve")
    assert out["posted"] is False and out["merged"] is True and vendor.writes() == []


def test_unreadable_earlier_findings_still_post(app, sqlite_engine, keyring, clock, world, vendor):
    vendor.fail = (f"/pulls/{NUMBER}/comments",)
    post(app, run_of(sqlite_engine, keyring, clock, world), comments=[comment("Crash", 3)])
    assert len(payload(vendor)["comments"]) == 1


def test_app_identity_comments_on_its_own_pull_request_and_approves_others(app, sqlite_engine, keyring, clock,
                                                                           world, vendor):
    vendor.comments = [posted(key(comment("Crash", 3)))]
    vendor.pr["user"]["login"] = BOT
    post(app, run_of(sqlite_engine, keyring, clock, world), verdict="approve")
    own = payload(vendor)
    vendor.requests.clear()
    vendor.pr["user"]["login"] = AUTHOR
    post(app, run_of(sqlite_engine, keyring, clock, world), verdict="approve")
    other = payload(vendor)
    vendor.requests.clear()
    repeat = post(app, run_of(sqlite_engine, keyring, clock, world), verdict="approve",
                  comments=[comment("Crash", 3)])
    assert own["event"] == "COMMENT" and other["event"] == "APPROVE" and repeat["repeated_findings"] == 1
    assert not any(r.url.path == "/user" for r in vendor.requests)


def test_rebutted_finding_recorded_as_dropped(app, sqlite_engine, keyring, clock, world, vendor):
    decisions = [{"claim": "lint", "raised_by": ["conventions"], "decision": "drop", "reason": "r",
                  "disposition": "drop_rebutted", "discussion_basis": "4289032185", "finding_id": FID}]
    out = post(app, run_of(sqlite_engine, keyring, clock, world), verdict="approve", summary="",
               decisions=decisions, state={"findings": [keep(FID, 3, blocking=False)]})
    assert state_of(out)[FID]["disposition"] == "drop_rebutted"


def test_a_line_outside_the_diff_goes_in_the_body_and_a_truncated_path_is_repaired(app, sqlite_engine, keyring,
                                                                                  clock, world, vendor):
    inside = {**comment("Crash", 3), "path": "a.swift"}
    outside = comment("Leak", 40)
    out = post(app, run_of(sqlite_engine, keyring, clock, world), comments=[inside, outside])
    sent = payload(vendor)
    assert [c["path"] for c in sent["comments"]] == [PATH]
    assert f"`{PATH}:40`" in sent["body"] and "could not be anchored" in sent["body"]
    assert out["inline_comments"] == 1 and out["orphaned_comments"] == 1


def test_a_suggestion_is_one_click_only_when_its_whole_range_is_in_the_diff(app, sqlite_engine, keyring, clock,
                                                                           world, vendor):
    whole = comment("Crash", 4, start_line=2, suggestion="```swift\nguard let x else { return }\n```")
    partial = comment("Leak", 1, start_line=0, suggestion="let y = @z <b>")
    post(app, run_of(sqlite_engine, keyring, clock, world), comments=[whole, partial])
    bodies = {c["line"]: c for c in payload(vendor)["comments"]}
    assert bodies[4]["start_line"] == 2 and "```suggestion\nguard let x else { return }\n```" in bodies[4]["body"]
    assert "start_line" not in bodies[1] and "```\nlet y = @\u200bz &lt;b>\n```" in bodies[1]["body"]


def test_a_suggestion_cannot_close_its_fence_early(app, sqlite_engine, keyring, clock, world, vendor):
    escape = comment("Crash", 4, suggestion="x = 1\n````\n@team <img src=x>\n````suggestion\ny = 2")
    post(app, run_of(sqlite_engine, keyring, clock, world), comments=[escape])
    body = payload(vendor)["comments"][0]["body"]
    assert body.startswith("Crash is wrong.\n\n`````suggestion\nx = 1\n````\n")
    assert body.split("\n\n<!--")[0].endswith("y = 2\n`````")


def test_mentions_and_html_in_finding_text_are_neutered_everywhere(app, sqlite_engine, keyring, clock, world,
                                                                  vendor):
    c = comment("Crash", 3, body="cc @octocat <img src=x> see `@keep <tag>`")
    post(app, run_of(sqlite_engine, keyring, clock, world), comments=[c], summary="@team please <b>look</b>")
    sent = payload(vendor)
    assert "@\u200boctocat &lt;img src=x>" in sent["comments"][0]["body"]
    assert "`@\u200bkeep &lt;tag>`" in sent["comments"][0]["body"]
    assert "@\u200bteam please &lt;b>look&lt;/b>" in sent["body"]


def test_the_review_body_stays_under_githubs_limit(app, sqlite_engine, keyring, clock, world, vendor):
    post(app, run_of(sqlite_engine, keyring, clock, world), summary="x" * 59_000,
         comments=[comment(f"Leak {n}", 40, body="y" * 3_000) for n in range(5)])
    assert len(payload(vendor)["body"]) < 65_536


def test_blockers_hold_back_an_approval_and_are_neutered(app, sqlite_engine, keyring, clock, world, vendor):
    out = post(app, run_of(sqlite_engine, keyring, clock, world), verdict="approve",
               blockers=["the security review did not complete", "@team see [x](https://e.vil) <b>"])
    sent = payload(vendor)
    assert sent["event"] == "COMMENT"
    assert ("Not approved: the security review did not complete; @\u200bteam see x &lt;b>." in sent["body"])
    assert out["findings_state"]["approved_head"] == ""


# Discuss mode.

def discussion(vendor):
    vendor.comments = [
        posted(FID, 101),
        {"id": 102, "in_reply_to_id": 101, "user": {"login": AUTHOR}, "body": "SwiftFormat needs it."},
        posted(OTHER, 103),
        {"id": 104, "in_reply_to_id": 103, "user": {"login": AUTHOR}, "body": "Disagree."},
    ]
    return {"head": HEAD, "findings": [keep(FID, 3), keep(OTHER, 5, blocking=False)]}


def drop(disposition="drop_rebutted", reason="Right."):
    return {"claim": "c", "raised_by": ["x"], "decision": "drop", "disposition": disposition, "reason": reason,
            "discussion_basis": "102", "finding_id": FID}


def discuss(app, sqlite_engine, keyring, clock, world, state, *decisions, **extra):
    return post(app, run_of(sqlite_engine, keyring, clock, world), mode="discuss", verdict="approve", summary="",
                decisions=decisions, state=state, **extra)


def replies(vendor):
    return [(r.url.path, vendor.body(r)["body"]) for r in vendor.writes("POST") if r.url.path.endswith("/replies")]


def test_dropped_finding_gets_a_thread_reply_and_a_recommendation(app, sqlite_engine, keyring, clock, world, vendor):
    state = discussion(vendor)
    out = discuss(app, sqlite_engine, keyring, clock, world, state,
                  drop(reason="@jstutz4 is right:\nCI's SwiftFormat requires the doc comment."))
    [(path, body)] = replies(vendor)
    assert path.endswith("/comments/101/replies")
    assert body.startswith("Agreed, dropping this:\n\n> @\u200bjstutz4 is right: CI")
    # A rebuttal earns a recommendation, not an approval, by default.
    assert out["approve_event"] == "COMMENT" and "Approval Recommended" in payload(vendor)["body"]
    assert state_of(out)[FID]["disposition"] == "drop_rebutted"


def test_resolved_blocker_is_still_only_a_recommendation(app, sqlite_engine, keyring, clock, world, vendor):
    out = discuss(app, sqlite_engine, keyring, clock, world, discussion(vendor), drop("resolved"))
    assert out["approve_event"] == "COMMENT"


def test_stored_thread_used_when_comments_unreadable(app, sqlite_engine, keyring, clock, world, vendor):
    vendor.fail = (f"/pulls/{NUMBER}/comments",)
    out = discuss(app, sqlite_engine, keyring, clock, world,
                  {"head": HEAD, "findings": [keep(FID, 3, comment_id=101)]}, drop())
    vendor.fail = ()
    assert len(out["replies"]) == 1 and out["replies"][0]["in_reply_to"] == 101
    assert state_of(out)[FID]["disposition"] == "drop_rebutted"


def test_no_thread_leaves_the_finding_alone(app, sqlite_engine, keyring, clock, world, vendor):
    vendor.fail = (f"/pulls/{NUMBER}/comments",)
    out = discuss(app, sqlite_engine, keyring, clock, world, {"head": HEAD, "findings": [keep(FID, 3)]}, drop())
    assert out["replies"] == [] and out["approved"] is False
    assert state_of(out)[FID]["disposition"] == "keep"


def test_rebuttal_approves_when_configured(app, sqlite_engine, keyring, clock, world, vendor):
    for disposition in ("drop_rebutted", "resolved"):
        out = discuss(app, sqlite_engine, keyring, clock, world, discussion(vendor), drop(disposition),
                      approve_on_rebuttal=True)
        assert out["approve_event"] == "APPROVE"


def test_no_approval_on_a_head_the_review_did_not_judge(app, sqlite_engine, keyring, clock, world, vendor):
    state = {**discussion(vendor), "head": "b" * 40}
    out = discuss(app, sqlite_engine, keyring, clock, world, state, drop("resolved"))
    assert len(out["replies"]) == 1 and out["approve_event"] is None


def test_failed_reply_keeps_the_finding_open(app, sqlite_engine, keyring, clock, world, vendor):
    state = discussion(vendor)
    vendor.fail = ("/replies",)
    out = discuss(app, sqlite_engine, keyring, clock, world, state, drop("resolved"))
    assert out["failed_replies"] == [FID] and out["approved"] is False
    assert state_of(out)[FID]["disposition"] == "keep"


def test_failed_approval_still_returns_the_state(app, sqlite_engine, keyring, clock, world, vendor):
    state = discussion(vendor)
    state["findings"][0]["comment_id"] = 101
    vendor.fail = (f"/pulls/{NUMBER}/reviews",)
    out = discuss(app, sqlite_engine, keyring, clock, world, state, drop("resolved"), approve_on_rebuttal=True)
    assert len(out["replies"]) == 1 and out["approve_error"] is not None
    assert "upstream body" not in out["approve_error"]
    assert state_of(out)[FID]["disposition"] == "resolved"
    assert out["findings_state"].get("approved_head") != HEAD


def test_quoted_reason_is_inert():
    text = sanitize.quoted("See [docs](https://x.y) ![i](https://x.y/i.png) "
                           "<!-- armature-finding:0123456789ab --> <img src=x> @team #12 " + "z" * 500)
    assert "<" not in text and "](" not in text and "See docs i" in text
    assert "@\u200bteam" in text and "#\u200b12" in text and "armature-finding" not in text
    assert len(text) <= 2 + 400 + 200


def test_kept_finding_answers_a_new_rebuttal(app, sqlite_engine, keyring, clock, world, vendor):
    state = discussion(vendor)
    out = discuss(app, sqlite_engine, keyring, clock, world, state,
                  {"claim": "c", "raised_by": ["x"], "decision": "suggest", "disposition": "keep",
                   "reason": "The retry still loops when the token is nil.", "discussion_basis": "104",
                   "finding_id": OTHER})
    assert [r["in_reply_to"] for r in out["replies"]] == [103]
    assert out["replies"][0]["body"].startswith("Keeping this:") and out["approve_event"] is None


def test_no_reply_without_an_armature_thread(app, sqlite_engine, keyring, clock, world, vendor):
    vendor.comments = [posted(FID, 201, login="mallory")]
    out = discuss(app, sqlite_engine, keyring, clock, world, {"head": HEAD, "findings": [keep(FID, 3)]},
                  {"claim": "c", "raised_by": ["x"], "decision": "drop", "disposition": "resolved",
                   "reason": "Fixed.", "discussion_basis": "a.swift:3", "finding_id": FID})
    assert out["replies"] == [] and replies(vendor) == []


# A gh CLI connection posts as a person, so only a marker says what Armature wrote.

def test_a_pr_the_gh_login_authored_is_still_reviewed(app, sqlite_engine, keyring, clock, world, vendor, gh_cli):
    vendor.pr["user"] = {"login": GH_LOGIN}
    out = post(app, run_of(sqlite_engine, keyring, clock, world), verdict="approve")
    assert out["posted"] is True and out["self_authored"] is True
    assert payload(vendor)["event"] == "COMMENT" and out["findings_state"]["approved_head"] == HEAD


def test_the_gh_logins_own_reply_counts_as_an_answer(app, sqlite_engine, keyring, clock, world, vendor, gh_cli):
    vendor.login = GH_LOGIN
    vendor.comments = [posted(FID, 101, login=GH_LOGIN),
                       {"id": 102, "in_reply_to_id": 101, "user": {"login": GH_LOGIN}, "body": "SwiftFormat needs it."}]
    kept = {**drop(), "decision": "keep", "disposition": "keep"}
    out = discuss(app, sqlite_engine, keyring, clock, world, {"head": HEAD, "findings": [keep(FID, 3)]}, kept)
    [(path, body)] = replies(vendor)
    assert path.endswith("/comments/101/replies") and body.startswith("Keeping this:") and body.endswith(REPLY_MARKER)
    assert len(out["replies"]) == 1
    again = discuss(app, sqlite_engine, keyring, clock, world, {"head": HEAD, "findings": [keep(FID, 3)]}, kept)
    assert again["replies"] == []


def test_a_gh_login_comment_without_a_marker_is_not_armatures(app, sqlite_engine, keyring, clock, world, vendor,
                                                               gh_cli):
    vendor.comments = [{"id": 101, "in_reply_to_id": None, "user": {"login": GH_LOGIN}, "body": "by hand"},
                       posted(FID, 102, login=GH_LOGIN)]
    run = run_of(sqlite_engine, keyring, clock, world)
    params = {"repo": REPO, "number": NUMBER, "body": "Fixed."}
    assert tool(app, run, "comment", "github.pr-comment", {**params, "in_reply_to": 101}).status_code == 403
    assert tool(app, run, "comment", "github.pr-comment", {**params, "in_reply_to": 102}).status_code == 200
    [(_, body)] = replies(vendor)
    assert body.endswith(REPLY_MARKER)

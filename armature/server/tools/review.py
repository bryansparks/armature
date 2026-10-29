"""github.post-review: the lead's decision as one GitHub review, ported from ops post_pr_review.py.

It keeps no state. The step passes the last ``findings_state`` in and stores
the one this returns. Every posted finding carries a hidden marker with a
stable id, and the markers already on the pull request decide what is posted
again. A discuss run posts no review. It replies in the app's own threads of
findings the lead now drops, and approves once nothing blocks.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from armature.server import sanitize
from armature.server.tools import Call
from armature.server.tools.github import (
    REVIEW,
    Api,
    api,
    login_of,
    me,
    ours,
    reply_body,
)
from armature.server.vendors import VendorError

FOLLOWUP_BRANCH = re.compile(r"(^|-)followup-[0-9]+$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
FINDING_MARKER = re.compile(r"<!--\s*armature-finding:([0-9a-f]{12})\s*-->")
FINDING_ID = re.compile(r"^[0-9a-f]{12}$")
BLOCKER_MAX = 200
# GitHub refuses a review or comment body of 65,536 characters or more.
BODY_MAX = 65_000
APPROVAL = "## :thumbsup: Approval Recommended"
CHANGES = "## :construction: Changes Recommended"


def commentable_lines(files: list[dict[str, Any]]) -> dict[str, set[int]]:
    """New-file line numbers GitHub accepts a RIGHT-side comment on.

    A comment on a line outside the diff makes the whole review 422, taking the
    good comments down with the bad one, so every finding is checked first.
    """
    out: dict[str, set[int]] = {}
    for f in files:
        patch = f.get("patch")
        if not patch or not isinstance(f.get("filename"), str):
            continue
        lines: set[int] = set()
        new_no = 0
        for raw in patch.split("\n"):
            m = HUNK.match(raw)
            if m:
                new_no = int(m.group(1))
                continue
            if raw.startswith("-") or raw.startswith("\\"):
                continue
            lines.add(new_no)
            new_no += 1
        out[f["filename"]] = lines
    return out


def suggestion_text(c: dict[str, Any]) -> str:
    suggestion = c.get("suggestion")
    if not (isinstance(suggestion, str) and suggestion.strip()):
        return ""
    # A fence the model added anyway would nest and render as text, and the suggestion button would never appear.
    text = suggestion.strip("\n")
    if text.lstrip().startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:])
    return text


def is_blocking(c: dict[str, Any]) -> bool:
    return c.get("blocking", True) is not False


def finding_key(c: dict[str, Any]) -> str:
    """Stable across runs: the path and the claim without punctuation or case, never the roles that raised it."""
    claim = re.sub(r"[^a-z0-9]+", " ", str(c.get("claim") or c.get("body") or "").lower()).strip()
    return hashlib.sha1(f"{c.get('path') or ''}\n{claim}".encode()).hexdigest()[:12]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _inline(value: Any) -> str:
    return re.sub(r"[`\r\n]", "", str(value if value is not None else "?"))[:300]


def finding_body(c: dict[str, Any], anchored: bool) -> str:
    """The finding, then the change: a one-click suggestion where GitHub can apply it, else plain code and steps."""
    body = sanitize.markdown(_text(c.get("body"))).strip()
    parts = [body if is_blocking(c) else f"**Non-blocking:** {body}"]
    text = sanitize.unmarked(suggestion_text(c))
    if text and anchored:
        # The one fence whose text stays as written, so the suggestion applies. It outlasts any backticks inside.
        mark = sanitize.fence(text)
        parts.append(f"{mark}suggestion\n{text}\n{mark}")
    elif text:
        # A suggestion anchored to a narrower range than it replaces would commit the wrong lines.
        first, last = c.get("start_line"), c.get("line")
        span = f" (replaces lines {first}-{last})" if isinstance(first, int) and isinstance(last, int) else ""
        shown = sanitize.markdown(text)
        mark = sanitize.fence(shown)
        parts.append(f"**Recommended change**{span}:\n\n{mark}\n{shown}\n{mark}")
    fix = sanitize.markdown(_text(c.get("fix"))).strip()
    if fix:
        parts.append(f"**How to fix:**\n\n{fix}")
    body = sanitize.capped("\n\n".join(p for p in parts if p), BODY_MAX)
    if FINDING_ID.fullmatch(str(c.get("finding_id") or "")):
        body += f"\n\n<!-- armature-finding:{c['finding_id']} -->"
    return body


def repair_path(path: str, files: dict[str, set[int]]) -> str:
    """The pull request file a truncated path names, when exactly one ends with it."""
    if path in files:
        return path
    tail = "/" + path.lstrip("/")
    matches = [f for f in files if f.endswith(tail)]
    return matches[0] if len(matches) == 1 else path


def posted_findings(gh: Api, number: int, own: Callable[[Any], bool]
                    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Finding id to the newest thread root this app posted it in, plus every review comment.

    An id found only in a review body maps to {}. Unreadable comments read as none, since a repeated finding costs
    less than a review that never posts.
    """
    try:
        comments = gh.pages(f"/repos/{gh.repo}/pulls/{number}/comments")
        reviews = gh.pages(f"/repos/{gh.repo}/pulls/{number}/reviews")
    except VendorError:
        return {}, []
    roots: dict[str, dict[str, Any]] = {}
    for r in reviews:
        if own(r):
            for fid in FINDING_MARKER.findall(r.get("body") or ""):
                roots.setdefault(fid, {})
    for c in comments:
        if not own(c) or c.get("in_reply_to_id"):
            continue
        m = FINDING_MARKER.search(c.get("body") or "")
        if m and (c.get("id") or 0) >= (roots.get(m.group(1)) or {}).get("id", 0):
            roots[m.group(1)] = c
    return roots, comments


def answered_since(root: dict[str, Any], comments: list[dict[str, Any]], own: Callable[[Any], bool]) -> bool:
    """Someone other than this app replied in the thread after it last did."""
    thread = [c for c in comments if c.get("in_reply_to_id") == root.get("id")]
    replied = [c.get("id") or 0 for c in thread if own(c)]
    last = max(replied + [root.get("id") or 0])
    return any((c.get("id") or 0) > last for c in thread if not own(c))


def _records(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(f) for f in state.get("findings") or [] if isinstance(f, dict) and f.get("id")]


def _blockers(params: dict[str, Any]) -> list[str]:
    return [sanitize.inert(b, BLOCKER_MAX) for b in params.get("blockers") or [] if isinstance(b, str) and b]


def post(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    repo, number = params["repo"], int(params["number"])
    with api(call, repo, REVIEW) as gh:
        pr = gh.json("GET", f"/repos/{repo}/pulls/{number}")
        if (params.get("mode") or "review") == "discuss":
            return discuss(call, gh, params, pr)
        return review(call, gh, params, pr)


def review(call: Call, gh: Api, params: dict[str, Any], pr: dict[str, Any]) -> dict[str, Any]:
    repo, number, head = params["repo"], int(params["number"]), params["head"]
    mine = me(call)
    author = login_of(pr)
    self_authored = mine == author and mine != ""
    merged = pr.get("merged") is True
    branch = str((pr.get("head") or {}).get("ref") or "")
    blockers = _blockers(params)
    verdict = params.get("verdict") or "comment"
    summary = sanitize.markdown(_text(params.get("summary"))).strip()
    findings = [c for c in params.get("comments") or [] if isinstance(c, dict)]
    decisions = [d for d in params.get("decisions") or [] if isinstance(d, dict)]
    state = params.get("findings_state") or {}
    known = {f["id"]: f for f in _records(state)}
    on_pr, _ = posted_findings(gh, number, ours(call))
    allowed = commentable_lines(gh.pages(f"/repos/{repo}/pulls/{number}/files"))

    inline, orphaned, records = [], [], {}
    repeated = 0
    for c in findings:
        path, line = repair_path(str(c.get("path") or ""), allowed), c.get("line")
        c = {**c, "path": path}
        given = str(c.get("finding_id") or "")
        fid = given if FINDING_ID.fullmatch(given) and (given in known or given in on_pr) else finding_key(c)
        c["finding_id"] = fid
        digest = _text(c.get("code_hash"))
        before = known.get(fid) or {}
        records[fid] = {**before, "id": fid, "path": path, "line": line, "start_line": c.get("start_line"),
                        "claim": _text(c.get("claim")), "raised_by": c.get("raised_by") or [],
                        "blocking": is_blocking(c), "severity": c.get("severity"), "body": _text(c.get("body")),
                        "code_hash": digest, "disposition": "keep", "head": head,
                        "comment_id": (on_pr.get(fid) or {}).get("id") or before.get("comment_id")}
        # Already on the pull request with the same lines. After a merge every finding posts, for the follow-up.
        if not merged and fid in on_pr and (not before.get("code_hash") or not digest
                                            or before["code_hash"] == digest):
            repeated += 1
            continue
        ok_lines = allowed.get(path, set())
        if not (isinstance(line, int) and line in ok_lines):
            orphaned.append(c)
            continue
        comment: dict[str, Any] = {"path": path, "line": line, "side": "RIGHT"}
        start = c.get("start_line")
        anchored = True
        if isinstance(start, int) and start < line:
            # GitHub rejects the whole review if either end of a multi-line comment is outside the diff.
            if start in ok_lines:
                comment["start_line"], comment["start_side"] = start, "RIGHT"
            else:
                anchored = False
        comment["body"] = finding_body(c, anchored)
        inline.append(comment)

    for d in decisions:
        before = known.get(d.get("finding_id") or "")
        if before and d.get("disposition") in ("drop_rebutted", "resolved") and d["finding_id"] not in records:
            records[d["finding_id"]] = {**before, "disposition": d["disposition"],
                                        "discussion_basis": d.get("discussion_basis") or "",
                                        "reason": d.get("reason") or ""}

    orphan_block = ""
    if orphaned:
        # A finding whose line moved is said here, at the location the lead gave, never dropped.
        rows = "\n\n".join(f"#### `{_inline(c.get('path'))}:{_inline(c.get('line'))}` "
                           f"({_inline(c.get('severity') or '?')})\n\n" + finding_body(c, anchored=False)
                           for c in orphaned)
        orphan_block = "\n\n### Findings that could not be anchored to a changed line\n\n" + rows

    # Code, not the model, decides: any blocking finding means changes, and so does the lead's own verdict.
    blocking = [c for c in findings if is_blocking(c)]
    decided = {d.get("finding_id") for d in decisions}
    optional = "\n\nThe non-blocking comments are optional." if len(blocking) < len(findings) else ""
    refused = ("\n\nNot approved: " + "; ".join(blockers) + ".") if blockers else ""
    changes = bool(blocking) or verdict != "approve"
    chained = FOLLOWUP_BRANCH.search(branch) is not None
    followup = merged and bool(blocking) and not chained

    def persist(review_url: str = "", ids: dict[str, Any] | None = None, approved: bool = False) -> dict[str, Any]:
        for fid, comment_id in (ids or {}).items():
            if fid in records:
                records[fid]["comment_id"] = comment_id
        # A full review of this head that neither restated nor judged an open finding did not find it again.
        untouched = [{**f, "disposition": "resolved", "reason": "not raised again by a full review"}
                     if f.get("disposition") == "keep" and fid not in decided else f
                     for fid, f in known.items() if fid not in records]
        return {"repo": repo, "number": number, "head": head, "run_id": call.run_id,
                "updated_at": call.now.timestamp(), "not_approved": blockers, "gates": blockers,
                "approved_head": head if approved else state.get("approved_head") or "",
                "review_url": review_url or state.get("review_url") or "",
                "findings": list(records.values()) + untouched, "decisions": decisions,
                "comments": state.get("comments") or [], "lead_verdict": verdict, "lead_summary": summary}

    if merged and not blocking:
        # Nobody can act on an approval after the merge.
        return {"posted": False, "merged": True, "reason": "merged with nothing blocking", "repo": repo,
                "number": number, "not_approved": blockers, "findings_state": persist()}
    if merged:
        body = (f"{CHANGES}\n\n" + summary + refused + orphan_block
                + ("\n\nThis pull request merged before the review finished, so a follow-up pull request will "
                   "carry these changes." if followup else ""))
        event = "COMMENT"
    elif not changes and not blockers:
        body = APPROVAL + optional + orphan_block
        # GitHub refuses a self-approval with 422, so the app comments instead.
        event = "COMMENT" if self_authored else "APPROVE"
    elif not changes:
        body, event = refused.strip() + optional + orphan_block, "COMMENT"
    else:
        body = f"{CHANGES}\n\n" + summary + refused + orphan_block
        event = "COMMENT"

    payload: dict[str, Any] = {"commit_id": head, "body": sanitize.capped(body or "No findings.", BODY_MAX),
                               "event": event}
    if inline:
        payload["comments"] = inline
    posted = gh.json("POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload)
    ids = {}
    try:
        for c in gh.pages(f"/repos/{repo}/pulls/{number}/reviews/{int(posted['id'])}/comments"):
            m = FINDING_MARKER.search(c.get("body") or "")
            if m:
                ids[m.group(1)] = c.get("id")
    except (VendorError, KeyError, TypeError, ValueError):
        pass
    return {"posted": True, "repo": repo, "number": number, "event": event, "review_id": posted.get("id"),
            "url": posted.get("html_url"), "inline_comments": len(inline), "orphaned_comments": len(orphaned),
            "repeated_findings": repeated, "self_authored": self_authored, "verdict": verdict,
            "blocking_comments": len(blocking), "not_approved": blockers, "merged": merged, "followup": followup,
            "findings_state": persist(posted.get("html_url") or "", ids,
                                      approved=not merged and not changes and not blockers)}


def discuss(call: Call, gh: Api, params: dict[str, Any], pr: dict[str, Any]) -> dict[str, Any]:
    """Re-judge the open findings after a comment: reply where a finding changed, and approve once nothing blocks."""
    repo, number, head = params["repo"], int(params["number"]), params["head"]
    mine = me(call)
    author = login_of(pr)
    state = dict(params.get("findings_state") or {})
    records = _records(state)
    by_id = {f["id"]: f for f in records}
    own = ours(call)
    roots, comments = posted_findings(gh, number, own)
    was_blocking = any(f.get("disposition") == "keep" and f.get("blocking") for f in records)

    # A change lands only once its reply posted, so a failed reply leaves the finding open to retry.
    changes = []
    for d in params.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        f = by_id.get(d.get("finding_id") or "")
        if not f or f.get("disposition") != "keep":
            continue
        # The stored thread id stands in when the comments could not be read.
        root = roots.get(f["id"]) or ({"id": f["comment_id"]} if f.get("comment_id") else {})
        if not root.get("id"):
            continue
        if d.get("disposition") in ("drop_rebutted", "resolved"):
            opener, disposition = "Agreed, dropping this:", d["disposition"]
        elif d.get("disposition") == "keep" and d.get("discussion_basis") and answered_since(root, comments, own):
            opener, disposition = "Keeping this:", "keep"
        else:
            continue
        changes.append((f, disposition, d, {"in_reply_to": int(root["id"]), "finding_id": f["id"],
                                            "body": f"{opener}\n\n{sanitize.quoted(d.get('reason'))}"}))

    replies, failed = [], []
    for f, disposition, d, reply in changes:
        try:
            gh.json("POST", f"/repos/{repo}/pulls/{number}/comments/{reply['in_reply_to']}/replies",
                    json={"body": reply_body(call, reply["body"])})
        except VendorError:
            failed.append(f["id"])
            continue
        replies.append(reply)
        f.update(disposition=disposition, discussion_basis=d.get("discussion_basis") or "",
                 reason=d.get("reason") or "", updated_at=call.now.timestamp())

    open_blocking = [f for f in records if f.get("disposition") == "keep" and f.get("blocking")]
    withdrawn = [disposition for f, disposition, _, _ in changes
                 if f.get("blocking") and disposition != "keep" and f["id"] not in failed]
    # Only the head the last full review judged, so an unreviewed push is never approved.
    approve = (was_blocking and not open_blocking and not state.get("not_approved")
               and state.get("head") == head and state.get("approved_head") != head)
    self_authored = mine == author and mine != ""
    payload = None
    if approve:
        rebutted = bool(withdrawn) and not params.get("approve_on_rebuttal")
        payload = {"commit_id": head, "body": APPROVAL,
                   "event": "COMMENT" if self_authored or rebutted else "APPROVE"}
    review_url, approve_error = state.get("review_url") or "", None
    if payload:
        try:
            posted = gh.json("POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload)
            review_url = posted.get("html_url") or review_url
            state["approved_head"] = head
        except VendorError as exc:
            approve_error = str(exc)
    state.update(findings=records, review_url=review_url, updated_at=call.now.timestamp(), discussed_head=head,
                 run_id=call.run_id, decisions=params.get("decisions") or [])
    return {"posted": bool(replies) or (bool(payload) and not approve_error), "mode": "discuss", "repo": repo,
            "number": number, "replies": replies, "failed_replies": failed,
            "approved": bool(payload) and not approve_error, "approve_event": (payload or {}).get("event"),
            "approve_error": approve_error, "open_blocking": len(open_blocking), "findings_state": state}

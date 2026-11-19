"""review.*: the server's side of a code review. Risk from GitHub's own file list, and the reviewed mark.

Each reads the commit it acts on from GitHub or from the run's own row, never from the step's values, so a workflow
can't rate or mark a commit it didn't review.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from sqlalchemy import Connection, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import risk as levels
from armature.server import sanitize, writer
from armature.server.db import new_id
from armature.server.db.schema import findings, gates, review_marks, runs, tool_calls
from armature.server.tools import Call, ToolError
from armature.server.tools.github import (
    CHECK_NAME,
    PAGE,
    PUSH_RISKS,
    Api,
    api,
    me,
    push_gate,
    upsert_check,
)
from armature.server.tools.review import FINDING_ID, finding_key
from armature.server.vendors import github as vendor

FILES_PAGES = levels.FILES_MAX // PAGE
# The compare API lists at most this many files, so a branch diff that reaches it may hide some.
COMPARE_MAX = 300
REASON_MAX = 300
READ = {"pull_requests": "read", "contents": "read"}
READ_CHECKS = {"pull_requests": "read", "checks": "read"}
ADDED = ("added", "copied")
RUN_PUSH_CAP = 3
RISK_TOOLS = ("review.risk", "review.record-risk")
# A gate these roles decide may hold a high-risk pull request. Contributors and viewers can't.
GATE_ROLES = ("project_lead", "team_lead", "admin")
HELD_REASON = "A pull request needs low or medium recorded risk, or a person's approval at the run's gate."
DECISION_FIELDS = ("claim", "body", "title", "path", "line", "severity", "raised_by", "decision", "reason",
                   "disposition")


def _engine(call: Call):
    if call.engine is None:
        raise ToolError(500, "The review tools need the server's database.")
    return call.engine


def _pr(gh: Api, repo: str, number: int) -> dict[str, Any]:
    pr = gh.json("GET", f"/repos/{repo}/pulls/{number}")
    head = ((pr.get("head") or {}).get("sha")) if isinstance(pr, dict) else None
    if not isinstance(head, str) or not head:
        raise ToolError(502, "GitHub did not say the pull request's head commit.")
    return pr


def _diff(gh: Api, repo: str, params: dict[str, Any]) -> tuple[str, list[dict[str, Any]], int | None, bool]:
    """The head commit, the changed files, the diff's file count, and whether GitHub listed fewer files than it read."""
    if params.get("number"):
        pr = _pr(gh, repo, int(params["number"]))
        files = gh.pages(f"/repos/{repo}/pulls/{int(params['number'])}/files", FILES_PAGES)
        total = pr.get("changed_files")
        total = total if isinstance(total, int) and not isinstance(total, bool) else len(files)
        return pr["head"]["sha"], files, total, len(files) < min(total, levels.FILES_MAX)
    if not params.get("branch") or not params.get("base"):
        raise ToolError(400, "Give number, or branch and base.")
    branch = gh.json("GET", f"/repos/{repo}/branches/{quote(params['branch'], safe='')}")
    head = ((branch.get("commit") or {}).get("sha")) if isinstance(branch, dict) else None
    if not isinstance(head, str) or not head:
        raise ToolError(502, "GitHub did not say the branch's head commit.")
    found = gh.json("GET", f"/repos/{repo}/compare/{quote(params['base'], safe='')}...{head}")
    files = [f for f in (found.get("files") or []) if isinstance(f, dict)] if isinstance(found, dict) else []
    return head, files, None, len(files) >= COMPARE_MAX


def _paths(files: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Every path the diff touches, a rename's old one too, and the paths it adds."""
    paths, added = [], []
    for f in files:
        for key in ("filename", "previous_filename"):
            if isinstance(f.get(key), str):
                paths.append(f[key])
        if f.get("status") in ADDED and isinstance(f.get("filename"), str):
            added.append(f["filename"])
    return paths, added


def risk(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """Rate the diff: the higher of the server's floors and the model's level, recorded on the run, never lowered."""
    repo = params["repo"]
    with api(call, repo, READ) as gh:
        head, files, total, truncated = _diff(gh, repo, params)
    # The run's head is only ever the commit its sessions reviewed, so a head that moved since is refused.
    if head != params["expect_head"]:
        raise ToolError(409, f"GitHub's head is {head[:12]}, not the reviewed {params['expect_head'][:12]}. "
                             "Review the new head.")
    paths, added = _paths(files)
    return {**_rate(call, params, head, paths, added, total, truncated), "files": len(files)}


def _rate(call: Call, params: dict[str, Any], head: str, paths: list[str], added: list[str], total: int | None,
          truncated: bool) -> dict[str, Any]:
    """Record the higher of the server's floors on ``paths`` and the model's level on the run, with its head."""
    model = [{"source": "model", "text": sanitize.plain(r, REASON_MAX)} for r in params.get("reasons") or []
             if isinstance(r, str) and r.strip()]
    with _engine(call).begin() as conn:
        extra = levels.extra_floors(conn, call.team_id, call.project_id)
        floor, reasons = levels.floor(paths, extra, total, added)
        if truncated:
            floor = "high"
            reasons.insert(0, {"source": "floor", "text": "GitHub listed fewer files than the diff changes"})
        held = levels.record(conn, call.run_id, levels.combine(floor, params["level"]),
                             (reasons + model)[:levels.MAX_REASONS])
        conn.execute(update(runs).where(runs.c.id == call.run_id).values(head_sha=head))
        since = None if truncated else _since(params)
        triggered = levels.triggers(params.get("triggers"), paths if since is None else since[0])
        if since is None or triggered is None:
            scope, at, files = "full", held, max(len(set(paths)), total or 0)
        else:
            scope, files = "since", len(set(since[0]))
            at = levels.combine(levels.floor(since[0], extra, None, since[1])[0], params["level"])
        panel = levels.panel(at, levels.low_risk_panel(conn, call.team_id, call.project_id), triggered,
                             small=params.get("small"), lines=params.get("lines"), files=files)
    return {"level": held, "floor": floor, "model": levels.known(params["level"]), "reasons": reasons + model,
            "head": head, "panel": panel, "skipped": [role for role in levels.SPECIALISTS if role not in panel],
            "triggers": triggered or {"model": [], "path": []}, "scope": scope, "panel_level": at}


def _since(params: dict[str, Any]) -> tuple[list[str], list[str]] | None:
    """The paths and added paths of the change since the last full review, or None when the pack sent none the
    server can read, so the panel follows the whole pull request. A listing as long as the compare API returns may
    be cut short, and an empty one may be a failed lookup."""
    paths, added = params.get("since_paths"), params.get("since_added")
    if not all(isinstance(v, list) and all(isinstance(p, str) for p in v) for v in (paths, added)):
        return None
    kept = [p for p in paths if p]
    return (kept, added) if kept and len(paths) < COMPARE_MAX else None


def record_risk(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """review.risk for a pack that read the diff itself: the server's floors on the paths it reports, and the run's
    recorded level and panel."""
    paths, total = params.get("paths") or [], params.get("total")
    truncated = params.get("truncated") is True or (total is not None and len(set(paths)) < min(total,
                                                                                               levels.FILES_MAX))
    return _rate(call, params, params["head"], paths, params.get("added") or [], total, truncated)


def _check_mark(gh: Api, call: Call, conn: Connection, repo: str, head: str) -> bool:
    """A successful Armature review check on ``head`` from this connection's own app that review.mark made, as its
    row records. Any other check of that name is forged."""
    if vendor.uses_gh_cli(call.config):
        return _status_mark(gh, call, conn, repo, head)
    app_id = call.identity.get("app_id")
    if not isinstance(app_id, int) or isinstance(app_id, bool) or not call.project_id:
        return False
    found = gh.json("GET", f"/repos/{repo}/commits/{head}/check-runs", params={"check_name": CHECK_NAME,
                                                                                "per_page": PAGE})
    checks = found.get("check_runs") if isinstance(found, dict) else None
    ids = [c.get("id") for c in checks or [] if isinstance(c, dict) and c.get("head_sha") == head
           and (c.get("app") or {}).get("id") == app_id and c.get("status") == "completed"
           and c.get("conclusion") == "success" and isinstance(c.get("id"), int)]
    return bool(ids) and conn.execute(select(review_marks.c.id).where(
        review_marks.c.project_id == call.project_id, review_marks.c.check_run_id.in_(ids))).first() is not None


def _status_mark(gh: Api, call: Call, conn: Connection, repo: str, head: str) -> bool:
    """For a gh CLI connection: the newest Armature review status its own login set on ``head`` is a success that
    review.mark recorded. A status anyone else set is forged."""
    login = me(call)
    if not login or not call.project_id:
        return False
    found = gh.json("GET", f"/repos/{repo}/commits/{head}/statuses", params={"per_page": PAGE})
    ours = [s for s in found if isinstance(s, dict) and s.get("context") == CHECK_NAME
            and str((s.get("creator") or {}).get("login") or "").lower() == login
            and isinstance(s.get("id"), int) and not isinstance(s.get("id"), bool)] if isinstance(found, list) else []
    if not ours:
        return False
    newest = max(ours, key=lambda s: (str(s.get("created_at") or ""), s["id"]))
    return newest.get("state") == "success" and conn.execute(select(review_marks.c.id).where(
        review_marks.c.project_id == call.project_id, review_marks.c.app_id.is_(None),
        review_marks.c.check_run_id == newest["id"])).first() is not None


def marked(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """Whether the pull request's head, as GitHub says it now, already carries a valid mark."""
    repo, number = params["repo"], int(params["number"])
    with api(call, repo, READ_CHECKS) as gh:
        head = _pr(gh, repo, number)["head"]["sha"]
        with _engine(call).connect() as conn:
            found = mark_of(conn, call.project_id, head)
            source = "database" if found else "check" if _check_mark(gh, call, conn, repo, head) else None
    if source:
        with _engine(call).begin() as conn:
            settle(conn, call.run_id, "noop")
    return {"valid": source is not None, "head": head, "source": source}


def mark_of(conn: Connection, project_id: str | None, head: str | None) -> Any:
    if not project_id or not head:
        return None
    return conn.execute(select(review_marks).where(review_marks.c.project_id == project_id,
                                                   review_marks.c.head_sha == head)).first()


def is_marked(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """review.marked for a pack that read the head from GitHub itself. Only the server's own mark row counts."""
    head = params["head"]
    with _engine(call).begin() as conn:
        valid = mark_of(conn, call.project_id, head) is not None
        if valid:
            settle(conn, call.run_id, "noop")
    return {"valid": valid, "head": head}


def review_detail(params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """What a live github.post-review call keeps on its row: the head it judged and whether it approved it."""
    posted = result.get("posted") is True
    approved = posted and (result.get("findings_state") or {}).get("approved_head") == params["head"]
    return {"head": params["head"], "number": params["number"], "posted": posted, "approved": approved}


def post_detail(params: dict[str, Any]) -> dict[str, Any]:
    """What a review.record-post row keeps, in review_detail's shape."""
    posted = params["posted"] is True
    detail = {"head": params["head"], "number": params["number"], "posted": posted,
              "approved": posted and params["approved"] is True}
    if params.get("review_id") is not None:
        detail["review_id"] = params["review_id"]
    return detail


def record_post(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """The broker settles the outcome and keeps the row review.record-mark reads, as it does for a live post."""
    return {"recorded": True, **post_detail(params)}


def settle(conn: Connection, run_id: str, outcome: str) -> str:
    """Set the run's outcome, except that a review never hides that the run opened its pull request. Returns it."""
    current = conn.execute(select(runs.c.outcome).where(runs.c.id == run_id)).scalar()
    if current == "pr_opened" and outcome != "blocked":
        return current
    writer.server_outcome(conn, run_id, outcome)
    return outcome


def opened(conn: Connection, run_id: str, result: dict[str, Any]) -> None:
    if result.get("number"):
        settle(conn, run_id, "pr_opened")


def reviewed(conn: Connection, run_id: str, detail: dict[str, Any]) -> None:
    """A posted review that didn't approve leaves the run's outcome at changes recommended."""
    if detail["posted"] and not detail["approved"]:
        settle(conn, run_id, "recommended")


def _pushed(conn: Connection, run_id: str) -> bool:
    return conn.execute(select(func.count()).select_from(tool_calls).where(
        tool_calls.c.run_id == run_id, tool_calls.c.tool == "github.push",
        tool_calls.c.outcome == "approved")).scalar_one() > 0


def mark(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """Mark the head this run reviewed as approved: a check run from this app, or a status from a gh CLI login, and
    the row the server trusts first.

    Only a head this run's own live review approved is marked, so the mark always follows a posted review.
    """
    repo, number = params["repo"], int(params["number"])
    head, refusal = _approved_head(call, repo, number, "github.post-review")
    if refusal:
        return refusal
    made = upsert_check(call, {"repo": repo, "head": head, "conclusion": "success", "title": "Reviewed and approved",
                            "summary": f"Armature reviewed and approved {head[:12]}."})
    app_id = call.identity.get("app_id")
    values = {"team_id": call.team_id, "repo": repo.lower(), "pr_number": number, "run_id": call.run_id,
              "check_run_id": made.get("check_run_id"), "conclusion": "success", "created_at": call.now,
              "app_id": app_id if isinstance(app_id, int) and not isinstance(app_id, bool) else None}
    with _engine(call).begin() as conn:
        existing = mark_of(conn, call.project_id, head)
        if existing is None:
            conn.execute(insert(review_marks).values(id=new_id(), project_id=call.project_id, head_sha=head,
                                                     **values))
        else:
            conn.execute(update(review_marks).where(review_marks.c.id == existing.id).values(**values))
        outcome = settle(conn, call.run_id, "pushed" if _pushed(conn, call.run_id) else "approved")
    return {"marked": True, "head": head, "check_run_id": made.get("check_run_id"), "url": made.get("url"),
            "outcome": outcome}


def _approved_head(call: Call, repo: str, number: int, post_tool: str) -> tuple[str, dict[str, Any] | None]:
    """The head the run reviewed, and a refusal unless the run's last live ``post_tool`` row on the pull request
    approved it."""
    with _engine(call).connect() as conn:
        head = conn.execute(select(runs.c.head_sha).where(runs.c.id == call.run_id)).scalar()
        posts = conn.execute(select(tool_calls.c.detail).where(
            tool_calls.c.run_id == call.run_id, tool_calls.c.tool == post_tool, tool_calls.c.mode == "live",
            tool_calls.c.target == f"{repo.lower()}#{number}").order_by(tool_calls.c.created_at)).scalars().all()
    last = posts[-1] if posts else None
    if not head:
        return "", {"marked": False, "reason": "the run has no reviewed head"}
    if not isinstance(last, dict) or last.get("head") != head or last.get("approved") is not True:
        return head, {"marked": False, "head": head, "reason": "this run posted no approving review of its head"}
    return head, None


def record_mark(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """review.mark for a pack that set the GitHub status itself: the row the server trusts, for the head this run's
    own last recorded post approved. A newer status id replaces an older status mark, never a check run's."""
    repo, number, status_id = params["repo"], int(params["number"]), params["status_id"]
    head, refusal = _approved_head(call, repo, number, "review.record-post")
    if refusal:
        return refusal
    if head != params["head"]:
        return {"marked": False, "head": head, "reason": f"this run reviewed {head[:12]}, not {params['head'][:12]}"}
    values = {"team_id": call.team_id, "repo": repo.lower(), "pr_number": number, "run_id": call.run_id,
              "check_run_id": status_id, "conclusion": "success", "created_at": call.now, "app_id": None}
    for attempt in range(2):
        try:
            with _engine(call).begin() as conn:
                existing = mark_of(conn, call.project_id, head)
                if existing is None:
                    conn.execute(insert(review_marks).values(id=new_id(), project_id=call.project_id,
                                                             head_sha=head, **values))
                else:
                    conn.execute(update(review_marks).where(
                        review_marks.c.id == existing.id, review_marks.c.app_id.is_(None),
                        or_(review_marks.c.check_run_id.is_(None), review_marks.c.check_run_id < status_id))
                        .values(**values))
                stored = mark_of(conn, call.project_id, head).check_run_id
                outcome = settle(conn, call.run_id, "pushed" if _pushed(conn, call.run_id) else "approved")
            break
        except IntegrityError:
            # Another run marked the same head first, so the second pass updates its row.
            if attempt:
                return {"marked": False, "head": head, "reason": "another run is marking this head"}
    return {"marked": True, "head": head, "check_run_id": stored, "outcome": outcome}


def pushable(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """Whether github.push would let this run push to the pull request now, asked before any fix is made."""
    with _engine(call).connect() as conn:
        refusal = push_refusal(conn, call.run_id, to_pr=True)
    if refusal:
        return {"pushable": False, "reason": refusal}
    found = push_gate(call, params)
    return {"pushable": found["approved"], "reason": found.get("reason") or found.get("rule")}


def push_refusal(conn: Connection, run_id: str, *, to_pr: bool) -> str | None:
    """Why the run's own history refuses another push: the cap, or a pull request head whose code wasn't
    re-reviewed since the run's last push. None when it may push."""
    approved = conn.execute(select(tool_calls.c.created_at).where(
        tool_calls.c.run_id == run_id, tool_calls.c.tool == "github.push", tool_calls.c.outcome == "approved")
        .order_by(tool_calls.c.created_at)).scalars().all()
    if len(approved) >= RUN_PUSH_CAP:
        return f"this run reached its cap of {RUN_PUSH_CAP} pushes"
    if to_pr and approved and conn.execute(select(tool_calls.c.id).where(
            tool_calls.c.run_id == run_id, tool_calls.c.tool.in_(RISK_TOOLS), tool_calls.c.mode == "live",
            tool_calls.c.outcome == "ok", tool_calls.c.created_at > approved[-1])).first() is None:
        return "the code this run last pushed was never re-reviewed"
    return None


def may_open(conn: Connection, run_id: str) -> bool:
    """Whether a run may open a pull request: low or medium recorded risk, or a gate a project lead or above approved
    after the run's latest rating. Missing risk is high."""
    if levels.recorded(conn, run_id) in PUSH_RISKS:
        return True
    rated = conn.execute(select(func.max(tool_calls.c.created_at)).where(
        tool_calls.c.run_id == run_id, tool_calls.c.tool.in_(RISK_TOOLS), tool_calls.c.mode == "live",
        tool_calls.c.outcome == "ok")).scalar()
    return rated is not None and conn.execute(select(gates.c.id).where(
        gates.c.run_id == run_id, gates.c.state == "approved", gates.c.min_role.in_(GATE_ROLES),
        gates.c.decided_at > rated)).first() is not None


def _approved_commit(conn: Connection, run_id: str) -> str | None:
    details = conn.execute(select(tool_calls.c.detail).where(
        tool_calls.c.run_id == run_id, tool_calls.c.tool == "github.push", tool_calls.c.outcome == "approved")
        .order_by(tool_calls.c.created_at)).scalars().all()
    commit = (details[-1] or {}).get("commit") if details else None
    return commit if isinstance(commit, str) else None


def decisions(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """The lead's decisions as finding payloads. The broker records them as the step's own finding events.

    A finding's key is its finding_id, else the key github.post-review gives the same claim and path. With
    ``fixed``, each is one of this run's own findings that its approved push fixed, at that push's commit.
    """
    found = []
    for given in params.get("decisions") or []:
        payload = {k: given[k] for k in DECISION_FIELDS if given.get(k) is not None}
        given_id = given.get("finding_id")
        payload["finding_key"] = given_id if isinstance(given_id, str) and FINDING_ID.fullmatch(given_id) \
            else finding_key(given)
        found.append(payload)
    if params.get("fixed"):
        with _engine(call).connect() as conn:
            commit = _approved_commit(conn, call.run_id)
            raised = set(conn.execute(select(findings.c.finding_key).where(
                findings.c.run_id == call.run_id, findings.c.decision.is_distinct_from("fixed"))).scalars())
        if commit is None:
            raise ToolError(409, "Only a run whose push was approved records fixed findings.")
        unknown = sorted({p["finding_key"] for p in found} - raised)
        if unknown:
            raise ToolError(409, f"This run never raised {', '.join(unknown)}.")
        for payload in found:
            payload.update(decision="fixed", fix_sha=commit)
    return {"recorded": len(found), "findings": found}

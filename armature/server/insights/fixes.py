"""Self-healing fixes: the proposal a pattern review keeps flagging becomes, its run, its gate, and its pull request.

A proposal is bound to the one job the weekly slot queued for it, and to that job's run when it opens. Every tool
below acts only on the proposal its own run is bound to, so a step can't read or change another proposal. The fix
stays on the server as a patch until a team lead approves it, and only then is it pushed, with the branch, base,
and draft flag from the proposal, never from the run's values.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import Connection, Engine, func, insert, select, update

from armature.server import sanitize
from armature.server.crypto import Keyring
from armature.server.db import new_id
from armature.server.db.schema import (
    finding_candidates,
    fix_proposals,
    gh_pull_requests,
    gh_repos,
    jobs,
    labels,
    llm_calls,
    projects,
    runs,
    tool_calls,
    workflows,
)
from armature.server.tools import Call, ToolError
from armature.server.tools.github import api
from armature.server.vendors import VendorError

SELF_HEAL = "self-heal"
JOB_KEY = "self-heal:"
BRANCH_PREFIX = "armature/fix-"
KINDS = ("check", "sweep", "guidance", "author_note")
KIND_WORDS = {"check": "a new automated check", "sweep": "a one-time cleanup",
              "guidance": "a review guidance change", "author_note": "author guidance"}
# A proposal in one of these blocks another for the same pattern.
ACTIVE = ("queued", "drafting", "waiting", "approved", "opened")
DELETABLE = ("declined", "expired", "failed")
# A lost run can come back, so it counts as ended only once its job has too.
ENDED_RUN = ("succeeded", "failed", "canceled")
ENDED_JOB = ("failed", "canceled", "superseded", "lost")
# Each state's opened fixes count against the repository's daily cap.
OPENING = ("approved", "opened", "merged", "closed")
KEY_MAX = 191
EXAMPLES_MAX = 10
CLAIM_MAX = 300
PATH_MAX = 300
PREVIEW_LINES = 12
LINE_MAX = 200
BEFORE = timedelta(days=30)
PATCH_MAX = 200 * 1024
DELETE_TRIES = 8
WRITE = {"contents": "write", "pull_requests": "read"}
NOTHING = "Nothing to fix was found."
TOO_LARGE = "The fix was too large to review here."
NO_LONGER = "The fix no longer applies to {sha}, the {base} commit it was made on."
NOT_TEXT = "The fix changes more than text files."
TURNED_OFF = "Fixes are turned off for {repo}."
NOT_OPENED = "The draft pull request didn't open, so its branch was deleted."
NOT_BOUND = "This run was not started for that fix proposal."
NOT_APPROVED = "A self-heal fix reaches GitHub only after a team lead approves it."
# What a fix that stopped in each state failed of, when its run says nothing.
STOPPED = {"queued": "The fix run never started.", "drafting": "The fix run ended without a change to review.",
           "waiting": "The fix run stopped while it waited for a decision.",
           "approved": "The fix was approved, but its draft pull request didn't open."}
RUN_WORDS = {"lost": "The fix run stopped responding.", "canceled": "The fix run was canceled."}
COUNT_WORDS = {1: "One fix", 2: "Two fixes", 3: "Three fixes", 4: "Four fixes", 5: "Five fixes"}
BINARY = re.compile(r"^(Binary files |GIT binary patch)", re.M)
# A symlink, a submodule, an executable bit, or a mode-only change: anything but a plain text file.
MODE = re.compile(r"^(new file|deleted file|old|new) mode (?!100644$)", re.M)
DIFF_HEADER = re.compile(r"^diff --git ", re.M)


def cluster_key(version: str, label: str, repo: str | None, area: str | None) -> str:
    """The pattern a proposal fixes, stable across the weekly cluster rebuild. A long one keeps a digest tail."""
    key = "|".join((version, label, (repo or "").lower(), area or ""))
    return key if len(key) <= KEY_MAX else f"{key[:KEY_MAX - 41]}|{hashlib.sha256(key.encode()).hexdigest()[:40]}"


def branch_of(proposal_id: str) -> str:
    return f"{BRANCH_PREFIX}{proposal_id}"


# The category names the Insights screens show (format.js CATEGORIES), where they differ from the id.
NAMES = {"crash-safety": "Crash safety", "lifecycle-memory": "Lifecycle and memory",
         "security-privacy": "Security and privacy", "data-contract": "Data contracts",
         "ui-layout": "Layout and screen states", "testing": "Tests", "api-misuse": "API misuse",
         "simplicity": "Unneeded complexity", "conventions": "Repo conventions",
         "build-dependencies": "Build and dependencies", "observability": "Logging and metrics"}


def category_name(label: str) -> str:
    return NAMES.get(label) or label.replace("-", " ").replace("_", " ").capitalize()


def pattern_words(label: str) -> str:
    name = category_name(label)
    return name if name.startswith("API") else name.lower()


def title_of(label: str, repo: str | None, area: str | None) -> str:
    where = area or (repo.rpartition("/")[2] if repo else "the repository")
    return f"Fix: {category_name(label)} in {where}"


def is_self_heal(conn: Connection, workflow_id: str | None) -> bool:
    return workflow_id is not None and conn.execute(select(workflows.c.id).where(
        workflows.c.id == workflow_id, workflows.c.name == SELF_HEAL, workflows.c.built_in.is_(True))).first() \
        is not None


def proposal_of(conn: Connection, run_id: str, team_id: str) -> Any:
    return conn.execute(select(fix_proposals).where(fix_proposals.c.run_id == run_id,
                                                    fix_proposals.c.team_id == team_id)).first()


def _set(conn: Connection, proposal_id: str, now: datetime, *, states: tuple[str, ...], **values: Any) -> bool:
    return conn.execute(update(fix_proposals).where(fix_proposals.c.id == proposal_id,
                                                    fix_proposals.c.state.in_(states))
                        .values(updated_at=now, **values)).rowcount == 1


def bind_run(conn: Connection, job: Any, run_id: str, now: datetime) -> None:
    """Bind the queued proposal the weekly slot queued this very job for to the run the job just opened."""
    if not is_self_heal(conn, job.workflow_id):
        return
    conn.execute(update(fix_proposals).where(
        fix_proposals.c.job_id == job.id, fix_proposals.c.team_id == job.team_id,
        fix_proposals.c.state == "queued", fix_proposals.c.run_id.is_(None))
        .values(run_id=run_id, state="drafting", updated_at=now))


def gate_opened(conn: Connection, run_id: str, team_id: str, gate_id: str, now: datetime) -> None:
    found = proposal_of(conn, run_id, team_id)
    if found is not None:
        _set(conn, found.id, now, states=("drafting",), state="waiting", gate_id=gate_id)


def gate_decided(conn: Connection, gate: Any, now: datetime) -> None:
    """Move the proposal waiting at ``gate`` to approved, declined with the note as its reason, or expired."""
    found = proposal_of(conn, gate.run_id, gate.team_id)
    if found is None or found.gate_id != gate.id:
        return
    if gate.state == "approved":
        _set(conn, found.id, now, states=("waiting",), state="approved", decided_by=gate.decided_by,
             decided_at=gate.decided_at)
    elif gate.state == "rejected":
        _set(conn, found.id, now, states=("waiting",), state="declined", decided_by=gate.decided_by,
             decided_at=gate.decided_at, decline_reason=gate.decision_note)
    elif gate.state == "expired":
        _set(conn, found.id, now, states=("waiting",), state="expired", decided_at=gate.decided_at)


def approve_refusal(conn: Connection, gate: Any, now: datetime) -> str | None:
    """Why approving a fix's gate now would pass the repository's daily cap of opened fixes. None when it wouldn't."""
    from armature.server import broker

    found = proposal_of(conn, gate.run_id, gate.team_id)
    if found is None or found.gate_id != gate.id:
        return None
    # The lock _admit_open takes, so two approvals at once can't both count under the cap.
    conn.execute(select(projects.c.id).where(projects.c.id == found.project_id).with_for_update())
    cap = broker.setting(conn, broker.FIX_PR_CAP_KEY, gate.team_id, found.project_id)
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0 else broker.DEFAULT_FIX_PR_CAP
    repo = ((found.evidence or {}).get("repo") or "this repository").rpartition("/")[2]
    if cap == 0:
        return TURNED_OFF.format(repo=repo)
    fp = fix_proposals.c
    same = fp.repo_row_id == found.repo_row_id if found.repo_row_id else fp.project_id == found.project_id
    count = conn.execute(select(func.count()).select_from(fix_proposals).where(
        fp.team_id == gate.team_id, fp.id != found.id, fp.state.in_(OPENING),
        fp.decided_at >= now - timedelta(days=1), same)).scalar_one()
    if count < cap:
        return None
    many = COUNT_WORDS.get(count, f"{count} fixes")
    return f"{many} already opened in {repo} today. Approve again tomorrow."


def opened(conn: Connection, proposal_id: str, result: dict[str, Any], now: datetime) -> None:
    if isinstance(result.get("number"), int):
        _set(conn, proposal_id, now, states=("approved",), state="opened", pr_number=result["number"],
             pr_url=result.get("url") if isinstance(result.get("url"), str) else None)


def _spent(conn: Connection, run_id: str) -> Any:
    return conn.execute(select(func.coalesce(func.sum(llm_calls.c.cost_usd), 0)).where(
        llm_calls.c.run_id == run_id, llm_calls.c.cached.is_(False))).scalar()


def reconcile(conn: Connection, team_id: str, now: datetime) -> int:
    """Fail each proposal whose job or run ended without reaching the next state, and keep its spend. Returns how
    many it failed."""
    failed = 0
    fp = fix_proposals.c
    for row in conn.execute(select(fp.id, fp.state, fp.run_id, fp.job_id).where(
            fp.team_id == team_id, fp.state.in_(("queued", "drafting", "waiting", "approved")))).all():
        reason = None
        if row.run_id is None:
            job = conn.execute(select(jobs.c.status, jobs.c.error).where(
                jobs.c.team_id == team_id, jobs.c.id == row.job_id)).first() if row.job_id else None
            if job is not None and job.status in ENDED_JOB:
                reason = job.error or STOPPED["queued"]
        else:
            run = conn.execute(select(runs.c.status, runs.c.error).where(runs.c.id == row.run_id)).first()
            latest = conn.execute(select(jobs.c.status).where(jobs.c.run_id == row.run_id)
                                  .order_by(jobs.c.created_at.desc()).limit(1)).scalar()
            if run is not None and (run.status in ENDED_RUN or (run.status == "lost" and latest in ENDED_JOB)):
                reason = RUN_WORDS.get(run.status) or run.error or STOPPED[row.state]
        if reason and _set(conn, row.id, now, states=(row.state,), state="failed", failure_reason=reason[:2000],
                           cost_usd=_spent(conn, row.run_id) if row.run_id else None):
            failed += 1
    return failed


def _area(path: str | None) -> str | None:
    folders = [part for part in (path or "").split("/")[:-1] if part]
    return "/".join(folders[:2]) or None


def flags(conn: Connection, team_id: str, evidence: dict[str, Any], start: datetime,
          end: datetime | None = None) -> int:
    """How many times review flagged the proposal's pattern (its category, repository, and area) in the window."""
    from armature.server.insights import metrics

    fc = finding_candidates.c
    query = (select(fc.run_id, fc.path).join(labels, (labels.c.subject_id == fc.id) & (labels.c.team_id == fc.team_id))
             .where(fc.team_id == team_id, fc.arm_id.is_(None), fc.created_at >= start,
                    labels.c.subject_kind == "candidate", labels.c.taxonomy_version == evidence.get("taxonomy_version"),
                    labels.c.label == evidence.get("label")))
    if end is not None:
        query = query.where(fc.created_at < end)
    rows = conn.execute(query).all()
    targets = metrics.run_targets(conn, team_id, {r.run_id for r in rows}) if rows else {}
    repo, area = (evidence.get("repo") or "").lower() or None, evidence.get("area")
    return sum(1 for r in rows if (targets.get(r.run_id, "").rpartition("#")[0] or None) == repo
               and _area(r.path) == area)


def track(conn: Connection, team_id: str, repo_row_id: str, now: datetime) -> int:
    """Move each opened fix of the repository to merged or closed from its pull request, by number or else by its
    branch. A merge records the flag count in the 30 days before it. Returns how many moved."""
    moved = 0
    for row in conn.execute(select(fix_proposals).where(fix_proposals.c.team_id == team_id,
                                                        fix_proposals.c.repo_row_id == repo_row_id,
                                                        fix_proposals.c.state == "opened")).all():
        pr = gh_pull_requests.c
        mine = (pr.number == row.pr_number) if row.pr_number is not None else (pr.head_ref == row.branch)
        found = conn.execute(select(pr.state, pr.merged_at_gh).where(pr.team_id == team_id,
                                                                     pr.repo_row_id == repo_row_id, mine)
                             .order_by(pr.created_at_gh.desc()).limit(1)).first()
        if found is None or found.state == "open":
            continue
        if found.state == "merged" and found.merged_at_gh is not None:
            done = _set(conn, row.id, now, states=("opened",), state="merged", merged_at=found.merged_at_gh,
                        flags_before=flags(conn, team_id, row.evidence or {}, found.merged_at_gh - BEFORE,
                                           found.merged_at_gh),
                        cost_usd=_spent(conn, row.run_id) if row.run_id else None)
        else:
            done = _set(conn, row.id, now, states=("opened",), state="closed",
                        cost_usd=_spent(conn, row.run_id) if row.run_id else None)
        moved += done
    return moved


# The tools a self-heal run calls through the broker.

def _engine(call: Call):
    if call.engine is None:
        raise ToolError(500, "The fix tools need the server's database.")
    return call.engine


def _bound(conn: Connection, call: Call, proposal_id: str) -> Any:
    found = conn.execute(select(fix_proposals).where(fix_proposals.c.id == proposal_id,
                                                     fix_proposals.c.team_id == call.team_id,
                                                     fix_proposals.c.run_id == call.run_id)).first()
    if found is None:
        raise ToolError(403, NOT_BOUND)
    return found


def _fenced(text: str) -> str:
    mark = sanitize.fence(text)
    return f"{mark}text\n{text}\n{mark}"


def _line(text: Any, cap: int) -> str:
    return " ".join(str(text or "").split())[:cap]


def evidence_text(evidence: dict[str, Any]) -> str:
    """The pattern's counts and examples as the implement agent reads them, each example's text fenced as data."""
    lines = [f"Category: {evidence.get('label')}", f"Repository: {evidence.get('repo')}",
             f"Area: {evidence.get('area') or 'the whole repository'}",
             f"Flagged {evidence.get('flags')} times in the last {evidence.get('days')} days.", "", "Examples:"]
    for n, example in enumerate((evidence.get("examples") or [])[:EXAMPLES_MAX], start=1):
        where = _line(example.get("path"), PATH_MAX) or "(no file)"
        line = example.get("line")
        lines += ["", f"{n}. {where}{f':{line}' if isinstance(line, int) else ''}",
                  _fenced(_line(example.get("claim"), CLAIM_MAX))]
    return "\n".join(lines)


def pr_body(evidence: dict[str, Any]) -> str:
    where = " in ".join(p for p in (evidence.get("area"), evidence.get("repo")) if p)
    examples = [f"- `{_line(e.get('path'), PATH_MAX).replace('`', '')}`" for e in
                (evidence.get("examples") or [])[:5] if e.get("path")]
    where = f" in {where}" if where else ""
    lead = (f"Armature's reviews flagged {pattern_words(str(evidence.get('label')))} {evidence.get('flags')} times "
            f"in {evidence.get('days')} days{where}. This change aims to stop it coming back.")
    return "\n".join([
        lead,
        *(["", "Some of the places it was flagged:", *examples] if examples else []),
        "", "This is an automated fix that a team lead approved in Armature before this draft opened."])


def context(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """insights.fix-context: the run's own proposal, its evidence fenced and capped, and the pull request's text."""
    with _engine(call).connect() as conn:
        found = _bound(conn, call, params["proposal"])
    evidence = found.evidence or {}
    label = str(evidence.get("label") or "")
    return {"proposal": found.id, "pattern": pattern_words(label), "label": label, "repo": evidence.get("repo"),
            "area": evidence.get("area"), "flags": evidence.get("flags"), "evidence": evidence_text(evidence),
            "pr_title": found.title, "pr_body": pr_body(evidence), "branch": found.branch, "base": found.base}


def _unquoted(path: str) -> str:
    return path[1:-1] if len(path) > 1 and path[0] == path[-1] == '"' else path


def patch_files(patch: str) -> list[dict[str, Any]]:
    """Each file a git diff changes: its path, whether it was added or removed, its counts, and its changed lines."""
    files: list[dict[str, Any]] = []
    for part in DIFF_HEADER.split(patch)[1:]:
        # Only \n ends a diff line, so a CRLF file's \r stays part of its line.
        lines = part.split("\n")
        old = new = None
        status, hunks, changed, adds, dels = "modified", False, [], 0, 0
        for line in lines[1:]:
            if not hunks:
                if line.startswith("new file mode"):
                    status = "added"
                elif line.startswith("deleted file mode"):
                    status = "removed"
                elif line.startswith("--- "):
                    old = _unquoted(line[4:])
                elif line.startswith("+++ "):
                    new = _unquoted(line[4:])
                elif line.startswith("@@"):
                    hunks = True
                continue
            if line.startswith("+"):
                adds += 1
                changed.append(["add", line[1:LINE_MAX + 1]])
            elif line.startswith("-"):
                dels += 1
                changed.append(["del", line[1:LINE_MAX + 1]])
        name = new if new and new != "/dev/null" else old
        path = name[2:] if name and name[:2] in ("a/", "b/") else name
        if path is None:
            head = lines[0].split(" b/", 1) if lines else []
            path = head[1] if len(head) == 2 else ""
        files.append({"filename": path, "status": status, "additions": adds, "deletions": dels, "changed": changed})
    return files


def preview_of(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Every changed file's name, the first PREVIEW_LINES changed lines under them, and the patch's totals."""
    lines: list[list[str]] = []
    shown, more = 0, False
    for f in files:
        lines.append(["file", _line(f["filename"], PATH_MAX)])
        for kind, text in f["changed"]:
            if shown >= PREVIEW_LINES:
                more = True
                break
            lines.append([kind, text])
            shown += 1
    return {"files": len(files), "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files), "lines": lines, "more": more}


def _fail(conn: Connection, proposal_id: str, now: datetime, reason: str, states: tuple[str, ...]) -> None:
    _set(conn, proposal_id, now, states=states, state="failed", failure_reason=reason)


def patch(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """insights.fix-patch: store the agent's change as a patch against the commit it started from. No change, a
    binary change, or a patch over PATCH_MAX fails the proposal, and the gate is then skipped."""
    text = params.get("patch")
    text = text if isinstance(text, str) else ""
    with _engine(call).begin() as conn:
        found = _bound(conn, call, params["proposal"])
        if found.state != "drafting" or found.patch:
            raise ToolError(409, "This fix is no longer being drafted.")
        reason = None
        if params["outcome"] != "implemented" or (not text.strip() and not params.get("oversize")
                                                  and not params.get("undecodable")):
            reason = NOTHING
        elif params.get("oversize") or params.get("undecodable") or len(text.encode()) > PATCH_MAX \
                or BINARY.search(text):
            reason = TOO_LARGE
        elif MODE.search(text) or any(f["filename"].rpartition("/")[2] == ".gitmodules"
                                      for f in patch_files(text)):
            reason = NOT_TEXT
        elif params["kind"] not in KINDS:
            raise ToolError(400, f"kind is one of {', '.join(KINDS)}.")
        if reason:
            _fail(conn, found.id, call.now, reason, ("drafting",))
            return {"stored": False, "reason": reason}
        shown = preview_of(patch_files(text))
        _set(conn, found.id, call.now, states=("drafting",), kind=params["kind"], patch=text,
             base_sha=params["base"], diff_preview=shown)
    return {"stored": True, "patch": _fenced(text), "kind": params["kind"], "kind_words": KIND_WORDS[params["kind"]],
            "base_sha": params["base"], "files": shown["files"], "additions": shown["additions"],
            "deletions": shown["deletions"]}


def rate(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """insights.fix-risk: review.risk for the proposal's stored patch, which nothing has pushed. The server's floors
    read the patch's own paths."""
    from armature.server.tools import marks

    with _engine(call).connect() as conn:
        found = _bound(conn, call, params["proposal"])
    if found.state != "drafting" or not found.patch or not found.base_sha:
        raise ToolError(409, "This fix has no stored patch to rate.")
    files = patch_files(found.patch)
    paths = [f["filename"] for f in files if f["filename"]]
    added = [f["filename"] for f in files if f["status"] == "added" and f["filename"]]
    rated = marks._rate(call, params, found.base_sha, paths, added, len(files), False)
    with _engine(call).begin() as conn:
        _set(conn, found.id, call.now, states=("drafting",), risk=rated["level"])
    return {**rated, "files": len(files)}


def apply(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """insights.fix-apply: the approved proposal's patch for the runner to apply, or, with outcome conflict, the
    proposal failed because the patch no longer applies to its base."""
    with _engine(call).begin() as conn:
        found = _bound(conn, call, params["proposal"])
        if found.state != "approved" or not found.patch or not found.base_sha:
            raise ToolError(409, NOT_APPROVED)
        if params.get("outcome") == "conflict":
            reason = NO_LONGER.format(sha=found.base_sha[:12], base=found.base)
            _fail(conn, found.id, call.now, reason, ("approved",))
            return {"failed": True, "reason": reason}
    return {"patch": found.patch, "base_sha": found.base_sha, "branch": found.branch, "title": found.title}


def foreign(tool: str, params: dict[str, Any]) -> str | None:
    """Why a run that isn't self-heal may not make this call: only self-heal pushes or opens armature/fix- branches."""
    if tool == "github.push" and str(params.get("branch") or "").startswith(BRANCH_PREFIX):
        return f"Only a self-heal run pushes to an {BRANCH_PREFIX} branch."
    if tool == "github.open-pr" and str(params.get("head") or "").startswith(BRANCH_PREFIX):
        return f"Only a self-heal run opens a pull request from an {BRANCH_PREFIX} branch."
    return None


def pinned(proposal: Any, tool: str, params: dict[str, Any]) -> str | None:
    """Why a self-heal run's call strays from its proposal's repository, branch, or base. None when it doesn't."""
    if proposal is None:
        return "A self-heal run acts only for the fix proposal it was started for."
    repo = (proposal.evidence or {}).get("repo") or ""
    if str(params.get("repo") or "").lower() != repo.lower():
        return f"This fix belongs to {repo}."
    if tool == "github.push" and (params.get("number") or params.get("branch") != proposal.branch):
        return f"A self-heal run pushes only to {proposal.branch}."
    if tool == "github.open-pr" and (params.get("head") != proposal.branch or params.get("base") != proposal.base):
        return f"A self-heal run opens only {proposal.branch} into {proposal.base}."
    if tool in ("github.push", "github.open-pr") and proposal.state != "approved":
        return NOT_APPROVED
    if tool == "github.delete-branch" and params.get("branch") != proposal.branch:
        return f"A self-heal run deletes only {proposal.branch}."
    return None


def delete_branch(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """github.delete-branch: an armature/fix- branch that no open pull request uses, whose proposal was declined,
    expired, or failed, or was approved and its own run pushed it but couldn't open the pull request. That proposal
    fails."""
    repo, branch = params["repo"], params["branch"]
    if not branch.startswith(BRANCH_PREFIX) or ".." in branch:
        raise ToolError(403, f"Only an {BRANCH_PREFIX} branch can be deleted.")
    with _engine(call).connect() as conn:
        found = conn.execute(select(fix_proposals.c.id, fix_proposals.c.state, fix_proposals.c.run_id,
                                    fix_proposals.c.pr_number).where(fix_proposals.c.team_id == call.team_id,
                                                                     fix_proposals.c.branch == branch)).first()
    if found is None:
        raise ToolError(403, "No fix proposal in this team uses that branch.")
    unopened = found.state == "approved" and found.run_id == call.run_id and found.pr_number is None
    if found.state not in DELETABLE and not unopened:
        return {"deleted": False, "branch": branch, "reason": f"the fix is {found.state}, not declined or expired"}
    owner = repo.split("/")[0]
    with api(call, repo, WRITE) as gh:
        prs = gh.json("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open",
                                                              "per_page": 1})
        if not isinstance(prs, list) or prs:
            return {"deleted": False, "branch": branch, "reason": "an open pull request uses the branch"}
        response = gh.request("DELETE", f"/repos/{repo}/git/refs/heads/{quote(branch, safe='/')}")
        if response.status_code in (404, 422):
            return {"deleted": False, "branch": branch, "reason": "the branch is already gone"}
        if response.status_code >= 300:
            raise VendorError(f"GitHub DELETE /repos/{{repo}}/git/refs answered HTTP {response.status_code}.",
                              response.status_code)
    if unopened:
        with _engine(call).begin() as conn:
            _fail(conn, found.id, call.now, NOT_OPENED, ("approved",))
    return {"deleted": True, "branch": branch}


def leftovers(conn: Connection, team_id: str) -> list[Any]:
    """Failed fixes whose run pushed a branch that opened no pull request, and that nothing has deleted yet."""
    tc, fp = tool_calls.c, fix_proposals.c
    pushed = select(tc.id).where(tc.run_id == fp.run_id, tc.tool == "github.push", tc.outcome == "approved").exists()
    deleted = select(tc.id).where(tc.run_id == fp.run_id, tc.tool == "github.delete-branch",
                                  tc.outcome == "ok").exists()
    tries = select(func.count(tc.id)).where(tc.run_id == fp.run_id, tc.tool == "github.delete-branch",
                                            tc.outcome == "failed").scalar_subquery()
    return conn.execute(select(fp.id, fp.run_id, fp.project_id, fp.repo_row_id, fp.branch, tries.label("tries"))
                        .where(fp.team_id == team_id, fp.state == "failed", fp.pr_number.is_(None),
                               fp.run_id.is_not(None), fp.repo_row_id.is_not(None),
                               fp.branch.startswith(BRANCH_PREFIX), pushed, ~deleted, tries < DELETE_TRIES)).all()


def sweep(engine: Engine, keyring: Keyring, team_id: str, now: datetime,
          transport: httpx.BaseTransport | None = None) -> int:
    """Delete, from the server, each leftover branch a run's own cleanup didn't, by delete_branch's rules, and
    record each try on the run as its cleanup would. A failed try is tried again next time, up to DELETE_TRIES
    tries, after which the proposal's failure_reason says so. Returns how many it deleted."""
    from armature.server.insights import ghapi

    with engine.connect() as conn:
        found = leftovers(conn, team_id)
    deleted = 0
    for row in found:
        with engine.connect() as conn:
            repo = conn.execute(select(gh_repos).where(gh_repos.c.id == row.repo_row_id,
                                                       gh_repos.c.team_id == team_id)).first()
            try:
                call = ghapi.call_for(conn, keyring, repo, now, transport) if repo is not None else None
            except ghapi.GitHubError:
                call = None
        if call is None:
            continue
        call.run_id, call.step_id, call.engine = row.run_id, "reconcile", engine
        try:
            result, outcome = delete_branch(call, {"repo": repo.full_name, "branch": row.branch}), "ok"
        except (ToolError, VendorError) as exc:
            result, outcome = {"error": str(exc)[:500]}, "failed"
        with engine.begin() as conn:
            conn.execute(insert(tool_calls).values(
                id=new_id(), run_id=row.run_id, team_id=team_id, project_id=row.project_id, step_id="reconcile",
                tool="github.delete-branch", mode="live", outcome=outcome,
                target=f"{repo.full_name.lower()}:{row.branch}"[:191], detail=result, created_at=now))
            if outcome == "failed" and row.tries + 1 >= DELETE_TRIES:
                reason = conn.execute(select(fix_proposals.c.failure_reason)
                                      .where(fix_proposals.c.id == row.id)).scalar()
                gone = f"Its branch {row.branch} couldn't be deleted after {DELETE_TRIES} tries. Delete it on GitHub."
                _set(conn, row.id, now, states=("failed",),
                     failure_reason=f"{reason} {gone}" if reason else gone)
        deleted += result.get("deleted") is True
    return deleted


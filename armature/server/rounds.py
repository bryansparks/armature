"""Review rounds: each run's round, previous round, kind, and head, and each finding's lineage, for one pull request.

A round is one distinct head of the pull request that received a posted review: a live github.post-review or
review.record-post call with posted true, or a runner's post step whose review_id names a review GitHub sync stored on
the run's own pull request under a login Armature posts as. Every run that posted a head gets that head's round, so a
rerun of a head is an attempt at its round. A run that posted nothing has no round.

derive recomputes a whole subject from scratch, so a round can change after it is written: a run that started earlier
and ends later, or a review sync stores later, shifts the rounds after it. The writer, sync, and the webhook collector
each call it after their own transaction commits, in a short transaction of its own. On Postgres an advisory lock per
team and pull request serializes those passes, so each one reads what the one before it wrote. Other databases take no
lock, and a pass that overlaps another can leave stale rounds until the next pass.

review_round, prev_round_run_id, and review_kind classify runs and must never authorize anything. Only an ended run
gets a derived head, and only where no head is recorded, so a derived head can't let a run mark a head or open a pull
request. A head from a synced review is written only when exactly one run on the pull request, open runs included,
claims that review. A head from a synced push must be the pull request's head when the run started.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import Connection, Engine, func, select, text, update

from armature.server import ingest
from armature.server.db import utcnow
from armature.server.db.schema import (
    findings,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_reviews,
    projects,
    run_events,
    run_steps,
    runs,
    tool_calls,
)
from armature.server.insights.attribution import REVIEW_TOOLS
from armature.server.insights.candidates import MIN_OVERLAP, _near, overlap
from armature.server.insights.people import own_logins
from armature.server.tools.review import finding_key

log = logging.getLogger(__name__)

STEPS = ("resolve", "incremental", "post")
COLUMNS = ("review_round", "prev_round_run_id", "review_kind")


def _ended() -> Any:
    from armature.server.history import OPEN_RUNS
    return runs.c.status.not_in((*OPEN_RUNS, "lost"))


def _sha(value: Any) -> str | None:
    return value if isinstance(value, str) and ingest.SHA.fullmatch(value) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _review_id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _pull_request(conn: Connection, subject: str, team_id: str, project_id: str) -> str | None:
    """The stored pull request ``subject`` names, when it is in the project's own repository."""
    name, _, number = subject.rpartition("#")
    repo = conn.execute(select(projects.c.repo).where(projects.c.id == project_id,
                                                      projects.c.team_id == team_id)).scalar()
    if not name or not number.isdigit() or not repo or repo.lower() != name.lower():
        return None
    return conn.execute(select(gh_pull_requests.c.id).join(gh_repos, gh_repos.c.id == gh_pull_requests.c.repo_row_id)
                        .where(gh_repos.c.team_id == team_id, func.lower(gh_repos.c.full_name) == name.lower(),
                               gh_pull_requests.c.team_id == team_id, gh_pull_requests.c.number == int(number))
                        .limit(1)).scalar()


def _synced(conn: Connection, team_id: str, pr_id: str | None) -> tuple[dict[int, str | None], list[Any]]:
    """Armature's reviews on the pull request with each one's head, and its pushes in the order they landed."""
    if pr_id is None:
        return {}, []
    logins = own_logins(conn, team_id)
    reviews = {r.gh_review_id: _sha(r.head_sha) for r in conn.execute(
        select(gh_reviews.c.gh_review_id, gh_reviews.c.reviewer_login, gh_reviews.c.head_sha)
        .where(gh_reviews.c.team_id == team_id, gh_reviews.c.pr_row_id == pr_id))
        if r.reviewer_login.lower() in logins}
    pushed = conn.execute(select(gh_pushes.c.pushed_at, gh_pushes.c.after_sha)
                          .where(gh_pushes.c.team_id == team_id, gh_pushes.c.pr_row_id == pr_id)
                          .order_by(gh_pushes.c.pushed_at, gh_pushes.c.id)).all()
    return reviews, pushed


def _head_at(pushed: list[Any], started_at: Any) -> str | None:
    """The pull request's head when a run started: the latest push at or before then."""
    found = None
    for push in pushed:
        if push.pushed_at <= started_at:
            found = push.after_sha
    return found


def _posts(conn: Connection, ids: list[str], subject: str) -> tuple[dict[str, list[str]], set[Any]]:
    """The heads each run's live review calls on ``subject`` posted, in call order, and the review IDs they name."""
    heads: dict[str, list[str]] = {}
    named: set[Any] = set()
    for row in conn.execute(select(tool_calls.c.run_id, tool_calls.c.target, tool_calls.c.detail).where(
            tool_calls.c.run_id.in_(ids), tool_calls.c.tool.in_(REVIEW_TOOLS), tool_calls.c.mode == "live")
            .order_by(tool_calls.c.created_at, tool_calls.c.id)):
        detail = _dict(row.detail)
        if (row.target or "").lower() != subject.lower() or detail.get("posted") is not True:
            continue
        if _review_id(detail.get("review_id")) is not None:
            named.add(detail["review_id"])
        if _sha(detail.get("head")):
            heads.setdefault(row.run_id, []).append(detail["head"])
    return heads, named


def _kind(steps: dict[str, Any], posted: bool) -> str | None:
    resolve = _dict(steps.get("resolve"))
    if resolve.get("mode") == "discuss":
        return "discuss"
    if resolve.get("reviewed") is False:
        return "resolve_declined"
    verdict = _dict(steps.get("incremental")).get("verdict")
    if verdict == "approve_incremental":
        return "incremental"
    return "full" if posted or verdict == "full_review" else None


def _text(finding: Any) -> str:
    return " ".join(filter(None, (finding.title, finding.body)))


def _match(finding: Any, earlier: list[tuple[Any, str]]) -> str | None:
    """The lineage of the finding in ``earlier`` this one restates: the same key, else the closest wording nearby."""
    for other, lineage in earlier:
        if other.finding_key == finding.finding_key:
            return lineage
    best: tuple[float, str] | None = None
    for other, lineage in earlier:
        if not finding.path or other.path != finding.path or not _near(finding, other):
            continue
        score = overlap(_text(finding), _text(other))
        if score >= MIN_OVERLAP and (best is None or score > best[0]):
            best = (score, lineage)
    return best[1] if best else None


def _lineage(conn: Connection, ordered: list[Any], round_of: dict[str, int], project_id: str) -> None:
    by_run: dict[str, list[Any]] = {}
    for row in conn.execute(select(findings.c.id, findings.c.run_id, findings.c.finding_key, findings.c.path,
                                   findings.c.line, findings.c.title, findings.c.body, findings.c.lineage_key)
                            .where(findings.c.run_id.in_([r.id for r in ordered]),
                                   findings.c.project_id == project_id)
                            .order_by(findings.c.created_at, findings.c.id)):
        by_run.setdefault(row.run_id, []).append(row)
    by_round: dict[int, list[tuple[Any, str]]] = {}
    position = {r.id: i for i, r in enumerate(ordered)}
    for run_id in sorted(by_run, key=lambda r: (round_of.get(r, 0), position[r])):
        number = round_of.get(run_id)
        for finding in by_run[run_id]:
            lineage = None
            for earlier in range(number - 1, 0, -1) if number else ():
                lineage = _match(finding, by_round.get(earlier, []))
                if lineage is not None:
                    break
            lineage = lineage or finding.finding_key
            if number:
                by_round.setdefault(number, []).append((finding, lineage))
            if finding.lineage_key != lineage:
                conn.execute(update(findings).where(findings.c.id == finding.id).values(lineage_key=lineage))


def _lock_key(team_id: str, subject: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{team_id}|{subject.lower()}".encode()).digest()[:8], "big", signed=True)


def _claimed(post: Any) -> int | None:
    post = _dict(post)
    return _review_id(post.get("review_id")) if post.get("posted") is True else None


def derive(conn: Connection, subject: str | None, team_id: str, project_id: str | None) -> None:
    """Recompute the rounds, kinds, derived heads, and finding lineage of the ended runs of one project on one pull
    request (``owner/name#number``), in start order, writing only what changed. Open runs' claims count, but nothing
    is written to an open run."""
    from armature.server.history import OPEN_RUNS

    if not subject or project_id is None:
        return
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(team_id, subject)})
    every = conn.execute(select(runs.c.id, runs.c.status, runs.c.started_at, runs.c.head_sha,
                                *(runs.c[c] for c in COLUMNS)).where(
        runs.c.team_id == team_id, runs.c.project_id == project_id, func.lower(runs.c.subject) == subject.lower())
        .order_by(runs.c.started_at, runs.c.id)).all()
    ordered = [r for r in every if r.status not in (*OPEN_RUNS, "lost")]
    if not ordered:
        return
    ids = [r.id for r in every]
    steps: dict[str, dict[str, Any]] = {}
    for row in conn.execute(select(run_steps.c.run_id, run_steps.c.step_key, run_steps.c.output).where(
            run_steps.c.run_id.in_(ids), run_steps.c.step_key.in_(STEPS))):
        steps.setdefault(row.run_id, {})[row.step_key] = row.output
    posted, named = _posts(conn, ids, subject)
    reviews, pushed = _synced(conn, team_id, _pull_request(conn, subject, team_id, project_id))
    claims: dict[int, list[str]] = {}
    for row in every:
        review_id = _claimed(steps.get(row.id, {}).get("post"))
        if review_id in reviews and review_id not in named:
            claims.setdefault(review_id, []).append(row.id)
    round_of_head: dict[str, int] = {}
    openers: list[str] = []
    round_of: dict[str, int] = {}
    for row in ordered:
        own = steps.get(row.id, {})
        claimants = claims.get(_claimed(own.get("post")), [])
        # A review credits the first run that claims it, and gives a head only when no other run claims it too.
        reviewed = reviews[_claimed(own.get("post"))] if claimants[:1] == [row.id] else None
        derived = reviewed if len(claimants) == 1 else None
        claimed = _sha(_dict(own.get("resolve")).get("head"))
        if derived is None and claimed is not None and claimed == _head_at(pushed, row.started_at):
            derived = claimed
        heads = posted.get(row.id) or ([reviewed] if reviewed else [])
        number = prev = None
        for head in heads:
            if head not in round_of_head:
                round_of_head[head] = len(openers) + 1
                openers.append(row.id)
        if heads:
            number = round_of[row.id] = round_of_head[heads[-1]]
            prev = openers[number - 2] if number > 1 else None
        wanted = (number, prev, _kind(own, bool(heads)))
        if tuple(getattr(row, c) for c in COLUMNS) != wanted:
            conn.execute(update(runs).where(runs.c.id == row.id).values(dict(zip(COLUMNS, wanted, strict=True))))
        if row.head_sha is None and derived is not None:
            conn.execute(update(runs).where(runs.c.id == row.id, runs.c.head_sha.is_(None)).values(head_sha=derived))
    _lineage(conn, ordered, round_of, project_id)


def derive_quietly(conn: Connection, subject: str | None, team_id: str, project_id: str | None) -> None:
    """derive, never failing the caller's transaction."""
    try:
        with conn.begin_nested():
            derive(conn, subject, team_id, project_id)
    except Exception:
        # Rounds are derived, and backfill-rounds rebuilds them, so a failure must not cost the caller's writes.
        log.exception("could not derive the review rounds of %s", subject)


def run_ended(conn: Connection, run_id: str) -> None:
    """Derive the pull request of a run whose end the caller already committed."""
    run = conn.execute(select(runs.c.subject, runs.c.team_id, runs.c.project_id).where(runs.c.id == run_id)).first()
    if run is not None:
        derive_quietly(conn, run.subject, run.team_id, run.project_id)


def pull_request_synced(engine: Engine, team_id: str, subject: str) -> None:
    """Derive the subject for each project with runs on it, in a transaction of its own, after the caller committed
    the pull request's reviews or pushes. A failure is only logged."""
    try:
        with engine.begin() as conn:
            if conn.dialect.name == "sqlite":
                # pysqlite opens no transaction before a SAVEPOINT, so releasing the first one would commit it alone.
                conn.exec_driver_sql("BEGIN")
            for project_id in conn.execute(select(runs.c.project_id).where(
                    runs.c.team_id == team_id, func.lower(runs.c.subject) == subject.lower(),
                    runs.c.project_id.is_not(None)).distinct()).scalars().all():
                derive_quietly(conn, subject, team_id, project_id)
    except Exception:
        log.exception("could not derive the review rounds of %s", subject)


def _legacy_key(path: str | None, line: int | None, body: str | None) -> str:
    # The key ingest gave a finding event without one, before it keyed them as github.post-review does.
    return hashlib.sha256(f"{path}|{line}|{body}".encode()).hexdigest()[:64]


def _rekey(conn: Connection, team_id: str, project_id: str, subject: str) -> int:
    """Key the subject's findings stored under the legacy key as github.post-review keys them. Returns how many."""
    ended = select(runs.c.id).where(runs.c.team_id == team_id, runs.c.project_id == project_id,
                                    func.lower(runs.c.subject) == subject, _ended())
    rows = [r for r in conn.execute(select(findings.c.id, findings.c.run_id, findings.c.finding_key, findings.c.path,
                                           findings.c.line, findings.c.body)
                                    .where(findings.c.run_id.in_(ended), func.length(findings.c.finding_key) == 64))
            if _legacy_key(r.path, r.line, r.body) == r.finding_key]
    if not rows:
        return 0
    claimed: dict[tuple[str, str], str] = {}
    now = utcnow()
    for run_id, payload in conn.execute(select(run_events.c.run_id, run_events.c.payload).where(
            run_events.c.run_id.in_({r.run_id for r in rows}), run_events.c.kind == "finding")):
        if isinstance(payload, dict):
            stored = ingest.finding_row(None, None, now, payload)
            claimed.setdefault((run_id, _legacy_key(stored["path"], stored["line"], stored["body"])),
                               stored["finding_key"])
    for row in rows:
        key = claimed.get((row.run_id, row.finding_key)) or finding_key({"path": row.path, "body": row.body})
        conn.execute(update(findings).where(findings.c.id == row.id, findings.c.finding_key == row.finding_key)
                     .values(finding_key=key))
    return len(rows)


def backfill(engine: Engine) -> dict[str, int]:
    """Re-key findings stored under the legacy key, then derive every subject's rounds, one subject per transaction,
    subjects in the order their first run started. Safe to rerun: a second pass changes nothing."""
    counts = {"subjects": 0, "rekeyed": 0}
    subject = func.lower(runs.c.subject)
    with engine.connect() as conn:
        listed = conn.execute(select(runs.c.team_id, runs.c.project_id, subject.label("subject"))
                              .where(runs.c.project_id.is_not(None), runs.c.subject.is_not(None), _ended())
                              .group_by(runs.c.team_id, runs.c.project_id, subject)
                              .order_by(func.min(runs.c.started_at), runs.c.team_id, runs.c.project_id, subject)).all()
    for row in listed:
        with engine.begin() as conn:
            counts["rekeyed"] += _rekey(conn, row.team_id, row.project_id, row.subject)
            derive(conn, row.subject, row.team_id, row.project_id)
        counts["subjects"] += 1
    return counts

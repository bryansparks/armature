"""The GitHub sync service: keeps each tracked repository's pull request history complete without webhooks.

Polling is the source of truth (decision 7). Each tracked repository has one gh_sync_state row per kind. A server
claims a due row by moving its next_run_at forward by a lease with a compare-and-set, so two servers never work one
repository and kind at once, and another server takes over once the lease runs out if this one dies.

- backfill pages pull requests newest first from its saved cursor, down to the backfill floor, then stops for good.
- poll refreshes the pull requests updated since its watermark, every POLL_EVERY.
- conflicts samples mergeable for every open pull request, every CONFLICTS_EVERY, keeping a sample when it changes.
- gap_fill compares GitHub's REST list for the last GAP_WINDOW with the stored rows nightly and refreshes what differs.

A refresh writes a pull request and its children in one transaction with the collector's upserts, so a webhook and
the service describing one object make one row. It derives pushes from the head's history and classifies them.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import Connection, Engine, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import broker, rounds
from armature.server.crypto import Keyring
from armature.server.db import utcnow
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    findings,
    gh_conflict_samples,
    gh_pr_commits,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    gh_sync_state,
    people,
    settings,
    tool_calls,
)
from armature.server.insights import (
    attribution,
    collect,
    defects,
    fixes,
    ghapi,
    pushes,
    repos,
)
from armature.server.insights import people as people_store
from armature.server.insights.people import gh_id
from armature.server.vendors import VendorError

log = logging.getLogger(__name__)

TICK_S = 15.0
POOL = 2
SCAN = 100
STOP_TIMEOUT_S = 30.0
# Longer than any run can take, so a lease only lapses when its server is gone.
LEASE = timedelta(minutes=30)
BUDGET_S = 240.0
POLL_EVERY = timedelta(minutes=5)
CONFLICTS_EVERY = timedelta(minutes=10)
GAP_EVERY = timedelta(hours=24)
GAP_WINDOW = timedelta(hours=48)
GAP_PAGES = 10
GAP_MAX = 200
BACKOFF = timedelta(minutes=1)
BACKOFF_MAX = timedelta(minutes=30)
DEPTH = timedelta(days=548)
ERROR_MAX = 2000
PAGE = 25
OPEN_PAGE = 100
EXTRAS_CHUNK = 40
REVERTS_MAX = 200
ASKS = 4
FIXES_CHUNK = 500
REVIEWS, COMMENTS, COMMITS, FORCE_PUSHES, FILES, SUITES, RUNS, HISTORY = 30, 30, 40, 30, 100, 10, 100, 30
SINCE_SETTING = "insights.backfill_since"
ENABLED_SETTING = "insights.sync_enabled"
OFF = ("false", "off", "no", "0")
LOST = "stopped: the lease lapsed"
BUSY = "busy: not due, or another server holds it"
GHOST = {"id": 10137891, "login": "ghost", "type": "User"}
MERGEABLE = {"MERGEABLE": "mergeable", "CONFLICTING": "conflicting"}
OID = re.compile(r"^[0-9a-f]{40}$")

WHO = "__typename login ... on User { databaseId } ... on Bot { databaseId } ... on Mannequin { databaseId }"
PULL = (
    "databaseId number title body state isDraft createdAt updatedAt closedAt mergedAt additions deletions "
    "changedFiles baseRefName headRefName baseRefOid headRefOid headRepository { databaseId } mergeCommit { oid } "
    f"author {{ {WHO} }} mergedBy {{ {WHO} }} "
    f"reviews(first: {REVIEWS}) {{ nodes {{ databaseId state submittedAt body url commit {{ oid }} "
    f"author {{ {WHO} }} comments(first: {COMMENTS}) {{ nodes {{ databaseId path line createdAt updatedAt url "
    f"replyTo {{ databaseId }} commit {{ oid }} author {{ {WHO} }} }} }} }} }} "
    f"commits(last: {COMMITS}) {{ totalCount nodes {{ commit {{ oid committedDate messageHeadline "
    "parents(first: 2) { nodes { oid } } author { email user { databaseId login } } statusCheckRollup { state } "
    "} } } "
    f"timelineItems(last: {FORCE_PUSHES}, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) {{ nodes {{ "
    f"... on HeadRefForcePushedEvent {{ createdAt actor {{ {WHO} }} beforeCommit {{ oid }} afterCommit {{ oid }} }} "
    f"}} }} files(first: {FILES}) {{ nodes {{ path }} }}"
)
RATE = "rateLimit { cost remaining resetAt }"
REPO = "repository(owner: $owner, name: $name) { databaseId nameWithOwner"
PULLS = ("query($owner: String!, $name: String!, $first: Int!, $after: String) { " + RATE + " " + REPO
         + " pullRequests(first: $first, after: $after, orderBy: {field: ORDER, direction: DESC}) "
         "{ pageInfo { hasNextPage endCursor } nodes { " + PULL + " } } } }")
BY_CREATED = PULLS.replace("ORDER", "CREATED_AT")
BY_UPDATED = PULLS.replace("ORDER", "UPDATED_AT")
ONE = ("query($owner: String!, $name: String!, $number: Int!) { " + RATE + " " + REPO
       + " pullRequest(number: $number) { " + PULL + " } } }")
OPEN = ("query($owner: String!, $name: String!, $first: Int!, $after: String) { " + RATE + " " + REPO
        + " pullRequests(states: OPEN, first: $first, after: $after) { pageInfo { hasNextPage endCursor } "
        "nodes { databaseId mergeable mergeStateStatus } } } }")
CHECKS = (f"checkSuites(first: {SUITES}) {{ nodes {{ checkRuns(first: {RUNS}) {{ nodes {{ databaseId name status "
          "conclusion startedAt completedAt } } } }")
HISTORY_FIELDS = f"history(first: {HISTORY}) {{ nodes {{ oid messageHeadline }} }}"


class SyncError(Exception):
    """A sync run can't go on. The message is safe to show and store."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class _Gone(Exception):
    """The repository stopped being tracked while the run was working on it."""


class _Lost(Exception):
    """The run's lease lapsed and another server may hold the state now, so this run must stop writing to it."""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nodes(value: Any, name: str) -> list[dict[str, Any]]:
    return [n for n in _dict(_dict(value).get(name)).get("nodes") or [] if isinstance(n, dict)]


def _oid(value: Any) -> str | None:
    found = _dict(value).get("oid")
    return found if isinstance(found, str) and OID.fullmatch(found) else None


def _lower(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


_time = collect._time


@contextmanager
def _begin(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            # pysqlite opens no transaction before a SAVEPOINT, so releasing the first one would commit it alone.
            conn.exec_driver_sql("BEGIN")
        yield conn


# GraphQL nodes in the REST shapes the collector reads


def _user(actor: Any) -> dict[str, Any] | None:
    actor = _dict(actor)
    login, kind = actor.get("login"), actor.get("__typename")
    if not isinstance(login, str) or not login:
        return None
    if kind == "Bot" and not login.endswith(people_store.BOT_SUFFIX):
        # GraphQL names an App's bot without the suffix its REST and webhook payloads carry.
        login += people_store.BOT_SUFFIX
    return {"id": actor.get("databaseId"), "login": login, "type": kind}


def _rest_pr(node: dict[str, Any]) -> dict[str, Any]:
    state = node.get("state")
    return {"id": node.get("databaseId"), "number": node.get("number"), "title": node.get("title"),
            "body": node.get("body"), "user": _user(node.get("author")) or GHOST,
            "state": "open" if state == "OPEN" else "closed", "merged": state == "MERGED",
            "draft": bool(node.get("isDraft")),
            "head": {"ref": node.get("headRefName"), "sha": node.get("headRefOid"),
                     "repo": {"id": _dict(node.get("headRepository")).get("databaseId")}},
            "base": {"ref": node.get("baseRefName"), "sha": node.get("baseRefOid")},
            "merge_commit_sha": _oid(node.get("mergeCommit")), "additions": node.get("additions"),
            "deletions": node.get("deletions"), "changed_files": node.get("changedFiles"),
            "created_at": node.get("createdAt"), "updated_at": node.get("updatedAt"),
            "closed_at": node.get("closedAt"), "merged_at": node.get("mergedAt"),
            "merged_by": _user(node.get("mergedBy"))}


def _rest_review(node: dict[str, Any]) -> dict[str, Any] | None:
    if node.get("state") == "PENDING" or not node.get("submittedAt"):
        return None
    return {"id": node.get("databaseId"), "user": _user(node.get("author")) or GHOST, "state": node.get("state"),
            "commit_id": _oid(node.get("commit")), "submitted_at": node.get("submittedAt"), "body": node.get("body"),
            "html_url": node.get("url")}


def _rest_comment(node: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    return {"id": node.get("databaseId"), "user": _user(node.get("author")) or GHOST,
            "created_at": node.get("createdAt"), "updated_at": node.get("updatedAt"),
            "pull_request_review_id": review.get("databaseId"), "path": node.get("path"), "line": node.get("line"),
            "commit_id": _oid(node.get("commit")), "html_url": node.get("url"),
            "in_reply_to_id": _dict(node.get("replyTo")).get("databaseId")}


def _rest_check(node: dict[str, Any], head: str, pr_id: Any) -> dict[str, Any]:
    return {"id": node.get("databaseId"), "name": node.get("name"), "head_sha": head,
            "status": _lower(node.get("status")), "conclusion": _lower(node.get("conclusion")),
            "started_at": node.get("startedAt"), "completed_at": node.get("completedAt"),
            "pull_requests": [{"id": pr_id}]}


def _commits(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [_dict(c.get("commit")) for c in _nodes(node, "commits") if OID.fullmatch(str(_dict(c.get("commit"))
                                                                                         .get("oid") or ""))]


def _review_heads(node: dict[str, Any]) -> set[str]:
    return {h for h in (_oid(r.get("commit")) for r in _nodes(node, "reviews")) if h}


@dataclass(frozen=True)
class _Forced:
    before: str
    after: str
    at: datetime | None
    actor: dict[str, Any] | None


def _force_pushes(node: dict[str, Any]) -> list[_Forced]:
    found = []
    for event in _nodes(node, "timelineItems"):
        before, after = _oid(event.get("beforeCommit")), _oid(event.get("afterCommit"))
        if before and after:
            found.append(_Forced(before, after, _time(event.get("createdAt")), _user(event.get("actor"))))
    return found


@dataclass(frozen=True)
class Derived:
    before: str
    after: str
    at: datetime | None
    forced: bool
    actor: dict[str, Any] | None


def derive_pushes(node: dict[str, Any]) -> list[Derived]:
    """Each head the pull request had, as a push from the head before it.

    Force pushes come from the timeline. Other heads are the commits CI ran on, the ones a review judged, and the
    current head, because a push of several commits leaves only its last one checked. Heads committed before the pull
    request opened lead up to the head it opened with, which no push to it made.
    """
    forced = _force_pushes(node)
    found = [Derived(f.before, f.after, f.at, True, f.actor) for f in forced]
    afters, reviewed, head = {f.after for f in forced}, _review_heads(node), node.get("headRefOid")
    opened = _time(node.get("createdAt"))
    previous = None
    for commit in _commits(node):
        oid, at = commit["oid"], _time(commit.get("committedDate"))
        if not (oid == head or oid in reviewed or oid in afters or commit.get("statusCheckRollup")):
            continue
        if previous is not None and oid not in afters and not (at and opened and at < opened):
            found.append(Derived(previous, oid, at, False, None))
        previous = oid
    return found


# Runs


@dataclass
class _Job:
    engine: Engine
    state: Any
    repo: Any
    gh: ghapi.Client
    clock: Callable[[], datetime]
    deadline: float
    lease: datetime
    stopping: threading.Event | None = None

    def time_left(self) -> bool:
        return time.monotonic() < self.deadline and not (self.stopping and self.stopping.is_set())

    def variables(self, **values: Any) -> dict[str, Any]:
        owner, name = self.repo.full_name.split("/")
        return {"owner": owner, "name": name, **values}

    def save(self, **values: Any) -> None:
        if not _write(self.engine, self.state.id, self.lease, self.clock(), **values):
            raise _Lost()


@dataclass(frozen=True)
class _Stored:
    row_id: str
    node: dict[str, Any]
    number: int
    state: str
    reverts: int | None


def _write(engine: Engine, state_id: str, lease: datetime, now: datetime, **values: Any) -> bool:
    """Write the state while this run still holds its lease. False when it no longer does."""
    with engine.begin() as conn:
        return conn.execute(update(gh_sync_state).where(gh_sync_state.c.id == state_id,
                                                        gh_sync_state.c.next_run_at == lease)
                            .values(updated_at=now, **values)).rowcount == 1


def _cursor(text: str | None) -> dict[str, Any]:
    try:
        found = json.loads(text) if text else {}
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _locked_repo(conn: Connection, repo_id: str) -> Any:
    # A shared lock, as the collector takes, so a removal waits for this write or this write finds it gone.
    row = conn.execute(select(gh_repos).where(gh_repos.c.id == repo_id, gh_repos.c.enabled.is_(True))
                       .with_for_update(read=True)).first()
    if row is None:
        raise _Gone()
    return row


def _seen(conn: Connection, repo: Any, now: datetime) -> collect._Seen:
    return collect._Seen(conn, repo.team_id, repo, now, people_store.bot_logins(conn, repo.team_id))


def _repository(job: _Job, answer: ghapi.Answer) -> dict[str, Any]:
    """The answer's repository, after checking it is still the tracked one. A rename updates the stored name."""
    node = answer.data.get("repository")
    if not isinstance(node, dict):
        raise SyncError(f"GitHub has no repository {job.repo.full_name} that this connection can read.")
    if node.get("databaseId") != job.repo.gh_repo_id:
        raise SyncError(f"{job.repo.full_name} now names a different repository on GitHub. Remove it and track it "
                        f"again.")
    name = node.get("nameWithOwner")
    if isinstance(name, str) and repos.NAME.fullmatch(name) and name.lower() != job.repo.full_name:
        _rename(job, name.lower())
    return node


def _rename(job: _Job, name: str) -> None:
    try:
        with job.engine.begin() as conn:
            conn.execute(update(gh_repos).where(gh_repos.c.id == job.repo.id).values(full_name=name,
                                                                                     updated_at=job.clock()))
            job.repo = conn.execute(select(gh_repos).where(gh_repos.c.id == job.repo.id)).first() or job.repo
    except IntegrityError:
        log.warning("tracked repository %s was renamed to %s, which the team already tracks", job.repo.full_name,
                    name)
        return
    job.gh.rename(name)


def _wanted(job: _Job, nodes: list[dict[str, Any]]) -> dict[str, set[str]]:
    """The commits a page needs more on: check runs for each judged or final head, history for each force push."""
    wanted: dict[str, set[str]] = defaultdict(set)
    ids = [i for i in (gh_id(n.get("databaseId")) for n in nodes) if i is not None]
    with job.engine.connect() as conn:
        rows = conn.execute(select(gh_pull_requests.c.gh_pr_id, gh_pushes.c.before_sha, gh_pushes.c.after_sha,
                                   gh_pushes.c.forced, gh_pushes.c.push_class)
                            .join(gh_pushes, gh_pushes.c.pr_row_id == gh_pull_requests.c.id)
                            .where(gh_pull_requests.c.team_id == job.repo.team_id,
                                   gh_pull_requests.c.gh_pr_id.in_(ids))).all() if ids else []
    classified = {(r.gh_pr_id, r.after_sha) for r in rows if r.push_class is not None}
    for row in rows:
        if row.push_class is None and row.forced:
            wanted[row.before_sha].add("history")
            wanted[row.after_sha].add("history")
    for node in nodes:
        for head in {node.get("headRefOid"), *_review_heads(node)}:
            if isinstance(head, str):
                wanted[head].add("checks")
        for forced in _force_pushes(node):
            if (node.get("databaseId"), forced.after) not in classified:
                wanted[forced.before].add("history")
                wanted[forced.after].add("history")
    return {oid: kinds for oid, kinds in wanted.items() if OID.fullmatch(oid)}


def _extras(job: _Job, wanted: dict[str, set[str]]) -> dict[str, dict[str, Any] | None]:
    """Each wanted commit's check runs or history, with aliased lookups in as few queries as fit. None when gone."""
    found: dict[str, dict[str, Any] | None] = {}
    oids, size = sorted(wanted), max(1, EXTRAS_CHUNK >> job.gh.halvings)
    for start in range(0, len(oids), size):
        chunk = oids[start:start + size]
        params = ", ".join(f"$o{i}: GitObjectID!" for i in range(len(chunk)))
        parts = " ".join(f"c{i}: object(oid: $o{i}) {{ ... on Commit {{ oid "
                         f"{CHECKS if 'checks' in wanted[oid] else ''} "
                         f"{HISTORY_FIELDS if 'history' in wanted[oid] else ''} }} }}" for i, oid in enumerate(chunk))
        query = (f"query($owner: String!, $name: String!, {params}) {{ {RATE} {REPO} {parts} }} }}")
        answer = job.gh.graphql(query, job.variables(**{f"o{i}": oid for i, oid in enumerate(chunk)}))
        node = _repository(job, answer)
        for i, oid in enumerate(chunk):
            found[oid] = node.get(f"c{i}") if isinstance(node.get(f"c{i}"), dict) else None
    return found


def _reverted(seen: collect._Seen, pr: dict[str, Any]) -> int | None:
    """The number of a pull request in this repository this one reverts, when it isn't stored yet."""
    body = pr.get("body")
    found = collect.REVERTS.search(body[:collect.BODY_SCAN]) if isinstance(body, str) else None
    if found is None or (found.group(1) or seen.repo.full_name).lower() != seen.repo.full_name:
        return None
    number = int(found.group(2))
    stored = seen.conn.execute(select(gh_pull_requests.c.id).where(
        gh_pull_requests.c.team_id == seen.team_id, gh_pull_requests.c.repo_row_id == seen.repo.id,
        gh_pull_requests.c.number == number)).first()
    return number if stored is None and number != pr.get("number") else None


def _users(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Everyone a pull request names, once each, in GitHub ID order."""
    found = [_user(node.get("author")) or GHOST, _user(node.get("mergedBy"))]
    for review in _nodes(node, "reviews"):
        found.append(_user(review.get("author")) or GHOST)
        found += [_user(c.get("author")) or GHOST for c in _nodes(review, "comments")]
    for commit in _commits(node):
        author = _dict(commit.get("author"))
        found.append(_user({**_dict(author.get("user")), "__typename": "User"}))
        email = people_store.NOREPLY.fullmatch(str(author.get("email") or "").strip().lower())
        if email:
            found.append({"id": int(email.group(1)), "login": email.group(2)})
    found += [f.actor for f in _force_pushes(node)]
    users = {gh_id(u.get("id")): u for u in found if u and gh_id(u.get("id")) is not None}
    return [users[i] for i in sorted(users)]


def _store(job: _Job, node: dict[str, Any], extras: dict[str, dict[str, Any] | None]) -> _Stored | None:
    """Write one pull request and everything under it in one transaction."""
    pr, now = _rest_pr(node), job.clock()
    with _begin(job.engine) as conn:
        seen = _seen(conn, _locked_repo(conn, job.repo.id), now)
        # People first and in ID order, so two transactions writing the same people lock them in the same order.
        for user in _users(node):
            seen.person(user)
        key, values = collect._pull_key(seen, pr), collect._pull_values(seen, pr)
        if key is None or values is None:
            return None
        row_id = collect._upsert(conn, gh_pull_requests, key, values, created={"created_at": now},
                                 guard=gh_pull_requests.c.gh_updated_at <= values["gh_updated_at"])
        for review in _nodes(node, "reviews"):
            found = _rest_review(review)
            if found is None:
                continue
            collect._review(seen, {"review": found, "pull_request": pr})
            for comment in _nodes(review, "comments"):
                collect._review_comment(seen, {"comment": _rest_comment(comment, review), "pull_request": pr})
        for commit in _commits(node):
            author = _dict(commit.get("author"))
            if author.get("user"):
                seen.person(_user({**_dict(author["user"]), "__typename": "User"}))
            people_store.add_email(conn, seen.team_id, author.get("email"), now, seen.bots)
        if values["merge_commit_sha"]:
            collect._found_or_added(conn, gh_pr_commits, {"team_id": seen.team_id, "repo_row_id": seen.repo.id,
                                                          "commit_sha": values["merge_commit_sha"]},
                                    {"pr_row_id": row_id, "created_at": now})
        for push in derive_pushes(node):
            collect._push_row(seen, row_id, values["number"], push.before, push.after, user=push.actor,
                              at=push.at, forced=push.forced, source="sync")
        for head in {node.get("headRefOid"), *_review_heads(node)}:
            for suite in _nodes(extras.get(head) if isinstance(head, str) else None, "checkSuites"):
                for run in _nodes(suite, "checkRuns"):
                    collect._check_run(seen, {"check_run": _rest_check(run, head, pr["id"])})
        if values["state"] == "merged":
            paths = [f["path"] for f in _nodes(node, "files") if isinstance(f.get("path"), str) and f["path"]]
            defects.add_files(conn, seen.team_id, seen.repo.id, row_id, paths, now)
        collect._link_revert(seen, row_id, pr)
        reverts = _reverted(seen, pr)
    rounds.pull_request_synced(job.engine, job.repo.team_id, f"{job.repo.full_name}#{values['number']}")
    return _Stored(row_id, node, values["number"], values["state"], reverts)


def _reviews(conn: Connection, team_id: str, pr: Any, target: str) -> tuple[pushes.Review, ...]:
    """Reviews by people other than the author, and Armature's posted reviews, with the files each one named."""
    paths: dict[int, set[str]] = defaultdict(set)
    for row in conn.execute(select(gh_review_comments.c.gh_review_id, gh_review_comments.c.path).where(
            gh_review_comments.c.pr_row_id == pr.id, gh_review_comments.c.deleted.is_(False),
            gh_review_comments.c.gh_review_id.is_not(None), gh_review_comments.c.path.is_not(None))):
        paths[row.gh_review_id].add(row.path)
    found: dict[Any, pushes.Review] = {
        r.gh_review_id: pushes.Review(r.submitted_at, r.head_sha, frozenset(paths[r.gh_review_id]))
        for r in conn.execute(select(gh_reviews.c.gh_review_id, gh_reviews.c.submitted_at, gh_reviews.c.head_sha)
                              .outerjoin(people, people.c.id == gh_reviews.c.reviewer_person_id)
                              .where(gh_reviews.c.pr_row_id == pr.id, gh_reviews.c.reviewer_gh_id != pr.author_gh_id,
                                     (gh_reviews.c.actor_source == attribution.ARMATURE)
                                     | (people.c.kind == "human")))}
    for call in conn.execute(select(tool_calls.c.id, tool_calls.c.run_id, tool_calls.c.detail,
                                    tool_calls.c.created_at).where(
            tool_calls.c.tool.in_(attribution.REVIEW_TOOLS), tool_calls.c.target == target,
            tool_calls.c.team_id == team_id, tool_calls.c.mode == "live")):
        detail = _dict(call.detail)
        if detail.get("posted") is not True:
            continue
        named = frozenset(conn.execute(select(findings.c.path).where(findings.c.run_id == call.run_id,
                                                                     findings.c.path.is_not(None))).scalars())
        stored = found.get(detail.get("review_id"))
        if stored is not None:
            # The call posted a review stored above, so its findings join that review rather than count again.
            found[detail["review_id"]] = pushes.Review(stored.at, stored.head, stored.paths | named)
        else:
            found[call.id] = pushes.Review(call.created_at, detail.get("head") if isinstance(detail.get("head"), str)
                                           else None, named)
    return tuple(found.values())


def _context(conn: Connection, job: _Job, item: _Stored, rows: list[Any],
             extras: dict[str, dict[str, Any] | None], wanted: dict[str, set[str]]) -> pushes.Context:
    node, team_id = item.node, job.repo.team_id
    target = f"{job.repo.full_name}#{item.number}"
    pr = conn.execute(select(gh_pull_requests).where(gh_pull_requests.c.id == item.row_id)).first()
    calls = conn.execute(select(tool_calls.c.detail).where(
        tool_calls.c.tool == attribution.PUSH_TOOL, tool_calls.c.target == target, tool_calls.c.team_id == team_id,
        tool_calls.c.outcome == "approved")).scalars()
    commits = {c["oid"]: pushes.Commit(tuple(p for p in (_oid(n) for n in _nodes(c, "parents")) if p),
                                       str(c.get("messageHeadline") or "")) for c in _commits(node)}
    histories: dict[str, tuple[tuple[str, str], ...] | None] = {}
    for oid, kinds in wanted.items():
        if "history" in kinds and oid in extras:
            history = _nodes(extras[oid], "history") if extras[oid] is not None else None
            histories[oid] = None if history is None else tuple(
                (h["oid"], str(h.get("messageHeadline") or "")) for h in history if isinstance(h.get("oid"), str))
    count = _dict(node.get("commits")).get("totalCount")
    return pushes.Context(
        base_ref=node.get("baseRefName") if isinstance(node.get("baseRefName"), str) else None,
        base_commits=frozenset(s for s in {node.get("baseRefOid"), *(r.base_sha for r in rows)} if isinstance(s, str)),
        armature_commits=frozenset(d["commit"] for d in calls if isinstance(d, dict) and isinstance(d.get("commit"),
                                                                                                     str)),
        commits=commits, histories=histories,
        commit_counts={node["headRefOid"]: count} if isinstance(node.get("headRefOid"), str)
        and isinstance(count, int) else {},
        reviews=_reviews(conn, team_id, pr, target), heads_at={r.after_sha: r.pushed_at for r in rows})


def _on_base(job: _Job, sha: str, base: str) -> bool | None:
    try:
        found = job.gh.rest(f"/repos/{job.repo.full_name}/compare/{sha}...{quote(base, safe='/')}").data
    except ghapi.GitHubError:
        return None
    status = _dict(found).get("status")
    return status in ("ahead", "identical") if isinstance(status, str) else None


def _changed(job: _Job, before: str, after: str) -> frozenset[str] | None:
    try:
        found = job.gh.rest(f"/repos/{job.repo.full_name}/compare/{before}...{after}").data
    except ghapi.GitHubError:
        return None
    files = _dict(found).get("files")
    if not isinstance(files, list):
        return None
    return frozenset(name for f in files if isinstance(f, dict)
                     for name in (f.get("filename"), f.get("previous_filename")) if isinstance(name, str))


def _verdict(job: _Job, row: Any, ctx: pushes.Context) -> pushes.Verdict | None:
    push = pushes.Push(row.before_sha, row.after_sha, row.forced, row.pushed_at,
                       row.actor_source == attribution.ARMATURE)
    for _ in range(ASKS):
        found = pushes.classify(push, ctx)
        if isinstance(found, pushes.Verdict):
            return found
        if isinstance(found, pushes.OnBase):
            ctx.on_base[found.sha] = _on_base(job, found.sha, ctx.base_ref or "")
        else:
            ctx.changed[(found.before, found.after)] = _changed(job, found.before, found.after)
    return None


def _classify(job: _Job, stored: list[_Stored], extras: dict[str, dict[str, Any] | None],
              wanted: dict[str, set[str]]) -> None:
    for item in stored:
        with job.engine.connect() as conn:
            rows = conn.execute(select(gh_pushes).where(gh_pushes.c.pr_row_id == item.row_id)
                                .order_by(gh_pushes.c.pushed_at, gh_pushes.c.id)).all()
            unclassified = [r for r in rows if r.push_class is None]
            if not unclassified:
                continue
            ctx = _context(conn, job, item, rows, extras, wanted)
        for row in unclassified:
            verdict = _verdict(job, row, ctx)
            if verdict is None:
                return
            with job.engine.begin() as conn:
                conn.execute(update(gh_pushes).where(gh_pushes.c.id == row.id, gh_pushes.c.push_class.is_(None))
                             .values(push_class=verdict.push_class, class_confidence=verdict.confidence,
                                     class_evidence=verdict.evidence, classifier_version=pushes.CLASSIFIER_VERSION))


def _refresh(job: _Job, nodes: list[dict[str, Any]]) -> list[_Stored]:
    """Store each pull request fully, then classify its pushes that have no class yet."""
    if not nodes:
        return []
    # The page is paid for, so it is stored whole, and a pause its answer started waits until after.
    with job.gh.page():
        wanted = _wanted(job, nodes)
        extras = _extras(job, wanted)
        stored = [found for found in (_store(job, node, extras) for node in nodes) if found is not None]
        _classify(job, stored, extras, wanted)
    return stored


def _link_fixes(job: _Job, ids: list[str]) -> None:
    for start in range(0, len(ids), FIXES_CHUNK):
        with _begin(job.engine) as conn:
            _locked_repo(conn, job.repo.id)
            defects.link_fixes(conn, job.repo.team_id, ids[start:start + FIXES_CHUNK], job.clock())


def _one(job: _Job, number: int) -> dict[str, Any] | None:
    found = _repository(job, job.gh.graphql(ONE, job.variables(number=number))).get("pullRequest")
    return found if isinstance(found, dict) else None


def _resolve_reverts(job: _Job, pending: list[list[Any]]) -> list[list[Any]]:
    """Link the reverts whose target a later page stored. The rest wait for the next page."""
    if not pending:
        return []
    left = []
    with _begin(job.engine) as conn:
        seen = _seen(conn, _locked_repo(conn, job.repo.id), job.clock())
        for from_row_id, number in pending:
            stored = conn.execute(select(gh_pull_requests.c.id).where(
                gh_pull_requests.c.team_id == seen.team_id, gh_pull_requests.c.repo_row_id == seen.repo.id,
                gh_pull_requests.c.number == number)).first()
            if stored is None:
                left.append([from_row_id, number])
            else:
                collect._link_revert(seen, from_row_id, {"body": f"Reverts #{number}", "title": None})
    return left[-REVERTS_MAX:]


def _floor(conn: Connection, repo: Any, now: datetime) -> datetime:
    """How far back backfill reaches: the repository's own date, else the setting, else DEPTH ago."""
    since = repo.backfill_since
    if since is None:
        value = broker.setting(conn, SINCE_SETTING, repo.team_id, repo.project_id)
        try:
            since = date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            log.warning("ignoring %s=%r, which is not a date such as 2025-05-01", SINCE_SETTING, value)
    return datetime(since.year, since.month, since.day, tzinfo=UTC) if since is not None else now - DEPTH


def _finish_backfill(job: _Job) -> None:
    """Link what pages couldn't, because backfill stores newer pull requests before the older ones they point at."""
    with _begin(job.engine) as conn:
        seen = _seen(conn, _locked_repo(conn, job.repo.id), job.clock())
        for row in conn.execute(select(gh_pull_requests.c.id, gh_pull_requests.c.title).where(
                gh_pull_requests.c.team_id == seen.team_id, gh_pull_requests.c.repo_row_id == seen.repo.id,
                gh_pull_requests.c.title.like('Revert "%'))).all():
            collect._link_revert(seen, row.id, {"title": row.title, "body": None})
        fixes = [r.id for r in conn.execute(select(gh_pull_requests.c.id, gh_pull_requests.c.title).where(
            gh_pull_requests.c.team_id == seen.team_id, gh_pull_requests.c.repo_row_id == seen.repo.id,
            gh_pull_requests.c.state == "merged")) if defects.is_fix(r.title)]
    _link_fixes(job, fixes)


def _backfill(job: _Job) -> dict[str, Any]:
    with job.engine.connect() as conn:
        floor = _floor(conn, job.repo, job.clock())
    cursor = _cursor(job.state.cursor)
    while True:
        answer = job.gh.graphql(BY_CREATED, job.variables(first=PAGE, after=cursor.get("after")))
        page = _dict(_repository(job, answer).get("pullRequests"))
        nodes = [n for n in page.get("nodes") or [] if isinstance(n, dict)]
        keep = [n for n in nodes if (_time(n.get("createdAt")) or floor) >= floor]
        stored = _refresh(job, keep)
        cursor["reverts"] = _resolve_reverts(job, [*cursor.get("reverts", []),
                                                   *([s.row_id, s.reverts] for s in stored if s.reverts)])
        info = _dict(page.get("pageInfo"))
        if len(keep) < len(nodes) or not info.get("hasNextPage") or not info.get("endCursor"):
            _finish_backfill(job)
            # The cursor still points at this page, so moving the floor earlier resumes here.
            return {"cursor": json.dumps({"after": cursor.get("after")}), "next_run_at": None}
        cursor["after"] = info["endCursor"]
        job.save(cursor=json.dumps(cursor))
        if answer.pause_until is not None:
            raise ghapi.Paused(answer.pause_until)
        if not job.time_left():
            return {"next_run_at": job.clock()}


def _poll(job: _Job) -> dict[str, Any]:
    cursor, watermark = _cursor(job.state.cursor), job.state.watermark
    newest = _time(cursor.get("newest"))
    while True:
        answer = job.gh.graphql(BY_UPDATED, job.variables(first=PAGE, after=cursor.get("after")))
        page = _dict(_repository(job, answer).get("pullRequests"))
        nodes = [n for n in page.get("nodes") or [] if isinstance(n, dict)]
        times = [t for t in (_time(n.get("updatedAt")) for n in nodes) if t is not None]
        # A pull request updated in the watermark's own second may not have been seen yet, so it counts as fresh.
        fresh = [n for n in nodes if watermark is None or (_time(n.get("updatedAt")) or watermark) >= watermark]
        newest = max([*times, *([newest] if newest else [])], default=None)
        stored = _refresh(job, fresh)
        _link_fixes(job, [s.row_id for s in stored if s.state == "merged"])
        if any(s.state != "open" for s in stored):
            with _begin(job.engine) as conn:
                fixes.track(conn, job.repo.team_id, job.repo.id, job.clock())
        info = _dict(page.get("pageInfo"))
        # Without a watermark this is the first poll, and backfill covers the history before it.
        if watermark is None or len(fresh) < len(nodes) or not info.get("hasNextPage") or not info.get("endCursor"):
            break
        cursor = {"after": info["endCursor"], "newest": newest.isoformat() if newest else None}
        job.save(cursor=json.dumps(cursor))
        if answer.pause_until is not None:
            raise ghapi.Paused(answer.pause_until)
        if not job.time_left():
            return {"next_run_at": job.clock()}
    job.save(cursor=None, watermark=newest or watermark)
    return {"next_run_at": job.clock() + POLL_EVERY}


def _sample(job: _Job, nodes: list[dict[str, Any]], now: datetime) -> None:
    by_id = {i: n for i, n in ((gh_id(n.get("databaseId")), n) for n in nodes) if i is not None}
    if not by_id:
        return
    with _begin(job.engine) as conn:
        _locked_repo(conn, job.repo.id)
        for row in conn.execute(select(gh_pull_requests.c.id, gh_pull_requests.c.gh_pr_id).where(
                gh_pull_requests.c.team_id == job.repo.team_id, gh_pull_requests.c.repo_row_id == job.repo.id,
                gh_pull_requests.c.gh_pr_id.in_(by_id))).all():
            node = by_id[row.gh_pr_id]
            # GitHub answers UNKNOWN while it computes, so that is stored as unknown, never as clean.
            mergeable = MERGEABLE.get(node.get("mergeable"), "unknown")
            state = (_lower(node.get("mergeStateStatus")) or "")[:32] or None
            last = conn.execute(select(gh_conflict_samples.c.mergeable, gh_conflict_samples.c.merge_state).where(
                gh_conflict_samples.c.pr_row_id == row.id).order_by(gh_conflict_samples.c.sampled_at.desc())
                .limit(1)).first()
            if last is not None and (last.mergeable, last.merge_state) == (mergeable, state):
                continue
            collect._found_or_added(conn, gh_conflict_samples, {"pr_row_id": row.id, "sampled_at": now},
                                    {"team_id": job.repo.team_id, "mergeable": mergeable, "merge_state": state})


def _conflicts(job: _Job) -> dict[str, Any]:
    now, after = job.clock(), None
    while True:
        answer = job.gh.graphql(OPEN, job.variables(first=OPEN_PAGE, after=after))
        page = _dict(_repository(job, answer).get("pullRequests"))
        _sample(job, [n for n in page.get("nodes") or [] if isinstance(n, dict)], now)
        info = _dict(page.get("pageInfo"))
        if not info.get("hasNextPage") or not info.get("endCursor"):
            return {"next_run_at": now + CONFLICTS_EVERY}
        if answer.pause_until is not None:
            raise ghapi.Paused(answer.pause_until)
        if not job.time_left():
            return {"next_run_at": job.clock()}
        after = info["endCursor"]


def _gap_fill(job: _Job) -> dict[str, Any]:
    now = job.clock()
    since, changed = now - GAP_WINDOW, []
    for number in range(1, GAP_PAGES + 1):
        answer = job.gh.rest(f"/repos/{job.repo.full_name}/pulls", {"state": "all", "sort": "updated",
                                                                    "direction": "desc", "per_page": 100,
                                                                    "page": number})
        if not isinstance(answer.data, list):
            raise SyncError(f"GitHub GET /repos/{job.repo.full_name}/pulls did not answer with a list.")
        items = [i for i in answer.data if isinstance(i, dict)]
        ids = [i for i in (gh_id(item.get("id")) for item in items) if i is not None]
        with job.engine.connect() as conn:
            stored = {r.gh_pr_id: r.gh_updated_at for r in conn.execute(
                select(gh_pull_requests.c.gh_pr_id, gh_pull_requests.c.gh_updated_at).where(
                    gh_pull_requests.c.team_id == job.repo.team_id, gh_pull_requests.c.gh_pr_id.in_(ids)))} \
                if ids else {}
        older = False
        for item in items:
            updated, pr_id, pr_number = _time(item.get("updated_at")), gh_id(item.get("id")), item.get("number")
            if updated is None or updated < since:
                older = True
                break
            if pr_id is not None and isinstance(pr_number, int) and (pr_id not in stored or stored[pr_id] < updated):
                changed.append(pr_number)
        if older or len(items) < 100:
            break
        if answer.pause_until is not None:
            raise ghapi.Paused(answer.pause_until)
    with job.engine.connect() as conn:
        # A pause or crash between storing a pull request and classifying its pushes leaves it looking unchanged.
        unclassified = conn.execute(select(gh_pull_requests.c.number).distinct()
                                    .join(gh_pushes, gh_pushes.c.pr_row_id == gh_pull_requests.c.id)
                                    .where(gh_pull_requests.c.team_id == job.repo.team_id,
                                           gh_pull_requests.c.repo_row_id == job.repo.id,
                                           gh_pull_requests.c.gh_updated_at >= since,
                                           gh_pushes.c.push_class.is_(None))).scalars().all()
    changed += sorted(set(unclassified) - set(changed))
    for pr_number in changed[:GAP_MAX]:
        if not job.time_left():
            return {"next_run_at": job.clock()}
        node = _one(job, pr_number)
        if node is not None:
            _link_fixes(job, [s.row_id for s in _refresh(job, [node]) if s.state == "merged"])
    return {"next_run_at": now + GAP_EVERY}


KINDS: dict[str, Callable[[_Job], dict[str, Any]]] = {"backfill": _backfill, "poll": _poll, "conflicts": _conflicts,
                                                       "gap_fill": _gap_fill}


def _safe(exc: Exception) -> str:
    if isinstance(exc, (ghapi.GitHubError, SyncError)):
        return exc.message
    if isinstance(exc, VendorError):
        return str(exc)
    # Only the type, because a database error's message holds the statement's bound values.
    return f"{type(exc).__name__} while syncing."


def _renamed(job: _Job) -> bool:
    """Whether GitHub knows the repository by a new name, looked up by its ID, which the stored name now follows."""
    try:
        found = _dict(job.gh.rest(f"/repositories/{job.repo.gh_repo_id}").data)
    except ghapi.GitHubError:
        return False
    name = found.get("full_name")
    if gh_id(found.get("id")) != job.repo.gh_repo_id or not isinstance(name, str) or not repos.NAME.fullmatch(name) \
            or name.lower() == job.repo.full_name:
        return False
    _rename(job, name.lower())
    return job.repo.full_name == name.lower()


def _work(job: _Job) -> dict[str, Any]:
    try:
        return KINDS[job.state.kind](job)
    except ghapi.GraphQLError as exc:
        # A renamed or transferred repository isn't found under its old name.
        if "NOT_FOUND" not in exc.types or not _renamed(job):
            raise
    with job.engine.connect() as conn:
        job.state = conn.execute(select(gh_sync_state).where(gh_sync_state.c.id == job.state.id)).first()
    return KINDS[job.state.kind](job)


def run(engine: Engine, keyring: Keyring, state_id: str, lease: datetime, *,
        clock: Callable[[], datetime] = utcnow, transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep, budget_s: float = BUDGET_S,
        stopping: threading.Event | None = None) -> str:
    """Work one sync state this server claimed until ``lease``, once, and record how it went.

    Returns a short status for logs and the CLI. Every write needs the lease still in place, so a run that outlived
    it stops instead of overwriting the server that took over.
    """
    with engine.connect() as conn:
        state = conn.execute(select(gh_sync_state).where(gh_sync_state.c.id == state_id)).first()
        repo = conn.execute(select(gh_repos).where(gh_repos.c.id == state.repo_row_id)).first() if state else None
    if state is None or repo is None or not repo.enabled:
        return "skipped"
    try:
        with engine.connect() as conn:
            call = ghapi.call_for(conn, keyring, repo, clock(), transport)
        with ghapi.Client(call, repo.full_name, clock=clock, sleep=sleep) as gh:
            job = _Job(engine, state, repo, gh, clock, time.monotonic() + budget_s, lease, stopping)
            values = _work(job)
            pause = gh.paused_until()
            if pause is not None and values.get("next_run_at") is not None and values["next_run_at"] < pause:
                # The last answer left too few points, so the next run waits for the window too.
                values["next_run_at"] = pause
    except ghapi.Paused as exc:
        written = _write(engine, state_id, lease, clock(), next_run_at=exc.until)
        return f"paused until {exc.until.isoformat()}" if written else LOST
    except _Gone:
        return "skipped"
    except _Lost:
        return LOST
    except Exception as exc:
        message = _safe(exc)
        failures = (state.failures or 0) + 1
        now = clock()
        log.warning("syncing %s (%s) failed: %s", repo.full_name, state.kind, message)
        written = _write(engine, state_id, lease, now, last_error=message[:ERROR_MAX], failures=failures,
                         next_run_at=now + min(BACKOFF * 2 ** min(failures, 10), BACKOFF_MAX))
        return f"error: {message}" if written else LOST
    return "ok" if _write(engine, state_id, lease, clock(), last_error=None, failures=0, **values) else LOST


def _on(value: Any) -> bool:
    return value is not False and not (isinstance(value, str) and value.strip().lower() in OFF)


def due(conn: Connection, now: datetime, limit: int = SCAN) -> list[Any]:
    """Due sync states of enabled repositories, in teams whose insights.sync_enabled isn't off, oldest first."""
    rows = conn.execute(select(settings.c.scope, settings.c.team_id, settings.c.project_id, settings.c.value)
                        .where(settings.c.key == ENABLED_SETTING)).all()
    instance = next((_on(r.value) for r in rows if r.scope == INSTANCE_SCOPE), True)
    teams = {r.team_id: _on(r.value) for r in rows if r.team_id and r.project_id is None}
    query = select(gh_sync_state.c.id, gh_sync_state.c.next_run_at) \
        .join(gh_repos, gh_repos.c.id == gh_sync_state.c.repo_row_id) \
        .where(gh_repos.c.enabled.is_(True), gh_sync_state.c.next_run_at <= now)
    if instance:
        off = [team for team, on in teams.items() if not on]
        if off:
            query = query.where(gh_sync_state.c.team_id.notin_(off))
    else:
        query = query.where(gh_sync_state.c.team_id.in_([team for team, on in teams.items() if on]))
    return conn.execute(query.order_by(gh_sync_state.c.next_run_at, gh_sync_state.c.id).limit(limit)).all()


def claim(conn: Connection, state_id: str, was: datetime, until: datetime) -> bool:
    """Move a due state's next run on to the lease's end, if no other server moved it first."""
    return conn.execute(update(gh_sync_state).where(gh_sync_state.c.id == state_id,
                                                    gh_sync_state.c.next_run_at == was)
                        .values(next_run_at=until)).rowcount == 1


def run_now(engine: Engine, keyring: Keyring, team_id: str, *, repo: str | None = None, kind: str | None = None,
            clock: Callable[[], datetime] = utcnow,
            transport: httpx.BaseTransport | None = None) -> list[tuple[str, str, str]]:
    """Claim and run the team's matching due sync states once, as (repository, kind, status).

    One that isn't due yet may be leased by a running server, so it is reported busy rather than taken.
    """
    query = select(gh_sync_state.c.id, gh_sync_state.c.kind, gh_sync_state.c.next_run_at, gh_repos.c.full_name) \
        .join(gh_repos, gh_repos.c.id == gh_sync_state.c.repo_row_id) \
        .where(gh_sync_state.c.team_id == team_id, gh_repos.c.enabled.is_(True))
    if repo is not None:
        query = query.where(gh_repos.c.full_name == repo.lower())
    if kind is not None:
        query = query.where(gh_sync_state.c.kind == kind)
    with engine.connect() as conn:
        rows = conn.execute(query.order_by(gh_repos.c.full_name, gh_sync_state.c.kind)).all()
    results = []
    for row in rows:
        if row.next_run_at is None:
            results.append((row.full_name, row.kind, "done"))
            continue
        now = clock()
        claimed = False
        if row.next_run_at <= now:
            with engine.begin() as conn:
                claimed = claim(conn, row.id, row.next_run_at, now + LEASE)
        status = run(engine, keyring, row.id, now + LEASE, clock=clock, transport=transport) if claimed else BUSY
        results.append((row.full_name, row.kind, status))
    return results


class SyncService:
    """Claims due sync states every ``interval`` seconds and works them on a small pool, claiming no more than the
    pool has room for, so the rest stay due for another server. Tests leave it stopped, and then ``tick`` works
    every due state inline.
    """

    def __init__(self, engine: Engine, keyring: Keyring, *, clock: Callable[[], datetime] = utcnow,
                 interval: float = TICK_S, transport: Callable[[], httpx.BaseTransport | None] = lambda: None,
                 sleep: Callable[[float], None] = time.sleep):
        self.engine = engine
        self.keyring = keyring
        self.interval = interval
        self._clock = clock
        self._transport = transport
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._busy = 0
        self._busy_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is None:
            self._pool = ThreadPoolExecutor(max_workers=POOL, thread_name_prefix="armature-gh-sync")
            self._thread = threading.Thread(target=self._loop, name="armature-gh-sync", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("GitHub sync tick failed")
            self._stop.wait(self.interval)

    def tick(self) -> list[str]:
        """Claim what is due. Started, hand it to the pool. Stopped, work it here and return each status."""
        now = self._clock()
        if self._pool is None:
            return [self._run(state_id, lease) for state_id, lease in self.claim_due(now)]
        with self._busy_lock:
            room = POOL - self._busy
        for state_id, lease in self.claim_due(now, room):
            with self._busy_lock:
                self._busy += 1
            self._pool.submit(self._counted, state_id, lease)
        return []

    def claim_due(self, now: datetime, limit: int = SCAN) -> list[tuple[str, datetime]]:
        """The due states this server claimed, each with the end of its lease."""
        if limit <= 0:
            return []
        with self.engine.connect() as conn:
            rows = due(conn, now)
        claimed: list[tuple[str, datetime]] = []
        for row in rows:
            if len(claimed) >= limit:
                break
            with self.engine.begin() as conn:
                if claim(conn, row.id, row.next_run_at, now + LEASE):
                    claimed.append((row.id, now + LEASE))
        return claimed

    def _run(self, state_id: str, lease: datetime) -> str:
        try:
            return run(self.engine, self.keyring, state_id, lease, clock=self._clock, transport=self._transport(),
                       sleep=self._sleep, stopping=self._stop)
        except Exception as exc:
            log.error("GitHub sync state %s failed: %s", state_id, type(exc).__name__)
            return "error"

    def _counted(self, state_id: str, lease: datetime) -> None:
        try:
            self._run(state_id, lease)
        finally:
            with self._busy_lock:
                self._busy -= 1

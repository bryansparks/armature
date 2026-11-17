"""Collect GitHub history from a webhook delivery into the gh_ tables, after the response, without calling GitHub.

Only a delivery the webhook admitted is collected, only into the webhook's own team, and only for a repository that
team tracks with tracking on. When the webhook verifies with a GitHub App connection, the delivery's installation
must be one of that connection's. A gh CLI connection's deliveries carry no installation, so they skip that check. A
late event never rolls back newer data, and nothing here stores comment text.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, Table, and_, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import rounds
from armature.server.db import new_id
from armature.server.db.schema import connections as connection_rows
from armature.server.db.schema import (
    gh_check_runs,
    gh_pr_commits,
    gh_pr_links,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    github_installations,
)
from armature.server.insights import attribution, people
from armature.server.insights.people import gh_id
from armature.server.vendors import github as github_vendor

log = logging.getLogger(__name__)

EVENTS: dict[str, frozenset[str] | None] = {
    "pull_request": frozenset({"opened", "edited", "reopened", "closed", "synchronize", "ready_for_review",
                               "converted_to_draft"}),
    "pull_request_review": frozenset({"submitted", "edited", "dismissed"}),
    "pull_request_review_comment": frozenset({"created", "edited", "deleted"}),
    "check_run": frozenset({"completed"}),
    "push": None,
}
DETECTOR_VERSION = "1"
JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b(?!-\d)")
REVERTS = re.compile(r"\bReverts\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(\d{1,9})")
REVERT_TITLE = re.compile(r'^Revert "(.+)"$')
BODY_SCAN = 64 * 1024
BRANCH = "refs/heads/"
EXPLICIT, TITLE_ONLY = 1.0, 0.6


@dataclass
class _Seen:
    conn: Connection
    team_id: str
    repo: Any
    now: datetime
    bots: frozenset[str]
    # The numbers of the pull requests this delivery stored a review or push for.
    touched: set[int] = field(default_factory=set)

    def person(self, user: Any) -> str | None:
        return people.person_for(self.conn, self.team_id, user, self.now, self.bots)

    def target(self, number: int) -> str:
        return f"{self.repo.full_name}#{number}"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found.astimezone(UTC) if found.tzinfo else found.replace(tzinfo=UTC)


def jira_key(head_ref: str | None, title: str | None) -> str | None:
    """The first Jira key in the head branch, else in the title."""
    for text in (head_ref, title):
        found = JIRA_KEY.search(text or "")
        if found:
            return found.group(0)[:64]
    return None


def wanted(event: str | None, payload: Any) -> bool:
    if event not in EVENTS or not isinstance(payload, dict):
        return False
    actions = EVENTS[event]
    return actions is None or payload.get("action") in actions


def _found_or_added(conn: Connection, table: Table, key: dict[str, Any], row: dict[str, Any]) -> tuple[str, bool]:
    """The id of the row at ``key``, inserting ``row`` there when it's missing, and whether this call inserted it."""
    where = [table.c[name] == value for name, value in key.items()]
    found = conn.execute(select(table.c.id).where(*where)).scalar()
    if found is not None:
        return found, False
    row_id = new_id()
    try:
        with conn.begin_nested():
            conn.execute(insert(table).values(id=row_id, **key, **row))
        return row_id, True
    except IntegrityError:
        # Another delivery or the sync service inserted it first. The locking read sees its committed row under
        # MySQL's repeatable read.
        found = conn.execute(select(table.c.id).where(*where).with_for_update()).scalar()
        if found is None:
            raise
        return found, False


def _upsert(conn: Connection, table: Table, key: dict[str, Any], values: dict[str, Any], *,
            created: dict[str, Any] | None = None, guard: Any = None) -> str:
    """Insert ``values`` and ``created`` at ``key``, or update the stored row to ``values`` where ``guard`` holds."""
    row_id, added = _found_or_added(conn, table, key, {**values, **(created or {})})
    if not added:
        query = update(table).where(table.c.id == row_id)
        conn.execute((query.where(guard) if guard is not None else query).values(**values))
    return row_id


def _pull_values(seen: _Seen, pr: dict[str, Any]) -> dict[str, Any] | None:
    user, head, base = _dict(pr.get("user")), _dict(pr.get("head")), _dict(pr.get("base"))
    number, author, login = _int(pr.get("number")), gh_id(user.get("id")), _text(user.get("login"), 191)
    created, updated = _time(pr.get("created_at")), _time(pr.get("updated_at"))
    if None in (number, author, login, created, updated):
        return None
    merged = bool(pr.get("merged")) or pr.get("merged_at") is not None
    title, head_ref = _text(pr.get("title"), 512), _text(head.get("ref"), 255)
    seen.person(pr.get("merged_by"))
    return {"repo_row_id": seen.repo.id, "number": number, "title": title,
            "author_person_id": seen.person(user), "author_gh_id": author, "author_login": login,
            "state": "merged" if merged else "closed" if pr.get("state") == "closed" else "open",
            "draft": bool(pr.get("draft")), "base_ref": _text(base.get("ref"), 255), "head_ref": head_ref,
            "head_repo_gh_id": gh_id(_dict(head.get("repo")).get("id")),
            "head_sha": _text(head.get("sha"), 64), "base_sha": _text(base.get("sha"), 64),
            "merge_commit_sha": _text(pr.get("merge_commit_sha"), 64) if merged else None,
            "additions": _int(pr.get("additions")), "deletions": _int(pr.get("deletions")),
            "changed_files": _int(pr.get("changed_files")), "created_at_gh": created,
            "closed_at_gh": _time(pr.get("closed_at")), "merged_at_gh": _time(pr.get("merged_at")),
            "merged_by_gh_id": gh_id(_dict(pr.get("merged_by")).get("id")), "gh_updated_at": updated,
            "jira_key": jira_key(head_ref, title), "updated_at": seen.now}


def _pull_key(seen: _Seen, pr: dict[str, Any]) -> dict[str, Any] | None:
    pr_id = gh_id(pr.get("id"))
    return {"team_id": seen.team_id, "gh_pr_id": pr_id} if pr_id is not None else None


def _pull_row(seen: _Seen, pr: Any) -> str | None:
    """The stored pull request another event is about, added from that event's copy when it's new.

    A review or comment event's copy lacks fields, so it never updates a stored row.
    """
    pr = _dict(pr)
    key, values = _pull_key(seen, pr), _pull_values(seen, pr)
    if key is None or values is None:
        return None
    return _found_or_added(seen.conn, gh_pull_requests, key, {**values, "created_at": seen.now})[0]


def _push_row(seen: _Seen, pr_row_id: str, number: int, before: Any, after: Any, *, user: Any,
              at: datetime | None, base: str | None = None, forced: bool | None = None,
              source: str = "webhook") -> None:
    before, after = _text(before, 64), _text(after, 64)
    if before is None or after is None:
        return
    pusher, person = _dict(user), seen.person(user)
    ours = attribution.push_is_armatures(seen.conn, seen.team_id, seen.target(number), after)
    created = {"team_id": seen.team_id, "before_sha": before, "pusher_gh_id": gh_id(pusher.get("id")),
               "pusher_login": _text(pusher.get("login"), 191),
               "pusher_person_id": attribution.armature_person(seen.conn, seen.team_id, seen.now) if ours
               else person, "actor_source": attribution.ARMATURE if ours else attribution.GITHUB,
               "pushed_at": at or seen.now, "source": source, "created_at": seen.now}
    key = {"pr_row_id": pr_row_id, "after_sha": after}
    seen.touched.add(number)
    if source == "sync":
        # A row already there wins. Sync only fills in what it lacks.
        _upsert(seen.conn, gh_pushes, key, {"forced": forced}, created={**created, "base_sha": base},
                guard=gh_pushes.c.forced.is_(None))
        return
    if forced is None:
        _upsert(seen.conn, gh_pushes, key, {"base_sha": base}, created=created, guard=gh_pushes.c.base_sha.is_(None))
    else:
        _upsert(seen.conn, gh_pushes, key, {"forced": forced}, created={**created, "base_sha": base})
    # A delivery knows who pushed and when, which a row the sync service derived could only guess.
    seen.conn.execute(update(gh_pushes).where(gh_pushes.c.pr_row_id == pr_row_id, gh_pushes.c.after_sha == after,
                                              gh_pushes.c.source == "sync")
                      .values({k: created[k] for k in ("pusher_gh_id", "pusher_login", "pusher_person_id",
                                                       "actor_source", "pushed_at", "source")}))


def _link_revert(seen: _Seen, row_id: str, pr: dict[str, Any]) -> None:
    body, title = pr.get("body"), pr.get("title")
    found = REVERTS.search(body[:BODY_SCAN]) if isinstance(body, str) else None
    if found:
        name = (found.group(1) or seen.repo.full_name).lower()
        target = seen.conn.execute(
            select(gh_pull_requests.c.id).join(gh_repos, gh_repos.c.id == gh_pull_requests.c.repo_row_id)
            .where(gh_repos.c.team_id == seen.team_id, gh_repos.c.full_name == name,
                   gh_pull_requests.c.team_id == seen.team_id, gh_pull_requests.c.number == int(found.group(2)))
        ).scalar()
        evidence, confidence = {"body": found.group(0)}, EXPLICIT
    else:
        named = REVERT_TITLE.match(title) if isinstance(title, str) else None
        if named is None:
            return
        target = seen.conn.execute(
            select(gh_pull_requests.c.id).where(gh_pull_requests.c.team_id == seen.team_id,
                                                gh_pull_requests.c.repo_row_id == seen.repo.id,
                                                gh_pull_requests.c.title == named.group(1),
                                                gh_pull_requests.c.state == "merged")
            .order_by(gh_pull_requests.c.merged_at_gh.desc()).limit(1)).scalar()
        evidence, confidence = {"title": title[:512]}, TITLE_ONLY
    if target is None or target == row_id:
        return
    _found_or_added(seen.conn, gh_pr_links, {"from_pr_row_id": row_id, "to_pr_row_id": target, "kind": "revert"},
                    {"team_id": seen.team_id, "evidence": evidence, "confidence": confidence,
                     "detector_version": DETECTOR_VERSION, "created_at": seen.now})


def _pull_request(seen: _Seen, payload: dict[str, Any]) -> None:
    pr = _dict(payload.get("pull_request"))
    key, values = _pull_key(seen, pr), _pull_values(seen, pr)
    if key is None or values is None:
        return
    row_id = _upsert(seen.conn, gh_pull_requests, key, values, created={"created_at": seen.now},
                     guard=gh_pull_requests.c.gh_updated_at <= values["gh_updated_at"])
    action = payload.get("action")
    if action == "synchronize":
        _push_row(seen, row_id, values["number"], payload.get("before"), payload.get("after"),
                  user=payload.get("sender"), at=values["gh_updated_at"], base=values["base_sha"])
    elif action == "closed" and values["merge_commit_sha"]:
        _found_or_added(seen.conn, gh_pr_commits, {"team_id": seen.team_id, "repo_row_id": seen.repo.id,
                                                   "commit_sha": values["merge_commit_sha"]},
                        {"pr_row_id": row_id, "created_at": seen.now})
    elif action == "opened":
        _link_revert(seen, row_id, pr)


def _review(seen: _Seen, payload: dict[str, Any]) -> None:
    review, user = _dict(payload.get("review")), _dict(_dict(payload.get("review")).get("user"))
    review_id, reviewer, login = gh_id(review.get("id")), gh_id(user.get("id")), _text(user.get("login"), 191)
    state = _text(review.get("state"), 32)
    if None in (review_id, reviewer, login, state):
        return
    pr_row_id = _pull_row(seen, payload.get("pull_request"))
    if pr_row_id is None:
        return
    body, head, submitted = review.get("body"), _text(review.get("commit_id"), 64), _time(review.get("submitted_at"))
    person = seen.person(user)
    ours = attribution.review_is_armatures(seen.conn, seen.team_id,
                                           seen.target(_dict(payload.get("pull_request"))["number"]), review_id,
                                           login, head, submitted)
    values = {"pr_row_id": pr_row_id, "reviewer_gh_id": reviewer, "reviewer_login": login,
              "reviewer_person_id": attribution.armature_person(seen.conn, seen.team_id, seen.now) if ours
              else person, "actor_source": attribution.ARMATURE if ours else attribution.GITHUB,
              "state": state.lower(), "head_sha": head, "submitted_at": submitted,
              "body_length": len(body) if isinstance(body, str) else 0,
              "url": _text(review.get("html_url"), 512), "updated_at": seen.now}
    # A dismissal is final, so a late submitted or edited event never undoes it.
    guard = None if values["state"] == "dismissed" else gh_reviews.c.state != "dismissed"
    _upsert(seen.conn, gh_reviews, {"team_id": seen.team_id, "gh_review_id": review_id}, values,
            created={"created_at": seen.now}, guard=guard)
    seen.touched.add(_dict(payload.get("pull_request"))["number"])


def _review_comment(seen: _Seen, payload: dict[str, Any]) -> None:
    comment = _dict(payload.get("comment"))
    user = _dict(comment.get("user"))
    comment_id, author, login = gh_id(comment.get("id")), gh_id(user.get("id")), _text(user.get("login"), 191)
    created, updated = _time(comment.get("created_at")), _time(comment.get("updated_at"))
    if None in (comment_id, author, login, created, updated):
        return
    pr_row_id = _pull_row(seen, payload.get("pull_request"))
    if pr_row_id is None:
        return
    body = comment.get("body")
    review_id = gh_id(comment.get("pull_request_review_id"))
    values = {"pr_row_id": pr_row_id, "gh_review_id": review_id, "author_person_id": seen.person(user),
              "author_gh_id": author, "author_login": login, "path": _text(comment.get("path"), 4096),
              "line": _int(comment.get("line")), "head_sha": _text(comment.get("commit_id"), 64),
              "created_at_gh": created, "updated_at_gh": updated, "url": _text(comment.get("html_url"), 512),
              "in_reply_to": gh_id(comment.get("in_reply_to_id"))}
    if isinstance(body, str):
        # The sync service never fetches the text, so the length stays unknown (NULL) or as stored.
        values["body_length"] = len(body)
    deleted = payload.get("action") == "deleted"
    if deleted:
        values["deleted"] = True
    _upsert(seen.conn, gh_review_comments, {"team_id": seen.team_id, "gh_comment_id": comment_id}, values,
            created={"created_at": seen.now, **({} if "body_length" in values else {"body_length": None})},
            guard=None if deleted else gh_review_comments.c.updated_at_gh <= updated)


def _check_run(seen: _Seen, payload: dict[str, Any]) -> None:
    run = _dict(payload.get("check_run"))
    run_id, name, head_sha, status = (gh_id(run.get("id")), _text(run.get("name"), 191),
                                      _text(run.get("head_sha"), 64), _text(run.get("status"), 32))
    if None in (run_id, name, head_sha, status):
        return
    values = {"repo_row_id": seen.repo.id, "name": name, "head_sha": head_sha, "status": status,
              "conclusion": _text(run.get("conclusion"), 32), "started_at": _time(run.get("started_at")),
              "completed_at": _time(run.get("completed_at"))}
    pr_ids = [i for i in (gh_id(_dict(p).get("id")) for p in run.get("pull_requests") or [] if isinstance(p, dict))
              if i is not None]
    if pr_ids:
        pr_row_id = seen.conn.execute(select(gh_pull_requests.c.id).where(
            gh_pull_requests.c.team_id == seen.team_id, gh_pull_requests.c.repo_row_id == seen.repo.id,
            gh_pull_requests.c.gh_pr_id.in_(pr_ids)).order_by(gh_pull_requests.c.gh_pr_id).limit(1)).scalar()
        if pr_row_id is not None:
            values["pr_row_id"] = pr_row_id
    # A completed run never goes back to an earlier status, whichever answer arrives last.
    _upsert(seen.conn, gh_check_runs, {"team_id": seen.team_id, "gh_check_run_id": run_id}, values,
            created={"created_at": seen.now},
            guard=None if status == "completed" else gh_check_runs.c.status != "completed")


def _push(seen: _Seen, payload: dict[str, Any]) -> None:
    ref = payload.get("ref")
    if not isinstance(ref, str) or not ref.startswith(BRANCH) or payload.get("deleted"):
        return
    # A fork's pull request can share the branch name, so the head must live in this repository too.
    prs = seen.conn.execute(select(gh_pull_requests.c.id, gh_pull_requests.c.number).where(
        gh_pull_requests.c.team_id == seen.team_id, gh_pull_requests.c.repo_row_id == seen.repo.id,
        gh_pull_requests.c.state == "open", gh_pull_requests.c.head_ref == ref[len(BRANCH):],
        gh_pull_requests.c.head_repo_gh_id == seen.repo.gh_repo_id)).all()
    if not prs:
        return
    sender = payload.get("sender")
    pusher = seen.person(sender)
    people.add_email(seen.conn, seen.team_id, _dict(payload.get("pusher")).get("email"), seen.now, seen.bots,
                     person_id=pusher)
    emails = {_dict(commit.get(role)).get("email") for commit in payload.get("commits") or []
              if isinstance(commit, dict) for role in ("author", "committer")}
    for email in sorted(e for e in emails if isinstance(e, str)):
        people.add_email(seen.conn, seen.team_id, email, seen.now, seen.bots)
    pushed = _int(_dict(payload.get("repository")).get("pushed_at"))
    at = datetime.fromtimestamp(pushed, UTC) if pushed is not None and pushed > 0 else None
    for pr in prs:
        _push_row(seen, pr.id, pr.number, payload.get("before"), payload.get("after"), user=sender, at=at,
                  forced=bool(payload.get("forced")))


HANDLERS = {"pull_request": _pull_request, "pull_request_review": _review,
            "pull_request_review_comment": _review_comment, "check_run": _check_run, "push": _push}


def _tracked(conn: Connection, team_id: str, payload: dict[str, Any]) -> Any:
    repo_id = gh_id(_dict(payload.get("repository")).get("id"))
    if repo_id is None:
        return None
    # A shared lock, so a removal that locks the row first waits for this delivery, or this one sees it gone.
    return conn.execute(select(gh_repos).where(gh_repos.c.team_id == team_id, gh_repos.c.gh_repo_id == repo_id,
                                               gh_repos.c.enabled.is_(True)).with_for_update(read=True)).first()


def _installed(conn: Connection, hook: Any, payload: dict[str, Any]) -> bool:
    if not hook.connection_id:
        return True
    config = conn.execute(select(connection_rows.c.config).where(connection_rows.c.id == hook.connection_id)).scalar()
    if github_vendor.uses_gh_cli(config):
        return True
    installation = gh_id(_dict(payload.get("installation")).get("id"))
    return installation is not None and conn.execute(select(github_installations.c.id).where(and_(
        github_installations.c.connection_id == hook.connection_id,
        github_installations.c.installation_id == installation))).first() is not None


def collect(engine: Engine, hook: Any, event: str | None, payload: Any, now: datetime) -> None:
    """Store one admitted GitHub delivery's facts. Runs after the response, so a failure is only logged.

    The log names only the error's type, because the statement's bound values hold logins and emails.
    """
    try:
        if not wanted(event, payload):
            return
        with engine.begin() as conn:
            if conn.dialect.name == "sqlite":
                # pysqlite opens no transaction before a SAVEPOINT, so releasing the first one would commit it alone.
                conn.exec_driver_sql("BEGIN")
            repo = _tracked(conn, hook.team_id, payload)
            if repo is None:
                return
            if not _installed(conn, hook, payload):
                log.warning("skipped a GitHub %s delivery to webhook %s: its installation is not the connection's",
                            event, hook.id)
                return
            seen = _Seen(conn, hook.team_id, repo, now, people.bot_logins(conn, hook.team_id))
            HANDLERS[event](seen, payload)
        # After the commit, in transactions of their own, so the rounds read what this delivery stored.
        for number in sorted(seen.touched):
            rounds.pull_request_synced(engine, seen.team_id, seen.target(number))
    except Exception as exc:
        log.error("collecting a GitHub %s delivery to webhook %s failed: %s", event, hook.id, type(exc).__name__)

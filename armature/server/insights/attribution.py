"""Which reviews and pushes Armature made, told by what the broker recorded rather than by login.

A gh CLI connection posts under a person's own login, so the login can't tell Armature's review from theirs. A
review is Armature's when a posted github.post-review or review.record-post call on the same pull request names its
review ID, or, with no ID, judged its head within REVIEW_WINDOW of its submission. A push is Armature's when an
approved github.push call on the pull request names its commit. Each is credited to the team's one Armature person.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server.db import new_id
from armature.server.db.schema import (
    gh_pull_requests,
    gh_repos,
    gh_reviews,
    people,
    person_identities,
    tool_calls,
)
from armature.server.insights.people import own_logins

log = logging.getLogger(__name__)

REVIEW_TOOLS = ("github.post-review", "review.record-post")
PUSH_TOOL = "github.push"
REVIEW_WINDOW = timedelta(minutes=10)
ARMATURE = "armature"
GITHUB = "github"


def armature_person(conn: Connection, team_id: str, now: datetime) -> str:
    """The team's Armature person, made the first time it is needed. The loser of a race reads the winner's."""
    def stored(locked: bool = False) -> Any:
        query = select(person_identities.c.person_id).where(
            person_identities.c.team_id == team_id, person_identities.c.kind == ARMATURE,
            person_identities.c.value == ARMATURE)
        return conn.execute(query.with_for_update() if locked else query).scalar()

    found = stored()
    if found is not None:
        return found
    person_id = new_id()
    try:
        with conn.begin_nested():
            conn.execute(insert(people).values(id=person_id, team_id=team_id, display_name="Armature", kind="bot",
                                               created_at=now, updated_at=now))
            conn.execute(insert(person_identities).values(id=new_id(), team_id=team_id, person_id=person_id,
                                                          kind=ARMATURE, value=ARMATURE, verified=True,
                                                          first_seen=now, last_seen=now))
        return person_id
    except IntegrityError:
        found = stored(locked=True)
        if found is None:
            raise
        return found


def _calls(conn: Connection, team_id: str, tools: tuple[str, ...], target: str) -> list[Any]:
    return conn.execute(select(tool_calls.c.detail, tool_calls.c.created_at).where(
        tool_calls.c.tool.in_(tools), tool_calls.c.target == target, tool_calls.c.team_id == team_id,
        tool_calls.c.mode == "live")).all()


def review_matches(detail: Any, called_at: datetime, review_id: int, head: str | None,
                   submitted: datetime | None) -> bool:
    if not isinstance(detail, dict) or detail.get("posted") is not True:
        return False
    if detail.get("review_id") is not None:
        return detail["review_id"] == review_id
    return bool(head) and detail.get("head") == head and submitted is not None \
        and abs(called_at - submitted) <= REVIEW_WINDOW


def review_is_armatures(conn: Connection, team_id: str, target: str, review_id: int, login: str, head: str | None,
                        submitted: datetime | None) -> bool:
    """Whether a review on ``target`` (owner/name#number) is one Armature posted, under a login it posts as."""
    if login.lower() not in own_logins(conn, team_id):
        return False
    return any(review_matches(r.detail, r.created_at, review_id, head, submitted)
               for r in _calls(conn, team_id, REVIEW_TOOLS, target))


def push_is_armatures(conn: Connection, team_id: str, target: str, after_sha: str) -> bool:
    return any(isinstance(r.detail, dict) and r.detail.get("commit") == after_sha
               for r in conn.execute(select(tool_calls.c.detail).where(
                   tool_calls.c.tool == PUSH_TOOL, tool_calls.c.target == target, tool_calls.c.team_id == team_id,
                   tool_calls.c.outcome == "approved")))


def claim_reviews(engine: Engine, team_id: str, target: str | None, detail: Any, now: datetime) -> None:
    """Credit Armature with stored reviews a review call just recorded, for when the webhook came first.

    The broker calls this after writing the call's row. A failure is only logged, so a review post never fails on it.
    """
    try:
        if not target or "#" not in target or not isinstance(detail, dict) or detail.get("posted") is not True:
            return
        name, number = target.rsplit("#", 1)
        if not number.isdigit():
            return
        with engine.begin() as conn:
            logins = own_logins(conn, team_id)
            rows = conn.execute(select(gh_reviews).join(gh_pull_requests,
                                                        gh_pull_requests.c.id == gh_reviews.c.pr_row_id)
                                .join(gh_repos, gh_repos.c.id == gh_pull_requests.c.repo_row_id)
                                .where(gh_reviews.c.team_id == team_id, gh_repos.c.full_name == name,
                                       gh_pull_requests.c.number == int(number),
                                       gh_reviews.c.actor_source == GITHUB)).all()
            claimed = [r.id for r in rows if r.reviewer_login.lower() in logins
                       and review_matches(detail, now, r.gh_review_id, r.head_sha, r.submitted_at)]
            if claimed:
                conn.execute(update(gh_reviews).where(gh_reviews.c.id.in_(claimed)).values(
                    reviewer_person_id=armature_person(conn, team_id, now), actor_source=ARMATURE, updated_at=now))
    except Exception as exc:
        # Only the error's type, because the statement's bound values hold logins.
        log.error("crediting Armature with its review on %s failed: %s", target, type(exc).__name__)

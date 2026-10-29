"""Threads: the Jira issue a run's work belongs to, so the runs on its pull request list beside it."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, String, cast, select, update

from armature.server.db.schema import runs

# Bounded so a key always fits runs.thread.
KEY = r"[A-Z][A-Z0-9_]{1,100}-[1-9][0-9]{0,9}"
ISSUE_KEY = re.compile(KEY)
TITLE_KEY = re.compile(rf"\[(?P<key>{KEY})\]")
PULL_SUBJECT = re.compile(r"^(?P<repo>[^#\s]+/[^#\s]+)#(?P<number>[1-9][0-9]{0,9})$")
PULL_URL = re.compile(r"https://github\.com/(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)"
                      r"/pull/(?P<number>[1-9][0-9]{0,9})/?")
PULL_LABEL = "pull request"
# An explicit thread id a trigger or run sets, such as slack:C1:1700.1, web:<id>, or gh:acme/app#7.
THREAD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,190}$")
# Prefixes the server's own sources mint, and the webhook source that may also render each. No webhook is "web".
RESERVED = {"web:": "web", "slack:": "slack"}
LINK_LOOKBACK = timedelta(days=30)
LINK_CANDIDATES = 20


def thread_of(subject: str | None, summary: Any) -> str | None:
    """A run's issue key: its subject when that is one, or the ``[KEY]`` its pull request's title starts with."""
    if not subject:
        return None
    if ISSUE_KEY.fullmatch(subject):
        return subject
    title = summary.get("title") if isinstance(summary, dict) else None
    match = TITLE_KEY.match(title) if isinstance(title, str) and PULL_SUBJECT.fullmatch(subject) else None
    return match["key"] if match else None


def linked_pulls(summary: Any) -> list[str]:
    """The pull request subjects a summary's "Pull request" links name."""
    links = summary.get("links") if isinstance(summary, dict) else None
    pulls = []
    for link in links if isinstance(links, list) else []:
        label, url = (link.get("label"), link.get("url")) if isinstance(link, dict) else (None, None)
        match = PULL_URL.fullmatch(url) if isinstance(label, str) and isinstance(url, str) else None
        if match and label.strip().lower() == PULL_LABEL:
            pulls.append(f"{match['repo']}#{match['number']}")
    return pulls


def links(subject: str | None, summary: Any) -> list[dict[str, Any]]:
    """A summary's links, with the pull request a pull request subject names when no link already points at it."""
    reported = summary.get("links") if isinstance(summary, dict) else None
    shown = [link for link in reported if isinstance(link, dict)] if isinstance(reported, list) else []
    match = PULL_SUBJECT.fullmatch(subject or "")
    if match and subject not in linked_pulls({"links": shown}):
        shown.insert(0, {"label": "Pull request", "url": f"https://github.com/{match['repo']}/pull/{match['number']}"})
    return shown


def joining(subject: str | None, summary: Any) -> tuple[str | None, list[str]]:
    """A run's thread, and the subjects whose unthreaded runs in its team join that thread with it."""
    thread = thread_of(subject, summary)
    if thread is None:
        return None, []
    return thread, linked_pulls(summary) if thread == subject else [subject]


def adopt(conn: Connection, team_id: str, subjects: list[str], thread: str | None) -> None:
    if thread is not None and subjects:
        conn.execute(update(runs).where(runs.c.team_id == team_id, runs.c.subject.in_(subjects),
                                        runs.c.thread.is_(None)).values(thread=thread))


def opening_thread(conn: Connection, team_id: str, subject: str | None, now: datetime) -> str | None:
    """A new run's thread: its own, an earlier run's on the same pull request, or the issue run linking it."""
    own = thread_of(subject, None)
    if own is not None or subject is None or not PULL_SUBJECT.fullmatch(subject):
        return own
    sibling = conn.execute(select(runs.c.thread).where(runs.c.team_id == team_id, runs.c.subject == subject,
                                                       runs.c.thread.is_not(None))
                           .order_by(runs.c.started_at.desc(), runs.c.id.desc()).limit(1)).scalar()
    if sibling is not None:
        return sibling
    repo, _, number = subject.rpartition("#")
    # A coarse text match that linked_pulls then confirms, so /pull/12 never takes /pull/123's issue.
    candidates = conn.execute(
        select(runs.c.subject, runs.c.summary)
        .where(runs.c.team_id == team_id, runs.c.started_at >= now - LINK_LOOKBACK, runs.c.subject != subject,
               cast(runs.c.summary, String).contains(f"github.com/{repo}/pull/{number}", autoescape=True))
        .order_by(runs.c.started_at.desc(), runs.c.id.desc()).limit(LINK_CANDIDATES)).all()
    return next((row.subject for row in candidates
                 if ISSUE_KEY.fullmatch(row.subject or "") and subject in linked_pulls(row.summary)), None)

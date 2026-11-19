"""People: the humans and bots behind a team's GitHub activity, each found by its identities.

A person is keyed on a GitHub user ID. Logins change, so each login seen adds an identity and old ones stay. A
GitHub noreply email carries the user ID and maps itself. Any other email is kept unverified, for a lead to confirm.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server.db import new_id
from armature.server.db.schema import connections, people, person_identities

BOT_SUFFIX = "[bot]"
NOREPLY = re.compile(r"^(\d{1,18})\+([a-z0-9-]{1,39}(?:\[bot\])?)@users\.noreply\.github\.com$")
VALUE_MAX = 191
SEEN_EVERY = timedelta(hours=1)


def gh_id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def own_logins(conn: Connection, team_id: str, keys: tuple[str, ...] = ("bot_login", "login")) -> frozenset[str]:
    """The logins Armature acts as in this team: each GitHub connection's App bot and gh CLI login."""
    found = set()
    for identity in conn.execute(select(connections.c.identity).where(connections.c.team_id == team_id,
                                                                      connections.c.kind == "github_app")).scalars():
        for key in keys:
            value = (identity or {}).get(key)
            if isinstance(value, str) and value:
                found.add(value.lower())
    return frozenset(found)


def bot_logins(conn: Connection, team_id: str) -> frozenset[str]:
    """The App bots' logins only. A gh CLI login is a person's own account, so it is never a bot."""
    return own_logins(conn, team_id, ("bot_login",))


def is_bot(user: dict[str, Any], bots: frozenset[str]) -> bool:
    login = str(user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith(BOT_SUFFIX) or login in bots


def _identity(conn: Connection, team_id: str, kind: str, value: str, *, locked: bool = False) -> Any:
    """The team's identity row. ``locked`` after a lost insert race, so MySQL's repeatable read sees the winner's."""
    query = select(person_identities).where(person_identities.c.team_id == team_id, person_identities.c.kind == kind,
                                            person_identities.c.value == value)
    return conn.execute(query.with_for_update() if locked else query).first()


def _seen(conn: Connection, row: Any, now: datetime) -> None:
    """Move last_seen forward at most hourly, so busy pull requests don't rewrite identity rows on every event."""
    if row.last_seen is None or row.last_seen < now - SEEN_EVERY:
        conn.execute(update(person_identities).where(person_identities.c.id == row.id).values(last_seen=now))


def add_identity(conn: Connection, team_id: str, person_id: str, kind: str, value: str, verified: bool,
                 now: datetime) -> None:
    """Add the identity unless the team has it already. One the team has for someone else stays theirs."""
    found = _identity(conn, team_id, kind, value)
    if found is None:
        try:
            with conn.begin_nested():
                conn.execute(insert(person_identities).values(id=new_id(), team_id=team_id, person_id=person_id,
                                                              kind=kind, value=value, verified=verified,
                                                              first_seen=now, last_seen=now))
            return
        except IntegrityError:
            found = _identity(conn, team_id, kind, value, locked=True)
            if found is None:
                raise
    if found.person_id == person_id:
        _seen(conn, found, now)


def person_for(conn: Connection, team_id: str, user: Any, now: datetime, bots: frozenset[str]) -> str | None:
    """The person a GitHub user object names, made with its ID and login identities the first time it is seen."""
    if not isinstance(user, dict):
        return None
    user_id, login = gh_id(user.get("id")), user.get("login")
    if user_id is None or not isinstance(login, str) or not login or len(login) > VALUE_MAX:
        return None
    bot = is_bot(user, bots)
    found = _identity(conn, team_id, "github_id", str(user_id))
    if found is None:
        person_id = new_id()
        try:
            with conn.begin_nested():
                conn.execute(insert(people).values(id=person_id, team_id=team_id, display_name=login,
                                                   kind="bot" if bot else "human", created_at=now, updated_at=now))
                conn.execute(insert(person_identities).values(id=new_id(), team_id=team_id, person_id=person_id,
                                                              kind="github_id", value=str(user_id), verified=True,
                                                              first_seen=now, last_seen=now))
        except IntegrityError:
            found = _identity(conn, team_id, "github_id", str(user_id), locked=True)
            if found is None:
                raise
    if found is not None:
        person_id = found.person_id
        _seen(conn, found, now)
        if bot:
            conn.execute(update(people).where(people.c.id == person_id, people.c.kind != "bot")
                         .values(kind="bot", updated_at=now))
    add_identity(conn, team_id, person_id, "github_login", login.lower(), True, now)
    return person_id


def add_email(conn: Connection, team_id: str, email: Any, now: datetime, bots: frozenset[str],
              person_id: str | None = None) -> None:
    """Map a GitHub noreply email to its user's person. Another email joins ``person_id`` unverified, if given."""
    if not isinstance(email, str):
        return
    value = email.strip().lower()
    if not value or len(value) > VALUE_MAX or "@" not in value:
        return
    found = NOREPLY.fullmatch(value)
    if found:
        owner = person_for(conn, team_id, {"id": int(found.group(1)), "login": found.group(2)}, now, bots)
        if owner is not None:
            add_identity(conn, team_id, owner, "email", value, True, now)
    elif person_id is not None:
        add_identity(conn, team_id, person_id, "email", value, False, now)

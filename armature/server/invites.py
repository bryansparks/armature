"""Single-use invites: the only way a local user sets a password and enrolls an authenticator.

Only an HMAC of the token is stored. The link carries the token in its URL
fragment, so it never reaches a server or proxy log.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets as _random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pyotp
from sqlalchemy import Connection, Engine, insert, select, update

from armature.server import audit
from armature.server.auth import (
    AuthError,
    begin_attempt,
    confirm_enrollment,
    finish_attempt,
    match_totp,
    otpauth_uri,
    start_session,
    totp_secret,
)
from armature.server.crypto import Keyring
from armature.server.db import new_id
from armature.server.db.schema import invites, users
from armature.server.passwords import hash_password
from armature.server.users import totp_aad

LIFETIME = timedelta(hours=72)
REFUSED = "This invite link is not valid, or the code was not accepted."


def invite_hmac(keyring: Keyring, token: str) -> str:
    return hmac.new(keyring.subkey("sessions"), b"invite." + token.encode(), hashlib.sha256).hexdigest()


def _valid_token(token: Any) -> bool:
    return isinstance(token, str) and 20 <= len(token) <= 64 and token.replace("-", "").replace("_", "").isalnum()


def issue(conn: Connection, keyring: Keyring, user_id: str, *, created_by: str | None, now: datetime,
          ip: str | None = None) -> str:
    """A new invite for the user. Any earlier unused invite stops working."""
    conn.execute(update(invites).where(invites.c.user_id == user_id, invites.c.used_at.is_(None))
                 .values(used_at=now))
    # Whoever saw the old invite saw its authenticator secret, so an unconfirmed one is made again.
    conn.execute(update(users).where(users.c.id == user_id, users.c.totp_confirmed_at.is_(None))
                 .values(totp_secret_enc=None))
    token = _random.token_urlsafe(32)
    invite_id = new_id()
    conn.execute(insert(invites).values(id=invite_id, token_hmac=invite_hmac(keyring, token), user_id=user_id,
                                        created_by=created_by, created_at=now, expires_at=now + LIFETIME))
    audit.record(conn, action="invite.issue", actor_id=created_by, target_kind="user", target_id=user_id,
                 detail={"invite_id": invite_id, "expires_at": (now + LIFETIME).isoformat()}, ip=ip)
    return token


def _open_invite(conn: Connection, keyring: Keyring, token: Any, now: datetime) -> tuple[Any, Any] | None:
    if not _valid_token(token):
        return None
    row = conn.execute(select(invites).where(invites.c.token_hmac == invite_hmac(keyring, token))).first()
    if row is None or row.used_at is not None or row.expires_at <= now:
        return None
    user = conn.execute(select(users).where(users.c.id == row.user_id)).first()
    if user is None or user.disabled or user.totp_confirmed_at is not None:
        return None
    return row, user


def _throttle_key(keyring: Keyring, token: Any) -> str:
    """Throttle per invite, so guessing tokens is limited by address and a real invite by its own failures."""
    return "invite:" + (invite_hmac(keyring, token)[:24] if _valid_token(token) else "invalid")


@dataclass(frozen=True)
class Inspected:
    username: str
    otpauth_uri: str


def inspect(engine: Engine, keyring: Keyring, token: Any, *, ip: str, now: datetime) -> Inspected:
    """What the invite page shows. The authenticator secret is made and sealed on the first look."""
    attempt_id = begin_attempt(engine, ip=ip, username=_throttle_key(keyring, token), now=now)
    with engine.begin() as conn:
        opened = _open_invite(conn, keyring, token, now)
        if opened is not None:
            _, user = opened
            if user.totp_secret_enc is None:
                conn.execute(update(users).where(users.c.id == user.id, users.c.totp_secret_enc.is_(None)).values(
                    totp_secret_enc=keyring.seal(pyotp.random_base32(), totp_aad(user.id), label="totp")))
            user = conn.execute(select(users).where(users.c.id == user.id)).one()
            uri = otpauth_uri(keyring, user)
        finish_attempt(conn, attempt_id, opened is not None, now)
    if opened is None or uri is None:
        raise AuthError(401, REFUSED)
    return Inspected(username=user.username, otpauth_uri=uri)


@dataclass(frozen=True)
class Accepted:
    token: str
    csrf: str
    user_id: str


class _Refused(Exception):
    pass


def accept(engine: Engine, keyring: Keyring, token: Any, *, password: str, code: Any, ip: str, now: datetime,
           cost: int | None = None) -> Accepted:
    """Set the password, confirm the authenticator, use up the invite, and start a session, all or nothing."""
    attempt_id = begin_attempt(engine, ip=ip, username=_throttle_key(keyring, token), now=now)
    password_hash = hash_password(password, **({"cost": cost} if cost else {}))
    try:
        with engine.begin() as conn:
            opened = _open_invite(conn, keyring, token, now)
            if opened is None:
                raise _Refused
            invite, user = opened
            step = match_totp(totp_secret(keyring, user), code, now)
            if step is None:
                raise _Refused
            used = conn.execute(update(invites).where(invites.c.id == invite.id, invites.c.used_at.is_(None))
                                .values(used_at=now))
            if used.rowcount != 1 or not confirm_enrollment(conn, user.id, step, now):
                raise _Refused
            conn.execute(update(users).where(users.c.id == user.id)
                         .values(password_hash=password_hash, last_login_at=now))
            finish_attempt(conn, attempt_id, True, now)
            session_token, csrf = start_session(conn, keyring, user.id, now, ip=ip, step_up=True)
            audit.record(conn, action="invite.accept", actor_id=user.id, target_kind="user", target_id=user.id,
                         detail={"invite_id": invite.id}, ip=ip)
    except _Refused:
        with engine.begin() as conn:
            audit.record(conn, action="invite.accept", actor_id=None, detail={"outcome": "refused"}, ip=ip)
        raise AuthError(401, REFUSED) from None
    return Accepted(token=session_token, csrf=csrf, user_id=user.id)


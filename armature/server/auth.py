"""Password and TOTP sign-in, server-side sessions, step-up, and throttles.

Ported from the armature-ops dashboard. Its in-process and advisory locks are
replaced by portable row operations: every attempt is recorded before it is
judged, so parallel guesses count against each other, and a TOTP step is
claimed with one conditional UPDATE whose row count decides the winner.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets as _random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pyotp
from sqlalchemy import Connection, Engine, delete, func, insert, or_, select, update

from armature.server import audit
from armature.server.crypto import Keyring, SealError
from armature.server.db import new_id
from armature.server.db.schema import login_attempts, sessions, users
from armature.server.passwords import hash_password, verify_password
from armature.server.users import USERNAME, normal_username, totp_aad

IDLE = timedelta(minutes=30)
ABSOLUTE = timedelta(hours=12)
STEP_UP = timedelta(minutes=5)
TOUCH = timedelta(seconds=60)
FAILURE_WINDOW = timedelta(minutes=15)
ATTEMPT_RETENTION = timedelta(days=30)
PER_IP_MAX = 5
PER_USER_FREE = 3
MAX_DELAY = timedelta(minutes=15)
TOTP_PERIOD = 30
# Every name that is not a valid username shares one throttle and is never stored as typed.
INVALID_USERNAME = "(invalid)"
GENERIC = "Sign-in failed. Check the username, password, and code."
GENERIC_PASSWORD = "Sign-in failed. Check the username and password."
THROTTLED = "Too many attempts. Wait and try again."
TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class AuthError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


@dataclass(frozen=True)
class Session:
    token_hmac: str
    csrf: str
    created_at: datetime
    last_seen_at: datetime
    step_up_at: datetime | None
    user: Any

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def sso(self) -> bool:
        """Signs in only through OIDC, so step-up means a fresh OIDC sign-in."""
        return self.user.password_hash is None and self.user.oidc_sub is not None

    def has_step_up(self, now: datetime) -> bool:
        return self.step_up_at is not None and now - self.step_up_at <= STEP_UP


def token_hmac(keyring: Keyring, token: str) -> str:
    return hmac.new(keyring.subkey("sessions"), token.encode("ascii"), hashlib.sha256).hexdigest()


def valid_token(token: str | None) -> bool:
    return bool(token) and len(token) == 43 and set(token) <= TOKEN_CHARS


# One IPv6 holder usually owns a whole /64, so the per-address limit counts the prefix.
def ip_bucket(ip: str | None) -> str:
    text = str(ip or "").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text[:45]
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


def backoff_delay(failures: int, free: int = PER_USER_FREE) -> timedelta:
    if failures < free:
        return timedelta(0)
    return min(MAX_DELAY, timedelta(seconds=2 ** min(failures - free, 20)))


def safe_equal(provided: Any, expected: str) -> bool:
    if not isinstance(provided, str):
        return False
    return hmac.compare_digest(hashlib.sha256(provided.encode()).digest(), hashlib.sha256(expected.encode()).digest())


def dummy_hash(cost: int | None = None) -> str:
    return hash_password(_random.token_hex(16), **({"cost": cost} if cost else {}))


def totp_secret(keyring: Keyring, user: Any) -> str | None:
    if not user.totp_secret_enc:
        return None
    try:
        return keyring.open_(user.totp_secret_enc, totp_aad(user.id), label="totp")
    except SealError:
        return None


def match_totp(secret: str | None, code: Any, now: datetime) -> int | None:
    """The step a code matches within one step either side, or None."""
    if not secret or not isinstance(code, str):
        return None
    code = "".join(code.split())
    if len(code) != 6 or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    step = int(now.timestamp()) // TOTP_PERIOD
    for candidate in (step - 1, step, step + 1):
        if hmac.compare_digest(totp.generate_otp(candidate), code):
            return candidate
    return None


def claim_totp_step(conn: Connection, user_id: str, step: int) -> bool:
    result = conn.execute(
        update(users).where(users.c.id == user_id,
                            or_(users.c.last_totp_step.is_(None), users.c.last_totp_step < step))
        .values(last_totp_step=step))
    return result.rowcount == 1


def load_session(conn: Connection, keyring: Keyring, token: str | None, now: datetime,
                 touch: bool = True) -> Session | None:
    if not valid_token(token):
        return None
    digest = token_hmac(keyring, token)
    row = conn.execute(select(sessions).where(sessions.c.token_hmac == digest)).first()
    if row is None:
        return None
    user = conn.execute(select(users).where(users.c.id == row.user_id)).first()
    if now - row.created_at > ABSOLUTE or now - row.last_seen_at > IDLE or user is None or user.disabled:
        conn.execute(delete(sessions).where(sessions.c.token_hmac == digest))
        return None
    last_seen = row.last_seen_at
    if touch and now - last_seen > TOUCH:
        conn.execute(update(sessions).where(sessions.c.token_hmac == digest).values(last_seen_at=now))
        last_seen = now
    return Session(token_hmac=digest, csrf=row.csrf, created_at=row.created_at, last_seen_at=last_seen,
                   step_up_at=row.step_up_at, user=user)


def start_session(conn: Connection, keyring: Keyring, user_id: str, now: datetime, *, ip: str | None,
                  step_up: bool) -> tuple[str, str]:
    token = _random.token_urlsafe(32)
    csrf = _random.token_urlsafe(32)
    conn.execute(insert(sessions).values(token_hmac=token_hmac(keyring, token), user_id=user_id, csrf=csrf,
                                         created_at=now, last_seen_at=now, step_up_at=now if step_up else None,
                                         ip=(ip or "")[:45] or None))
    conn.execute(delete(sessions).where(or_(sessions.c.created_at < now - ABSOLUTE,
                                            sessions.c.last_seen_at < now - IDLE)))
    return token, csrf


def end_session(conn: Connection, digest: str) -> None:
    conn.execute(delete(sessions).where(sessions.c.token_hmac == digest))


def end_user_sessions(conn: Connection, user_id: str, keep: str | None = None) -> None:
    query = delete(sessions).where(sessions.c.user_id == user_id)
    if keep:
        query = query.where(sessions.c.token_hmac != keep)
    conn.execute(query)


def begin_attempt(engine: Engine, *, ip: str, username: str, now: datetime) -> str:
    """Record a failed attempt up front, then refuse it if this address or name is over its limit.

    Recording first means parallel guesses see each other, so they cannot all slip under the limit.
    """
    bucket = ip_bucket(ip)
    attempt_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(login_attempts).values(id=attempt_id, username=username, ip=bucket, succeeded=False,
                                                   at=now))
    since = now - FAILURE_WINDOW
    with engine.connect() as conn:
        ip_failures = conn.execute(
            select(func.count()).select_from(login_attempts)
            .where(login_attempts.c.ip == bucket, login_attempts.c.succeeded.is_(False),
                   login_attempts.c.at > since)).scalar_one()
        last_ok = conn.execute(
            select(func.max(login_attempts.c.at))
            .where(login_attempts.c.username == username, login_attempts.c.succeeded.is_(True))).scalar_one()
        failures = (select(func.count(), func.max(login_attempts.c.at))
                    .where(login_attempts.c.username == username, login_attempts.c.succeeded.is_(False),
                           login_attempts.c.at > since, login_attempts.c.id != attempt_id))
        if last_ok is not None:
            failures = failures.where(login_attempts.c.at >= last_ok)
        user_failures, user_last = conn.execute(failures).one()
    wait = timedelta(0)
    if ip_failures > PER_IP_MAX:
        wait = FAILURE_WINDOW
    elif user_last is not None:
        delay = backoff_delay(user_failures)
        if delay:
            wait = max(timedelta(0), user_last + delay - now)
    if wait > timedelta(0):
        with engine.begin() as conn:
            conn.execute(delete(login_attempts).where(login_attempts.c.id == attempt_id))
        retry = int(max(wait, timedelta(seconds=60)).total_seconds())
        raise AuthError(429, THROTTLED, {"Retry-After": str(retry)})
    return attempt_id


def count_call(engine: Engine, *, key: str, limit: int, now: datetime, window: timedelta = FAILURE_WINDOW) -> None:
    """Allow ``limit`` calls per key in ``window``. Recorded first, like a sign-in attempt."""
    call_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(login_attempts).values(id=call_id, username=key, ip=None, succeeded=True, at=now))
        calls = conn.execute(select(func.count()).select_from(login_attempts).where(
            login_attempts.c.username == key, login_attempts.c.at > now - window)).scalar_one()
    if calls > limit:
        with engine.begin() as conn:
            conn.execute(delete(login_attempts).where(login_attempts.c.id == call_id))
        raise AuthError(429, THROTTLED, {"Retry-After": str(int(window.total_seconds()))})


def finish_attempt(conn: Connection, attempt_id: str, ok: bool, now: datetime) -> None:
    if ok:
        conn.execute(update(login_attempts).where(login_attempts.c.id == attempt_id).values(succeeded=True))
        conn.execute(delete(login_attempts).where(login_attempts.c.at < now - ATTEMPT_RETENTION))


@dataclass(frozen=True)
class SignedIn:
    token: str
    csrf: str
    user_id: str


def login(engine: Engine, keyring: Keyring, *, username: Any, password: Any, code: Any, ip: str, now: datetime,
          dummy: str, old_token: str | None = None, require_totp: bool = True) -> SignedIn:
    typed = normal_username(username)
    name = typed if USERNAME.fullmatch(typed) else INVALID_USERNAME
    actor_name = None if name == INVALID_USERNAME else name
    try:
        attempt_id = begin_attempt(engine, ip=ip, username=name, now=now)
    except AuthError:
        with engine.begin() as conn:
            audit.record(conn, action="login", actor_id=None, ip=ip,
                         detail={"outcome": "throttled", "username": actor_name})
        raise
    with engine.connect() as conn:
        user = conn.execute(select(users).where(users.c.username == name)).first() if actor_name else None
    password = password if isinstance(password, str) else ""
    stored = user.password_hash if user is not None and user.password_hash else dummy
    # A user who never confirmed an authenticator signs in only by accepting an invite.
    ok = verify_password(password, stored) and user is not None and user.password_hash is not None \
        and (user.totp_confirmed_at is not None or not require_totp) and not user.disabled
    with engine.begin() as conn:
        if ok and require_totp:
            step = match_totp(totp_secret(keyring, user), code, now)
            ok = step is not None and claim_totp_step(conn, user.id, step)
        finish_attempt(conn, attempt_id, ok, now)
        if not ok:
            audit.record(conn, action="login", actor_id=user.id if user is not None else None, ip=ip,
                         detail={"outcome": "refused", "username": actor_name})
        else:
            if valid_token(old_token):
                end_session(conn, token_hmac(keyring, old_token))
            token, csrf = start_session(conn, keyring, user.id, now, ip=ip, step_up=True)
            conn.execute(update(users).where(users.c.id == user.id).values(last_login_at=now))
            audit.record(conn, action="login", actor_id=user.id, ip=ip, target_kind="user", target_id=user.id,
                         detail={"outcome": "ok", "method": "password"})
    if not ok:
        raise AuthError(401, GENERIC if require_totp else GENERIC_PASSWORD)
    return SignedIn(token=token, csrf=csrf, user_id=user.id)


def _throttled_check(engine: Engine, session: Session, *, ip: str, now: datetime, action: str, check) -> None:
    """Run a password or code check under the sign-in throttle. ``check(conn)`` returns True on success."""
    name = session.user.username or INVALID_USERNAME
    attempt_id = begin_attempt(engine, ip=ip, username=name, now=now)
    with engine.begin() as conn:
        ok = bool(check(conn))
        finish_attempt(conn, attempt_id, ok, now)
        audit.record(conn, action=action, actor_id=session.user_id, ip=ip, target_kind="user",
                     target_id=session.user_id, detail={"outcome": "ok" if ok else "refused"})
    if not ok:
        raise AuthError(401, "Password or code not accepted.")


def confirm_enrollment(conn: Connection, user_id: str, step: int, now: datetime) -> bool:
    """Mark the authenticator confirmed with the step that proved it, once."""
    result = conn.execute(
        update(users).where(users.c.id == user_id, users.c.totp_confirmed_at.is_(None),
                            or_(users.c.last_totp_step.is_(None), users.c.last_totp_step < step))
        .values(totp_confirmed_at=now, last_totp_step=step))
    return result.rowcount == 1


def step_up(engine: Engine, keyring: Keyring, session: Session, *, password: Any, code: Any, ip: str,
            now: datetime) -> None:
    user = session.user
    if session.sso:
        raise AuthError(400, "This account confirms by signing in again with single sign-on.")

    def check(conn: Connection) -> bool:
        if not verify_password(password if isinstance(password, str) else "", user.password_hash):
            return False
        step = match_totp(totp_secret(keyring, user), code, now)
        if step is None or not claim_totp_step(conn, user.id, step):
            return False
        conn.execute(update(sessions).where(sessions.c.token_hmac == session.token_hmac).values(step_up_at=now))
        return True

    _throttled_check(engine, session, ip=ip, now=now, action="step_up", check=check)


def change_password(engine: Engine, session: Session, *, current: Any, new: str, ip: str, now: datetime,
                    cost: int | None = None) -> None:
    user = session.user
    if user.password_hash is None:
        raise AuthError(400, "This account signs in with single sign-on and has no password.")
    new_hash = hash_password(new, **({"cost": cost} if cost else {}))

    def check(conn: Connection) -> bool:
        if not verify_password(current if isinstance(current, str) else "", user.password_hash):
            return False
        conn.execute(update(users).where(users.c.id == user.id).values(password_hash=new_hash))
        end_user_sessions(conn, user.id, keep=session.token_hmac)
        return True

    _throttled_check(engine, session, ip=ip, now=now, action="account.password", check=check)


def otpauth_uri(keyring: Keyring, user: Any, issuer: str = "Armature") -> str | None:
    secret = totp_secret(keyring, user)
    if secret is None:
        return None
    return pyotp.TOTP(secret).provisioning_uri(name=user.username or user.id, issuer_name=issuer)

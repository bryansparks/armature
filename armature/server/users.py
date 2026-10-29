"""User records. Sign-in and sessions live in ``armature.server.auth``."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pyotp
from sqlalchemy import Connection, delete, func, insert, or_, select, update

from armature.server import audit
from armature.server.crypto import Keyring
from armature.server.db import new_id, utcnow
from armature.server.db.schema import secrets, sessions, users
from armature.server.passwords import check_password_policy, hash_password

USERNAME_RULE = "username must be 2-40 characters of a-z, 0-9, '.', '_', or '-', starting with a letter or digit"

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
TOTP_ISSUER = "Armature"
DISPLAY_NAME_MAX = 120
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,126}$")
SLACK_WORKSPACE = re.compile(r"^[TE][A-Z0-9]{2,31}$")
SLACK_USER = re.compile(r"^[UW][A-Z0-9]{2,31}$")


class LastAdmin(ValueError):
    """The change would leave no enabled admin."""


class SlackTaken(ValueError):
    """Another user already linked that Slack account."""


@dataclass(frozen=True)
class NewAdmin:
    user_id: str
    otpauth_uri: str | None


def totp_aad(user_id: str) -> dict[str, str]:
    return {"user_id": user_id}


def has_admin(conn: Connection, require_totp: bool = True) -> bool:
    """An admin who can sign in: a confirmed authenticator, an OIDC subject, or a password when no code is required."""
    signs_in = [users.c.totp_confirmed_at.is_not(None), users.c.oidc_sub.is_not(None)]
    if not require_totp:
        signs_in.append(users.c.password_hash.is_not(None))
    return conn.execute(select(users.c.id).where(users.c.is_admin.is_(True), or_(*signs_in))
                        .limit(1)).first() is not None


def has_sealed_values(conn: Connection) -> bool:
    if conn.execute(select(secrets.c.id).limit(1)).first():
        return True
    return conn.execute(select(users.c.id).where(users.c.totp_secret_enc.is_not(None)).limit(1)).first() is not None


def normal_username(username: Any) -> str:
    """Usernames are stored and compared lowercased, so they are case-insensitive on every backend."""
    return username.strip().lower() if isinstance(username, str) else ""


def create_admin(conn: Connection, keyring: Keyring, *, username: str, password: str,
                 actor_id: str | None = None, require_totp: bool = True) -> NewAdmin:
    """An admin whose authenticator, when required, is confirmed by ``confirm_admin`` before the CLI exits."""
    username = normal_username(username)
    if not USERNAME.fullmatch(username):
        raise ValueError(USERNAME_RULE)
    check_password_policy(password)
    if conn.execute(select(users.c.id).where(users.c.username == username)).first():
        raise ValueError(f"user {username!r} already exists")
    user_id = new_id()
    totp_secret = pyotp.random_base32() if require_totp else None
    conn.execute(insert(users).values(
        id=user_id, username=username, display_name=username, password_hash=hash_password(password),
        totp_secret_enc=keyring.seal(totp_secret, totp_aad(user_id), label="totp") if totp_secret else None,
        last_totp_step=0, is_admin=True, disabled=False, created_at=utcnow(),
    ))
    audit.record(conn, action="user.create_admin", actor_id=actor_id or user_id,
                 target_kind="user", target_id=user_id, detail={"username": username})
    uri = pyotp.TOTP(totp_secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER) if totp_secret else None
    return NewAdmin(user_id=user_id, otpauth_uri=uri)


def public_user(row: Any) -> dict[str, Any]:
    return {
        "id": row.id, "username": row.username, "display_name": row.display_name, "email": row.email,
        "is_admin": bool(row.is_admin), "disabled": bool(row.disabled), "sso": row.oidc_sub is not None,
        "totp_confirmed": row.totp_confirmed_at is not None, "has_password": row.password_hash is not None,
        "created_at": row.created_at,
        "last_login_at": row.last_login_at,
        "slack_workspace_id": row.slack_workspace_id, "slack_user_id": row.slack_user_id,
    }


def check_display_name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.strip()) > DISPLAY_NAME_MAX or CONTROL.search(value):
        raise ValueError(f"display name must be at most {DISPLAY_NAME_MAX} plain characters")
    return value.strip() or None


def check_email(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not EMAIL.fullmatch(value.strip()):
        raise ValueError("email is not valid")
    return value.strip()


def create_user(conn: Connection, *, username: Any, display_name: Any, email: Any, actor_id: str | None,
                ip: str | None = None) -> str:
    """A local user with no password or authenticator. Both are set by accepting an invite."""
    username = normal_username(username)
    if not USERNAME.fullmatch(username):
        raise ValueError(USERNAME_RULE)
    display = check_display_name(display_name)
    mail = check_email(email)
    if conn.execute(select(users.c.id).where(users.c.username == username)).first():
        raise ValueError(f"user {username!r} already exists")
    user_id = new_id()
    conn.execute(insert(users).values(
        id=user_id, username=username, display_name=display or username, email=mail, password_hash=None,
        totp_secret_enc=None, last_totp_step=0, is_admin=False, disabled=False, created_at=utcnow(),
    ))
    audit.record(conn, action="user.create", actor_id=actor_id, target_kind="user", target_id=user_id,
                 detail={"username": username}, ip=ip)
    return user_id


def reset_sign_in(conn: Connection, user_id: str, *, actor_id: str | None, ip: str | None = None) -> None:
    """Clear the password and authenticator and end every session, so only a new invite signs the user in."""
    conn.execute(update(users).where(users.c.id == user_id).values(
        password_hash=None, totp_secret_enc=None, totp_confirmed_at=None))
    conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
    audit.record(conn, action="user.reset_sign_in", actor_id=actor_id, target_kind="user", target_id=user_id, ip=ip)


def update_user(conn: Connection, user_id: str, *, is_admin: bool | None = None, disabled: bool | None = None,
                display_name: Any = None, actor_id: str | None, ip: str | None = None) -> dict[str, Any] | None:
    """Apply the given changes. Returns the changes, or None when the user does not exist."""
    active_admins: list[str] = []
    if is_admin is not None or disabled is not None:
        # Locked in id order before the target, so two admins demoting each other serialize instead of deadlocking.
        active_admins = list(conn.execute(
            select(users.c.id).where(users.c.is_admin.is_(True), users.c.disabled.is_(False))
            .order_by(users.c.id).with_for_update()).scalars())
    row = conn.execute(select(users).where(users.c.id == user_id).with_for_update()).first()
    if row is None:
        return None
    values: dict[str, Any] = {}
    if is_admin is not None and bool(is_admin) != bool(row.is_admin):
        values["is_admin"] = bool(is_admin)
    if disabled is not None and bool(disabled) != bool(row.disabled):
        values["disabled"] = bool(disabled)
    if display_name is not None:
        display = check_display_name(display_name)
        if display != row.display_name:
            values["display_name"] = display
    if not values:
        return {}
    was_active_admin = row.is_admin and not row.disabled
    stays_active_admin = values.get("is_admin", row.is_admin) and not values.get("disabled", row.disabled)
    if was_active_admin and not stays_active_admin and not [a for a in active_admins if a != user_id]:
        raise LastAdmin("this is the last enabled admin; make another admin first")
    conn.execute(update(users).where(users.c.id == user_id).values(**values))
    # SQLite ignores FOR UPDATE, so count again after the write, which holds SQLite's write lock.
    if was_active_admin and not stays_active_admin and active_admin_count(conn) == 0:
        raise LastAdmin("this is the last enabled admin; make another admin first")
    if "is_admin" in values or "disabled" in values:
        conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
    changes = {key: {"old": row._mapping[key], "new": value} for key, value in values.items()}
    audit.record(conn, action="user.update", actor_id=actor_id, target_kind="user", target_id=user_id,
                 detail=changes, ip=ip)
    return changes


def _slack_id(value: Any) -> str:
    if value is not None and not isinstance(value, str):
        raise ValueError("Slack ids are text.")
    return (value or "").strip().upper()


def set_slack(conn: Connection, user_id: str, *, workspace_id: Any, slack_user_id: Any, actor_id: str | None,
              ip: str | None = None) -> bool:
    """Link the user to one Slack account, or unlink with both empty. False when the user does not exist.

    Raises ValueError for ids that aren't Slack's, and SlackTaken when another user holds the account.
    """
    workspace, member = _slack_id(workspace_id), _slack_id(slack_user_id)
    if bool(workspace) != bool(member):
        raise ValueError("Give both the Slack workspace id and the Slack member id, or neither.")
    if workspace and not (SLACK_WORKSPACE.fullmatch(workspace) and SLACK_USER.fullmatch(member)):
        raise ValueError("A Slack workspace id looks like T0123ABCD and a member id like U0123ABCD.")
    row = conn.execute(select(users.c.slack_workspace_id, users.c.slack_user_id).where(users.c.id == user_id)).first()
    if row is None:
        return False
    if workspace and conn.execute(select(users.c.id).where(users.c.slack_workspace_id == workspace,
                                                           users.c.slack_user_id == member,
                                                           users.c.id != user_id)).first():
        raise SlackTaken("Another user already linked this Slack account.")
    conn.execute(update(users).where(users.c.id == user_id).values(slack_workspace_id=workspace or None,
                                                                   slack_user_id=member or None))
    audit.record(conn, action="user.slack", actor_id=actor_id, target_kind="user", target_id=user_id,
                 detail={"old": [row.slack_workspace_id, row.slack_user_id], "new": [workspace or None, member or None]},
                 ip=ip)
    return True


def active_admin_count(conn: Connection) -> int:
    return conn.execute(select(func.count()).select_from(users)
                        .where(users.c.is_admin.is_(True), users.c.disabled.is_(False))).scalar_one()

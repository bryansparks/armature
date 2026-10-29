"""Linking a user's own Slack account: the bot sends a one-time code by direct message, and they enter it.

Only someone who can read that Slack account's messages learns the code, so a
linked account is one the user holds. The code is kept only as an HMAC, lasts
ten minutes, and allows five tries. An admin can still link an account
directly.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets as _random
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, delete, insert, select, update

from armature.server import audit, connections, rbac
from armature.server.crypto import Keyring, SealError
from armature.server.db.schema import connections as connection_rows
from armature.server.db.schema import slack_links, users
from armature.server.tools import Call
from armature.server.tools import slack as slack_tools
from armature.server.users import SLACK_USER, SLACK_WORKSPACE, SlackTaken, set_slack
from armature.server.vendors import VendorError

CODE_TTL = timedelta(minutes=10)
TRIES = 5
DIGITS = 6
MESSAGE = ("Your Armature code is {code}. Enter it on your Armature account page to link this Slack account. "
           "It expires in 10 minutes. If you didn't ask for it, ignore this message.")


class LinkError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _digest(keyring: Keyring, user_id: str, code: str) -> str:
    return hmac.new(keyring.subkey("slack-link"), f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _ids(workspace_id: Any, slack_user_id: Any) -> tuple[str, str]:
    workspace = workspace_id.strip().upper() if isinstance(workspace_id, str) else ""
    member = slack_user_id.strip().upper() if isinstance(slack_user_id, str) else ""
    if not (SLACK_WORKSPACE.fullmatch(workspace) and SLACK_USER.fullmatch(member)):
        raise LinkError(400, "A Slack workspace id looks like T0123ABCD and a member id like U0123ABCD.")
    return workspace, member


def _connection(conn: Connection, user_id: str, is_admin: bool, workspace: str) -> Any:
    """A verified Slack connection of that workspace, in a team the user belongs to."""
    access = rbac.load_access(conn, user_id, is_admin)
    rows = conn.execute(select(connection_rows).where(connection_rows.c.kind == "slack",
                                                      connection_rows.c.state == "ok")
                        .order_by(connection_rows.c.created_at, connection_rows.c.id)).all()
    for row in rows:
        if (row.identity or {}).get("team_id") == workspace and rbac.sees_team(access, row.team_id):
            return row
    raise LinkError(409, f"No verified Slack connection of workspace {workspace} is in a team of yours.")


def start(engine: Engine, keyring: Keyring, *, user_id: str, is_admin: bool, workspace_id: Any,
          slack_user_id: Any, now: datetime, ip: str | None,
          transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Send a new code to the Slack account, replacing any earlier one. Nothing is linked yet."""
    workspace, member = _ids(workspace_id, slack_user_id)
    code = f"{_random.randbelow(10 ** DIGITS):0{DIGITS}d}"
    with engine.begin() as conn:
        if conn.execute(select(users.c.id).where(users.c.slack_workspace_id == workspace,
                                                 users.c.slack_user_id == member, users.c.id != user_id)).first():
            raise LinkError(409, "Another user already linked this Slack account.")
        row = _connection(conn, user_id, is_admin, workspace)
        try:
            ctx = Call(run_id="", step_id="", connection=row, creds=connections.credentials(keyring, row), now=now,
                       transport=transport)
        except (SealError, ValueError) as exc:
            raise LinkError(409, f"The credentials of connection {row.name} can't be opened.") from exc
        conn.execute(delete(slack_links).where(slack_links.c.user_id == user_id))
        conn.execute(insert(slack_links).values(user_id=user_id, workspace_id=workspace, slack_user_id=member,
                                                code_hmac=_digest(keyring, user_id, code), attempts=0,
                                                created_at=now, expires_at=now + CODE_TTL))
        audit.record(conn, action="user.slack_code", actor_id=user_id, target_kind="user", target_id=user_id,
                     detail={"workspace_id": workspace, "slack_user_id": member}, ip=ip)
    try:
        slack_tools.post(ctx, {"channel": member, "text": MESSAGE.format(code=code)})
    except VendorError as exc:
        with engine.begin() as conn:
            conn.execute(delete(slack_links).where(slack_links.c.user_id == user_id))
        raise LinkError(502, f"The code could not be sent: {exc}") from exc
    return {"sent": True, "workspace_id": workspace, "slack_user_id": member,
            "expires_at": (now + CODE_TTL).isoformat()}


def confirm(conn: Connection, keyring: Keyring, *, user_id: str, code: Any, now: datetime, ip: str | None) -> bool:
    """Link the account the code was sent to when the code matches. False when it doesn't."""
    pending = conn.execute(select(slack_links).where(slack_links.c.user_id == user_id).with_for_update()).first()
    if pending is None or pending.expires_at <= now or pending.attempts >= TRIES:
        raise LinkError(409, "No code is waiting. Ask for a new one.")
    conn.execute(update(slack_links).where(slack_links.c.user_id == user_id)
                 .values(attempts=slack_links.c.attempts + 1))
    given = code.strip() if isinstance(code, str) else ""
    if not hmac.compare_digest(_digest(keyring, user_id, given), pending.code_hmac):
        return False
    conn.execute(delete(slack_links).where(slack_links.c.user_id == user_id))
    try:
        set_slack(conn, user_id, workspace_id=pending.workspace_id, slack_user_id=pending.slack_user_id,
                  actor_id=user_id, ip=ip)
    except SlackTaken as exc:
        raise LinkError(409, str(exc)) from exc
    return True


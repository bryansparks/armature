"""A gate's Slack card: posted where its stage says, and its buttons answered through the webhook receiver.

A click counts only from a Slack user linked to an Armature user who may decide
the gate, and only on the gate's open row with its nonce, so a second or
replayed click changes nothing. Anyone else gets a note only they can see.
The worker's sweep posts new cards and rewrites decided ones, outside any
transaction.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, select, update

from armature.server import connections, conversations, gates, rbac, sanitize
from armature.server.crypto import Keyring, SealError
from armature.server.db.schema import gates as gate_rows
from armature.server.db.schema import runs, users, workflows
from armature.server.tools import Call
from armature.server.tools import slack as slack_tools
from armature.server.vendors import VendorError

log = logging.getLogger(__name__)

APPROVE, REJECT = "armature_gate:approve", "armature_gate:reject"
VALUE = re.compile(r"^(?P<gate>[0-9a-f-]{36}):(?P<nonce>[0-9a-f]{64})$")
SLACK_ID = re.compile(r"^[A-Z][A-Z0-9]{2,20}$")
SLACK_TS = re.compile(r"^[0-9]{1,12}\.[0-9]{1,8}$")
PRESENT_MAX = 2800
LINK_FIRST = "Link your Slack account on your Armature account page before you decide gates in Slack."
NOT_ALLOWED = "You can't decide this gate. It needs a {role} on its project."
STEP_UP = "This gate needs a recent sign-in with your authenticator. Decide it on the web."
DECIDED = "This gate was already decided, or it expired."
UNKNOWN = "This approval card is out of date."
OUTCOMES = {"approved": "Approved", "rejected": "Rejected", "expired": "Expired", "canceled": "Canceled"}


@dataclass(frozen=True)
class Click:
    gate_id: str
    nonce: str
    approved: bool
    user: str
    workspace: str
    channel: str | None


@dataclass(frozen=True)
class Outcome:
    """What a click did: whether it decided the gate, why, and the note for the clicker."""
    decided: bool
    reason: str
    notice: Notice | None = None


@dataclass(frozen=True)
class Notice:
    """A note for the person who clicked, sent after the click's transaction commits."""
    channel: str
    user: str
    text: str


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and SLACK_ID.fullmatch(value) else None


def clicked(payload: Any) -> Click | None:
    """The gate button a ``block_actions`` payload carries, or None when it carries none."""
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return None
    actions = payload.get("actions")
    action = actions[0] if isinstance(actions, list) and actions and isinstance(actions[0], dict) else {}
    found = VALUE.fullmatch(action.get("value") or "") if isinstance(action.get("value"), str) else None
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
    channel = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
    container = payload.get("container") if isinstance(payload.get("container"), dict) else {}
    workspace = _id(team.get("id")) or _id(user.get("team_id"))
    if action.get("action_id") not in (APPROVE, REJECT) or found is None or not _id(user.get("id")) \
            or workspace is None:
        return None
    return Click(gate_id=found["gate"], nonce=found["nonce"], approved=action["action_id"] == APPROVE,
                 user=user["id"], workspace=workspace, channel=_id(channel.get("id")) or _id(container.get("channel_id")))


def delivery_key(payload: Any) -> str | None:
    """block_actions carry no event_id, so a click is its message, its action's time, and who clicked."""
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return None
    container = payload.get("container") if isinstance(payload.get("container"), dict) else {}
    actions = payload.get("actions")
    action = actions[0] if isinstance(actions, list) and actions and isinstance(actions[0], dict) else {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    parts = (container.get("message_ts"), action.get("action_ts"), user.get("id"))
    if not all(isinstance(p, str) and p for p in parts):
        return None
    return "block_actions:" + ":".join(parts)


def click(conn: Connection, hook: Any, found: Click, now: datetime, *, step_up_required: bool = False) -> Outcome:
    """Decide the gate a click names, as the Armature user linked to the clicker."""
    def refused(reason: str, text: str) -> Outcome:
        notice = Notice(channel=found.channel, user=found.user, text=text) if found.channel else None
        return Outcome(decided=False, reason=reason, notice=notice)

    gate = conn.execute(select(gate_rows).where(gate_rows.c.id == found.gate_id)).first()
    if gate is None or gate.team_id != hook.team_id:
        return refused("the click names no gate of this team", UNKNOWN)
    user = conn.execute(select(users.c.id, users.c.username, users.c.is_admin).where(
        users.c.slack_workspace_id == found.workspace, users.c.slack_user_id == found.user,
        users.c.disabled.is_(False))).first()
    if user is None:
        return refused(f"Slack user {found.user} is not linked to an Armature user", LINK_FIRST)
    access = rbac.load_access(conn, user.id, bool(user.is_admin))
    role = rbac.role_for(access, rbac.Target(team_id=gate.team_id, project_id=gate.project_id))
    if not (rbac.at_least(role, "project_lead") and rbac.at_least(role, gate.min_role)):
        return refused(f"{user.username} may not decide this gate",
                       NOT_ALLOWED.format(role=gate.min_role.replace("_", " ")))
    if step_up_required and rbac.RANK[gate.min_role] >= rbac.RANK["team_lead"]:
        return refused(f"{user.username} needs a step-up to decide this gate", STEP_UP)
    try:
        decided = gates.decide(conn, gate_id=gate.id, approved=found.approved, nonce=found.nonce, actor_id=user.id,
                               via="slack", note=None, now=now)
    except gates.GateError:
        return refused("the gate was already decided, or expired", DECIDED)
    return Outcome(decided=True, reason=f"gate {decided['state']} by {user.username}")


def _call(conn: Connection, keyring: Keyring, team_id: str, project_id: str | None, now: datetime,
          transport: httpx.BaseTransport | None, connection_id: str | None = None) -> Call:
    if connection_id:
        row = connections.get(conn, connection_id)
    else:
        row = connections.resolve(conn, team_id=team_id, project_id=project_id, kind="slack")
    if row.state != "ok":
        raise connections.ConnError(409, f"Connection {row.name} is not verified.")
    return Call(run_id="", step_id="", connection=row, creds=connections.credentials(keyring, row), now=now,
                transport=transport)


def tell(engine: Engine, keyring: Keyring, hook: Any, notice: Notice, now: datetime,
         transport: httpx.BaseTransport | None = None) -> None:
    """Send a clicker their note through the webhook's own Slack connection. A failure is only logged."""
    try:
        with engine.connect() as conn:
            ctx = _call(conn, keyring, hook.team_id, None, now, transport, hook.connection_id)
        slack_tools.ephemeral(ctx, notice.channel, notice.user, notice.text)
    except (connections.ConnError, SealError, ValueError, VendorError) as exc:
        log.warning("a gate click's note was not sent: %s", exc)


def _blocks(row: Any, public_url: str, decided: str | None) -> tuple[str, list[dict[str, Any]]]:
    present = sanitize.slack(row.present or "Review required.", PRESENT_MAX)
    workflow = sanitize.slack(row.workflow or "a workflow", 200)
    step = sanitize.slack(row.step_id, 200)
    head = f"*Approval needed* in {workflow}, step {step}"
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n{present}"}}]
    if decided:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": decided}]})
    else:
        value = f"{row.id}:{row.nonce}"
        blocks.append({"type": "actions", "block_id": "armature_gate", "elements": [
            {"type": "button", "action_id": APPROVE, "style": "primary", "value": value,
             "text": {"type": "plain_text", "text": "Approve"}},
            {"type": "button", "action_id": REJECT, "style": "danger", "value": value,
             "text": {"type": "plain_text", "text": "Reject"}}]})
    expires = row.expires_at.strftime("%Y-%m-%d %H:%M UTC")
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                                                    "text": f"<{public_url}/#/run/{row.run_id}|Open the run>"
                                                            + ("" if decided else f". Expires {expires}.")}]})
    return f"Approval needed in {row.workflow or 'a workflow'}", blocks


def _outcome(row: Any) -> str:
    who = f" by {sanitize.slack(row.decided_by_name, 100)}" if row.decided_by_name else ""
    via = " in Slack" if row.decided_via == "slack" else " on the web" if row.decided_via == "web" else ""
    return f"{OUTCOMES.get(row.state, row.state)}{who}{via}."


def _due(engine: Engine, now: datetime) -> list[str]:
    """Gates whose card is due: posted cards of decided gates first become due for a rewrite."""
    with engine.begin() as conn:
        conn.execute(update(gate_rows).where(gate_rows.c.card_state == "posted", gate_rows.c.state != "open")
                     .values(card_state="update", card_due_at=now, card_attempts=0))
        # A card being posted right now is left to finish, and is rewritten after.
        conn.execute(update(gate_rows).where(gate_rows.c.card_state == "post", gate_rows.c.state != "open",
                                             gate_rows.c.card_due_at <= now)
                     .values(card_state="done", card_due_at=None))
        return list(conn.execute(select(gate_rows.c.id).where(
            gate_rows.c.card_state.in_(("post", "update")), gate_rows.c.card_due_at <= now)
            .order_by(gate_rows.c.card_due_at)).scalars())


def _send(engine: Engine, keyring: Keyring, public_url: str, gate_id: str, now: datetime,
          transport: httpx.BaseTransport | None) -> bool:
    """Post or rewrite one card, once claimed so no other server sends it too. A failure is tried again with
    backoff, and after the last try the gate keeps the error, which the run page shows."""
    with engine.begin() as conn:
        claimed = conn.execute(update(gate_rows).where(
            gate_rows.c.id == gate_id, gate_rows.c.card_state.in_(("post", "update")),
            gate_rows.c.card_due_at <= now).values(card_due_at=now + conversations.POST_LEASE)).rowcount == 1
        if not claimed:
            return False
        row = conn.execute(
            select(gate_rows, workflows.c.name.label("workflow"), users.c.username.label("decided_by_name"))
            .select_from(gate_rows.join(runs, runs.c.id == gate_rows.c.run_id)
                         .outerjoin(workflows, workflows.c.id == runs.c.workflow_id)
                         .outerjoin(users, users.c.id == gate_rows.c.decided_by))
            .where(gate_rows.c.id == gate_id)).one()
    slack = dict((row.channel or {}).get("slack") or {})
    error = None
    try:
        with engine.connect() as conn:
            ctx = _call(conn, keyring, row.team_id, row.project_id, now, transport)
        if row.card_state == "post":
            text, blocks = _blocks(row, public_url, None)
            ts = slack_tools.card(ctx, slack["channel"], text, blocks, slack.get("thread_ts"))
            if isinstance(ts, str) and SLACK_TS.fullmatch(ts):
                slack["ts"] = ts
            else:
                error = "Slack gave the card no ts."
        else:
            text, blocks = _blocks(row, public_url, _outcome(row))
            slack_tools.replace_card(ctx, slack["channel"], slack["ts"], text, blocks)
    except (connections.ConnError, SealError, ValueError, VendorError) as exc:
        error = str(getattr(exc, "message", exc))
    with engine.begin() as conn:
        mine = (gate_rows.c.id == gate_id, gate_rows.c.card_state == row.card_state)
        if error is None:
            done = "posted" if row.card_state == "post" else "done"
            conn.execute(update(gate_rows).where(*mine).values(
                channel={"slack": slack}, card_state=done, card_due_at=None, card_attempts=0, card_error=None))
            return True
        attempts = row.card_attempts + 1
        log.warning("gate %s: its Slack card failed (try %d): %s", gate_id, attempts, error)
        failed = attempts >= conversations.POST_TRIES
        conn.execute(update(gate_rows).where(*mine).values(
            card_attempts=attempts, card_error=error[:conversations.ERROR_MAX] if failed else None,
            card_state="failed" if failed else row.card_state,
            card_due_at=None if failed else now + conversations.backoff(attempts)))
    return False


def sync(engine: Engine, keyring: Keyring, public_url: str, now: datetime,
         transport: httpx.BaseTransport | None = None) -> None:
    """Post the card of each open gate that has a Slack target, and show the decision on each decided one."""
    for gate_id in _due(engine, now):
        try:
            _send(engine, keyring, public_url, gate_id, now, transport)
        except Exception:
            log.exception("gate %s: its Slack card could not be sent", gate_id)

"""Run actions: configured host commands, such as a rerun or a cancel, that a user starts from a run's page.

The server decides only whether an action is offered (workflow, status, role).
The command makes the final call and says so through its exit code: 0 done,
3 refused, anything else failed. Run fields reach it only through a JSON file
named by ``ARMATURE_ACTION_RUN``, never as arguments.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import audit, hostcmd, rbac
from armature.server.config import ActionConfig
from armature.server.crypto import Keyring
from armature.server.db import new_id
from armature.server.db.schema import operations, run_steps, runner_keys, runs

log = logging.getLogger(__name__)

ACTION_TIMEOUT = 120
REFUSED_EXIT = 3
MESSAGE_MAX = 2 * 1024
LOG_MAX = 64 * 1024
READ_MAX = 256 * 1024
RUN_ENV = "ARMATURE_ACTION_RUN"
# Past this an unfinished row belongs to a server that died, so it no longer blocks another.
STALE_AFTER = timedelta(seconds=ACTION_TIMEOUT * 2 + 60)
NOT_STORED = "[not stored: the server could not load the secrets to redact it]"
INTERRUPTED = "The server stopped before the command finished."
NOT_HOST_RUN = "Actions work only on runs the host's own runner started."
FAILED_FOR_VIEWERS = "The action failed. An admin can see why on the Status page."


class ActionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def find(actions: list[ActionConfig], action_id: str) -> ActionConfig | None:
    return next((action for action in actions if action.id == action_id), None)


def applies(action: ActionConfig, workflow: str | None) -> bool:
    return workflow is not None and workflow in action.workflows


def unavailable(action: ActionConfig, summary: dict[str, Any], role: str | None) -> str | None:
    """Why ``action`` is not offered on this run for this role, or None when it is."""
    # A team runner key can register any run fields, so only the host's own runs may drive host commands.
    if not summary.get("host_run"):
        return NOT_HOST_RUN
    if not applies(action, summary.get("workflow")):
        return "This action does not apply to this workflow."
    if action.statuses and summary.get("status") not in action.statuses:
        return "This action does not apply to a run in this state."
    if action.admin_only and role != "admin":
        return "Only an admin can do this."
    if not rbac.at_least(role, action.min_role):
        return "Your role cannot do this."
    return None


def offered(actions: list[ActionConfig], summary: dict[str, Any], role: str | None) -> list[dict[str, Any]]:
    out = []
    for action in actions:
        if not applies(action, summary.get("workflow")):
            continue
        reason = unavailable(action, summary, role)
        out.append({"id": action.id, "label": action.label, "confirm": action.confirm,
                    "enabled": reason is None, "reason": reason})
    return out


def _clear_stale(conn: Connection, now: datetime) -> None:
    conn.execute(update(operations).where(operations.c.finished_at.is_(None),
                                          operations.c.started_at < now - STALE_AFTER)
                 .values(finished_at=now, outcome="failed", message=INTERRUPTED, active_run=None,
                         active_action=None))


def begin(engine: Engine, action: ActionConfig, run_id: str, user_id: str, now: datetime) -> str:
    """Claim the run and the action for one operation. Raises ActionError(409) while either is taken."""
    op_id = new_id()
    with engine.begin() as conn:
        _clear_stale(conn, now)
    try:
        with engine.begin() as conn:
            conn.execute(insert(operations).values(id=op_id, action_id=action.id, run_id=run_id,
                                                   requested_by=user_id, started_at=now, active_run=run_id,
                                                   active_action=action.id))
    except IntegrityError as exc:
        with engine.connect() as conn:
            busy_run = conn.execute(select(operations.c.id).where(operations.c.active_run == run_id)).first()
        if busy_run is not None:
            raise ActionError(409, "Another operation on this run is still going.") from exc
        raise ActionError(409, f"{action.label} is already running for another run. Try again in a moment.") from exc
    return op_id


def recover(engine: Engine, now: datetime) -> int:
    """Close every operation an earlier server process left open."""
    with engine.begin() as conn:
        return conn.execute(update(operations).where(operations.c.finished_at.is_(None))
                            .values(finished_at=now, outcome="failed", message=INTERRUPTED, active_run=None,
                                    active_action=None)).rowcount


def host_run(conn: Connection, run_id: str) -> bool:
    """Whether an instance runner key, the host's own, registered this run."""
    key_team = conn.execute(select(runner_keys.c.id, runner_keys.c.team_id)
                            .join(runs, runs.c.runner_key_id == runner_keys.c.id)
                            .where(runs.c.id == run_id)).first()
    return key_team is not None and key_team.team_id is None


def run_payload(conn: Connection, summary: dict[str, Any], actor_username: str) -> dict[str, Any]:
    prepare = conn.execute(select(run_steps.c.output).where(run_steps.c.run_id == summary["id"],
                                                            run_steps.c.step_key == "prepare")).scalar()
    trigger_ref = conn.execute(select(runs.c.trigger_ref).where(runs.c.id == summary["id"])).scalar()
    started = summary.get("started_at")
    return {"run_id": summary["id"], "workflow": summary.get("workflow"), "status": summary.get("status"),
            "subject": summary.get("subject"), "trigger_ref": trigger_ref,
            "summary": summary.get("summary"), "actor_username": actor_username,
            "started_at": started.isoformat() if isinstance(started, datetime) else started,
            "prepare": prepare if isinstance(prepare, dict) else None}


def _outcome(result: hostcmd.Result) -> tuple[str, str]:
    reported = hostcmd.last_json(result.stdout) or {}
    message = reported.get("message") if isinstance(reported.get("message"), str) else ""
    if result.error:
        return "failed", result.error
    if result.timed_out:
        return "failed", f"The command did not finish within {ACTION_TIMEOUT} s and was stopped."
    if result.exit_code == 0:
        return "ok", message or "Done."
    if not message:
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        message = lines[-1].strip() if lines else ""
    if result.exit_code == REFUSED_EXIT:
        return "refused", message or "The host refused this action."
    return "failed", message or f"The command exited {result.exit_code}."


def _log(result: hostcmd.Result) -> str:
    parts = []
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr}")
    return "\n".join(parts)


def execute(engine: Engine, keyring: Keyring, action: ActionConfig, payload: dict[str, Any], op_id: str, *,
            team_id: str, user_id: str, now, ip: str | None = None) -> Any:
    """Run the action's command for one claimed operation, record the outcome and an audit row, and return it."""
    scratch = Path(tempfile.mkdtemp(prefix="armature-action-"))
    try:
        run_file = scratch / "run.json"
        fd = os.open(run_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(payload, out, default=str)
        result = hostcmd.run(action.command, cwd=hostcmd.working_dir(action.cwd),
                             env=hostcmd.minimal_env({RUN_ENV: str(run_file)}), timeout=ACTION_TIMEOUT,
                             scratch=scratch, read_limit=READ_MAX)
    except Exception:
        log.exception("operation %s: the command could not be run", op_id)
        result = hostcmd.Result(None, "", "", error="The command could not be run. The server log says why.")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    outcome, message = _outcome(result)
    try:
        with engine.connect() as conn:
            secret_values = hostcmd.instance_secret_values(conn, keyring)
        message = hostcmd.cap(hostcmd.scrub(message, secret_values), MESSAGE_MAX)
        log_text = hostcmd.cap(hostcmd.scrub(_log(result), secret_values), LOG_MAX, keep_tail=True)
    except Exception:
        log.exception("operation %s: secrets for redaction did not load", op_id)
        message, log_text = NOT_STORED, NOT_STORED
    with engine.begin() as conn:
        conn.execute(update(operations).where(operations.c.id == op_id).values(
            finished_at=now(), exit_code=result.exit_code, outcome=outcome, message=message, log=log_text,
            active_run=None, active_action=None))
        audit.record(conn, action="operation.run", actor_id=user_id, team_id=team_id, target_kind="run",
                     target_id=payload["run_id"], ip=ip,
                     detail={"action_id": action.id, "operation_id": op_id, "outcome": outcome,
                             "exit_code": result.exit_code})
        row = conn.execute(select(operations).where(operations.c.id == op_id)).first()
    return row


def shown(row: Any, *, with_log: bool = False, admin: bool = False) -> dict[str, Any]:
    # A failure message can carry host paths from the command's output, so only admins read it.
    message = row.message if admin or row.outcome != "failed" else FAILED_FOR_VIEWERS
    out = {"id": row.id, "action_id": row.action_id, "run_id": row.run_id, "requested_by": row.requested_by,
           "started_at": row.started_at, "finished_at": row.finished_at, "exit_code": row.exit_code,
           "outcome": row.outcome, "message": message}
    if with_log:
        out["log"] = row.log
    return out

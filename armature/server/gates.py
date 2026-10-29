"""Human gates: a run paused at a ``gate: human`` stage, the decision on it, and the job that resumes the run.

A gate opens when a run's process exits paused, from the request it left beside
its checkpoint. Deciding is one conditional UPDATE on the open row and its
nonce, so of two approvers only one wins, and the resume job is queued in the
same transaction. A gate nobody decides before it expires is rejected as
expired. The resume job runs the same run on from the stored checkpoint, with
the decision as the gate stage's result.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets as _random
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, func, insert, select, update

from armature.nodes.gate import gate_digest
from armature.server import audit, defs, ingest, rbac, runners, worker, writer
from armature.server.crypto import Keyring
from armature.server.db import new_id
from armature.server.db.schema import gates, jobs, run_events, runs, users, workflows

DEFAULT_EXPIRES_HOURS = 72.0
MAX_EXPIRES_HOURS = 720.0
PRESENT_MAX = 8 * 1024
CHECKPOINT_MAX = 4 * 1024 * 1024
NOTE_MAX = 2000
STEP_MAX = 191
MIN_ROLES = ("project_lead", "team_lead", "admin")
EXPIRED = "expired"
DECIDED = ("approved", "rejected", EXPIRED)
PAUSED_RUN = ("running", "lost", "waiting")
ITERATION = r"(__iter_[1-9][0-9]{0,8})?"
SLACK_CHANNEL = re.compile(r"^[CG][A-Z0-9]{2,20}$")
SLACK_THREAD = re.compile(r"^slack:(?P<channel>[CG][A-Z0-9]{2,20}):(?P<ts>[0-9]{1,12}\.[0-9]{1,8})$")

log = logging.getLogger(__name__)


class GateError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _stages(conn: Connection, job: Any) -> list[dict[str, Any]]:
    """The stages of the job's pinned workflow version that have an id."""
    body = None if job.workflow_version is None else defs.body_of(conn, defs.WORKFLOW, job.workflow_id,
                                                                  job.workflow_version)
    data = worker._load(body)
    stages = data.get("stages") if isinstance(data, dict) else None
    return [s for s in stages if isinstance(s, dict) and isinstance(s.get("id"), str)] \
        if isinstance(stages, list) else []


def _downstream(stages: list[dict[str, Any]], step_id: str) -> set[str]:
    """Every stage that depends on ``step_id``, directly or through others."""
    def needs(stage: dict[str, Any]) -> set[Any]:
        return set(stage["depends_on"]) if isinstance(stage.get("depends_on"), list) else set()

    found, frontier = set(), {step_id}
    while frontier:
        frontier = {s["id"] for s in stages if s["id"] not in found and frontier & needs(s)}
        found |= frontier
    return found


def _is_stage_key(key: str, stage_ids: set[str]) -> bool:
    return any(re.fullmatch(re.escape(stage) + ITERATION, key) for stage in stage_ids)


def answer(row: Any) -> dict[str, Any]:
    """A decided gate's result, as the gate stage returns it on resume."""
    return {"approved": row.state == "approved", "feedback": row.decision_note, "decided_by": row.decided_by,
            "decided_at": _iso(row.decided_at) if row.decided_at else None}


def _signed_suspends(conn: Connection, keyring: Keyring, run_id: str) -> list[dict[str, Any]]:
    """The run's run_suspend payloads its engine signed, oldest first. Any other run_suspend is a plain event."""
    payloads = conn.execute(select(run_events.c.payload).where(run_events.c.run_id == run_id,
                                                               run_events.c.kind == "run_suspend")
                            .order_by(run_events.c.seq)).scalars().all()
    return [p for p in payloads if runners.signed_suspend(keyring, run_id, p)]


def suspend_reported(conn: Connection, keyring: Keyring, run_id: str) -> bool:
    """Whether the engine sent a signed run_suspend no gate holds yet, so a gate request from its host is real."""
    held = conn.execute(select(func.count()).select_from(gates).where(gates.c.run_id == run_id)).scalar_one()
    return len(_signed_suspends(conn, keyring, run_id)) > held


def _hours(value: Any) -> float:
    if value is None:
        return DEFAULT_EXPIRES_HOURS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= MAX_EXPIRES_HOURS:
        raise GateError(400, f"expires_hours is more than 0 and at most {MAX_EXPIRES_HOURS:g}.")
    return float(value)


def open_gate(conn: Connection, keyring: Keyring, *, job_id: str, request: Any, now: datetime) -> str:
    """Record the gate a running job's run paused at. The job is suspended, the run waits, its token goes.

    Any step could write the file, so the request must match the run's own last unmatched signed run_suspend:
    its step, its key, and the digest of its checkpoint and question. Who may decide and how long it waits come
    from the stage's ``approval:`` in the pinned workflow. The checkpoint and question are redacted like events.
    """
    job = conn.execute(select(jobs).where(jobs.c.id == job_id)).first()
    if job is None or job.run_id is None or job.status not in worker.HELD:
        raise GateError(409, "This job is not running.")
    if not isinstance(request, dict) or not isinstance(request.get("checkpoint"), dict):
        raise GateError(400, "A gate request has a checkpoint object.")
    stages = _stages(conn, job)
    stage_ids = {s["id"] for s in stages if s.get("gate") == "human"}
    step_id, step_key = request.get("step_id"), request.get("key")
    if step_id not in stage_ids:
        raise GateError(400, "step_id is not a gate: human stage of the workflow this job runs.")
    if not isinstance(step_key, str) or len(step_key) > STEP_MAX or not _is_stage_key(step_key, {step_id}):
        raise GateError(400, "key is the gate's step id, or step_id__iter_N in a loop.")
    signed = _signed_suspends(conn, keyring, job.run_id)
    held = conn.execute(select(func.count()).select_from(gates).where(gates.c.run_id == job.run_id)).scalar_one()
    sent = signed[-1] if len(signed) > held else None
    if sent is None or (sent.get("step_id"), sent.get("key")) != (step_id, step_key):
        raise GateError(409, "The gate request doesn't match the pause the run sent.")
    if not hmac.compare_digest(str(sent.get("digest")), gate_digest(request)):
        raise GateError(409, "The gate request isn't the one the run paused with.")
    approval = next(s for s in stages if s["id"] == step_id).get("approval")
    approval = approval if isinstance(approval, dict) else {}
    min_role = approval.get("min_role") or "project_lead"
    if min_role not in MIN_ROLES:
        raise GateError(400, f"min_role is one of {', '.join(MIN_ROLES)}.")
    present = request.get("present")
    if present is not None and not isinstance(present, str):
        raise GateError(400, "present must be text.")
    # Gate answers come only from decided gate rows, and nothing after this gate has run yet.
    dropped = stage_ids | _downstream(stages, step_id)
    uploaded = {k: v for k, v in request["checkpoint"].items() if not _is_stage_key(str(k), dropped)}
    secret_values = ingest.team_secret_values(conn, keyring, job.team_id)
    checkpoint = json.dumps(ingest.redact(uploaded, secret_values), default=str)
    if len(checkpoint.encode("utf-8", "surrogatepass")) > CHECKPOINT_MAX:
        raise GateError(413, "The run's checkpoint is larger than 4 MB, so it can't wait at a gate.")
    if conn.execute(update(runs).where(runs.c.id == job.run_id, runs.c.status.in_(PAUSED_RUN)).values(
            status=worker.RUN_WAITING, finished_at=None, error=None)).rowcount != 1:
        raise GateError(409, "This run already ended.")
    gate_id = new_id()
    card = card_target(approval.get("slack_channel"), job.thread)
    conn.execute(insert(gates).values(
        id=gate_id, run_id=job.run_id, job_id=job.id, team_id=job.team_id, project_id=job.project_id,
        step_id=step_id, step_key=step_key,
        present=ingest.redact(present, secret_values)[:PRESENT_MAX] if present else None, state="open",
        min_role=min_role, nonce=_random.token_hex(32), checkpoint=checkpoint, workflow_version=job.workflow_version,
        thread=job.thread, channel=card, card_state="post" if card else None, card_due_at=now if card else None,
        created_at=now, expires_at=now + timedelta(hours=_hours(approval.get("expires_hours")))))
    conn.execute(update(jobs).where(jobs.c.id == job.id, jobs.c.status.in_(worker.HELD)).values(
        status="suspended", finished_at=now, lease_expires_at=None, error=None))
    runners.revoke_tokens(conn, [job.run_id])
    audit.record(conn, action="gate.open", actor_id=None, team_id=job.team_id, target_kind="gate",
                 target_id=gate_id, detail={"run_id": job.run_id, "step_id": request.get("step_id")})
    return gate_id


def card_target(channel: Any, thread: str | None) -> dict[str, Any] | None:
    """Where a gate's Slack card goes: the stage's channel, else the Slack thread the run belongs to, else nowhere."""
    if isinstance(channel, str) and SLACK_CHANNEL.fullmatch(channel):
        return {"slack": {"channel": channel}}
    found = SLACK_THREAD.fullmatch(thread or "")
    return {"slack": {"channel": found["channel"], "thread_ts": found["ts"]}} if found else None


def end_run(conn: Connection, *, run_id: str, error: str, now: datetime, status: str = "failed",
            ending: tuple[str, ...] = PAUSED_RUN) -> None:
    """End a run no open gate holds, when it is in one of ``ending``. Its token and checkpoints go with it."""
    if conn.execute(select(gates.c.id).where(gates.c.run_id == run_id, gates.c.state == "open")).first():
        return
    conn.execute(update(runs).where(runs.c.id == run_id, runs.c.status.in_(ending))
                 .values(status=status, error=error, finished_at=now))
    runners.revoke_tokens(conn, [run_id])
    conn.execute(update(gates).where(gates.c.run_id == run_id, gates.c.checkpoint.is_not(None))
                 .values(checkpoint=None))


def abandon(conn: Connection, *, job_id: str, error: str, now: datetime) -> None:
    """Fail a job whose run paused but couldn't open its gate, and its run, which nothing would resume."""
    run_id = conn.execute(select(jobs.c.run_id).where(jobs.c.id == job_id)).scalar()
    conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(worker.HELD))
                 .values(status="failed", error=error, finished_at=now))
    if run_id is not None:
        end_run(conn, run_id=run_id, error=error, now=now)


def fail_waiting(conn: Connection, *, job_id: str, error: str, now: datetime) -> None:
    """After a job failed or was reaped: a run of it left waiting with no open gate fails too."""
    run_id = conn.execute(select(jobs.c.run_id).where(jobs.c.id == job_id)).scalar()
    if run_id is not None:
        end_run(conn, run_id=run_id, error=error, now=now, ending=(worker.RUN_WAITING,))


def settle_paused(engine: Engine, keyring: Keyring, *, job_id: str, request: Any, now: datetime) -> str:
    """Open the gate, or fail the job and run when it can't open. Returns the job's status.

    A job someone asked to cancel ends canceled, run and all, instead of waiting at a gate.
    """
    with engine.begin() as conn:
        job = conn.execute(select(jobs.c.run_id, jobs.c.status, jobs.c.cancel_requested_at)
                           .where(jobs.c.id == job_id)).first()
        if job is not None and job.cancel_requested_at is not None and job.status in worker.HELD:
            conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(worker.HELD))
                         .values(status="canceled", error="Canceled.", finished_at=now))
            if job.run_id is not None:
                end_run(conn, run_id=job.run_id, error="Canceled.", now=now, status="canceled")
            return "canceled"
    try:
        with engine.begin() as conn:
            open_gate(conn, keyring, job_id=job_id, request=request, now=now)
        return "suspended"
    except GateError as exc:
        error = exc.message
    except Exception:
        log.exception("opening the gate for job %s failed", job_id)
        error = "The server could not open the gate this run paused at."
    with engine.begin() as conn:
        abandon(conn, job_id=job_id, error=error, now=now)
    return "failed"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _resume(conn: Connection, gate: Any, now: datetime) -> str:
    """Queue the job that runs the decided gate's run on."""
    paused = conn.execute(select(jobs).where(jobs.c.id == gate.job_id)).one()
    job_id = worker.queue_job(conn, team_id=gate.team_id, project_id=gate.project_id,
                              workflow_id=paused.workflow_id, inputs=paused.inputs, now=now,
                              trigger_id=paused.trigger_id, delivery_id=paused.delivery_id, thread=gate.thread,
                              resume=(gate.id, paused))
    conn.execute(update(gates).where(gates.c.id == gate.id).values(resume_job_id=job_id))
    return job_id


def decide(conn: Connection, *, gate_id: str, approved: bool, nonce: Any, actor_id: str, via: str, note: Any,
           now: datetime, ip: str | None = None) -> dict[str, Any]:
    """Approve or reject an open gate and queue its run's resume job. 409 when it is decided or expired."""
    if note is not None and not isinstance(note, str):
        raise GateError(400, "note must be text.")
    note = (note or "").strip()[:NOTE_MAX] or None
    if not isinstance(nonce, str):
        raise GateError(400, "nonce is required.")
    state = "approved" if approved else "rejected"
    won = conn.execute(update(gates).where(
        gates.c.id == gate_id, gates.c.state == "open", gates.c.nonce == nonce, gates.c.expires_at > now).values(
        state=state, decided_at=now, decided_by=actor_id, decided_via=via, decision_note=note)).rowcount == 1
    if not won:
        raise GateError(409, "This gate was already decided, or it expired.")
    gate = conn.execute(select(gates).where(gates.c.id == gate_id)).one()
    if not approved:
        writer.server_outcome(conn, gate.run_id, "blocked")
    job_id = _resume(conn, gate, now)
    audit.record(conn, action=f"gate.{'approve' if approved else 'reject'}", actor_id=actor_id,
                 team_id=gate.team_id, target_kind="gate", target_id=gate_id,
                 detail={"run_id": gate.run_id, "via": via, "resume_job_id": job_id}, ip=ip)
    return public(conn.execute(select(gates).where(gates.c.id == gate_id)).one())


def expire(conn: Connection, now: datetime) -> list[str]:
    """Reject every open gate past its expiry as expired, and queue each run's resume job. Returns their ids."""
    expired = []
    for gate in conn.execute(select(gates).where(gates.c.state == "open", gates.c.expires_at <= now)).all():
        if conn.execute(update(gates).where(gates.c.id == gate.id, gates.c.state == "open").values(
                state=EXPIRED, decided_at=now, decision_note=EXPIRED)).rowcount != 1:
            continue
        writer.server_outcome(conn, gate.run_id, "blocked")
        job_id = _resume(conn, gate, now)
        audit.record(conn, action="gate.expire", actor_id=None, team_id=gate.team_id, target_kind="gate",
                     target_id=gate.id, detail={"run_id": gate.run_id, "resume_job_id": job_id})
        expired.append(gate.id)
    return expired


def prune(conn: Connection) -> int:
    """Drop the stored checkpoint of every gate whose run has ended. Nothing resumes from it any more."""
    ended = select(runs.c.id).where(runs.c.status.not_in((*worker.RUN_OPEN, worker.RUN_WAITING)))
    return conn.execute(update(gates).where(gates.c.checkpoint.is_not(None), gates.c.run_id.in_(ended))
                        .values(checkpoint=None)).rowcount


def cancel_run(conn: Connection, *, run_id: str, actor_id: str | None, now: datetime) -> None:
    """Close a run's open gates and end it canceled when it was waiting at one."""
    conn.execute(update(gates).where(gates.c.run_id == run_id, gates.c.state == "open").values(
        state="canceled", decided_at=now, decided_by=actor_id))
    conn.execute(update(runs).where(runs.c.id == run_id, runs.c.status == worker.RUN_WAITING).values(
        status="canceled", finished_at=now, cancelled_by=actor_id))
    runners.revoke_tokens(conn, [run_id])


def resume_checkpoint(conn: Connection, job: Any) -> dict[str, Any] | None:
    """The checkpoint a resume job's run reads: the gate's, with every decided gate of the run as its result."""
    stored = conn.execute(select(gates.c.checkpoint).where(gates.c.id == job.resume_of_gate,
                                                           gates.c.run_id == job.run_id,
                                                           gates.c.resume_job_id == job.id,
                                                           gates.c.state.in_(DECIDED))).first()
    if stored is None or stored.checkpoint is None:
        return None
    decided = conn.execute(select(gates).where(gates.c.run_id == job.run_id, gates.c.state.in_(DECIDED))).all()
    return {**json.loads(stored.checkpoint), **{row.step_key: answer(row) for row in decided}}


def target(conn: Connection, gate_id: str) -> rbac.Target:
    row = conn.execute(select(gates.c.team_id, gates.c.project_id).where(gates.c.id == gate_id)).first()
    if row is None:
        raise rbac.NotFound("No such gate.")
    return rbac.Target(team_id=row.team_id, project_id=row.project_id)


def public(row: Any) -> dict[str, Any]:
    keys = ("id", "run_id", "team_id", "project_id", "step_id", "present", "state", "min_role", "nonce",
            "thread", "created_at", "expires_at", "decided_at", "decided_by", "decided_via", "decision_note",
            "resume_job_id", "card_error")
    out = {key: row._mapping[key] for key in keys}
    for key in ("workflow", "decided_by_name"):
        if key in row._mapping:
            out[key] = row._mapping[key]
    return out


def _listed():
    return (select(gates, workflows.c.name.label("workflow"),
                   users.c.username.label("decided_by_name"))
            .select_from(gates.join(runs, runs.c.id == gates.c.run_id)
                         .outerjoin(workflows, workflows.c.id == runs.c.workflow_id)
                         .outerjoin(users, users.c.id == gates.c.decided_by)))


def list_gates(conn: Connection, scope: rbac.Scope | None, *, state: str | None = None, run_id: str | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
    """Gates newest first. A None scope is for a caller that already checked access, as to one run."""
    query = _listed()
    if scope is not None:
        query = query.where(rbac.scope_clause(scope, gates.c.team_id, gates.c.project_id))
    if state is not None:
        query = query.where(gates.c.state == state)
    if run_id is not None:
        query = query.where(gates.c.run_id == run_id)
    rows = conn.execute(query.order_by(gates.c.created_at.desc(), gates.c.id).limit(limit)).all()
    return [public(r) for r in rows]


def get_gate(conn: Connection, gate_id: str) -> dict[str, Any] | None:
    row = conn.execute(_listed().where(gates.c.id == gate_id)).first()
    return public(row) if row is not None else None

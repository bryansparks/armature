"""Residents: one long-lived instance of a workflow, its event inbox, and the turns that drain it.

A resident is addressed by (workflow_id, instance_key) — the empty key is the workflow's single
unnamed instance — and is created idle at the workflow's current version, which it stays pinned to
across later workflow upgrades. Events land in the inbox ordered by ``seq``, assigned under the
row lock exactly as conversation messages are; a duplicate ``dedupe_key`` collapses to nothing.
Every real event refreshes the instance's lifetime: ``last_event_at`` moves up, the idle streak
and its next idle run are cleared. A paused instance keeps its events for the resume; a closed
one refuses them. What a turn may do with an event is decided in code, never by the model.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from sqlalchemy import Connection, Engine, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import audit, defs, worker
from armature.server.db import new_id
from armature.server.db.schema import jobs, resident_events, residents, runs, workflows
from armature.spec.models import ResidentCfg

log = logging.getLogger(__name__)

RESIDENT_STATES = ("idle", "paused", "closed")
RESIDENT_EVENT_KINDS = ("request", "ask", "schedule", "signal", "idle", "custom")
# The jobs.kind of a resident's turn job, claimable like a workflow's own.
RESIDENT_JOB_KIND = "resident"
# worker.HELD plus queued and suspended. Spelled out because the worker will import this module.
OPEN_TURN = ("queued", "suspended", "provisioning", "claimed", "running")
MAX_ATTEMPTS = 3
ENABLED_KEY = "resident.enabled"
# The lifetime cap that closes an instance when its spec's lifetime.max_turns is unset.
MAX_TURNS = 5000

_yaml = YAML(typ="safe")


def _resident_block(body: str | None) -> dict | None:
    """The parsed ``resident:`` mapping of a workflow body, or None when there is none to read."""
    try:
        data = _yaml.load(body) if body else None
    except YAMLError:
        return None
    block = data.get("resident") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else None


class ResidentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _instance(conn: Connection, workflow_id: str, instance_key: str) -> Any:
    return conn.execute(select(residents).where(residents.c.workflow_id == workflow_id,
                                                residents.c.instance_key == instance_key)).first()


def _locked(conn: Connection, resident_id: str) -> Any:
    return conn.execute(select(residents).where(residents.c.id == resident_id).with_for_update()).first()


def ensure(conn: Connection, *, team_id: str, project_id: str | None, workflow_id: str,
           instance_key: str = "", now: datetime) -> Any:
    """The workflow's resident for this key, created idle at the workflow's current version if absent.

    Two first events at once both try the insert, and the one that loses reads the winner's row.
    Only a new row is audited, as the instance's creation is a governed act.
    """
    row = _instance(conn, workflow_id, instance_key)
    if row is not None:
        return row
    version = conn.execute(select(workflows.c.current_version).where(
        workflows.c.id == workflow_id)).scalar()
    if version is None:
        raise ResidentError(404, "No such workflow.")
    resident_id = new_id()
    created = False
    try:
        with conn.begin_nested():
            conn.execute(insert(residents).values(
                id=resident_id, team_id=team_id, project_id=project_id, workflow_id=workflow_id,
                workflow_version=version, instance_key=instance_key, state="idle", data={},
                turns=0, idle_streak=0, created_at=now, updated_at=now))
        created = True
    except IntegrityError:
        pass
    row = _instance(conn, workflow_id, instance_key)
    if row is None:
        raise ResidentError(409, "The resident instance could not be created.")
    if created:
        audit.record(conn, action="resident.create", actor_id=None, team_id=team_id,
                     target_kind="resident", target_id=resident_id)
    return row


def enqueue(conn: Connection, resident_id: str, kind: str, payload: Any = None, *,
            name: str | None = None, source: Any = None, reply_to: Any = None,
            dedupe_key: str | None = None, dry_run: bool = False, now: datetime) -> int | None:
    """Add an event at the next seq. None when this dedupe_key is already in the inbox.

    A custom kind arrives as ``custom.<name>`` or as ``custom`` with ``name``, and is stored as
    ``kind="custom"`` with its name. A real event keeps the instance's lifetime fresh and wakes
    the instance for its next turn; a paused instance keeps its events for the resume, a closed
    one refuses them.
    """
    resident = _locked(conn, resident_id)
    if resident is None:
        raise ResidentError(404, "No such resident.")
    if resident.state == "closed":
        raise ResidentError(409, "instance is closed")
    if kind.startswith("custom."):
        kind, _, suffix = kind.partition(".")
        name = name or suffix or None
    if kind not in RESIDENT_EVENT_KINDS:
        raise ResidentError(400, f"kind is one of {', '.join(RESIDENT_EVENT_KINDS)}.")
    if dedupe_key is not None and conn.execute(select(resident_events.c.id).where(
            resident_events.c.resident_id == resident_id,
            resident_events.c.dedupe_key == dedupe_key)).first():
        return None
    seq = conn.execute(select(func.coalesce(func.max(resident_events.c.seq), 0)).where(
        resident_events.c.resident_id == resident_id)).scalar_one() + 1
    conn.execute(insert(resident_events).values(
        id=new_id(), resident_id=resident_id, seq=seq, kind=kind, name=name, payload=payload,
        source=source, reply_to=reply_to, dedupe_key=dedupe_key, dry_run=dry_run, created_at=now))
    conn.execute(update(residents).where(residents.c.id == resident_id).values(
        last_event_at=now, idle_streak=0, next_idle_at=None, updated_at=now))
    wake(conn, resident_id, now)
    return seq


def _close(conn: Connection, resident: Any, reason: str, now: datetime) -> None:
    conn.execute(update(residents).where(residents.c.id == resident.id).values(
        state="closed", close_reason=reason, closed_at=now, updated_at=now))
    audit.record(conn, action="resident.close", actor_id=None, team_id=resident.team_id,
                 target_kind="resident", target_id=resident.id, detail={"reason": reason})


def _unhandled(conn: Connection, resident_id: str) -> list[Any]:
    return conn.execute(select(resident_events).where(
        resident_events.c.resident_id == resident_id, resident_events.c.handled.is_(False))
        .order_by(resident_events.c.seq)).all()


def _day_reset(conn: Connection, resident: Any, now: datetime) -> bool:
    """Zero today's counters when the day rolled over, so later budget checks compare against today.

    Returns whether the reset ran; the row it was given still carries yesterday's values.
    """
    today = now.date().isoformat()
    if resident.spent_day == today:
        return False
    conn.execute(update(residents).where(residents.c.id == resident.id).values(
        spent_usd_day=0, turns_day=0, idle_turns_day=0, emits_day=0, spent_day=today))
    return True


def _pause(conn: Connection, resident: Any, reason: str, now: datetime) -> None:
    conn.execute(update(residents).where(residents.c.id == resident.id).values(
        state="paused", close_reason=reason, updated_at=now))
    audit.record(conn, action="resident.pause", actor_id=None, team_id=resident.team_id,
                 target_kind="resident", target_id=resident.id, detail={"reason": reason})


def _spec(conn: Connection, resident: Any) -> ResidentCfg | None:
    """The instance's resident config, read from the workflow version it is pinned to. Or None.

    None means the pinned version carries no readable ``resident:`` block — the spec was removed,
    or never parsed — and an instance that would turn on it pauses until a version brings the
    block back; it never guesses a handler.
    """
    body = defs.body_of(conn, defs.WORKFLOW, resident.workflow_id, resident.workflow_version)
    block = _resident_block(body)
    if block is None:
        return None
    try:
        return ResidentCfg.model_validate(block)
    except ValidationError:
        return None


def _handler(spec: ResidentCfg, event: Any) -> Any:
    """The handler config an event's kind maps to, or None when the spec names none for it.

    Custom kinds map through ``custom.<name>``; ``idle`` lives under ``idle:``, not ``on:``.
    """
    if event.kind == "idle":
        return spec.idle
    if event.kind == "custom":
        return spec.on.get(f"custom.{event.name}") if event.name else None
    return spec.on.get(event.kind)


def _handler_key(event: Any) -> str:
    return f"custom.{event.name}" if event.kind == "custom" and event.name else event.kind


def _named(conn: Connection, scope: str, workflow: str, where: str) -> str:
    """Resolve a named handler workflow in the instance workflow's own scope, as a call resolves."""
    row = conn.execute(select(workflows.c.id, workflows.c.name).where(
        workflows.c.scope == scope, workflows.c.name == workflow)).first()
    # MySQL compares names without case, so the name is matched again here.
    if row is None or row.name != workflow:
        raise ValueError(f"resident handler {where} names workflow '{workflow}', "
                          "which is not in the caller's own project, team, or instance")
    return row.id


def _gated_now(conn: Connection, workflow_id: str) -> bool:
    """Whether the workflow's current version has a gate (the conversations.gated pattern)."""
    version = conn.execute(select(workflows.c.current_version).where(
        workflows.c.id == workflow_id)).scalar()
    return version is not None and worker.gated(defs.body_of(conn, defs.WORKFLOW, workflow_id, version))


def check_handlers(conn: Connection, spec_body: str | None, workflow_id: str) -> None:
    """Refuse a spec whose resident handlers would stall: raise ValueError when any has a human gate.

    A turn must end when it answers, and a gate holds the instance lock for hours, so no handler —
    self, named, or the idle workflow — may run a gated workflow (docs/RESIDENT-WORKFLOWS.md,
    Gates). Called from the version-save path, so a bad save 400s instead of waking a turn that
    can never end. A block that doesn't parse is left to the save's own validation to refuse.
    """
    block = _resident_block(spec_body)
    if block is None:
        return
    try:
        spec = ResidentCfg.model_validate(block)
    except ValidationError:
        return
    scope = conn.execute(select(workflows.c.scope).where(
        workflows.c.id == workflow_id)).scalar()
    handlers = [(f"on.{key}", handler.workflow) for key, handler in spec.on.items()]
    if spec.idle is not None:
        handlers.append(("idle", spec.idle.workflow))
    for where, workflow in handlers:
        if workflow == "self":
            if worker.gated(spec_body):
                raise ValueError(f"resident handler {where} runs this workflow itself, "
                                "which has a human gate; a turn must end when it answers")
        else:
            target = _named(conn, scope, workflow, where)
            if _gated_now(conn, target):
                raise ValueError(f"resident handler {where} names workflow '{workflow}', "
                                 "whose current version has a human gate; "
                                 "a turn must end when it answers")


def _inputs(resident: Any, batch: list[Any]) -> dict[str, Any]:
    """A turn job's inputs: the batch's range and kind, plus the event and instance snapshots.

    Baked at queue time under the lock the turn holds until settle, so they can't drift. Event
    payloads are not baked; a turn reads them fenced through resident.events.
    """
    kind, name = batch[0].kind, batch[0].name
    return {
        "resident_id": resident.id, "instance_key": resident.instance_key,
        "event_seq_from": batch[0].seq, "event_seq_to": batch[-1].seq, "kind": kind,
        "_event": {"kind": kind, "name": name,
                   "is": {k: kind == k for k in RESIDENT_EVENT_KINDS}},
        "_resident": {"state": dict(resident.data) if resident.data else {},
                       "meta": {"turns": resident.turns,
                                "created_at": resident.created_at.isoformat(),
                                "last_turn_at": resident.last_turn_at.isoformat()
                                if resident.last_turn_at is not None else None,
                                "idle_streak": resident.idle_streak}},
    }


def wake(conn: Connection, resident_id: str, now: datetime) -> str | None:
    """Queue the instance's next turn: one job over the oldest unhandled events. Returns the job id, or None.

    One turn at a time: ``active_job_id`` is the lock, taken with one conditional UPDATE; a paused
    or closed instance turns nothing. The spec the instance is pinned to decides everything a
    turn may do: which workflow handles each kind (self at the pinned version, or a named one
    pinned at its current version like a call), how many events one turn batches, and the
    lifetime and budget caps, which close or pause with a reason as they fire. A kind with no
    handler drains as skipped — handled, never turned — so the inbox always moves. An instance
    whose pinned version lost its resident: block pauses until a version brings it back; a
    callee that doesn't resolve counts one attempt against the batch and turns nothing.
    """
    resident = _locked(conn, resident_id)
    if resident is None:
        return None
    fresh = _day_reset(conn, resident, now)
    if resident.state != "idle" or resident.active_job_id is not None:
        return None
    events = _unhandled(conn, resident_id)
    if not events:
        return None
    if events[0].kind == "idle" and any(e.kind != "idle" for e in events):
        conn.execute(update(resident_events).where(
            resident_events.c.resident_id == resident_id, resident_events.c.handled.is_(False),
            resident_events.c.kind == "idle").values(handled=True))
        events = [e for e in events if e.kind != "idle"]
    spec = _spec(conn, resident)
    if spec is None:  # a turn would run on a spec with no resident: block
        _pause(conn, resident, "spec_removed", now)
        return None
    limit = spec.lifetime.max_turns if spec.lifetime.max_turns is not None else MAX_TURNS
    if resident.turns >= limit:
        _close(conn, resident, "max_turns", now)
        return None
    idle_for = spec.lifetime.close_after_idle
    if idle_for is not None and now - (resident.last_event_at or resident.created_at) >= idle_for:
        _close(conn, resident, "lifetime_idle", now)  # real events alone keep an instance alive
        return None
    cap = spec.budget.usd_per_day
    spent = 0 if fresh else resident.spent_usd_day  # the locked row still holds yesterday's counters
    if cap is not None and spent >= round(cap * 1_000_000):
        _pause(conn, resident, "budget", now)  # the sweep resumes it when the day rolls over
        return None
    while events:  # kinds with no handler drain as skipped, so the next-oldest event turns
        handler = _handler(spec, events[0])
        if handler is not None:
            break
        conn.execute(update(resident_events).where(
            resident_events.c.id == events[0].id).values(handled=True))
        events = events[1:]
    if not events:
        return None
    batch = [events[0]]
    for event in events[1:]:
        following = _handler(spec, event)
        if len(batch) >= spec.batch or following is None or following.workflow != handler.workflow:
            break
        batch.append(event)
    where = _handler_key(batch[0])
    try:
        if handler.workflow == "self":
            workflow_id, version = resident.workflow_id, resident.workflow_version
        else:
            scope = conn.execute(select(workflows.c.scope).where(
                workflows.c.id == resident.workflow_id)).scalar()
            workflow_id, version = _named(conn, scope, handler.workflow, where), None
        job_id = worker.queue_job(conn, team_id=resident.team_id, project_id=resident.project_id,
                                  workflow_id=workflow_id, workflow_version=version,
                                  kind=RESIDENT_JOB_KIND, resident_id=resident_id,
                                  key=f"resident:{resident_id}",
                                  thread=f"resident:{resident.instance_key}" if resident.instance_key else "resident",
                                  inputs=_inputs(resident, batch),
                                  dry_run=batch[0].dry_run, now=now)
    except ValueError:
        log.warning("resident %s: its handler for %s doesn't resolve", resident_id, where)
        conn.execute(update(resident_events).where(
            resident_events.c.id.in_([e.id for e in batch])).values(
            attempts=resident_events.c.attempts + 1))
        return None
    # Changing updated_at too keeps the guard a real change, as MySQL counts only changed rows.
    taken = conn.execute(update(residents).where(residents.c.id == resident_id,
                                                 residents.c.active_job_id.is_(None),
                                                 residents.c.state == "idle")
                         .values(active_job_id=job_id, updated_at=now)).rowcount == 1
    if not taken:
        conn.execute(delete(jobs).where(jobs.c.id == job_id))
        return None
    return job_id


def settle(conn: Connection, now: datetime) -> list[str]:
    """Settle every turn whose job finished, then wake each instance for whatever events still wait.

    A succeeded turn's staged state becomes the resident's data — the whole doc, as its turn read and
    rewrote it under the one-turn lock — and its events are handled by its job. Any other end discards
    what was staged, counts one attempt against the batch's events, and gives an event up as poison
    past ``MAX_ATTEMPTS``, handled and audited. The turn's cost lands on today's counters, which roll
    over first; the state column is never touched, so a pause or close set mid-turn survives. Returns
    the resident ids settled.
    """
    ended = conn.execute(select(residents.c.id, jobs.c.id.label("job_id"), jobs.c.status, jobs.c.inputs,
                                jobs.c.run_id)
                         .select_from(residents.join(jobs, jobs.c.id == residents.c.active_job_id))
                         .where(jobs.c.status.not_in(OPEN_TURN))).all()
    done = []
    for row in ended:
        resident = _locked(conn, row.id)
        if resident is None or resident.active_job_id != row.job_id:
            continue
        fresh = _day_reset(conn, resident, now)
        inputs = row.inputs or {}
        staged = resident.staged or {}
        values: dict[str, Any] = {"staged": None, "last_turn_at": now, "active_job_id": None, "updated_at": now}
        if row.status == "succeeded":
            upto = inputs.get("event_seq_to")
            if isinstance(upto, int):
                conn.execute(update(resident_events).where(
                    resident_events.c.resident_id == resident.id, resident_events.c.seq <= upto,
                    resident_events.c.handled.is_(False)).values(handled=True, handled_by_job=row.job_id))
            state = staged.get("state")
            if isinstance(state, dict):
                data = dict(state)
                previous = resident.data or {}
                audit.record(conn, action="resident.state_write", actor_id=None, team_id=resident.team_id,
                             target_kind="resident", target_id=resident.id,
                             detail={"data_version": resident.data_version + 1,
                                     "bytes": len(json.dumps(data, default=str).encode("utf-8")),
                                     "keys": sorted(k for k in set(previous) | set(data)
                                                    if previous.get(k) != data.get(k))})
                values["data"] = data
                values["data_version"] = resident.data_version + 1
            values["turns"] = resident.turns + 1
            values["turns_day"] = (0 if fresh else resident.turns_day) + 1
            if inputs.get("kind") == "idle":
                values["idle_turns_day"] = (0 if fresh else resident.idle_turns_day) + 1
            cost = conn.execute(select(runs.c.cost_usd).where(runs.c.id == row.run_id)).scalar() \
                if row.run_id else None
            values["spent_usd_day"] = (0 if fresh else resident.spent_usd_day) \
                + int(round(float(cost or 0) * 1_000_000))
            values["idle_streak"] = 1 if staged.get("idle_nothing_to_do") else 0
        else:
            seq_from, seq_to = inputs.get("event_seq_from"), inputs.get("event_seq_to")
            if isinstance(seq_from, int) and isinstance(seq_to, int):
                conn.execute(update(resident_events).where(
                    resident_events.c.resident_id == resident.id, resident_events.c.seq >= seq_from,
                    resident_events.c.seq <= seq_to, resident_events.c.handled.is_(False)
                ).values(attempts=resident_events.c.attempts + 1))
                poison = list(conn.execute(select(resident_events.c.seq).where(
                    resident_events.c.resident_id == resident.id, resident_events.c.seq >= seq_from,
                    resident_events.c.seq <= seq_to, resident_events.c.handled.is_(False),
                    resident_events.c.attempts >= MAX_ATTEMPTS)).scalars())
                if poison:
                    conn.execute(update(resident_events).where(
                        resident_events.c.resident_id == resident.id, resident_events.c.seq.in_(poison),
                        resident_events.c.handled.is_(False)).values(handled=True, handled_by_job=row.job_id))
                    audit.record(conn, action="resident.event_poison", actor_id=None, team_id=resident.team_id,
                                 target_kind="resident", target_id=resident.id,
                                 detail={"job_id": row.job_id, "seq": poison})
        conn.execute(update(residents).where(residents.c.id == resident.id,
                                             residents.c.active_job_id == row.job_id).values(**values))
        wake(conn, resident.id, now)
        done.append(resident.id)
    return done


def sweep(engine: Engine, now: datetime) -> None:
    """The resident housekeeping the worker's tick runs: settle finished turns, then resume yesterday's pauses.

    Settle first, so an instance whose turn just ended wakes for whatever still waits before the day pass
    looks at it. The budget is daily, so the pause is too: an instance paused for ``budget`` resumes as
    idle when its ``spent_day`` rolls over, and wakes for the events that waited out the pause. The other
    lifetime closure lives where it belongs — ``max_turns`` in wake — and the idle scheduler slots in here.
    """
    with engine.begin() as conn:
        settle(conn, now)
        today = now.date().isoformat()
        paused = conn.execute(select(residents.c.id).where(
            residents.c.state == "paused", residents.c.close_reason == "budget",
            residents.c.spent_day != today).with_for_update()).scalars().all()
        for resident_id in paused:
            conn.execute(update(residents).where(residents.c.id == resident_id).values(
                state="idle", close_reason=None, updated_at=now))
            wake(conn, resident_id, now)  # wake rolls the day's counters over before the turn spends

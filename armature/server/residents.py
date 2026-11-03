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

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import audit
from armature.server.db import new_id
from armature.server.db.schema import resident_events, residents, workflows

RESIDENT_STATES = ("idle", "paused", "closed")
RESIDENT_EVENT_KINDS = ("request", "ask", "schedule", "signal", "idle", "custom")
# worker.HELD plus queued and suspended. Spelled out because the worker will import this module.
OPEN_TURN = ("queued", "suspended", "provisioning", "claimed", "running")
MAX_ATTEMPTS = 3
ENABLED_KEY = "resident.enabled"


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
    ``kind="custom"`` with its name. A real event keeps the instance's lifetime fresh: a paused
    instance keeps its events for the resume, a closed one refuses them.
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
    return seq
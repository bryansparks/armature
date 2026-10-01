"""Work-unit lifecycle: the state machine and records (design §2.2).

This module is the ONE place the work-unit state machine's legality rules run
(design §5): every writer — the local CLI executor, a dispatch worker applying a
closure record — goes through the same transition table, so a work unit's state
can never move illegally no matter who moves it.

Slice-2 scope: WorkUnitState / TRANSITIONS / records, then the WorkStore
protocol and LocalWorkStore (task 2 of the slice-2 plan). Budget metering
(spent_usd) and ClosureRecord application arrive in slice 3; spent_usd ships
here at 0.0 so the record shape is stable.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from armature.spec.mission import MissionSpec, WorkUnit, resolve_posture


class WorkUnitState(str, Enum):
    """Lifecycle states of a work unit (design §2.2)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    RETRY_PENDING = "retry_pending"
    BLOCKED_ON = "blocked_on"
    HANDED_OFF = "handed_off"
    ESCALATION = "escalation"
    CANCELED = "canceled"


IN_PROGRESS = WorkUnitState.IN_PROGRESS
BLOCKED_ON = WorkUnitState.BLOCKED_ON
CANCELED = WorkUnitState.CANCELED
DONE = WorkUnitState.DONE
FAILED = WorkUnitState.FAILED
RETRY_PENDING = WorkUnitState.RETRY_PENDING
ESCALATION = WorkUnitState.ESCALATION
PENDING = WorkUnitState.PENDING
HANDED_OFF = WorkUnitState.HANDED_OFF

# Legal transitions (design §2.2). Terminal states admit nothing.
TRANSITIONS: dict[WorkUnitState, frozenset[WorkUnitState]] = {
    PENDING: frozenset({IN_PROGRESS, BLOCKED_ON, CANCELED}),
    IN_PROGRESS: frozenset({DONE, FAILED, BLOCKED_ON, HANDED_OFF, ESCALATION, CANCELED}),
    FAILED: frozenset({RETRY_PENDING, ESCALATION, CANCELED}),
    RETRY_PENDING: frozenset({IN_PROGRESS, ESCALATION, CANCELED}),
    BLOCKED_ON: frozenset({IN_PROGRESS, PENDING, CANCELED}),
    ESCALATION: frozenset({IN_PROGRESS, PENDING, CANCELED}),
    DONE: frozenset(),
    HANDED_OFF: frozenset(),
    CANCELED: frozenset(),
}


class TransitionError(ValueError):
    """A state move the lifecycle table does not permit."""


def ensure_legal(frm: WorkUnitState, to: WorkUnitState) -> None:
    """Raise TransitionError unless `frm → to` is in the transition table."""
    if to not in TRANSITIONS[frm]:
        raise TransitionError(f"illegal transition {frm.value} → {to.value}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkUnitRecord(BaseModel):
    """Durable state of one work unit — the serialized shape the S3-backed
    WorkStore in dispatch mirrors (design §5, §6). Identity is the unit, not
    any single run: `last_job_id` records the most recent attempt."""

    model_config = ConfigDict(extra="forbid")

    mission: str
    unit_id: str
    title: str
    objective: str = ""
    workflow: str                              # as authored in the mission doc
    workflow_path: str | None = None           # loader-stamped resolved path
    inputs: dict[str, Any] = Field(default_factory=dict)
    requires: list[str] = Field(default_factory=list)
    posture: str = "human-led"
    state: WorkUnitState = WorkUnitState.PENDING
    attempts: int = 0
    max_attempts: int = 2
    max_budget_usd: float | None = None
    timeout_hours: float | None = None
    last_job_id: str | None = None
    spent_usd: float = 0.0                     # metered in slice 3
    created_at: str = ""
    updated_at: str = ""

    def model_post_init(self, _ctx: Any) -> None:
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at


class TransitionRecord(BaseModel):
    """One append-only audit line for a state move. `from_state` is None only
    for the seeding transition that materializes a record."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    ts: str
    mission: str
    unit_id: str
    from_state: WorkUnitState | None
    to_state: WorkUnitState
    reason: str = ""
    job_id: str | None = None
    actor: str = "system"


class WorkStore(Protocol):
    """Persistence seam for work-unit state (design §5).

    Armature ships the local implementation; the dispatch transport implements
    this same protocol against S3 in slice 4. `apply` is the only door through
    which a state may move — legality is enforced here, not at call sites.
    """

    def load(self, mission: str, unit_id: str) -> WorkUnitRecord | None: ...
    def save(self, record: WorkUnitRecord) -> None: ...
    def list_units(self, mission: str) -> list[WorkUnitRecord]: ...
    def list_transitions(self, mission: str) -> list[TransitionRecord]: ...
    def ensure_unit(self, mission: MissionSpec, unit: WorkUnit) -> WorkUnitRecord: ...
    def apply(
        self,
        mission: str,
        unit_id: str,
        to_state: WorkUnitState,
        *,
        reason: str = "",
        job_id: str | None = None,
        actor: str = "system",
    ) -> WorkUnitRecord: ...


class LocalWorkStore:
    """Dir-of-JSON WorkStore for one machine.

    Layout (records identical to the S3 mirror; layout is store-internal):
        <base>/<mission>/<unit_id>.json      WorkUnitRecord
        <base>/<mission>/transitions.jsonl   append-only audit lines

    Writes are atomic per file (temp file + os.replace). On `apply` the record
    is written first, then the transition appended: a crash between the two
    leaves a moved state without its audit line — visible locally, and the
    record on disk is the authority for current state.
    """

    def __init__(self, base_dir: Path | str) -> None:
        self.base = Path(base_dir).expanduser()

    # -- paths -------------------------------------------------------------

    def _mission_dir(self, mission: str) -> Path:
        return self.base / mission

    def _unit_path(self, mission: str, unit_id: str) -> Path:
        return self._mission_dir(mission) / f"{unit_id}.json"

    def _transitions_path(self, mission: str) -> Path:
        return self._mission_dir(mission) / "transitions.jsonl"

    # -- low-level IO ------------------------------------------------------

    def _write_atomic(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _append_line(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # -- WorkStore ---------------------------------------------------------

    def load(self, mission: str, unit_id: str) -> WorkUnitRecord | None:
        path = self._unit_path(mission, unit_id)
        if not path.exists():
            return None
        return WorkUnitRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, record: WorkUnitRecord) -> None:
        self._write_atomic(
            self._unit_path(record.mission, record.unit_id),
            record.model_dump_json(indent=2),
        )

    def list_units(self, mission: str) -> list[WorkUnitRecord]:
        mdir = self._mission_dir(mission)
        if not mdir.exists():
            return []
        return [
            WorkUnitRecord.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(mdir.glob("*.json"))
        ]

    def list_transitions(self, mission: str) -> list[TransitionRecord]:
        path = self._transitions_path(mission)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        return [TransitionRecord.model_validate_json(line) for line in lines if line]

    def ensure_unit(self, mission: MissionSpec, unit: WorkUnit) -> WorkUnitRecord:
        """Materialize the unit's record from the mission doc if absent.

        The doc is the origin for local use: a missing record is seeded pending.
        An existing record is returned untouched — live state is never clobbered
        by doc state.
        """
        existing = self.load(mission.name, unit.id)
        if existing is not None:
            return existing
        record = WorkUnitRecord(
            mission=mission.name,
            unit_id=unit.id,
            title=unit.title,
            objective=unit.objective,
            workflow=unit.workflow,
            workflow_path=unit.workflow_path,
            inputs=dict(unit.inputs),
            requires=list(unit.requires),
            posture=resolve_posture(mission, unit),
            state=WorkUnitState.PENDING,
            max_attempts=unit.max_attempts,
            max_budget_usd=unit.max_budget_usd,
            timeout_hours=unit.timeout_hours,
        )
        self.save(record)
        self._append_transition(TransitionRecord(
            seq=self._next_seq(mission.name),
            ts=_now_iso(),
            mission=mission.name,
            unit_id=unit.id,
            from_state=None,
            to_state=WorkUnitState.PENDING,
            reason="seeded from mission document",
            actor="system",
        ))
        return record

    def apply(
        self,
        mission: str,
        unit_id: str,
        to_state: WorkUnitState,
        *,
        reason: str = "",
        job_id: str | None = None,
        actor: str = "system",
    ) -> WorkUnitRecord:
        """Move a unit to `to_state` through the legality table, and audit it.

        Refuses (KeyError) when the unit has no record: a typo'd unit id must
        surface as an error, not a ghost record. Never creates.
        """
        record = self.load(mission, unit_id)
        if record is None:
            raise KeyError(
                f"work unit '{mission}/{unit_id}' has no record; "
                "create one via ensure_unit before applying a transition"
            )
        frm = record.state
        ensure_legal(frm, to_state)
        record.state = to_state
        record.updated_at = _now_iso()
        if job_id is not None:
            record.last_job_id = job_id
        self.save(record)
        self._append_transition(TransitionRecord(
            seq=self._next_seq(mission),
            ts=_now_iso(),
            mission=mission,
            unit_id=unit_id,
            from_state=frm,
            to_state=to_state,
            reason=reason,
            job_id=job_id,
            actor=actor,
        ))
        return record

    # -- internals ---------------------------------------------------------

    def _append_transition(self, tr: TransitionRecord) -> None:
        self._append_line(self._transitions_path(tr.mission), tr.model_dump_json())

    def _next_seq(self, mission: str) -> int:
        return len(self.list_transitions(mission)) + 1

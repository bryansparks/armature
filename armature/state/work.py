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

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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

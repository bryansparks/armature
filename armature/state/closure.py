"""The inter-run closure contract: typed record + extraction (design §3).

A run whose spec declares `closure: {stage: ...}` ends by having that stage's
guided_json output extracted into a ClosureRecord. This module is pure: it
converts and validates. Applying the record to work-unit state happens
through the WorkStore in apply_closure() (below, Task 3) — the one door
legality runs through. Executors (the local mission CLI, dispatch's runner
step 7.6) call the same two functions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from armature.spec.models import CLOSURE_REASONS, HarnessSpec


class ClosureError(ValueError):
    """A closure stage output that does not satisfy the closure contract."""


class FollowOnUnit(BaseModel):
    """A mini work-unit spec declared by a closure (design §3). `id` is the
    durable address (design §7) — required, like every other work unit."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    objective: str = ""
    workflow: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class ClosureRecord(BaseModel):
    """Typed closure of one run, as applied to a work unit through the store."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    notes: str = ""
    follow_on: list[FollowOnUnit] = Field(default_factory=list)
    unit_id: str | None = None
    mission: str | None = None
    job_id: str | None = None


def extract_closure(spec: HarnessSpec, results: dict[str, Any]) -> ClosureRecord | None:
    """Extract the ClosureRecord from a completed run's results.

    None when the spec declares no closure or the closure stage produced no
    output (a failed run). Raises ClosureError when an output exists that
    violates the closure contract — silence is explicit, malformed is not.
    """
    if spec.closure is None:
        return None
    raw = results.get(spec.closure.stage)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ClosureError(
            f"closure stage '{spec.closure.stage}' output must be an object, "
            f"got {type(raw).__name__}")
    payload = {k: v for k, v in raw.items() if not k.startswith("_")}
    if payload.get("reason") not in CLOSURE_REASONS:
        raise ClosureError(
            f"closure reason must be one of {list(CLOSURE_REASONS)}, "
            f"got {payload.get('reason')!r}")
    try:
        return ClosureRecord.model_validate(payload)
    except Exception as exc:
        raise ClosureError(f"invalid closure record: {exc}") from exc
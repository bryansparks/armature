"""Mission documents — the armature work layer above workflows (design §2).

A mission is a larger objective plus a set of work units, each outliving any
single run. Work units declare which workflow accomplishes them; the lifecycle
and closure machinery arrive in later slices. This module is the document
type: models, loader, validator. Executors (local run, dispatch) store and
schedule what this grammar defines.

Field names map one-to-one onto the design doc §2.1 table.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ruamel.yaml import YAML, YAMLError

from armature.runtime.dag import topological_order
from armature.spec.loader import resolve_spec_ref
from armature.spec.validator import SpecError, SpecValidationError


class WorkUnit(BaseModel):
    """One unit of work — the durable identity in the work layer (design §7)."""
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    objective: str = ""                       # slice context, layered with the mission objective
    workflow: str                             # registered name, or path to a spec (as authored)
    workflow_path: str | None = None          # loader-stamped resolved path when path-like
    inputs: dict[str, Any] = Field(default_factory=dict)
    requires: list[str] = Field(default_factory=list)   # work-unit ids that must reach done first
    posture: Literal["human-led", "delegated"] | None = None  # None → inherit mission default
    max_budget_usd: float | None = None
    timeout_hours: float | None = None
    max_attempts: int = 2


class MissionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = "1.0"
    description: str = ""
    objective: str = ""                       # THE context every run of every unit inherits
    posture: Literal["human-led", "delegated"] = "human-led"
    budget_usd: float | None = None           # mission-level allocation ceiling
    work: list[WorkUnit] = Field(default_factory=list)


_YAML = YAML(typ="safe")


def _is_path_like(workflow: str) -> bool:
    return workflow.endswith((".yml", ".yaml"))


def load_mission(path: Path) -> MissionSpec:
    """Load and type-validate a mission document. Loud on every failure mode."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f"mission document not found: {path}")
    try:
        data = _YAML.load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise ValueError(f"mission document is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"mission document must be a YAML mapping, got {type(data).__name__}")
    if "work" in data and not isinstance(data["work"], list):
        raise ValueError("mission 'work' must be a list of work units")
    try:
        mission = MissionSpec(**data)
    except ValidationError as exc:
        # pydantic dumps field paths like "work -> 0 -> id"; flatten to one clear message
        raise ValueError(f"invalid mission document: {exc}") from exc
    base = path.resolve().parent
    for unit in mission.work:
        if _is_path_like(unit.workflow):
            # subagent_spec convention via the shared helper: absolute as-is,
            # else the document's own directory first, then process cwd.
            # An unresolvable ref still gets the doc-dir candidate stamped so
            # validation can report WORKFLOW_NOT_REGISTERED.
            resolved = resolve_spec_ref(unit.workflow, base)
            unit.workflow_path = str(resolved if resolved is not None
                                     else (base / unit.workflow).resolve())
    return mission


def resolve_posture(mission: MissionSpec, unit: WorkUnit) -> str:
    """Posture precedence (design §2.3): unit override → mission default.
    The default is human-led — matching OpenRig's grantsAuthority: false posture:
    executors may notify in human-led scope, never act."""
    return unit.posture or mission.posture


def validate_mission(mission: MissionSpec, *, strict: bool = True) -> list[SpecError]:
    """Validate a MissionSpec; same contract as validate_spec.

    The lifecycle rules defined here (references, cycles, budgets) are the
    static half of design §2.2 — the dynamic half (state transitions) arrives
    with WorkStore in slice 2 and applies these same concepts.
    """
    errors: list[SpecError] = []
    unit_ids = [u.id for u in mission.work]

    # ── Empty mission ──────────────────────────────────────────────────────
    if not mission.work:
        errors.append(SpecError(
            code="MISSION_NO_WORK_UNITS",
            message="Mission declares no work units — it can never run anything",
            severity="warning",
        ))

    # ── Duplicate unit IDs ─────────────────────────────────────────────────
    seen: set[str] = set()
    for uid in unit_ids:
        if uid in seen:
            errors.append(SpecError(
                code="DUPLICATE_WORK_UNIT",
                message=f"Work unit '{uid}' is defined more than once",
                stage_id=uid,
            ))
        seen.add(uid)

    # ── Field sanity (slice-2 hardening: blank ids, numeric bounds) ───────
    if mission.budget_usd is not None and mission.budget_usd < 0:
        errors.append(SpecError(
            code="MISSION_FIELD_INVALID",
            message=f"mission budget_usd must be >= 0, got {mission.budget_usd}",
        ))
    for unit in mission.work:
        if not unit.id.strip():
            errors.append(SpecError(
                code="MISSION_FIELD_INVALID",
                message=f"work unit id must be non-empty, got {unit.id!r}",
                stage_id=unit.id,
            ))
        if unit.max_attempts < 1:
            errors.append(SpecError(
                code="MISSION_FIELD_INVALID",
                message=f"unit '{unit.id}' max_attempts must be >= 1, got {unit.max_attempts}",
                stage_id=unit.id,
            ))
        if unit.max_budget_usd is not None and unit.max_budget_usd < 0:
            errors.append(SpecError(
                code="MISSION_FIELD_INVALID",
                message=(f"unit '{unit.id}' max_budget_usd must be >= 0, "
                         f"got {unit.max_budget_usd}"),
                stage_id=unit.id,
            ))
        if unit.timeout_hours is not None and unit.timeout_hours < 0:
            errors.append(SpecError(
                code="MISSION_FIELD_INVALID",
                message=(f"unit '{unit.id}' timeout_hours must be >= 0, "
                         f"got {unit.timeout_hours}"),
                stage_id=unit.id,
            ))

    # ── Undefined requires references (order-independent) ──────────────────
    for unit in mission.work:
        for dep in unit.requires:
            if dep not in seen:
                errors.append(SpecError(
                    code="UNKNOWN_WORK_UNIT",
                    message=f"work unit '{unit.id}' requires unknown unit '{dep}'",
                    stage_id=unit.id,
                ))

    # ── Cycle detection — same primitive as the workflow validator ────────
    try:
        deps = {u.id: u.requires for u in mission.work}
        topological_order(deps)
    except ValueError:
        errors.append(SpecError(
            code="CIRCULAR_DEPENDENCY",
            message="Work unit dependencies form a cycle (including self-reference)",
        ))

    # ── Budget ceilings ────────────────────────────────────────────────────
    for unit in mission.work:
        if (mission.budget_usd is not None and unit.max_budget_usd is not None
                and unit.max_budget_usd > mission.budget_usd):
            errors.append(SpecError(
                code="MISSION_BUDGET_CONFLICT",
                message=(f"unit '{unit.id}' max_budget_usd {unit.max_budget_usd} "
                         f"exceeds mission budget_usd {mission.budget_usd}"),
                stage_id=unit.id,
            ))

    # ── Workflow references ────────────────────────────────────────────────
    for unit in mission.work:
        if unit.workflow_path is not None:
            if not Path(unit.workflow_path).exists():
                errors.append(SpecError(
                    code="WORKFLOW_NOT_REGISTERED",
                    message=f"unit '{unit.id}' workflow path does not resolve: {unit.workflow_path}",
                    stage_id=unit.id,
                ))
        else:
            # Registry-style bare name: registration is executor-side, so local
            # validation can only warn (design §9 Q-decisions: WORKFLOW_NOT_REGISTERED
            # is the error for unresolvable paths; names defer to executors).
            errors.append(SpecError(
                code="WORKFLOW_UNVERIFIED_NAME",
                message=(f"unit '{unit.id}' names workflow '{unit.workflow}' — "
                         "bare names are registered by executors; cannot verify locally"),
                stage_id=unit.id,
                severity="warning",
            ))

    if strict:
        hard_errors = [e for e in errors if e.severity != "warning"]
        if hard_errors:
            raise SpecValidationError(hard_errors)
    return errors

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
        data = _YAML.load(path.read_text())
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
            # subagent_spec convention: document's own directory first, then cwd
            candidate = (base / unit.workflow).resolve()
            unit.workflow_path = str(candidate)
    return mission
"""CampaignPlan schema and loader."""
from __future__ import annotations

from pathlib import Path
import re

import yaml
from pydantic import BaseModel, Field, field_validator

KNOWN_LEVERS = {"none", "input_difficulty_ramp", "spec_corruption",
                "model_tier_degradation"}
KNOWN_GATHERS = {
    "hqs_trace", "improve_log", "spec_history", "pending",
    "dashboard_json", "stderr",
}


class Budget(BaseModel):
    max_runs: int = 50
    max_wallclock_hours: float | None = None
    max_llm_calls: int | None = None
    max_tokens: int | None = None


class SelfImprove(BaseModel):
    enabled: bool = True
    target_hqs: float = 0.75
    min_traces: int = 3
    max_rounds: int = 3
    apply: bool = False          # False => review-only (--no-apply, writes .pending.yaml)
    gather_corpora: bool = True


class Phase(BaseModel):
    id: str
    lever: str = "none"
    inputs: dict[str, str] = Field(default_factory=dict)
    repeats: int = 1
    self_improve: SelfImprove | None = None
    gathers: list[str] = Field(default_factory=lambda: list(KNOWN_GATHERS))
    # Reset the trace DB at the start of this phase so the phase's runs are not
    # diluted by prior phases' traces. Armature computes self-improve's
    # hqs_before across the last ~200 traces in ONE shared DB, so a few failure
    # runs after many successful ones never drop the aggregate below target and
    # self_improve never fires. fresh_db isolates a degradation phase so its
    # failures actually drive hqs_before < target_hqs -> needs_improvement True.
    fresh_db: bool = False

    @field_validator("lever")
    @classmethod
    def _check_lever(cls, v: str) -> str:
        if v not in KNOWN_LEVERS:
            raise ValueError(f"unknown lever: {v!r} (known: {sorted(KNOWN_LEVERS)})")
        return v

    @field_validator("gathers")
    @classmethod
    def _check_gathers(cls, v: list[str]) -> list[str]:
        bad = [g for g in v if g not in KNOWN_GATHERS]
        if bad:
            raise ValueError(f"unknown gather: {bad}")
        return v


class Verdicts(BaseModel):
    hqs_tracks_difficulty: dict = Field(default_factory=dict)
    self_improve_fires_and_recovers: dict = Field(default_factory=dict)
    hqs_formula_consistency: dict = Field(default_factory=dict)
    memory_carry_forward_helps: dict = Field(default_factory=dict)


class CampaignPlan(BaseModel):
    name: str
    description: str = ""
    workflow: str
    traces_db: str | None = None          # default resolved by sandbox
    working_spec: str | None = None       # default resolved by sandbox
    budget: Budget = Field(default_factory=Budget)
    tiers: dict[str, dict] = Field(default_factory=dict)
    phases: list[Phase]
    verdicts: Verdicts = Field(default_factory=Verdicts)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            raise ValueError("name must be lowercase kebab/snake")
        return v


def load_plan(path: Path) -> CampaignPlan:
    """Load and validate a campaign plan YAML file."""
    raw = yaml.safe_load(Path(path).read_text())
    return CampaignPlan.model_validate(raw)
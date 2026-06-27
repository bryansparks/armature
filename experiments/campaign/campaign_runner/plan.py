"""CampaignPlan schema and loader."""
from __future__ import annotations

from pathlib import Path
import re

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

KNOWN_LEVERS = {"none", "input_difficulty_ramp", "spec_corruption",
                "model_tier_degradation", "memory_cold_warm"}
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


class Concurrency(BaseModel):
    workers: int = 2
    driver: str = "armature_loop"
    shared_db: bool = True
    reps_per_worker: int = 20

    @field_validator("driver")
    @classmethod
    def _check_driver(cls, v: str) -> str:
        if v not in {"armature_loop", "armature_run_force"}:
            raise ValueError(f"unknown concurrency driver: {v!r} (known: armature_loop, armature_run_force)")
        return v

    @field_validator("workers")
    @classmethod
    def _check_workers(cls, v: int) -> int:
        if v < 2:
            raise ValueError("concurrency.workers must be >= 2")
        return v


class TierOverride(BaseModel):
    apply: bool = True
    tiers: dict[str, dict] = Field(default_factory=dict)


class SoakVerdicts(BaseModel):
    no_unclean_exits: dict = Field(default_factory=dict)
    trace_db_integrity: dict = Field(default_factory=dict)
    no_row_loss_under_concurrency: dict = Field(default_factory=dict)
    hqs_stability_no_drift: dict = Field(default_factory=dict)
    wallclock_stability: dict = Field(default_factory=dict)
    checkpoint_resume_correctness: dict = Field(default_factory=dict)
    budget_obeyed: dict = Field(default_factory=dict)
    agent_spawn_count: dict = Field(default_factory=dict)


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
    workflow: str | None = None
    concurrency: Concurrency | None = None

    @model_validator(mode="after")
    def _check_concurrency_self_improve(self):
        if (self.concurrency is not None and self.self_improve is not None
                and self.self_improve.enabled):
            raise ValueError(
                "CONCURRENCY_AND_SELF_IMPROVE_CONFLICT: a phase may not set both "
                "concurrency and an enabled self_improve")
        return self

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
    purpose: str = ""
    tier_override: TierOverride | None = None
    soak_verdicts: SoakVerdicts | None = None

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
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from armature.spec.models import HarnessSpec


@dataclass
class RiskFactor:
    label: str
    delta: int


@dataclass
class SpecRiskResult:
    score: int
    tier: str
    factors: list[RiskFactor] = field(default_factory=list)


def _tier_for(score: int) -> str:
    if score < 30:
        return "low"
    if score < 60:
        return "medium"
    if score < 85:
        return "high"
    return "critical"


def compute_spec_risk(spec: "HarnessSpec") -> SpecRiskResult:
    """Compute a KYA-inspired static risk score for a HarnessSpec.

    Score [0–100] is built from additive factors:
      +4 per tool-call stage (non-LLM tool execution)
      +15 if no judge stage (unvalidated outputs)
      +8 per require_approval safety rule
      +6 per fan-out stage (amplified blast radius)
      −10 if safety_mode == "strict" (governance credit)
    """
    factors: list[RiskFactor] = []
    raw = 0

    from armature.spec.models import RoleType

    tool_stages = [s for s in spec.stages if s.tool_call is not None]
    if tool_stages:
        delta = len(tool_stages) * 4
        factors.append(RiskFactor(f"{len(tool_stages)} tool-call stage(s) (+4 each)", delta))
        raw += delta

    has_judge = any(
        s.role is not None and s.role.type == RoleType.JUDGE
        for s in spec.stages
    )
    if not has_judge:
        factors.append(RiskFactor("no judge stage (unvalidated outputs)", 15))
        raw += 15

    approval_rules = [r for r in spec.safety_rules if r.action == "require_approval"]
    if approval_rules:
        delta = len(approval_rules) * 8
        factors.append(RiskFactor(f"{len(approval_rules)} require_approval rule(s) (+8 each)", delta))
        raw += delta

    fan_out_stages = [s for s in spec.stages if s.fan_out is not None]
    if fan_out_stages:
        delta = len(fan_out_stages) * 6
        factors.append(RiskFactor(f"{len(fan_out_stages)} fan-out stage(s) (+6 each)", delta))
        raw += delta

    if getattr(spec, "safety_mode", "permissive") == "strict":
        factors.append(RiskFactor("strict safety mode (governance credit)", -10))
        raw -= 10

    score = max(0, min(100, raw))
    return SpecRiskResult(score=score, tier=_tier_for(score), factors=factors)

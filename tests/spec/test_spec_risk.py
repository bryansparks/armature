"""Tests for KYA-inspired static spec risk scoring (armature/spec/risk.py)."""
import pytest
from armature.spec.risk import compute_spec_risk, SpecRiskResult, RiskFactor
from armature.spec.models import HarnessSpec, Stage, Role, RoleType, ToolCallConfig, ToolSafetyRule, SafetyCondition


def _stage(id: str, *, role_type: RoleType | None = None, tool_call: bool = False, fan_out: int | None = None) -> Stage:
    role = Role(name=id, type=role_type, description="test role") if role_type else None
    tc = ToolCallConfig(name="bash", args={}) if tool_call else None
    return Stage(id=id, role=role, depends_on=[], tool_call=tc, fan_out=fan_out)


def _spec(**kwargs) -> HarnessSpec:
    defaults = {
        "name": "test-wf",
        "stages": [_stage("s1", role_type=RoleType.WORKER)],
        "safety_rules": [],
    }
    defaults.update(kwargs)
    return HarnessSpec(**defaults)


# ── tier boundaries ───────────────────────────────────────────────────────────

def test_empty_worker_no_tools_is_low_risk():
    spec = _spec(stages=[_stage("s1", role_type=RoleType.WORKER)])
    result = compute_spec_risk(spec)
    # No tool_call, no judge needed penalty absent, score should be low
    assert isinstance(result, SpecRiskResult)
    assert result.tier == "low"
    assert 0 <= result.score <= 29


def test_tool_call_stages_add_four_each():
    """Each tool-call stage contributes +4 to score."""
    spec_no_tool = _spec(stages=[_stage("s1", role_type=RoleType.WORKER)])
    spec_with_tool = _spec(stages=[_stage("s1", role_type=RoleType.WORKER, tool_call=True)])
    result_no = compute_spec_risk(spec_no_tool)
    result_with = compute_spec_risk(spec_with_tool)
    assert result_with.score == result_no.score + 4


def test_no_judge_stage_adds_fifteen():
    """Missing judge stage adds +15 (unvalidated outputs)."""
    spec_no_judge = _spec(stages=[_stage("s1", role_type=RoleType.WORKER)])
    spec_with_judge = _spec(stages=[
        _stage("s1", role_type=RoleType.WORKER),
        _stage("judge", role_type=RoleType.JUDGE),
    ])
    result_no = compute_spec_risk(spec_no_judge)
    result_with = compute_spec_risk(spec_with_judge)
    assert result_no.score == result_with.score + 15


def test_require_approval_rules_add_eight_each():
    """Each require_approval safety rule adds +8."""
    base_rule = ToolSafetyRule(
        tool="bash",
        condition=SafetyCondition(field="cmd", op="contains", value="rm"),
        action="require_approval",
        message="destructive",
    )
    spec_no_rule = _spec()
    spec_with_rule = _spec(safety_rules=[base_rule])
    r_no = compute_spec_risk(spec_no_rule)
    r_with = compute_spec_risk(spec_with_rule)
    assert r_with.score == r_no.score + 8


def test_strict_safety_mode_reduces_score():
    """safety_mode='strict' grants a governance credit (negative delta)."""
    spec_permissive = _spec(safety_mode="permissive")
    spec_strict = _spec(safety_mode="strict")
    r_permissive = compute_spec_risk(spec_permissive)
    r_strict = compute_spec_risk(spec_strict)
    assert r_strict.score < r_permissive.score


def test_fan_out_stages_add_six_each():
    """Fan-out stages add +6 (expanded blast radius)."""
    spec_no_fanout = _spec(stages=[_stage("s1", role_type=RoleType.WORKER)])
    spec_with_fanout = _spec(stages=[_stage("s1", role_type=RoleType.WORKER, fan_out=4)])
    r_no = compute_spec_risk(spec_no_fanout)
    r_with = compute_spec_risk(spec_with_fanout)
    assert r_with.score == r_no.score + 6


def test_score_clamped_at_100():
    """Score never exceeds 100 regardless of factor accumulation."""
    rules = [
        ToolSafetyRule(
            tool="bash",
            condition=SafetyCondition(field="cmd", op="contains", value=f"x{i}"),
            action="require_approval",
            message="test",
        )
        for i in range(20)  # 20 × 8 = 160 points from rules alone
    ]
    stages = [_stage(f"s{i}", role_type=RoleType.WORKER, tool_call=True) for i in range(10)]
    spec = _spec(stages=stages, safety_rules=rules)
    result = compute_spec_risk(spec)
    assert result.score <= 100


def test_result_includes_factor_list():
    """SpecRiskResult carries a list of RiskFactor explaining the score."""
    spec = _spec(
        stages=[_stage("s1", role_type=RoleType.WORKER, tool_call=True)],
        safety_mode="permissive",
    )
    result = compute_spec_risk(spec)
    assert isinstance(result.factors, list)
    assert len(result.factors) > 0
    assert all(isinstance(f, RiskFactor) for f in result.factors)
    assert all(isinstance(f.label, str) and isinstance(f.delta, int) for f in result.factors)


def test_tier_boundaries_correct():
    """Verify the four tier labels map to correct score ranges."""
    # Build specs that hit each tier
    from armature.spec.risk import _tier_for
    assert _tier_for(0) == "low"
    assert _tier_for(29) == "low"
    assert _tier_for(30) == "medium"
    assert _tier_for(59) == "medium"
    assert _tier_for(60) == "high"
    assert _tier_for(84) == "high"
    assert _tier_for(85) == "critical"
    assert _tier_for(100) == "critical"

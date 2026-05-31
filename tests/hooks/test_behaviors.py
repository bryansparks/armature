"""Tests for BehaviorRule / BehaviorRegistry trace-triggered reactive hooks."""
from armature.state.traces import TraceRecord


def make_trace(success: bool = True, output_valid: bool = True, latency_ms: float = 100.0) -> TraceRecord:
    return TraceRecord(
        run_id="r1",
        workflow_name="wf",
        stage_id="s1",
        role_type="worker",
        model="gpt-4o-mini",
        success=success,
        output_valid=output_valid,
        latency_ms=latency_ms,
    )


# ── BehaviorRule / BehaviorRegistry ───────────────────────────────────────────

def test_behavior_rule_fires_when_pattern_matches():
    from armature.hooks.lifecycle import BehaviorRule, BehaviorRegistry
    fired = []
    rule = BehaviorRule(
        name="always",
        description="fires always",
        pattern=lambda traces: True,
        handler=lambda traces: fired.append(True),
    )
    registry = BehaviorRegistry()
    registry.register(rule)
    registry.evaluate([make_trace()])
    assert fired == [True]


def test_behavior_rule_skips_when_pattern_no_match():
    from armature.hooks.lifecycle import BehaviorRule, BehaviorRegistry
    fired = []
    rule = BehaviorRule(
        name="never",
        description="never fires",
        pattern=lambda traces: False,
        handler=lambda traces: fired.append(True),
    )
    registry = BehaviorRegistry()
    registry.register(rule)
    registry.evaluate([make_trace()])
    assert fired == []


def test_multiple_rules_evaluated_independently():
    from armature.hooks.lifecycle import BehaviorRule, BehaviorRegistry
    results = []
    rule_a = BehaviorRule(name="a", description="", pattern=lambda t: True,  handler=lambda t: results.append("a"))
    rule_b = BehaviorRule(name="b", description="", pattern=lambda t: False, handler=lambda t: results.append("b"))
    rule_c = BehaviorRule(name="c", description="", pattern=lambda t: True,  handler=lambda t: results.append("c"))
    registry = BehaviorRegistry()
    for r in [rule_a, rule_b, rule_c]:
        registry.register(r)
    registry.evaluate([make_trace()])
    assert results == ["a", "c"]


# ── ihr_feedback built-in behavior ────────────────────────────────────────────

def test_ihr_feedback_pattern_fires_below_threshold():
    from armature.hooks.lifecycle import _ihr_feedback_pattern
    bad_traces = [make_trace(success=False, output_valid=False, latency_ms=9000.0) for _ in range(5)]
    assert _ihr_feedback_pattern(bad_traces) is True


def test_ihr_feedback_pattern_skips_above_threshold():
    from armature.hooks.lifecycle import _ihr_feedback_pattern
    good_traces = [make_trace(success=True, output_valid=True, latency_ms=50.0) for _ in range(5)]
    assert _ihr_feedback_pattern(good_traces) is False


def test_ihr_feedback_pattern_requires_min_traces():
    from armature.hooks.lifecycle import _ihr_feedback_pattern
    # Fewer than 3 traces → False regardless of quality
    bad_traces = [make_trace(success=False, output_valid=False) for _ in range(2)]
    assert _ihr_feedback_pattern(bad_traces) is False

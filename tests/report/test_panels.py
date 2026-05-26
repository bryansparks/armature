"""Tests for Rich panel renderables — verify they produce renderable output."""
from __future__ import annotations
import pytest
from rich.console import Console
from io import StringIO

from armature.report.aggregator import (
    DashboardData, StageStats, ImprovementCycle, SafetyStats,
    load_safety_stats,
)
from armature.report.panels import (
    health_strip,
    stage_breakdown,
    improvement_timeline,
    safety_governance,
)
from armature.state.traces import TraceRecord


def render(renderable) -> str:
    """Render a Rich object to a plain string (no ANSI codes)."""
    sio = StringIO()
    console = Console(file=sio, highlight=False, markup=True, width=120)
    console.print(renderable)
    return sio.getvalue()


def make_dashboard(
    ihr_trend=None,
    stage_stats=None,
    improvement_cycles=None,
    total_runs=10,
    workflow_name="test-wf",
) -> DashboardData:
    return DashboardData(
        workflow_name=workflow_name,
        total_runs=total_runs,
        traces=[],
        stage_stats=stage_stats or {},
        improvement_cycles=improvement_cycles or [],
        safety_stats=load_safety_stats([]),
        ihr_trend=ihr_trend or [0.75, 0.80],
        last_run_id="abc123",
    )


# ── health_strip ──────────────────────────────────────────────────────────────

class TestHealthStrip:
    def test_renders_workflow_name(self):
        data = make_dashboard(workflow_name="my-workflow")
        out = render(health_strip(data))
        assert "my-workflow" in out

    def test_renders_ihr_value(self):
        data = make_dashboard(ihr_trend=[0.83])
        out = render(health_strip(data))
        assert "0.8" in out  # IHR value present

    def test_renders_run_count(self):
        data = make_dashboard(total_runs=42)
        out = render(health_strip(data))
        assert "42" in out

    def test_renders_sparkline_characters(self):
        blocks = "▁▂▃▄▅▆▇█"
        data = make_dashboard(ihr_trend=[0.6, 0.7, 0.8, 0.75])
        out = render(health_strip(data))
        assert any(ch in out for ch in blocks)

    def test_returns_renderable(self):
        data = make_dashboard()
        p = health_strip(data)
        # Rich renderables have __rich_console__ or are Panel/Text etc.
        assert p is not None


# ── stage_breakdown ───────────────────────────────────────────────────────────

class TestStageBreakdown:
    def test_renders_stage_id(self):
        stats = {
            "analyst": StageStats(
                stage_id="analyst", role_type="worker",
                run_count=5, failure_rate=0.2, avg_latency_ms=1200.0,
                avg_quorum=None, escalation_rate=0.0, is_post_run=False,
            )
        }
        data = make_dashboard(stage_stats=stats)
        out = render(stage_breakdown(data))
        assert "analyst" in out

    def test_renders_failure_rate(self):
        stats = {
            "bad_stage": StageStats(
                stage_id="bad_stage", role_type="worker",
                run_count=10, failure_rate=0.40, avg_latency_ms=500.0,
                avg_quorum=None, escalation_rate=0.0, is_post_run=False,
            )
        }
        data = make_dashboard(stage_stats=stats)
        out = render(stage_breakdown(data))
        assert "40" in out  # 40% failure rate

    def test_empty_stages_still_renders(self):
        data = make_dashboard(stage_stats={})
        out = render(stage_breakdown(data))
        assert out  # non-empty output

    def test_post_run_stage_rendered(self):
        stats = {
            "refiner": StageStats(
                stage_id="refiner", role_type="researcher",
                run_count=3, failure_rate=0.0, avg_latency_ms=3000.0,
                avg_quorum=None, escalation_rate=0.0, is_post_run=True,
            )
        }
        data = make_dashboard(stage_stats=stats)
        out = render(stage_breakdown(data))
        assert "refiner" in out


# ── improvement_timeline ──────────────────────────────────────────────────────

class TestImprovementTimeline:
    def test_no_cycles_renders_placeholder(self):
        data = make_dashboard(improvement_cycles=[])
        out = render(improvement_timeline(data))
        assert out  # renders something

    def test_renders_cycle_ihr(self):
        cycles = [
            ImprovementCycle(
                cycle_number=1,
                timestamp="2026-05-26T10:00:00Z",
                ihr_before=0.72,
                drift_score=0.0,
                applied=True,
                requires_review=False,
                verified_fixes=0,
                missed_predictions=0,
                unexpected_regressions=0,
                predicted_fixes=[],
                predicted_regressions=[],
            )
        ]
        data = make_dashboard(improvement_cycles=cycles)
        out = render(improvement_timeline(data))
        assert "0.72" in out

    def test_review_required_cycle_rendered(self):
        cycles = [
            ImprovementCycle(
                cycle_number=2,
                timestamp="2026-05-26T10:00:00Z",
                ihr_before=0.65,
                drift_score=0.0,
                applied=False,
                requires_review=True,
                verified_fixes=0,
                missed_predictions=0,
                unexpected_regressions=0,
                predicted_fixes=[],
                predicted_regressions=[],
            )
        ]
        data = make_dashboard(improvement_cycles=cycles)
        out = render(improvement_timeline(data))
        # Should indicate pending/review state
        assert "review" in out.lower() or "pending" in out.lower() or "⏳" in out

    def test_high_drift_score_visible(self):
        cycles = [
            ImprovementCycle(
                cycle_number=1,
                timestamp="2026-05-26T00:00:00Z",
                ihr_before=0.60,
                drift_score=0.75,
                applied=True,
                requires_review=False,
                verified_fixes=0,
                missed_predictions=0,
                unexpected_regressions=0,
                predicted_fixes=[],
                predicted_regressions=[],
            )
        ]
        data = make_dashboard(improvement_cycles=cycles)
        out = render(improvement_timeline(data))
        assert "0.75" in out


# ── safety_governance ─────────────────────────────────────────────────────────

class TestSafetyGovernance:
    def test_renders_policy_version(self):
        safety = SafetyStats(
            warn_hits=2, block_hits=0, approval_hits=1,
            postcondition_failures=0,
            current_policy_version="abc12345",
            stale_memory_count=0,
        )
        data = make_dashboard()
        data = DashboardData(
            workflow_name="wf",
            total_runs=5,
            traces=[],
            stage_stats={},
            improvement_cycles=[],
            safety_stats=safety,
            ihr_trend=[0.80],
            last_run_id="r1",
        )
        out = render(safety_governance(data))
        assert "abc12345" in out

    def test_stale_memory_count_rendered(self):
        safety = SafetyStats(
            warn_hits=0, block_hits=0, approval_hits=0,
            postcondition_failures=0,
            current_policy_version=None,
            stale_memory_count=3,
        )
        data = DashboardData(
            workflow_name="wf",
            total_runs=5,
            traces=[],
            stage_stats={},
            improvement_cycles=[],
            safety_stats=safety,
            ihr_trend=[0.80],
            last_run_id="r1",
        )
        out = render(safety_governance(data))
        assert "3" in out

    def test_postcondition_failures_rendered(self):
        safety = SafetyStats(
            warn_hits=0, block_hits=0, approval_hits=0,
            postcondition_failures=5,
            current_policy_version=None,
            stale_memory_count=0,
        )
        data = DashboardData(
            workflow_name="wf",
            total_runs=5,
            traces=[],
            stage_stats={},
            improvement_cycles=[],
            safety_stats=safety,
            ihr_trend=[0.80],
            last_run_id="r1",
        )
        out = render(safety_governance(data))
        assert "5" in out

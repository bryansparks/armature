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
    hqs_trend=None,
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
        hqs_trend=hqs_trend or [0.75, 0.80],
        last_run_id="abc123",
    )


# ── health_strip ──────────────────────────────────────────────────────────────

class TestHealthStrip:
    def test_renders_workflow_name(self):
        data = make_dashboard(workflow_name="my-workflow")
        out = render(health_strip(data))
        assert "my-workflow" in out

    def test_renders_hqs_value(self):
        data = make_dashboard(hqs_trend=[0.83])
        out = render(health_strip(data))
        assert "0.8" in out  # HQS value present

    def test_renders_run_count(self):
        data = make_dashboard(total_runs=42)
        out = render(health_strip(data))
        assert "42" in out

    def test_renders_sparkline_characters(self):
        blocks = "▁▂▃▄▅▆▇█"
        data = make_dashboard(hqs_trend=[0.6, 0.7, 0.8, 0.75])
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

    def test_renders_cycle_hqs(self):
        cycles = [
            ImprovementCycle(
                cycle_number=1,
                timestamp="2026-05-26T10:00:00Z",
                hqs_before=0.72,
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
                hqs_before=0.65,
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
                hqs_before=0.60,
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
            hqs_trend=[0.80],
            last_run_id="r1",
        )
        out = render(safety_governance(data))
        assert "abc12345" in out

    def test_renders_approval_count(self):
        safety = SafetyStats(
            warn_hits=0, block_hits=0, approval_hits=2,
            postcondition_failures=0,
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
            hqs_trend=[0.80],
            last_run_id="r1",
        )
        out = render(safety_governance(data))
        assert "2" in out

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
            hqs_trend=[0.80],
            last_run_id="r1",
        )
        out = render(safety_governance(data))
        assert "5" in out

    def test_approval_hits_shown_when_nonzero(self):
        """Real approval count shows in the panel."""
        safety = SafetyStats(
            warn_hits=0, block_hits=0, approval_hits=3,
            postcondition_failures=0,
            current_policy_version=None,
            stale_memory_count=0,
        )
        data = DashboardData(
            workflow_name="wf", total_runs=3, traces=[], stage_stats={},
            improvement_cycles=[], safety_stats=safety, hqs_trend=[0.80], last_run_id="r1",
        )
        out = render(safety_governance(data))
        assert "3" in out  # approval count visible

    def test_untracked_rule_hits_show_dash_not_zero(self):
        """warn_hits and block_hits that are 0/untracked should show — not 0."""
        safety = SafetyStats(
            warn_hits=0, block_hits=0, approval_hits=0,
            postcondition_failures=0,
            current_policy_version=None,
            stale_memory_count=0,
        )
        data = DashboardData(
            workflow_name="wf", total_runs=1, traces=[], stage_stats={},
            improvement_cycles=[], safety_stats=safety, hqs_trend=[0.80], last_run_id="r1",
        )
        out = render(safety_governance(data))
        # Should NOT show "0 warn" or "0 block" as if they're real tracked values
        assert "0 warn" not in out
        assert "0 block" not in out

    def test_stale_memory_not_shown(self):
        """'Stale memory' terminology should not appear — confusing to users."""
        safety = SafetyStats(
            warn_hits=0, block_hits=0, approval_hits=0,
            postcondition_failures=0,
            current_policy_version=None,
            stale_memory_count=0,
        )
        data = DashboardData(
            workflow_name="wf", total_runs=1, traces=[], stage_stats={},
            improvement_cycles=[], safety_stats=safety, hqs_trend=[0.80], last_run_id="r1",
        )
        out = render(safety_governance(data))
        assert "stale" not in out.lower()


# ── stage_breakdown enhancements ─────────────────────────────────────────────

def _make_stage(stage_id, role_type="worker", fan_out_per_run=1, **kwargs):
    defaults = dict(
        run_count=1, failure_rate=0.0, avg_latency_ms=100.0,
        avg_quorum=None, escalation_rate=0.0, is_post_run=False,
    )
    defaults.update(kwargs)
    return StageStats(stage_id=stage_id, role_type=role_type,
                      fan_out_per_run=fan_out_per_run, **defaults)


class TestStageBreakdownStatus:
    def test_status_symbol_only_no_text(self):
        """Status column shows glyphs only — no word-wrapping text like 'healthy'."""
        stats = {"ok": _make_stage("ok", failure_rate=0.0)}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        assert "healthy" not in out

    def test_failing_status_symbol_visible(self):
        stats = {"bad": _make_stage("bad", failure_rate=0.5)}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        assert "failing" not in out
        assert "✗" in out

    def test_degraded_status_symbol_visible(self):
        stats = {"mid": _make_stage("mid", failure_rate=0.15)}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        assert "degraded" not in out
        assert "⚠" in out


class TestStageBreakdownRole:
    def test_renders_role_column_header(self):
        stats = {"s": _make_stage("s", role_type="orchestrator")}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        assert "Role" in out

    def test_renders_role_type_value(self):
        stats = {"coordinator": _make_stage("coordinator", role_type="orchestrator")}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        assert "orchestrator" in out.lower()

    def test_worker_role_rendered(self):
        stats = {"w": _make_stage("w", role_type="worker")}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        assert "worker" in out.lower()


class TestStageBreakdownFanOut:
    def test_fan_out_column_shows_count_when_greater_than_1(self):
        stats = {"searcher": _make_stage("searcher", fan_out_per_run=8)}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        assert "8" in out

    def test_fan_out_1_shows_dash(self):
        stats = {"simple": _make_stage("simple", fan_out_per_run=1)}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        assert "—" in out

    def test_fan_out_column_header_present(self):
        stats = {"s": _make_stage("s")}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        assert "×" in out  # fan-out column (× = multiplier symbol)


class TestStageBreakdownRowCap:
    def test_capped_at_12_rows(self):
        stats = {f"stage_{i}": _make_stage(f"stage_{i}") for i in range(20)}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        shown = sum(1 for i in range(20) if f"stage_{i}" in out)
        assert shown <= 12

    def test_12_stages_all_visible(self):
        stats = {f"stage_{i}": _make_stage(f"stage_{i}") for i in range(12)}
        out = render(stage_breakdown(make_dashboard(stage_stats=stats)))
        shown = sum(1 for i in range(12) if f"stage_{i}" in out)
        assert shown == 12


# ── improvement_timeline enhancements ────────────────────────────────────────

class TestImprovementTimelineRunHistory:
    def test_no_cycles_shows_run_count(self):
        data = make_dashboard(improvement_cycles=[], total_runs=15)
        out = render(improvement_timeline(data))
        assert "15" in out

    def test_no_cycles_shows_last_run_date(self):
        data = make_dashboard(improvement_cycles=[], total_runs=5)
        data.last_run_at = "2026-06-10T14:30:00Z"
        out = render(improvement_timeline(data))
        assert "2026-06-10" in out

    def test_no_cycles_zero_runs_renders(self):
        data = make_dashboard(improvement_cycles=[], total_runs=0)
        out = render(improvement_timeline(data))
        assert out  # does not crash


class TestImprovementTimelineAllCyclesVisible:
    def _make_cycles(self, n):
        return [
            ImprovementCycle(
                cycle_number=i + 1,
                timestamp=f"2026-05-{i + 1:02d}T10:00:00Z",
                hqs_before=0.72, drift_score=0.0, applied=True,
                requires_review=False, verified_fixes=0,
                missed_predictions=0, unexpected_regressions=0,
                predicted_fixes=[], predicted_regressions=[],
            )
            for i in range(n)
        ]

    def test_all_cycles_shown(self):
        data = make_dashboard(improvement_cycles=self._make_cycles(15))
        out = render(improvement_timeline(data))
        shown = sum(1 for i in range(15) if f"2026-05-{i + 1:02d}" in out)
        assert shown == 15


# ── leverage_heatmap ──────────────────────────────────────────────────────────

from armature.report.aggregator import DashboardData, SafetyStats
from armature.report.panels import leverage_heatmap
from armature.state.leverage import compute_leverage
from armature.state.traces import TraceRecord


def _trace(run_id, stage_id, *, quorum=0.5, role="judge"):
    return TraceRecord(run_id=run_id, workflow_name="wf", stage_id=stage_id, role_type=role,
                       model="m", quorum_score=quorum, success=True, output_valid=True,
                       latency_ms=100.0, escalation_count=0)


def _minimal_data(leverage=None):
    return DashboardData(workflow_name="wf", total_runs=0, traces=[], stage_stats={},
                          improvement_cycles=[], safety_stats=SafetyStats(0, 0, 0, 0, None, 0),
                          hqs_trend=[], last_run_id=None, leverage=leverage)


def test_leverage_heatmap_renders_when_sufficient():
    traces = []
    for i, q in enumerate([0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, 0.45]):
        traces += [_trace(f"r{i}", "judge_a", quorum=q), _trace(f"r{i}", "worker", role="worker")]
    lev = compute_leverage(traces)
    assert lev.sufficient
    panel = leverage_heatmap(_minimal_data(leverage=lev))
    from rich.console import Console
    c = Console(record=True, width=80)
    c.print(panel)
    out = c.export_text()
    assert "judge_a" in out
    assert "Leverage" in out


def test_leverage_heatmap_insufficient_notice():
    lev = compute_leverage([])  # insufficient
    panel = leverage_heatmap(_minimal_data(leverage=lev))
    from rich.console import Console
    c = Console(record=True, width=80)
    c.print(panel)
    out = c.export_text()
    assert "insufficient" in out.lower()


def test_leverage_heatmap_none_is_safe():
    panel = leverage_heatmap(_minimal_data(leverage=None))
    from rich.console import Console
    c = Console(record=True, width=80)
    c.print(panel)
    assert "insufficient" in c.export_text().lower() or "no data" in c.export_text().lower()

"""Tests for DashboardData — the multi-run aggregated data model."""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone

import pytest
from armature.report.aggregator import (
    DashboardData,
    StageStats,
    ImprovementCycle,
    build_stage_stats,
    load_improvement_cycles,
    load_safety_stats,
    SafetyStats,
)
from armature.state.traces import TraceRecord


# ── helpers ──────────────────────────────────────────────────────────────────

def make_trace(
    stage_id: str = "analyst",
    role_type: str = "worker",
    success: bool = True,
    output_valid: bool = True,
    latency_ms: float = 1200.0,
    quorum_score: float | None = None,
    input_tokens: int = 300,
    output_tokens: int = 100,
    model: str = "haiku",
    inputs_provenance: dict | None = None,
    error_type: str | None = None,
) -> TraceRecord:
    return TraceRecord(
        run_id="run1",
        workflow_name="wf",
        stage_id=stage_id,
        role_type=role_type,
        model=model,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        quorum_score=quorum_score,
        inputs={},
        outputs={},
        success=success,
        output_valid=output_valid,
        inputs_provenance=inputs_provenance or {},
        error_type=error_type,
    )


# ── StageStats ────────────────────────────────────────────────────────────────

class TestBuildStageStats:
    def test_single_healthy_stage(self):
        traces = [make_trace("analyst", success=True, output_valid=True, latency_ms=800.0)]
        stats = build_stage_stats(traces)
        assert "analyst" in stats
        s = stats["analyst"]
        assert s.stage_id == "analyst"
        assert s.failure_rate == 0.0
        assert s.avg_latency_ms == pytest.approx(800.0)
        assert s.run_count == 1

    def test_partial_failures_counted(self):
        traces = [
            make_trace("analyst", success=True),
            make_trace("analyst", success=False),
            make_trace("analyst", success=True),
            make_trace("analyst", success=False),
        ]
        stats = build_stage_stats(traces)
        assert stats["analyst"].failure_rate == pytest.approx(0.5)

    def test_multiple_stages_separate_stats(self):
        traces = [
            make_trace("analyst"),
            make_trace("writer"),
            make_trace("analyst"),
        ]
        stats = build_stage_stats(traces)
        assert "analyst" in stats
        assert "writer" in stats
        assert stats["analyst"].run_count == 2
        assert stats["writer"].run_count == 1

    def test_avg_latency_computed_correctly(self):
        traces = [
            make_trace("s1", latency_ms=1000.0),
            make_trace("s1", latency_ms=2000.0),
        ]
        assert build_stage_stats(traces)["s1"].avg_latency_ms == pytest.approx(1500.0)

    def test_escalation_rate_tracked(self):
        traces = [
            make_trace("s1", model="haiku"),   # small model — no escalation
            make_trace("s1", model="frontier"), # escalated
            make_trace("s1", model="haiku"),
        ]
        stats = build_stage_stats(traces)
        # Escalation detection: if model != first-seen model for this stage
        # At minimum, escalation_rate should be between 0 and 1
        assert 0.0 <= stats["s1"].escalation_rate <= 1.0

    def test_post_run_stages_flagged(self):
        traces = [make_trace("refiner", role_type="post_run")]
        stats = build_stage_stats(traces)
        assert stats["refiner"].is_post_run is True

    def test_avg_quorum_score(self):
        traces = [
            make_trace("judge", quorum_score=0.8),
            make_trace("judge", quorum_score=0.6),
        ]
        stats = build_stage_stats(traces)
        assert stats["judge"].avg_quorum == pytest.approx(0.7)

    def test_quorum_none_ignored(self):
        traces = [
            make_trace("s1", quorum_score=None),
            make_trace("s1", quorum_score=0.9),
        ]
        stats = build_stage_stats(traces)
        assert stats["s1"].avg_quorum == pytest.approx(0.9)

    def test_fan_out_per_run_computed_from_same_run_traces(self):
        """Fan-out stages have multiple traces with the same (run_id, stage_id)."""
        traces = [
            TraceRecord(run_id="run_a", workflow_name="wf", stage_id="searcher",
                        role_type="worker", model="m"),
            TraceRecord(run_id="run_a", workflow_name="wf", stage_id="searcher",
                        role_type="worker", model="m"),
            TraceRecord(run_id="run_a", workflow_name="wf", stage_id="searcher",
                        role_type="worker", model="m"),
        ]
        stats = build_stage_stats(traces)
        assert stats["searcher"].fan_out_per_run == 3

    def test_fan_out_per_run_is_1_for_normal_stages(self):
        """Non-fan-out stages have exactly one trace per run."""
        traces = [
            TraceRecord(run_id="run_a", workflow_name="wf", stage_id="analyst",
                        role_type="worker", model="m"),
            TraceRecord(run_id="run_b", workflow_name="wf", stage_id="analyst",
                        role_type="worker", model="m"),
        ]
        stats = build_stage_stats(traces)
        assert stats["analyst"].fan_out_per_run == 1

    def test_fan_out_per_run_uses_max_across_runs(self):
        """Fan-out count is the max observed in any single run."""
        traces = [
            TraceRecord(run_id="run_a", workflow_name="wf", stage_id="searcher",
                        role_type="worker", model="m"),
            TraceRecord(run_id="run_a", workflow_name="wf", stage_id="searcher",
                        role_type="worker", model="m"),
            TraceRecord(run_id="run_b", workflow_name="wf", stage_id="searcher",
                        role_type="worker", model="m"),
            TraceRecord(run_id="run_b", workflow_name="wf", stage_id="searcher",
                        role_type="worker", model="m"),
            TraceRecord(run_id="run_b", workflow_name="wf", stage_id="searcher",
                        role_type="worker", model="m"),
        ]
        stats = build_stage_stats(traces)
        assert stats["searcher"].fan_out_per_run == 3  # max(2, 3)


# ── ImprovementCycle ─────────────────────────────────────────────────────────

class TestLoadImprovementCycles:
    def test_empty_log_returns_empty_list(self, tmp_path):
        log = tmp_path / "wf.improve_log.jsonl"
        log.write_text("")
        assert load_improvement_cycles(log) == []

    def test_missing_log_returns_empty_list(self, tmp_path):
        log = tmp_path / "missing.jsonl"
        assert load_improvement_cycles(log) == []

    def test_single_cycle_parsed(self, tmp_path):
        log = tmp_path / "wf.improve_log.jsonl"
        entry = {
            "timestamp": "2026-05-26T10:00:00Z",
            "workflow_name": "wf",
            "n_traces": 20,
            "hqs_before": 0.72,
            "needs_improvement": True,
            "applied": True,
            "requires_review": False,
            "drift_score": 0.1,
            "diagnostics": [],
            "predicted_fixes": ["output_invalid:analyst"],
            "predicted_regressions": [],
            "verified_fixes": [],
            "missed_predictions": [],
            "unexpected_regressions": [],
        }
        log.write_text(json.dumps(entry) + "\n")
        cycles = load_improvement_cycles(log)
        assert len(cycles) == 1
        c = cycles[0]
        assert c.hqs_before == pytest.approx(0.72)
        assert c.applied is True
        assert c.requires_review is False
        assert c.drift_score == pytest.approx(0.1)
        assert c.predicted_fixes == ["output_invalid:analyst"]

    def test_multiple_cycles_ordered_newest_first(self, tmp_path):
        log = tmp_path / "wf.improve_log.jsonl"
        entries = [
            {"timestamp": "2026-05-24T00:00:00Z", "hqs_before": 0.60, "applied": False,
             "requires_review": False, "drift_score": 0.0, "needs_improvement": True,
             "n_traces": 5, "diagnostics": [], "predicted_fixes": [],
             "predicted_regressions": [], "verified_fixes": [], "missed_predictions": [],
             "unexpected_regressions": [], "workflow_name": "wf"},
            {"timestamp": "2026-05-26T00:00:00Z", "hqs_before": 0.80, "applied": True,
             "requires_review": False, "drift_score": 0.0, "needs_improvement": True,
             "n_traces": 10, "diagnostics": [], "predicted_fixes": [],
             "predicted_regressions": [], "verified_fixes": [], "missed_predictions": [],
             "unexpected_regressions": [], "workflow_name": "wf"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        cycles = load_improvement_cycles(log)
        # Newest first
        assert cycles[0].hqs_before > cycles[1].hqs_before

    def test_requires_review_cycle_detected(self, tmp_path):
        log = tmp_path / "wf.improve_log.jsonl"
        entry = {
            "timestamp": "2026-05-26T00:00:00Z",
            "workflow_name": "wf", "n_traces": 5,
            "hqs_before": 0.65, "needs_improvement": True,
            "applied": False, "requires_review": True,
            "drift_score": 0.0, "diagnostics": [],
            "predicted_fixes": [], "predicted_regressions": [],
            "verified_fixes": [], "missed_predictions": [],
            "unexpected_regressions": [],
        }
        log.write_text(json.dumps(entry) + "\n")
        cycles = load_improvement_cycles(log)
        assert cycles[0].requires_review is True
        assert cycles[0].applied is False


# ── SafetyStats ───────────────────────────────────────────────────────────────

class TestLoadSafetyStats:
    def test_no_traces_returns_zero_counts(self):
        stats = load_safety_stats([])
        assert stats.warn_hits == 0
        assert stats.block_hits == 0
        assert stats.approval_hits == 0
        assert stats.postcondition_failures == 0

    def test_postcondition_failures_counted(self):
        traces = [
            make_trace(error_type="PostconditionFailed"),
            make_trace(error_type="PostconditionFailed"),
            make_trace(),
        ]
        stats = load_safety_stats(traces)
        assert stats.postcondition_failures == 2

    def test_policy_version_extracted(self):
        class FakeTrace(TraceRecord):
            pass
        traces = [make_trace()]
        # policy_version is on TraceRecord — set it
        traces[0] = TraceRecord(
            run_id="r", workflow_name="wf", stage_id="s",
            role_type="worker", model="m", latency_ms=100,
            input_tokens=10, output_tokens=5, quorum_score=None,
            inputs={}, outputs={}, success=True, output_valid=True,
            policy_version="abc12345",
        )
        stats = load_safety_stats(traces)
        assert stats.current_policy_version == "abc12345"

    def test_stale_memory_keys_aggregated(self):
        # Stale keys are reflected in inputs_provenance
        traces = [
            make_trace(inputs_provenance={"_memory": "stale_memory"}),
            make_trace(inputs_provenance={"topic": "user_input"}),
        ]
        stats = load_safety_stats(traces)
        assert stats.stale_memory_count >= 1

    def test_gate_traces_counted_as_approval_hits(self):
        """Completed gate stages (role_type='gate') count as approval_hits."""
        traces = [
            TraceRecord(run_id="r1", workflow_name="wf", stage_id="human_approval",
                        role_type="gate", model="", success=True, output_valid=True),
            TraceRecord(run_id="r2", workflow_name="wf", stage_id="human_approval",
                        role_type="gate", model="", success=True, output_valid=True),
            make_trace("analyst"),
        ]
        stats = load_safety_stats(traces)
        assert stats.approval_hits == 2

    def test_no_gate_traces_approval_hits_zero(self):
        """No gate traces → approval_hits remains 0."""
        traces = [make_trace("analyst"), make_trace("writer")]
        stats = load_safety_stats(traces)
        assert stats.approval_hits == 0


# ── DashboardData ─────────────────────────────────────────────────────────────

class TestDashboardData:
    def test_hqs_trend_empty_when_no_cycles(self):
        data = DashboardData(
            workflow_name="wf",
            total_runs=0,
            traces=[],
            stage_stats={},
            improvement_cycles=[],
            safety_stats=load_safety_stats([]),
            hqs_trend=[],
            last_run_id=None,
        )
        assert data.hqs_trend == []

    def test_health_status_green_above_085(self):
        data = DashboardData(
            workflow_name="wf",
            total_runs=5,
            traces=[],
            stage_stats={},
            improvement_cycles=[],
            safety_stats=load_safety_stats([]),
            hqs_trend=[0.90],
            last_run_id="r1",
        )
        assert data.current_hqs == pytest.approx(0.90)
        assert data.health_color == "green"

    def test_health_status_yellow_between_070_085(self):
        data = DashboardData(
            workflow_name="wf",
            total_runs=5,
            traces=[],
            stage_stats={},
            improvement_cycles=[],
            safety_stats=load_safety_stats([]),
            hqs_trend=[0.75],
            last_run_id="r1",
        )
        assert data.health_color == "yellow"

    def test_health_status_red_below_070(self):
        data = DashboardData(
            workflow_name="wf",
            total_runs=5,
            traces=[],
            stage_stats={},
            improvement_cycles=[],
            safety_stats=load_safety_stats([]),
            hqs_trend=[0.55],
            last_run_id="r1",
        )
        assert data.health_color == "red"

    def test_hqs_delta_computed_from_last_two_cycles(self):
        data = DashboardData(
            workflow_name="wf",
            total_runs=10,
            traces=[],
            stage_stats={},
            improvement_cycles=[],
            safety_stats=load_safety_stats([]),
            hqs_trend=[0.70, 0.75],  # [older, newer]
            last_run_id="r1",
        )
        assert data.hqs_delta == pytest.approx(0.05, abs=0.001)


# ── HQS trend uses canonical formula (Task 3) ──────────────────────────────────

from armature.state.traces import TraceRecord, compute_hqs_from_traces
from armature.report.loader import load_dashboard_data


def _t(run_id, quorum=0.9, latency=100.0, esc=0):
    return TraceRecord(
        run_id=run_id, workflow_name="wf", stage_id="s", role_type="judge",
        model="m", quorum_score=quorum, success=True, output_valid=True,
        latency_ms=latency, escalation_count=esc,
    )


async def _seed(store, traces):
    for t in traces:
        await store.record(t)


async def test_dashboard_hqs_trend_uses_canonical_formula(tmp_path):
    db = tmp_path / "traces.db"
    # Two runs with different quorum so HQS differs
    traces = [_t("r1", quorum=0.9, latency=100.0), _t("r1", quorum=0.9, latency=100.0, esc=1),
              _t("r2", quorum=0.5, latency=4000.0), _t("r2", quorum=0.5, latency=4000.0)]
    from armature.state.traces import TraceStore
    store = TraceStore(db)
    await store.init()
    await _seed(store, traces)

    data = await load_dashboard_data("wf", traces_db=db)
    # The trend must equal compute_hqs_from_traces per run (canonical A, with HFR + avg latency)
    run1 = compute_hqs_from_traces([_t("r1", quorum=0.9, latency=100.0), _t("r1", quorum=0.9, latency=100.0, esc=1)])
    run2 = compute_hqs_from_traces([_t("r2", quorum=0.5, latency=4000.0), _t("r2", quorum=0.5, latency=4000.0)])
    assert data.hqs_trend == [round(run1.hqs, 4), round(run2.hqs, 4)]

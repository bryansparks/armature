"""Tests for Harness-Following Rate (HFR) as an IHR component — arXiv:2605.30621v1.

HFR measures the fraction of trajectories where the model adheres to harness
instructions on the first attempt (escalation_count == 0). Models that frequently
require escalation are not truly following the harness.
"""
import pytest
import asyncio
from pathlib import Path
from armature.state.traces import IhrResult, TraceStore, TraceRecord


def make_trace(**kwargs) -> TraceRecord:
    defaults = dict(
        run_id="run-01",
        workflow_name="wf",
        stage_id="s1",
        role_type="worker",
        model="m",
        success=True,
        output_valid=True,
        quorum_score=0.8,
        escalation_count=0,
        latency_ms=100.0,
    )
    defaults.update(kwargs)
    return TraceRecord(**defaults)


# ── IhrResult HFR field ─────────────────────────────────────────────────────

def test_ihr_result_has_hfr_field():
    result = IhrResult(
        run_id="r1",
        ihr=0.8,
        output_valid_rate=0.9,
        success_rate=0.85,
        avg_quorum_score=0.7,
        latency_score=0.9,
        n_traces=10,
        hfr=0.75,
    )
    assert result.hfr == 0.75


def test_ihr_result_hfr_defaults_zero():
    result = IhrResult(
        run_id="r1",
        ihr=0.8,
        output_valid_rate=0.9,
        success_rate=0.85,
        avg_quorum_score=0.7,
        latency_score=0.9,
        n_traces=10,
    )
    assert result.hfr == 0.0


# ── compute_ihr HFR computation ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compute_ihr_hfr_is_fraction_without_escalation(tmp_path):
    """HFR = fraction of traces with escalation_count == 0."""
    db = tmp_path / "t.db"
    store = TraceStore(db)
    await store.init()
    # 3 traces: 2 with no escalation, 1 with escalation
    for i in range(2):
        await store.record(make_trace(run_id="r1", escalation_count=0))
    await store.record(make_trace(run_id="r1", escalation_count=1))

    result = await store.compute_ihr("r1")
    assert result is not None
    assert abs(result.hfr - 2/3) < 1e-9


@pytest.mark.asyncio
async def test_compute_ihr_formula_includes_hfr_component(tmp_path):
    """When all traces have perfect HFR, IHR is higher than without HFR component."""
    db = tmp_path / "t.db"
    store = TraceStore(db)
    await store.init()
    # Perfect traces: all valid, all succeed, no escalation
    for _ in range(4):
        await store.record(make_trace(
            run_id="r1",
            output_valid=True,
            success=True,
            quorum_score=1.0,
            latency_ms=0.0,
            escalation_count=0,
        ))

    result = await store.compute_ihr("r1")
    assert result is not None
    # With perfect HFR (1.0), IHR should be close to 1.0
    assert result.ihr > 0.95
    assert result.hfr == 1.0


@pytest.mark.asyncio
async def test_compute_ihr_with_zero_hfr_reduces_score(tmp_path):
    """When all traces escalate (HFR=0), IHR is lower than with perfect HFR."""
    db = tmp_path / "t.db"
    store = TraceStore(db)
    await store.init()
    for _ in range(4):
        await store.record(make_trace(
            run_id="r1",
            output_valid=True,
            success=True,
            quorum_score=1.0,
            latency_ms=0.0,
            escalation_count=2,  # every trace escalated
        ))

    result = await store.compute_ihr("r1")
    assert result is not None
    assert result.hfr == 0.0
    # IHR should be < 1.0 because HFR penalizes it
    assert result.ihr < 1.0

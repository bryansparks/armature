"""Tests for Harness-Following Rate (HFR) as an HQS component — arXiv:2605.30621v1.

HFR measures the fraction of trajectories where the model adheres to harness
instructions on the first attempt (escalation_count == 0). Models that frequently
require escalation are not truly following the harness.
"""
import pytest
import asyncio
from pathlib import Path
from armature.state.traces import HqsResult, TraceStore, TraceRecord


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


# ── HqsResult HFR field ─────────────────────────────────────────────────────

def test_hqs_result_has_hfr_field():
    result = HqsResult(
        run_id="r1",
        hqs=0.8,
        output_valid_rate=0.9,
        success_rate=0.85,
        avg_quorum_score=0.7,
        latency_score=0.9,
        n_traces=10,
        hfr=0.75,
    )
    assert result.hfr == 0.75


def test_hqs_result_hfr_defaults_zero():
    result = HqsResult(
        run_id="r1",
        hqs=0.8,
        output_valid_rate=0.9,
        success_rate=0.85,
        avg_quorum_score=0.7,
        latency_score=0.9,
        n_traces=10,
    )
    assert result.hfr == 0.0


# ── compute_hqs HFR computation ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compute_hqs_hfr_is_fraction_without_escalation(tmp_path):
    """HFR = fraction of traces with escalation_count == 0."""
    db = tmp_path / "t.db"
    store = TraceStore(db)
    await store.init()
    # 3 traces: 2 with no escalation, 1 with escalation
    for i in range(2):
        await store.record(make_trace(run_id="r1", escalation_count=0))
    await store.record(make_trace(run_id="r1", escalation_count=1))

    result = await store.compute_hqs("r1")
    assert result is not None
    assert abs(result.hfr - 2/3) < 1e-9


@pytest.mark.asyncio
async def test_compute_hqs_formula_includes_hfr_component(tmp_path):
    """When all traces have perfect HFR, HQS is higher than without HFR component."""
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

    result = await store.compute_hqs("r1")
    assert result is not None
    # With perfect HFR (1.0), HQS should be close to 1.0
    assert result.hqs > 0.95
    assert result.hfr == 1.0


@pytest.mark.asyncio
async def test_compute_hqs_with_zero_hfr_reduces_score(tmp_path):
    """When all traces escalate (HFR=0), HQS is lower than with perfect HFR."""
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

    result = await store.compute_hqs("r1")
    assert result is not None
    assert result.hfr == 0.0
    # HQS should be < 1.0 because HFR penalizes it
    assert result.hqs < 1.0

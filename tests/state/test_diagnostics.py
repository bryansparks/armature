"""Tests for DiagnosticAnalyzer — failure signature detection from trace records."""
import pytest
from armature.state.diagnostics import DiagnosticAnalyzer, DiagnosticCode
from armature.state.traces import TraceRecord


def make_trace(**kwargs) -> TraceRecord:
    defaults = dict(
        run_id="run-01",
        workflow_name="wf",
        stage_id="s1",
        role_type="worker",
        model="m",
        success=True,
        output_valid=True,
        quorum_score=None,
        escalation_count=0,
        error_type=None,
    )
    defaults.update(kwargs)
    return TraceRecord(**defaults)


# ── clean runs ────────────────────────────────────────────────────────────────

def test_no_diagnostics_on_clean_run():
    traces = [
        make_trace(stage_id="s1", role_type="worker"),
        make_trace(stage_id="s2", role_type="judge", quorum_score=0.9),
    ]
    result = DiagnosticAnalyzer(traces).analyze()
    assert result == []


# ── stage_failed ──────────────────────────────────────────────────────────────

def test_stage_failed_on_success_false():
    traces = [make_trace(stage_id="ingest", success=False, error_type="RuntimeError")]
    result = DiagnosticAnalyzer(traces).analyze()
    assert len(result) == 1
    assert result[0].code == DiagnosticCode.STAGE_FAILED
    assert result[0].stage_id == "ingest"


def test_stage_failed_includes_error_type():
    traces = [make_trace(success=False, error_type="TimeoutError")]
    result = DiagnosticAnalyzer(traces).analyze()
    assert "TimeoutError" in result[0].details


# ── output_invalid ─────────────────────────────────────────────────────────────

def test_output_invalid_on_output_valid_false():
    traces = [make_trace(stage_id="brief", output_valid=False)]
    result = DiagnosticAnalyzer(traces).analyze()
    assert any(d.code == DiagnosticCode.OUTPUT_INVALID and d.stage_id == "brief" for d in result)


def test_both_failed_and_invalid_emitted_separately():
    traces = [make_trace(success=False, output_valid=False)]
    codes = {d.code for d in DiagnosticAnalyzer(traces).analyze()}
    assert DiagnosticCode.STAGE_FAILED in codes
    assert DiagnosticCode.OUTPUT_INVALID in codes


# ── low_confidence ─────────────────────────────────────────────────────────────

def test_low_confidence_on_judge_below_threshold():
    traces = [make_trace(stage_id="judge", role_type="judge", quorum_score=0.2)]
    result = DiagnosticAnalyzer(traces).analyze()
    assert any(d.code == DiagnosticCode.LOW_CONFIDENCE for d in result)


def test_no_low_confidence_on_worker_regardless_of_score():
    traces = [make_trace(stage_id="w", role_type="worker", quorum_score=0.1)]
    result = DiagnosticAnalyzer(traces).analyze()
    assert not any(d.code == DiagnosticCode.LOW_CONFIDENCE for d in result)


def test_no_low_confidence_when_judge_score_adequate():
    traces = [make_trace(role_type="judge", quorum_score=0.7)]
    result = DiagnosticAnalyzer(traces).analyze()
    assert not any(d.code == DiagnosticCode.LOW_CONFIDENCE for d in result)


def test_low_confidence_threshold_is_exclusive_at_boundary():
    """Score exactly at threshold (0.3) should NOT trigger low_confidence."""
    traces = [make_trace(role_type="judge", quorum_score=0.30)]
    result = DiagnosticAnalyzer(traces).analyze()
    assert not any(d.code == DiagnosticCode.LOW_CONFIDENCE for d in result)


# ── high_escalation ────────────────────────────────────────────────────────────

def test_high_escalation_on_repeated_retries():
    traces = [make_trace(stage_id="brittle", escalation_count=2)]
    result = DiagnosticAnalyzer(traces).analyze()
    assert any(d.code == DiagnosticCode.HIGH_ESCALATION and d.stage_id == "brittle" for d in result)


def test_no_high_escalation_below_threshold():
    traces = [make_trace(escalation_count=1)]
    result = DiagnosticAnalyzer(traces).analyze()
    assert not any(d.code == DiagnosticCode.HIGH_ESCALATION for d in result)


# ── multiple stages ────────────────────────────────────────────────────────────

def test_diagnostics_across_multiple_stages():
    traces = [
        make_trace(stage_id="a", success=False),
        make_trace(stage_id="b", role_type="judge", quorum_score=0.1),
        make_trace(stage_id="c"),  # clean
    ]
    result = DiagnosticAnalyzer(traces).analyze()
    stage_ids = {d.stage_id for d in result}
    assert "a" in stage_ids
    assert "b" in stage_ids
    assert "c" not in stage_ids


def test_empty_traces_returns_empty():
    assert DiagnosticAnalyzer([]).analyze() == []


# ── Phase D: Post-condition Verification (RED) ────────────────────────────────

def test_postcondition_failed_diagnostic_code_exists():
    assert hasattr(DiagnosticCode, "POSTCONDITION_FAILED")
    assert DiagnosticCode.POSTCONDITION_FAILED.value == "postcondition_failed"


def test_postcondition_failed_trace_produces_diagnostic():
    traces = [make_trace(success=False, output_valid=False, error_type="PostconditionFailed")]
    results = DiagnosticAnalyzer(traces).analyze()
    codes = [r.code for r in results]
    assert DiagnosticCode.POSTCONDITION_FAILED in codes


def test_postcondition_failed_diagnostic_has_correct_stage():
    traces = [make_trace(stage_id="uploader", success=False, error_type="PostconditionFailed")]
    results = DiagnosticAnalyzer(traces).analyze()
    pf_results = [r for r in results if r.code == DiagnosticCode.POSTCONDITION_FAILED]
    assert len(pf_results) == 1
    assert pf_results[0].stage_id == "uploader"


def test_non_postcondition_error_does_not_produce_postcondition_diagnostic():
    traces = [make_trace(success=False, error_type="TimeoutError")]
    results = DiagnosticAnalyzer(traces).analyze()
    codes = [r.code for r in results]
    assert DiagnosticCode.POSTCONDITION_FAILED not in codes

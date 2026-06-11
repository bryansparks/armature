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


# ── CausalAttribution ─────────────────────────────────────────────────────────

from armature.state.diagnostics import CausalAttribution, TerminalCause, CausalStatus, FailureMechanism


def test_causal_attribution_on_stage_failed_timeout():
    traces = [make_trace(stage_id="fetcher", success=False, error_type="TimeoutError")]
    result = DiagnosticAnalyzer(traces).analyze()
    sf = next(r for r in result if r.code == DiagnosticCode.STAGE_FAILED)
    assert sf.causal_attribution is not None
    assert sf.causal_attribution.terminal_cause == TerminalCause.EXECUTION_ERROR
    assert sf.causal_attribution.causal_status == CausalStatus.SPEC_PROBLEM
    assert sf.causal_attribution.mechanism == FailureMechanism.TIMEOUT


def test_causal_attribution_on_stage_failed_runtime():
    traces = [make_trace(stage_id="writer", success=False, error_type="ValueError")]
    result = DiagnosticAnalyzer(traces).analyze()
    sf = next(r for r in result if r.code == DiagnosticCode.STAGE_FAILED)
    assert sf.causal_attribution.terminal_cause == TerminalCause.EXECUTION_ERROR
    assert sf.causal_attribution.causal_status == CausalStatus.MODEL_PROBLEM
    assert sf.causal_attribution.mechanism == FailureMechanism.RUNTIME_ERROR


def test_causal_attribution_on_output_invalid_low_escalation():
    """Schema validation failed on first attempt → schema too strict."""
    traces = [make_trace(stage_id="analyst", output_valid=False, escalation_count=0)]
    result = DiagnosticAnalyzer(traces).analyze()
    oi = next(r for r in result if r.code == DiagnosticCode.OUTPUT_INVALID)
    assert oi.causal_attribution.terminal_cause == TerminalCause.SCHEMA_VALIDATION
    assert oi.causal_attribution.causal_status == CausalStatus.SPEC_PROBLEM
    assert oi.causal_attribution.mechanism == FailureMechanism.SCHEMA_TOO_STRICT


def test_causal_attribution_on_output_invalid_high_escalation():
    """Schema validation failed after escalation → model underpowered."""
    traces = [make_trace(stage_id="analyst", output_valid=False, escalation_count=2)]
    result = DiagnosticAnalyzer(traces).analyze()
    oi = next(r for r in result if r.code == DiagnosticCode.OUTPUT_INVALID)
    assert oi.causal_attribution.causal_status == CausalStatus.MODEL_PROBLEM
    assert oi.causal_attribution.mechanism == FailureMechanism.MODEL_UNDERPOWERED


def test_causal_attribution_on_low_confidence():
    traces = [make_trace(stage_id="judge", role_type="judge", quorum_score=0.1)]
    result = DiagnosticAnalyzer(traces).analyze()
    lc = next(r for r in result if r.code == DiagnosticCode.LOW_CONFIDENCE)
    assert lc.causal_attribution.terminal_cause == TerminalCause.LOW_CONFIDENCE
    assert lc.causal_attribution.causal_status == CausalStatus.MODEL_PROBLEM
    assert lc.causal_attribution.mechanism == FailureMechanism.JUDGE_UNCERTAIN


def test_causal_attribution_on_high_escalation():
    traces = [make_trace(stage_id="parser", escalation_count=3)]
    result = DiagnosticAnalyzer(traces).analyze()
    he = next(r for r in result if r.code == DiagnosticCode.HIGH_ESCALATION)
    assert he.causal_attribution.terminal_cause == TerminalCause.SCHEMA_ESCALATION
    assert he.causal_attribution.causal_status == CausalStatus.MODEL_PROBLEM
    assert he.causal_attribution.mechanism == FailureMechanism.TIER_INSUFFICIENT


def test_causal_attribution_on_postcondition_failed():
    traces = [make_trace(stage_id="uploader", success=False, error_type="PostconditionFailed")]
    result = DiagnosticAnalyzer(traces).analyze()
    pf = next(r for r in result if r.code == DiagnosticCode.POSTCONDITION_FAILED)
    assert pf.causal_attribution.terminal_cause == TerminalCause.POSTCONDITION
    assert pf.causal_attribution.causal_status == CausalStatus.TOOL_PROBLEM
    assert pf.causal_attribution.mechanism == FailureMechanism.TOOL_VIOLATION


def test_causal_attribution_on_low_skill_activation():
    traces = [make_trace(stage_id="searcher", tools_declared=["web_search"], tools_called=[])]
    result = DiagnosticAnalyzer(traces).analyze()
    la = next(r for r in result if r.code == DiagnosticCode.LOW_SKILL_ACTIVATION)
    assert la.causal_attribution.terminal_cause == TerminalCause.PROMPT_WEAK
    assert la.causal_attribution.causal_status == CausalStatus.SPEC_PROBLEM
    assert la.causal_attribution.mechanism == FailureMechanism.PROMPT_MISSING_INSTRUCTION


def test_clean_trace_has_no_diagnostics():
    traces = [make_trace(stage_id="s1")]
    result = DiagnosticAnalyzer(traces).analyze()
    assert result == []

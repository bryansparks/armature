"""Tests for Skill-Load Rate (SLR) diagnostics — arXiv:2605.30621v1.

SLR measures the fraction of trajectories where declared tools/skills are
actually invoked. Low SLR indicates weak models failing to activate tools.
"""
import pytest
from armature.state.traces import TraceRecord
from armature.state.diagnostics import DiagnosticAnalyzer, DiagnosticCode


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


# ── TraceRecord SLR fields ──────────────────────────────────────────────────

def test_trace_record_has_tools_declared_field():
    t = make_trace(tools_declared=["search", "calc"])
    assert t.tools_declared == ["search", "calc"]


def test_trace_record_has_tools_called_field():
    t = make_trace(tools_called=["search"])
    assert t.tools_called == ["search"]


def test_trace_record_defaults_empty_tools_lists():
    t = make_trace()
    assert t.tools_declared == []
    assert t.tools_called == []


# ── DiagnosticCode low_skill_activation ────────────────────────────────────

def test_low_skill_activation_code_exists():
    assert hasattr(DiagnosticCode, "LOW_SKILL_ACTIVATION")
    assert DiagnosticCode.LOW_SKILL_ACTIVATION.value == "low_skill_activation"


def test_low_skill_activation_when_tools_declared_but_none_called():
    """Stage declares tools but model never invokes any — low SLR."""
    traces = [make_trace(tools_declared=["search", "calc"], tools_called=[])]
    results = DiagnosticAnalyzer(traces).analyze()
    codes = [r.code for r in results]
    assert DiagnosticCode.LOW_SKILL_ACTIVATION in codes


def test_no_low_skill_activation_when_no_tools_declared():
    """Stage declares no tools — SLR diagnostic does not apply."""
    traces = [make_trace(tools_declared=[], tools_called=[])]
    results = DiagnosticAnalyzer(traces).analyze()
    codes = [r.code for r in results]
    assert DiagnosticCode.LOW_SKILL_ACTIVATION not in codes


def test_no_low_skill_activation_when_tools_declared_and_called():
    """Stage declares tools and model uses them — SLR is healthy."""
    traces = [make_trace(tools_declared=["search"], tools_called=["search"])]
    results = DiagnosticAnalyzer(traces).analyze()
    codes = [r.code for r in results]
    assert DiagnosticCode.LOW_SKILL_ACTIVATION not in codes


def test_low_skill_activation_details_show_declared_tools():
    """Diagnostic details include the declared tool names for actionability."""
    traces = [make_trace(tools_declared=["fetch", "parse"], tools_called=[])]
    results = DiagnosticAnalyzer(traces).analyze()
    lsa = [r for r in results if r.code == DiagnosticCode.LOW_SKILL_ACTIVATION]
    assert len(lsa) == 1
    assert "fetch" in lsa[0].details or "parse" in lsa[0].details

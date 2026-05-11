"""Tests for armature report: ReportBuilder and CLI report command."""
from __future__ import annotations
import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from armature.cli import app
from armature.reporting import ReportBuilder, ReportData
from armature.state.traces import TraceRecord, IhrResult
from armature.state.session import SessionEvent
from armature.state.evaluator import EvaluationResult
from armature.state.knowledge import KnowledgeRecord


runner = CliRunner()


# ── helpers ───────────────────────────────────────────────────────────────────

def make_trace(
    stage_id: str = "research",
    role_type: str = "researcher",
    model: str = "haiku-4-5",
    latency_ms: float = 1200.0,
    input_tokens: int = 500,
    output_tokens: int = 800,
    quorum_score: float | None = 0.90,
    inputs: dict | None = None,
    outputs: dict | None = None,
    success: bool = True,
    output_valid: bool = True,
) -> TraceRecord:
    return TraceRecord(
        run_id="abc123",
        workflow_name="research_wf",
        stage_id=stage_id,
        role_type=role_type,
        model=model,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        quorum_score=quorum_score,
        inputs=inputs or {"topic": "climate change"},
        outputs=outputs or {"brief": "CO2 levels are rising rapidly."},
        success=success,
        output_valid=output_valid,
    )


def make_eval(stage_id: str, score: float, passed: list[str], failed: list[str]) -> EvaluationResult:
    return EvaluationResult(
        run_id="abc123",
        workflow_name="research_wf",
        stage_id=stage_id,
        score=score,
        criteria_passed=passed,
        criteria_failed=failed,
        notes="auto-evaluated",
    )


def make_knowledge(fact: str, entity: str = "domain", confidence: float = 0.88) -> KnowledgeRecord:
    return KnowledgeRecord(
        workflow_name="research_wf",
        entity=entity,
        fact=fact,
        confidence=confidence,
        source_run_id="abc123",
    )


def make_ihr(ihr: float = 0.91) -> IhrResult:
    return IhrResult(
        run_id="abc123",
        ihr=ihr,
        output_valid_rate=1.0,
        success_rate=1.0,
        avg_quorum_score=0.90,
        latency_score=0.76,
        n_traces=2,
        avg_escalation_count=0.0,
    )


def minimal_data(**overrides) -> ReportData:
    defaults = dict(
        run_id="abc123",
        workflow_name="research_wf",
        traces=[make_trace()],
        events=[],
        evaluations=[],
        knowledge=[],
        ihr=None,
    )
    defaults.update(overrides)
    return ReportData(**defaults)


# ── header ────────────────────────────────────────────────────────────────────

def test_report_header_contains_run_id():
    data = minimal_data()
    assert "abc123" in ReportBuilder(data).build()


def test_report_header_contains_workflow_name():
    data = minimal_data()
    assert "research_wf" in ReportBuilder(data).build()


def test_report_header_shows_stage_count():
    data = minimal_data(traces=[make_trace("s1"), make_trace("s2")])
    assert "2" in ReportBuilder(data).build()


# ── health / issues section ───────────────────────────────────────────────────

def test_report_issues_shown_for_failed_stage():
    data = minimal_data(traces=[make_trace(success=False, stage_id="cleanup")])
    report = ReportBuilder(data).build()
    assert "cleanup" in report
    assert "✗" in report or "fail" in report.lower()


def test_report_no_issues_section_when_all_ok():
    data = minimal_data(traces=[make_trace(success=True)])
    report = ReportBuilder(data).build()
    # "Issues" header only appears when there are problems
    assert "Issues" not in report and "ISSUE" not in report.upper()


def test_report_ihr_shown_in_health_when_present():
    data = minimal_data(ihr=make_ihr(0.91))
    assert "0.91" in ReportBuilder(data).build()


def test_report_no_ihr_when_absent():
    data = minimal_data(ihr=None)
    assert "IHR" not in ReportBuilder(data).build()


# ── stage timeline ────────────────────────────────────────────────────────────

def test_report_stage_table_contains_stage_id():
    data = minimal_data()
    assert "research" in ReportBuilder(data).build()


def test_report_stage_table_shows_latency():
    data = minimal_data(traces=[make_trace(latency_ms=1234.0)])
    assert "1234" in ReportBuilder(data).build()


def test_report_stage_table_shows_token_totals():
    data = minimal_data(traces=[make_trace(input_tokens=500, output_tokens=800)])
    # Total tokens = 1300
    assert "1300" in ReportBuilder(data).build()


def test_report_stage_table_shows_quorum_for_judge():
    data = minimal_data(traces=[make_trace(role_type="judge", quorum_score=0.90)])
    assert "0.90" in ReportBuilder(data).build()


def test_report_stage_failed_shown_in_timeline():
    data = minimal_data(traces=[make_trace(success=False)])
    report = ReportBuilder(data).build()
    assert "✗" in report


def test_report_slow_stage_flagged():
    data = minimal_data(traces=[make_trace(latency_ms=45000.0)])
    report = ReportBuilder(data).build()
    # Slow stages (>30s) should be marked
    assert "SLOW" in report or "slow" in report.lower() or "⚠" in report or "!" in report


# ── quality signals (judge/orchestrator decisions) ────────────────────────────

def test_report_quality_signals_section_exists_for_judge():
    trace = make_trace(
        stage_id="judge",
        role_type="judge",
        outputs={"accept": True, "confidence": 0.92, "notes": "All findings valid."},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "Quality" in report or "Decision" in report or "Judge" in report.lower() or "judge" in report


def test_report_quality_signals_shows_judge_confidence():
    trace = make_trace(
        stage_id="validate",
        role_type="judge",
        outputs={"accept": True, "confidence": 0.95, "notes": "Clean results."},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "0.95" in report


def test_report_quality_signals_shows_accepted_decision():
    trace = make_trace(
        stage_id="validate",
        role_type="judge",
        outputs={"accept": True, "confidence": 0.95, "notes": "All good."},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "ACCEPTED" in report or "accepted" in report.lower() or "✓" in report


def test_report_quality_signals_shows_rejected_decision():
    trace = make_trace(
        stage_id="validate",
        role_type="judge",
        outputs={"accept": False, "confidence": 0.40, "notes": "Too many false positives."},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "REJECTED" in report or "rejected" in report.lower() or "✗" in report


def test_report_quality_signals_shows_judge_notes():
    trace = make_trace(
        stage_id="validate",
        role_type="judge",
        outputs={"accept": True, "confidence": 0.95, "notes": "Strong evidence provided."},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "Strong evidence provided." in report


def test_report_quality_signals_shows_judge_feedback():
    trace = make_trace(
        stage_id="validate",
        role_type="judge",
        outputs={"accept": True, "confidence": 0.88, "feedback": "Prioritization is accurate."},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "Prioritization is accurate." in report


def test_report_quality_signals_orchestrator_included():
    trace = make_trace(
        stage_id="synthesize",
        role_type="orchestrator",
        outputs={"findings": [], "total_count": 7, "by_severity": {"critical": 3}},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    # orchestrator outputs should be visible
    assert "synthesize" in report


# ── inputs NOT shown (context accumulation is noise) ─────────────────────────

def test_report_inputs_not_shown_for_worker():
    """Accumulated context inputs are noise — never render them."""
    trace = make_trace(
        role_type="worker",
        inputs={"UNIQUE_INPUT_SENTINEL_DO_NOT_SHOW": "this should not appear"},
        outputs={"result": "some output"},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "UNIQUE_INPUT_SENTINEL_DO_NOT_SHOW" not in report


def test_report_inputs_not_shown_for_judge():
    trace = make_trace(
        role_type="judge",
        inputs={"UNIQUE_JUDGE_INPUT_NOISE": "accumulated context"},
        outputs={"accept": True, "confidence": 0.9, "notes": "Fine."},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "UNIQUE_JUDGE_INPUT_NOISE" not in report


# ── key outputs (worker stage outputs) ────────────────────────────────────────

def test_report_key_outputs_shows_worker_output_text():
    trace = make_trace(
        stage_id="generate_report",
        role_type="worker",
        outputs={"report": "## Security Summary\n\n13 findings found."},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "13 findings found." in report


def test_report_key_outputs_truncates_long_text():
    long_text = "X" * 2000
    trace = make_trace(
        stage_id="generate",
        role_type="worker",
        outputs={"report": long_text},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    # Should appear but truncated — report should not be 2000 Xs
    x_count = report.count("X")
    assert x_count < 2000
    assert x_count > 0  # some of it shown


def test_report_key_outputs_shows_finding_summary():
    trace = make_trace(
        stage_id="synthesize_findings",
        role_type="orchestrator",
        outputs={"total_count": 13, "by_severity": {"critical": 6, "high": 4, "medium": 2, "low": 1}},
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "13" in report


def test_report_script_output_shows_exit_code():
    trace = make_trace(
        stage_id="cleanup",
        role_type="script",
        model="",
        outputs={"stdout": "", "stderr": "syntax error", "exit_code": 1},
        success=False,
    )
    data = minimal_data(traces=[trace])
    report = ReportBuilder(data).build()
    assert "syntax error" in report or "exit_code" in report or "1" in report


# ── evaluations ───────────────────────────────────────────────────────────────

def test_report_evaluations_section_present_when_data():
    evals = [make_eval("judge", 0.75, ["decision is clear"], ["confidence above 0.80"])]
    data = minimal_data(evaluations=evals)
    assert "Evaluation" in ReportBuilder(data).build()


def test_report_evaluation_score_shown():
    evals = [make_eval("judge", 0.75, ["decision is clear"], ["confidence above 0.80"])]
    data = minimal_data(evaluations=evals)
    assert "0.75" in ReportBuilder(data).build()


def test_report_evaluation_passed_criteria_shown():
    evals = [make_eval("judge", 1.0, ["decision is clear"], [])]
    data = minimal_data(evaluations=evals)
    assert "decision is clear" in ReportBuilder(data).build()


def test_report_evaluation_failed_criteria_shown():
    evals = [make_eval("judge", 0.5, [], ["confidence above 0.80"])]
    data = minimal_data(evaluations=evals)
    assert "confidence above 0.80" in ReportBuilder(data).build()


def test_report_no_evaluations_section_absent():
    data = minimal_data(evaluations=[])
    assert "Evaluation Scores" not in ReportBuilder(data).build()


# ── knowledge ────────────────────────────────────────────────────────────────

def test_report_knowledge_section_present_when_data():
    facts = [make_knowledge("CO2 above 420ppm is a key threshold")]
    data = minimal_data(knowledge=facts)
    assert "Knowledge" in ReportBuilder(data).build()


def test_report_knowledge_fact_shown():
    facts = [make_knowledge("CO2 above 420ppm is a key threshold")]
    data = minimal_data(knowledge=facts)
    assert "CO2 above 420ppm is a key threshold" in ReportBuilder(data).build()


def test_report_knowledge_confidence_shown():
    facts = [make_knowledge("fact", confidence=0.93)]
    data = minimal_data(knowledge=facts)
    assert "0.93" in ReportBuilder(data).build()


def test_report_no_knowledge_section_absent():
    data = minimal_data(knowledge=[])
    assert "Knowledge Extracted" not in ReportBuilder(data).build()


# ── CLI ────────────────────────────────────────────────────────────────────────

def test_cli_report_command_exists():
    result = runner.invoke(app, ["report", "--help"])
    assert result.exit_code == 0


def test_cli_report_missing_traces_db_exits_1(tmp_path):
    result = runner.invoke(app, [
        "report",
        "--run-id", "nonexistent-run-id",
        "--traces", str(tmp_path / "no_traces.db"),
    ])
    assert result.exit_code == 1


def test_cli_report_no_traces_for_run_id_exits_1(tmp_path):
    import aiosqlite

    db_path = tmp_path / "traces.db"

    async def _create():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, "
                "stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER, "
                "output_tokens INTEGER, latency_ms REAL, success INTEGER, output_valid INTEGER, "
                "quorum_score REAL, timestamp TEXT, inputs_json TEXT, outputs_json TEXT, "
                "error_type TEXT, escalation_count INTEGER, spec_version TEXT)"
            )
            await db.commit()

    asyncio.run(_create())

    result = runner.invoke(app, [
        "report",
        "--run-id", "unknown-run",
        "--traces", str(db_path),
    ])
    assert result.exit_code == 1


def test_cli_report_full_run_produces_output(tmp_path):
    import aiosqlite
    import json

    db_path = tmp_path / "traces.db"

    async def _seed():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, "
                "stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER, "
                "output_tokens INTEGER, latency_ms REAL, success INTEGER, output_valid INTEGER, "
                "quorum_score REAL, timestamp TEXT, inputs_json TEXT, outputs_json TEXT, "
                "error_type TEXT, escalation_count INTEGER, spec_version TEXT)"
            )
            await db.execute(
                "INSERT INTO traces VALUES (1, 'run1', 'demo_wf', 'research', 'researcher', "
                "'haiku-4-5', 400, 700, 1100.0, 1, 1, 0.88, '2026-05-11T10:00:00+00:00', "
                "?, ?, NULL, 0, '1.0')",
                (
                    json.dumps({"topic": "climate"}),
                    json.dumps({"brief": "Temperatures are rising."}),
                ),
            )
            await db.commit()

    asyncio.run(_seed())

    result = runner.invoke(app, [
        "report",
        "--run-id", "run1",
        "--traces", str(db_path),
    ])
    assert result.exit_code == 0
    assert "run1" in result.output
    assert "demo_wf" in result.output
    assert "Temperatures are rising." in result.output

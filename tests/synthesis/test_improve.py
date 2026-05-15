"""Tests for SelfImproveRunner and SpecRefiner."""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from armature.synthesis.improve import SelfImproveRunner, SpecRefiner, ImprovementReport, RefinerResult
from armature.state.traces import TraceStore, TraceRecord
from armature.state.diagnostics import DiagnosticCode

# Sentinel that makes SpecRefiner.refine() return None without hitting LLM
_NO_REFINE = AsyncMock(return_value=None)


# ── helpers ───────────────────────────────────────────────────────────────────

_MINIMAL_SPEC_YAML = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic and produce findings.
"""

_REVISED_SPEC_YAML = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: >
        Analyze the topic and produce detailed findings.
        Include specific evidence and confidence level.
"""


def make_trace(
    *,
    run_id: str = "run-01",
    stage_id: str = "analyst",
    role_type: str = "researcher",
    success: bool = True,
    output_valid: bool = True,
    quorum_score: float | None = 0.91,
    escalation_count: int = 0,
    error_type: str | None = None,
) -> TraceRecord:
    return TraceRecord(
        run_id=run_id,
        workflow_name="test-wf",
        stage_id=stage_id,
        role_type=role_type,
        model="claude-sonnet",
        success=success,
        output_valid=output_valid,
        quorum_score=quorum_score,
        escalation_count=escalation_count,
        error_type=error_type,
        inputs={"topic": "climate"},
        outputs={"brief": "analysis here"},
    )


async def seed_store(store: TraceStore, traces: list[TraceRecord]) -> None:
    await store.init()
    for t in traces:
        await store.record(t)


# ── ImprovementReport structure ───────────────────────────────────────────────

async def test_analyze_returns_improvement_report(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.92)])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, auto_apply=False)
    report = await runner.analyze()
    assert isinstance(report, ImprovementReport)


async def test_analyze_report_has_correct_workflow_name(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [make_trace()])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, auto_apply=False)
    report = await runner.analyze()
    assert report.workflow_name == "test-wf"


# ── healthy workflow — no improvement needed ──────────────────────────────────

async def test_analyze_healthy_workflow_does_not_need_improvement(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    # IHR will be high — all success, high quorum
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.95),
        make_trace(run_id="r2", quorum_score=0.92),
        make_trace(run_id="r3", quorum_score=0.91),
    ])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.85, auto_apply=False)
    report = await runner.analyze()
    assert report.needs_improvement is False


async def test_analyze_healthy_does_not_apply_changes(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    original_content = spec_file.read_text()
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [make_trace(run_id="r1", quorum_score=0.95)])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.85, auto_apply=True)
    await runner.analyze()
    assert spec_file.read_text() == original_content


# ── insufficient data ─────────────────────────────────────────────────────────

async def test_analyze_insufficient_traces_returns_not_needs_improvement(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    # Only 1 trace, min_traces=3
    await seed_store(store, [make_trace()])

    runner = SelfImproveRunner(spec_file, db, min_traces=3, auto_apply=False)
    report = await runner.analyze()
    assert report.needs_improvement is False
    assert report.n_traces < 3


async def test_analyze_no_traces_returns_report(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await store.init()  # empty store

    runner = SelfImproveRunner(spec_file, db, auto_apply=False)
    report = await runner.analyze()
    assert report.needs_improvement is False
    assert report.n_traces == 0


# ── low quality — improvement needed ─────────────────────────────────────────

async def test_analyze_low_ihr_needs_improvement(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.25, success=False),
        make_trace(run_id="r3", quorum_score=0.18),
    ])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.85, min_traces=1, auto_apply=False)
    with patch.object(SpecRefiner, "refine", new_callable=AsyncMock, return_value=None):
        report = await runner.analyze()
    assert report.needs_improvement is True


async def test_analyze_has_diagnostics_when_improvement_needed(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", success=False, error_type="TimeoutError"),
        make_trace(run_id="r2", success=False, error_type="TimeoutError"),
        make_trace(run_id="r3", quorum_score=0.15, role_type="judge"),
    ])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.85, min_traces=1, auto_apply=False)
    with patch.object(SpecRefiner, "refine", new_callable=AsyncMock, return_value=None):
        report = await runner.analyze()
    assert len(report.diagnostics) > 0
    codes = {d.code for d in report.diagnostics}
    assert DiagnosticCode.STAGE_FAILED in codes


# ── auto_apply behaviour ──────────────────────────────────────────────────────

async def test_auto_apply_true_writes_revised_spec(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, min_traces=1, auto_apply=True)

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        report = await runner.analyze()

    assert report.applied is True
    assert "Include specific evidence" in spec_file.read_text()


async def test_auto_apply_false_does_not_write_spec(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20),
        make_trace(run_id="r3", quorum_score=0.20),
    ])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, min_traces=1, auto_apply=False)

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        report = await runner.analyze()

    assert report.applied is False
    assert spec_file.read_text() == _MINIMAL_SPEC_YAML


async def test_report_proposed_spec_available_when_not_applied(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20),
        make_trace(run_id="r2", quorum_score=0.20),
        make_trace(run_id="r3", quorum_score=0.20),
    ])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, min_traces=1, auto_apply=False)

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        report = await runner.analyze()

    assert report.proposed_spec is not None
    assert report.proposed_spec.name == "test-wf"


# ── improvement log ────────────────────────────────────────────────────────────

async def test_analyze_writes_log_file(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.92)])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    assert log_file.exists()


async def test_log_entry_is_valid_json(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.95)])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    entry = json.loads(log_file.read_text().strip())
    assert "timestamp" in entry
    assert "workflow_name" in entry
    assert "ihr_before" in entry
    assert "needs_improvement" in entry
    assert "applied" in entry


async def test_log_entry_records_ihr_before(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.95),
        make_trace(run_id="r2", quorum_score=0.90),
    ])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    entry = json.loads(log_file.read_text().strip())
    assert entry["ihr_before"] is not None
    assert 0.0 < entry["ihr_before"] <= 1.0


async def test_log_appends_across_multiple_analyze_calls(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.95)])

    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    await runner.analyze()
    lines = [l for l in log_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 2


# ── SpecRefiner ───────────────────────────────────────────────────────────────

async def test_spec_refiner_calls_llm_with_current_spec(tmp_path):
    from armature.state.diagnostics import DiagnosticResult

    refiner = SpecRefiner(model="claude-sonnet-4-6")
    captured_messages = []

    async def mock_llm(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return _make_llm_response(_REVISED_SPEC_YAML)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        await refiner.refine(
            spec_yaml=_MINIMAL_SPEC_YAML,
            diagnostics=[],
            ihr=None,
        )

    combined = " ".join(m["content"] for m in captured_messages)
    assert "test-wf" in combined


async def test_spec_refiner_includes_diagnostic_codes_in_prompt(tmp_path):
    from armature.state.diagnostics import DiagnosticResult, DiagnosticCode

    refiner = SpecRefiner(model="claude-sonnet-4-6")
    captured_messages = []

    async def mock_llm(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return _make_llm_response(_REVISED_SPEC_YAML)

    diags = [
        DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="analyst", details="TimeoutError"),
    ]
    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=diags, ihr=None)

    combined = " ".join(m["content"] for m in captured_messages)
    assert "stage_failed" in combined
    assert "analyst" in combined


async def test_spec_refiner_returns_none_on_unparseable_yaml():
    refiner = SpecRefiner(model="claude-sonnet-4-6")

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response("this is not yaml: [[[")
        result = await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], ihr=None)

    assert result is None


async def test_spec_refiner_returns_harness_spec_on_valid_yaml():
    refiner = SpecRefiner(model="claude-sonnet-4-6")

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        result = await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], ihr=None)

    assert result is not None
    assert result.spec.name == "test-wf"


async def test_spec_refiner_strips_invalid_changes():
    """If refiner adds stages (violating constraints), result is still None or valid."""
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    bad_yaml = "not: a: valid: spec: at all"

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(bad_yaml)
        result = await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], ihr=None)

    assert result is None


# ── RefinerResult — predictions ───────────────────────────────────────────────

_REVISED_SPEC_YAML_WITH_PREDICTIONS = (
    _REVISED_SPEC_YAML
    + '\n---PREDICTIONS---\n{"predicted_fixes": ["output_invalid:analyst"], "predicted_regressions": []}'
)


def test_spec_refiner_parse_extracts_predictions_from_separator():
    result = SpecRefiner._parse(_REVISED_SPEC_YAML_WITH_PREDICTIONS)
    assert result is not None
    assert result.predicted_fixes == ["output_invalid:analyst"]
    assert result.predicted_regressions == []


def test_spec_refiner_parse_empty_predictions_when_no_separator():
    result = SpecRefiner._parse(_REVISED_SPEC_YAML)
    assert result is not None
    assert result.predicted_fixes == []
    assert result.predicted_regressions == []


async def test_refiner_result_has_spec_and_yaml_text():
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        result = await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], ihr=None)
    assert result is not None
    assert isinstance(result, RefinerResult)
    assert result.spec.name == "test-wf"
    assert "Include specific evidence" in result.yaml_text


# ── _verify_predictions (unit) ────────────────────────────────────────────────

def test_verify_predictions_verified_fix_when_signature_resolved():
    verified, missed, unexpected = SelfImproveRunner._verify_predictions(
        prev_diag_keys={"output_invalid:analyst"},
        predicted_fixes=["output_invalid:analyst"],
        predicted_regressions=[],
        curr_diag_keys=set(),
    )
    assert "output_invalid:analyst" in verified
    assert missed == []
    assert unexpected == []


def test_verify_predictions_missed_when_signature_persists():
    verified, missed, unexpected = SelfImproveRunner._verify_predictions(
        prev_diag_keys={"output_invalid:analyst"},
        predicted_fixes=["output_invalid:analyst"],
        predicted_regressions=[],
        curr_diag_keys={"output_invalid:analyst"},
    )
    assert verified == []
    assert "output_invalid:analyst" in missed
    assert unexpected == []


def test_verify_predictions_unexpected_regression_when_new_signature_appears():
    verified, missed, unexpected = SelfImproveRunner._verify_predictions(
        prev_diag_keys=set(),
        predicted_fixes=[],
        predicted_regressions=[],
        curr_diag_keys={"stage_failed:worker"},
    )
    assert "stage_failed:worker" in unexpected
    assert verified == []
    assert missed == []


def test_verify_predictions_no_unexpected_regression_when_predicted():
    verified, missed, unexpected = SelfImproveRunner._verify_predictions(
        prev_diag_keys=set(),
        predicted_fixes=[],
        predicted_regressions=["stage_failed:worker"],
        curr_diag_keys={"stage_failed:worker"},
    )
    assert unexpected == []


def test_verify_predictions_all_empty_when_no_predictions_and_no_change():
    verified, missed, unexpected = SelfImproveRunner._verify_predictions(
        prev_diag_keys=set(),
        predicted_fixes=[],
        predicted_regressions=[],
        curr_diag_keys=set(),
    )
    assert verified == []
    assert missed == []
    assert unexpected == []


# ── ImprovementReport prediction fields ───────────────────────────────────────

async def test_report_predicted_fixes_populated_from_refiner(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])
    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, min_traces=1, auto_apply=False)
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML_WITH_PREDICTIONS)
        report = await runner.analyze()
    assert report.predicted_fixes == ["output_invalid:analyst"]
    assert report.predicted_regressions == []


async def test_first_cycle_verification_fields_are_empty(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.92)])
    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, auto_apply=False, log_path=log_file)
    report = await runner.analyze()
    assert report.verified_fixes == []
    assert report.missed_predictions == []
    assert report.unexpected_regressions == []


async def test_second_cycle_computes_verified_fixes(tmp_path):
    """Predicted fix that disappears in cycle 2 → verified_fixes."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    log_file = tmp_path / "improve.log.jsonl"

    # Cycle 1: bad DB — output_invalid fires and is predicted to be fixed
    db1 = tmp_path / "traces_bad.db"
    store1 = TraceStore(db1)
    await seed_store(store1, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])
    runner1 = SelfImproveRunner(
        spec_file, db1, target_ihr=0.90, min_traces=1, auto_apply=False, log_path=log_file
    )
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML_WITH_PREDICTIONS)
        await runner1.analyze()

    # Cycle 2: clean DB — only good traces; same log_path so cycle 1 predictions are read
    db2 = tmp_path / "traces_good.db"
    store2 = TraceStore(db2)
    await seed_store(store2, [
        make_trace(run_id="g1", quorum_score=0.95, output_valid=True),
        make_trace(run_id="g2", quorum_score=0.95, output_valid=True),
        make_trace(run_id="g3", quorum_score=0.95, output_valid=True),
    ])
    runner2 = SelfImproveRunner(
        spec_file, db2, target_ihr=0.90, min_traces=1, auto_apply=False, log_path=log_file
    )
    with patch.object(SpecRefiner, "refine", new_callable=AsyncMock, return_value=None):
        report2 = await runner2.analyze()

    assert "output_invalid:analyst" in report2.verified_fixes


async def test_second_cycle_computes_missed_predictions(tmp_path):
    """Predicted fix that still fires in cycle 2 → missed_predictions."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])
    runner = SelfImproveRunner(
        spec_file, db, target_ihr=0.90, min_traces=1, auto_apply=False, log_path=log_file
    )
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML_WITH_PREDICTIONS)
        await runner.analyze()  # cycle 1 — logs predicted_fixes

    # Cycle 2: same bad traces, output_invalid:analyst still present
    with patch.object(SpecRefiner, "refine", new_callable=AsyncMock, return_value=None):
        report2 = await runner.analyze()

    assert "output_invalid:analyst" in report2.missed_predictions


# ── log includes prediction and verification fields ────────────────────────────

async def test_log_entry_includes_predicted_fixes_field(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.92)])
    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    entry = json.loads(log_file.read_text().strip())
    assert "predicted_fixes" in entry
    assert "predicted_regressions" in entry


async def test_log_entry_includes_verification_fields(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.92)])
    runner = SelfImproveRunner(spec_file, db, target_ihr=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    entry = json.loads(log_file.read_text().strip())
    assert "verified_fixes" in entry
    assert "missed_predictions" in entry
    assert "unexpected_regressions" in entry


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_llm_response(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp

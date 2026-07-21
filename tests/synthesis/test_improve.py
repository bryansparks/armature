"""Tests for SelfImproveRunner and SpecRefiner."""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from armature.synthesis.improve import (
    SelfImproveRunner,
    SpecRefiner,
    ImprovementReport,
    RefinerResult,
    resolve_trigger_overrides,
)
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


# ── resolve_trigger_overrides ────────────────────────────────────────────────

def _spec_with_trigger(*, target_hqs=None, min_traces=None, drift_threshold=None):
    from armature.spec.models import HarnessSpec
    si_yaml = ""
    if target_hqs is not None or min_traces is not None or drift_threshold is not None:
        parts = ["self_improvement:"]
        if target_hqs is not None:
            parts.append(f"  target_hqs: {target_hqs}")
        if min_traces is not None:
            parts.append(f"  min_traces: {min_traces}")
        if drift_threshold is not None:
            parts.append(f"  drift_threshold: {drift_threshold}")
        si_yaml = "\n".join(parts) + "\n"
    yaml = f"""\
name: test-wf
version: "1.0"
{si_yaml}stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze.
"""
    from armature.spec.loader import load_spec
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, yaml.encode())
    os.close(fd)
    return load_spec(Path(path))


def test_resolve_trigger_overrides_uses_default_when_spec_and_cli_absent():
    spec = _spec_with_trigger()
    target, min_t, drift_t = resolve_trigger_overrides(
        None, None, None, spec, default_target_hqs=0.90, default_min_traces=3,
        default_drift_threshold=0.5,
    )
    assert target == 0.90
    assert min_t == 3
    assert drift_t == 0.5


def test_resolve_trigger_overrides_spec_field_wins_over_default():
    spec = _spec_with_trigger(target_hqs=0.95, min_traces=10, drift_threshold=0.4)
    target, min_t, drift_t = resolve_trigger_overrides(
        None, None, None, spec, default_target_hqs=0.90, default_min_traces=3,
        default_drift_threshold=0.5,
    )
    assert target == 0.95
    assert min_t == 10
    assert drift_t == 0.4


def test_resolve_trigger_overrides_cli_flag_wins_over_spec():
    spec = _spec_with_trigger(target_hqs=0.95, min_traces=10, drift_threshold=0.4)
    target, min_t, drift_t = resolve_trigger_overrides(
        0.80, 5, 0.3, spec, default_target_hqs=0.90, default_min_traces=3,
        default_drift_threshold=0.5,
    )
    assert target == 0.80
    assert min_t == 5
    assert drift_t == 0.3


def test_resolve_trigger_overrides_spec_partial_override():
    # spec sets only target_hqs; min_traces + drift_threshold fall through to default
    spec = _spec_with_trigger(target_hqs=0.75)
    target, min_t, drift_t = resolve_trigger_overrides(
        None, None, None, spec, default_target_hqs=0.90, default_min_traces=3,
        default_drift_threshold=0.5,
    )
    assert target == 0.75
    assert min_t == 3
    assert drift_t == 0.5


def test_resolve_trigger_overrides_drift_threshold_default_when_absent():
    spec = _spec_with_trigger()
    _, _, drift_t = resolve_trigger_overrides(
        None, None, None, spec, default_target_hqs=0.90, default_min_traces=3,
        default_drift_threshold=0.5,
    )
    assert drift_t == 0.5


def test_resolve_trigger_overrides_drift_threshold_spec_wins():
    spec = _spec_with_trigger(drift_threshold=0.6)
    _, _, drift_t = resolve_trigger_overrides(
        None, None, None, spec, default_target_hqs=0.90, default_min_traces=3,
        default_drift_threshold=0.5,
    )
    assert drift_t == 0.6


def test_resolve_trigger_overrides_drift_threshold_cli_wins():
    spec = _spec_with_trigger(drift_threshold=0.6)
    _, _, drift_t = resolve_trigger_overrides(
        None, None, 0.35, spec, default_target_hqs=0.90, default_min_traces=3,
        default_drift_threshold=0.5,
    )
    assert drift_t == 0.35


# ── ImprovementReport structure ───────────────────────────────────────────────

async def test_analyze_returns_improvement_report(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.92)])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, auto_apply=False)
    report = await runner.analyze()
    assert isinstance(report, ImprovementReport)


async def test_analyze_report_has_correct_workflow_name(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [make_trace()])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, auto_apply=False)
    report = await runner.analyze()
    assert report.workflow_name == "test-wf"


# ── healthy workflow — no improvement needed ──────────────────────────────────

async def test_analyze_healthy_workflow_does_not_need_improvement(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    # HQS will be high — all success, high quorum
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.95),
        make_trace(run_id="r2", quorum_score=0.92),
        make_trace(run_id="r3", quorum_score=0.91),
    ])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.85, auto_apply=False)
    report = await runner.analyze()
    assert report.needs_improvement is False


async def test_analyze_healthy_does_not_apply_changes(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    original_content = spec_file.read_text()
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [make_trace(run_id="r1", quorum_score=0.95)])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.85, auto_apply=True)
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

async def test_analyze_low_hqs_needs_improvement(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.25, success=False),
        make_trace(run_id="r3", quorum_score=0.18),
    ])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.85, min_traces=1, auto_apply=False)
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

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.85, min_traces=1, auto_apply=False)
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

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True)

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

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False)

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

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False)

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

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    assert log_file.exists()


async def test_log_entry_is_valid_json(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.95)])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    entry = json.loads(log_file.read_text().strip())
    assert "timestamp" in entry
    assert "workflow_name" in entry
    assert "hqs_before" in entry
    assert "needs_improvement" in entry
    assert "applied" in entry


async def test_log_entry_records_hqs_before(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.95),
        make_trace(run_id="r2", quorum_score=0.90),
    ])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    entry = json.loads(log_file.read_text().strip())
    assert entry["hqs_before"] is not None
    assert 0.0 < entry["hqs_before"] <= 1.0


async def test_log_appends_across_multiple_analyze_calls(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [make_trace(quorum_score=0.95)])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, auto_apply=False, log_path=log_file)
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
            hqs=None,
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
        await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=diags, hqs=None)

    combined = " ".join(m["content"] for m in captured_messages)
    assert "stage_failed" in combined
    assert "analyst" in combined


async def test_spec_refiner_returns_none_on_unparseable_yaml():
    refiner = SpecRefiner(model="claude-sonnet-4-6")

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response("this is not yaml: [[[")
        result = await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], hqs=None)

    assert result is None


async def test_spec_refiner_returns_harness_spec_on_valid_yaml():
    refiner = SpecRefiner(model="claude-sonnet-4-6")

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        result = await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], hqs=None)

    assert result is not None
    assert result.spec.name == "test-wf"


async def test_spec_refiner_strips_invalid_changes():
    """If refiner adds stages (violating constraints), result is still None or valid."""
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    bad_yaml = "not: a: valid: spec: at all"

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(bad_yaml)
        result = await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], hqs=None)

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
        result = await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], hqs=None)
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
    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False)
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
    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, auto_apply=False, log_path=log_file)
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
        spec_file, db1, target_hqs=0.90, min_traces=1, auto_apply=False, log_path=log_file
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
        spec_file, db2, target_hqs=0.90, min_traces=1, auto_apply=False, log_path=log_file
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
        spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False, log_path=log_file
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
    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, auto_apply=False, log_path=log_file)
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
    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, auto_apply=False, log_path=log_file)
    await runner.analyze()
    entry = json.loads(log_file.read_text().strip())
    assert "verified_fixes" in entry
    assert "missed_predictions" in entry
    assert "unexpected_regressions" in entry


# ── spec versioning ──────────────────────────────────────────────────────────

async def test_spec_history_written_before_auto_apply(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True)

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        report = await runner.analyze()

    assert report.applied is True
    history_file = tmp_path / "wf.spec_history.jsonl"
    assert history_file.exists()
    import json
    entry = json.loads(history_file.read_text().strip())
    assert entry["yaml"] == _MINIMAL_SPEC_YAML
    assert "timestamp" in entry


async def test_spec_history_not_written_when_no_improvement(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [make_trace(run_id="r1", quorum_score=0.95)])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.85, auto_apply=True)
    await runner.analyze()

    history_file = tmp_path / "wf.spec_history.jsonl"
    assert not history_file.exists()


async def test_spec_history_appends_across_multiple_apply_cycles(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True)

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        await runner.analyze()
        mock_llm.return_value = _make_llm_response(_MINIMAL_SPEC_YAML)
        await runner.analyze()

    history_file = tmp_path / "wf.spec_history.jsonl"
    import json
    lines = [l for l in history_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 2


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_llm_response(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ── Phase C: Drift Score (RED) ────────────────────────────────────────────────


async def test_drift_score_field_defaults_to_zero():
    report = ImprovementReport(
        workflow_name="wf", spec_path=Path("/tmp/wf.yaml"),
        n_traces=1, hqs_before=0.8, needs_improvement=False,
        applied=False, diagnostics=[],
    )
    assert report.drift_score == 0.0


async def test_load_all_verified_fixes_empty_log(tmp_path):
    from armature.synthesis.improve import _load_all_verified_fixes
    log = tmp_path / "nolog.jsonl"
    result = _load_all_verified_fixes(log)
    assert result == set()


async def test_load_all_verified_fixes_reads_all_entries(tmp_path):
    from armature.synthesis.improve import _load_all_verified_fixes
    log = tmp_path / "improve.log.jsonl"
    log.write_text(
        json.dumps({"verified_fixes": ["output_invalid:analyst", "stage_failed:writer"]}) + "\n" +
        json.dumps({"verified_fixes": ["low_confidence:judge"]}) + "\n"
    )
    result = _load_all_verified_fixes(log)
    assert result == {"output_invalid:analyst", "stage_failed:writer", "low_confidence:judge"}


async def test_load_all_verified_fixes_tolerates_missing_field(tmp_path):
    from armature.synthesis.improve import _load_all_verified_fixes
    log = tmp_path / "improve.log.jsonl"
    log.write_text(
        json.dumps({"verified_fixes": ["output_invalid:analyst"]}) + "\n" +
        json.dumps({"other_field": "value"}) + "\n"
    )
    result = _load_all_verified_fixes(log)
    assert result == {"output_invalid:analyst"}


async def test_drift_score_zero_when_no_regressions(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    # log has one verified_fix that is NOT currently failing
    log_file.write_text(json.dumps({"verified_fixes": ["stage_failed:other_stage"]}) + "\n")
    store = TraceStore(db)
    await seed_store(store, [make_trace(run_id="r1", quorum_score=0.92)])  # healthy
    runner = SelfImproveRunner(spec_file, db, target_hqs=0.99, auto_apply=False, log_path=log_file)
    report = await runner.analyze()
    assert report.drift_score == 0.0


async def test_drift_score_nonzero_when_previously_fixed_issue_regresses(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    # Prior cycle "fixed" output_invalid:analyst — but it is currently failing again
    log_file.write_text(json.dumps({"verified_fixes": ["output_invalid:analyst"]}) + "\n")
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", output_valid=False),
        make_trace(run_id="r2", output_valid=False),
        make_trace(run_id="r3", output_valid=False),
    ])
    runner = SelfImproveRunner(spec_file, db, target_hqs=0.99, auto_apply=False, log_path=log_file)
    with patch("armature.synthesis.improve.SpecRefiner.refine", _NO_REFINE):
        report = await runner.analyze()
    assert report.drift_score > 0.0


async def test_drift_score_logged_to_jsonl(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await seed_store(store, [make_trace(run_id="r1", quorum_score=0.92)])
    runner = SelfImproveRunner(spec_file, db, target_hqs=0.99, auto_apply=False, log_path=log_file)
    await runner.analyze()
    entry = json.loads(log_file.read_text().strip())
    assert "drift_score" in entry


# ── #5: drift_score as an implicit trigger (RED) ───────────────────────────────


async def test_drift_trigger_fires_when_hqs_healthy_but_drift_high(tmp_path):
    """HQS ≥ target but drift ≥ threshold → needs_improvement, triggered_by_drift."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    # Prior cycle "fixed" high_escalation:analyst — it is currently failing again.
    log_file.write_text(json.dumps({"verified_fixes": ["high_escalation:analyst"]}) + "\n")
    store = TraceStore(db)
    # Healthy HQS (quorum 0.95, valid, success) but high escalation reappears.
    # high_escalation only costs the happy-path term (0.10), so HQS ≈ 0.89 ≥ 0.85.
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.95, escalation_count=2),
        make_trace(run_id="r2", quorum_score=0.95, escalation_count=2),
        make_trace(run_id="r3", quorum_score=0.95, escalation_count=2),
    ])
    runner = SelfImproveRunner(
        spec_file, db, target_hqs=0.85, min_traces=1, drift_threshold=0.5,
        auto_apply=False, log_path=log_file,
    )
    with patch("armature.synthesis.improve.SpecRefiner.refine", _NO_REFINE):
        report = await runner.analyze()
    assert report.needs_improvement is True
    assert report.triggered_by_drift is True


async def test_drift_trigger_inert_when_drift_below_threshold(tmp_path):
    """HQS ≥ target and drift < threshold → no improvement needed."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    # A verified fix that is NOT currently failing → drift 0.0.
    log_file.write_text(json.dumps({"verified_fixes": ["stage_failed:other_stage"]}) + "\n")
    store = TraceStore(db)
    await seed_store(store, [make_trace(run_id="r1", quorum_score=0.92)])
    runner = SelfImproveRunner(
        spec_file, db, target_hqs=0.90, min_traces=1, drift_threshold=0.5,
        auto_apply=False, log_path=log_file,
    )
    report = await runner.analyze()
    assert report.needs_improvement is False
    assert report.triggered_by_drift is False


async def test_drift_trigger_not_set_when_hqs_drives_improvement(tmp_path):
    """HQS < target (regardless of drift) → triggered_by_drift False (HQS-driven)."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    log_file.write_text(json.dumps({"verified_fixes": ["output_invalid:analyst"]}) + "\n")
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])
    runner = SelfImproveRunner(
        spec_file, db, target_hqs=0.90, min_traces=1, drift_threshold=0.5,
        auto_apply=False, log_path=log_file,
    )
    with patch("armature.synthesis.improve.SpecRefiner.refine", _NO_REFINE):
        report = await runner.analyze()
    assert report.needs_improvement is True
    assert report.triggered_by_drift is False  # HQS-driven, not drift-driven


async def test_drift_triggered_proposal_forces_review_and_suppresses_auto_apply(tmp_path):
    """Drift-triggered proposal: even with auto_apply=True, no apply — pending written."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    log_file.write_text(json.dumps({"verified_fixes": ["high_escalation:analyst"]}) + "\n")
    store = TraceStore(db)
    # Healthy HQS, reappearing high_escalation:analyst → drift = 1.0.
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.95, escalation_count=2),
        make_trace(run_id="r2", quorum_score=0.95, escalation_count=2),
        make_trace(run_id="r3", quorum_score=0.95, escalation_count=2),
    ])
    runner = SelfImproveRunner(
        spec_file, db, target_hqs=0.85, min_traces=1, drift_threshold=0.5,
        auto_apply=True, log_path=log_file,
    )
    # Refiner proposes a description-only change (allowed surface, auto-apply eligible).
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        report = await runner.analyze()

    assert report.triggered_by_drift is True
    assert report.escalated_oscillation is True
    assert report.applied is False
    assert report.requires_review is True
    assert report.pending_path is not None
    assert report.pending_path.exists()  # .pending.yaml written
    assert spec_file.read_text() == _MINIMAL_SPEC_YAML  # spec unchanged


async def test_hqs_triggered_path_still_auto_applies(tmp_path):
    """Regression guard: HQS-driven proposal with auto_apply=True still applies."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    # No prior verified fixes → drift 0.0; low HQS drives improvement.
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])
    runner = SelfImproveRunner(
        spec_file, db, target_hqs=0.90, min_traces=1, drift_threshold=0.5,
        auto_apply=True, log_path=log_file,
    )
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_SPEC_YAML)
        report = await runner.analyze()

    assert report.triggered_by_drift is False
    assert report.escalated_oscillation is False
    assert report.applied is True  # happy path preserved
    assert "Include specific evidence" in spec_file.read_text()


async def test_drift_log_entry_records_trigger_fields(tmp_path):
    """_write_log records triggered_by_drift, drift_threshold, escalated_oscillation."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    log_file.write_text(json.dumps({"verified_fixes": ["high_escalation:analyst"]}) + "\n")
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.95, escalation_count=2),
        make_trace(run_id="r2", quorum_score=0.95, escalation_count=2),
        make_trace(run_id="r3", quorum_score=0.95, escalation_count=2),
    ])
    runner = SelfImproveRunner(
        spec_file, db, target_hqs=0.85, min_traces=1, drift_threshold=0.5,
        auto_apply=True, log_path=log_file,
    )
    with patch("armature.synthesis.improve.SpecRefiner.refine", _NO_REFINE):
        await runner.analyze()
    # The last log line is this cycle's entry.
    lines = [l for l in log_file.read_text().splitlines() if l.strip()]
    entry = json.loads(lines[-1])
    assert entry["triggered_by_drift"] is True
    assert entry["drift_threshold"] == 0.5
    assert "escalated_oscillation" in entry


async def test_improvement_report_drift_trigger_fields_default():
    """ImprovementReport.escalated_oscillation / triggered_by_drift default False."""
    report = ImprovementReport(
        workflow_name="wf", spec_path=Path("/tmp/wf.yaml"),
        n_traces=1, hqs_before=0.8, needs_improvement=False,
        applied=False, diagnostics=[],
    )
    assert report.escalated_oscillation is False
    assert report.triggered_by_drift is False


# ── Phase F: Component Governance (RED) ─────────────────────────────────────


async def test_classify_changes_routes_description_to_auto(tmp_path):
    from armature.synthesis.improve import _classify_changes
    from armature.spec.loader import load_spec

    old_yaml = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Old description.
"""
    new_yaml = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: New description with more detail.
"""
    old_file = tmp_path / "old.yaml"
    new_file = tmp_path / "new.yaml"
    old_file.write_text(old_yaml)
    new_file.write_text(new_yaml)
    old_spec = load_spec(old_file)
    new_spec = load_spec(new_file)
    auto, review = _classify_changes(old_spec, new_spec)
    assert "description" in str(auto) or len(auto) > 0
    assert len(review) == 0


async def test_classify_changes_routes_stage_addition_to_review(tmp_path):
    from armature.synthesis.improve import _classify_changes
    from armature.spec.loader import load_spec

    old_yaml = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic.
"""
    new_yaml = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic.
  - id: writer
    role:
      name: Writer
      type: worker
      description: Write the report.
"""
    old_file = tmp_path / "old.yaml"
    new_file = tmp_path / "new.yaml"
    old_file.write_text(old_yaml)
    new_file.write_text(new_yaml)
    old_spec = load_spec(old_file)
    new_spec = load_spec(new_file)
    auto, review = _classify_changes(old_spec, new_spec)
    assert len(review) > 0


async def test_classify_changes_routes_model_tier_to_auto(tmp_path):
    """A role.model_tier change is detected and routed to auto (not silently omitted)."""
    from armature.synthesis.improve import _classify_changes
    from armature.spec.loader import load_spec

    model_tiers = """\
model_tiers:
  small:
    provider: openrouter
    model: qwen/qwen3.6-27b
  large:
    provider: openrouter
    model: moonshotai/kimi-k2.6
"""
    old_yaml = f"""\
name: test-wf
version: "1.0"
{model_tiers}stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      model_tier: small
      description: Analyze the topic.
"""
    new_yaml = f"""\
name: test-wf
version: "1.0"
{model_tiers}stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      model_tier: large
      description: Analyze the topic.
"""
    old_file = tmp_path / "old.yaml"
    new_file = tmp_path / "new.yaml"
    old_file.write_text(old_yaml)
    new_file.write_text(new_yaml)
    old_spec = load_spec(old_file)
    new_spec = load_spec(new_file)
    auto, review = _classify_changes(old_spec, new_spec)
    assert any(k.startswith("model_tier:") for k in auto)
    assert len(review) == 0


async def test_classify_changes_detects_global_model_tiers_block_change(tmp_path):
    """A change to the model_tiers definitions (not a per-stage assignment) is detected → auto."""
    from armature.synthesis.improve import _classify_changes
    from armature.spec.loader import load_spec

    old_yaml = """\
name: test-wf
version: "1.0"
model_tiers:
  large:
    provider: openrouter
    model: moonshotai/kimi-k2.6
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      model_tier: large
      description: Analyze the topic.
"""
    new_yaml = """\
name: test-wf
version: "1.0"
model_tiers:
  large:
    provider: openrouter
    model: z-ai/glm-5.2
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      model_tier: large
      description: Analyze the topic.
"""
    old_file = tmp_path / "old.yaml"
    new_file = tmp_path / "new.yaml"
    old_file.write_text(old_yaml)
    new_file.write_text(new_yaml)
    old_spec = load_spec(old_file)
    new_spec = load_spec(new_file)
    auto, review = _classify_changes(old_spec, new_spec)
    assert any("model_tiers" in k for k in auto)
    assert len(review) == 0


async def test_classify_changes_no_model_tier_false_positive_when_unchanged(tmp_path):
    """No model_tier change → no model_tier entry in auto."""
    from armature.synthesis.improve import _classify_changes
    from armature.spec.loader import load_spec

    model_tiers = """\
model_tiers:
  small:
    provider: openrouter
    model: qwen/qwen3.6-27b
"""
    yaml = f"""\
name: test-wf
version: "1.0"
{model_tiers}stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      model_tier: small
      description: Analyze the topic.
"""
    f = tmp_path / "spec.yaml"
    f.write_text(yaml)
    spec = load_spec(f)
    auto, review = _classify_changes(spec, spec)
    assert not any("model_tier" in k for k in auto)
    assert not any("model_tiers" in k for k in auto)
    assert len(auto) == 0
    assert len(review) == 0


async def test_requires_review_flag_on_improvement_report():
    report = ImprovementReport(
        workflow_name="wf", spec_path=Path("/tmp/wf.yaml"),
        n_traces=1, hqs_before=0.8, needs_improvement=False,
        applied=False, diagnostics=[],
    )
    assert report.requires_review is False
    assert report.pending_path is None


async def test_pending_yaml_written_when_review_required(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])

    # A spec that adds a stage — this should trigger review_required
    _REVISED_WITH_NEW_STAGE = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic.
  - id: writer
    role:
      name: Writer
      type: worker
      description: Write the findings.
"""
    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True)

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_WITH_NEW_STAGE)
        report = await runner.analyze()

    assert report.requires_review is True
    assert report.applied is False
    pending = tmp_path / "wf.pending.yaml"
    assert pending.exists()


async def test_spec_not_overwritten_when_review_required(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])

    _REVISED_WITH_NEW_STAGE = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic.
  - id: writer
    role:
      name: Writer
      type: worker
      description: Write the findings.
"""
    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True)

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_WITH_NEW_STAGE)
        await runner.analyze()

    # Original spec must be unchanged
    assert spec_file.read_text() == _MINIMAL_SPEC_YAML


# ── editable surfaces in SpecRefiner prompt ───────────────────────────────────

async def test_spec_refiner_includes_editable_surfaces_in_prompt():
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    captured_messages = []

    async def mock_llm(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return _make_llm_response(_REVISED_SPEC_YAML)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        await refiner.refine(
            spec_yaml=_MINIMAL_SPEC_YAML,
            diagnostics=[],
            hqs=None,
            editable_surfaces=["descriptions", "retry_counts"],
        )

    system_content = next(m["content"] for m in captured_messages if m["role"] == "system")
    assert "descriptions" in system_content
    assert "retry_counts" in system_content


async def test_spec_refiner_lists_locked_surfaces_as_do_not_modify():
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    captured_messages = []

    async def mock_llm(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return _make_llm_response(_REVISED_SPEC_YAML)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        await refiner.refine(
            spec_yaml=_MINIMAL_SPEC_YAML,
            diagnostics=[],
            hqs=None,
            editable_surfaces=["descriptions"],
        )

    system_content = next(m["content"] for m in captured_messages if m["role"] == "system")
    assert "DO NOT modify" in system_content
    assert "schemas" in system_content
    assert "model_tiers" in system_content


async def test_spec_refiner_no_surface_restriction_when_surfaces_is_none():
    """When editable_surfaces is None, the system prompt should not restrict surfaces."""
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    captured_messages = []

    async def mock_llm(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return _make_llm_response(_REVISED_SPEC_YAML)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], hqs=None)

    system_content = next(m["content"] for m in captured_messages if m["role"] == "system")
    assert "DO NOT modify" not in system_content


# ── refine_many ───────────────────────────────────────────────────────────────

async def test_refine_many_returns_n_valid_results():
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    call_count = 0

    async def mock_llm(**kwargs):
        nonlocal call_count
        call_count += 1
        return _make_llm_response(_REVISED_SPEC_YAML)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        results = await refiner.refine_many(
            spec_yaml=_MINIMAL_SPEC_YAML,
            diagnostics=[],
            hqs=None,
            n_proposals=3,
        )

    assert call_count == 3
    assert len(results) == 3
    assert all(isinstance(r, RefinerResult) for r in results)


async def test_refine_many_filters_none_results():
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    call_count = 0

    async def mock_llm(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_llm_response("not: valid: yaml: [[[")
        return _make_llm_response(_REVISED_SPEC_YAML)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        results = await refiner.refine_many(
            spec_yaml=_MINIMAL_SPEC_YAML,
            diagnostics=[],
            hqs=None,
            n_proposals=3,
        )

    assert len(results) == 2


async def test_refine_many_uses_diversity_hints():
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    captured_system_prompts = []

    async def mock_llm(**kwargs):
        msgs = kwargs.get("messages", [])
        sys_msg = next((m["content"] for m in msgs if m["role"] == "system"), "")
        captured_system_prompts.append(sys_msg)
        return _make_llm_response(_REVISED_SPEC_YAML)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        await refiner.refine_many(
            spec_yaml=_MINIMAL_SPEC_YAML,
            diagnostics=[],
            hqs=None,
            n_proposals=3,
        )

    assert len(set(captured_system_prompts)) == 3


# ── _pick_best_proposal ───────────────────────────────────────────────────────

def _load(tmp_path, yaml_text):
    from armature.spec.loader import load_spec
    f = tmp_path / "s.yaml"
    f.write_text(yaml_text)
    return load_spec(f)


def test_pick_best_proposal_selects_highest_coverage(tmp_path):
    from armature.synthesis.improve import _pick_best_proposal
    from armature.state.diagnostics import DiagnosticResult, DiagnosticCode

    diags = [
        DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="analyst"),
        DiagnosticResult(code=DiagnosticCode.OUTPUT_INVALID, stage_id="writer"),
    ]
    # Same spec for both → equal latency_risk (0) → coverage decides (regression guard).
    spec = _load(tmp_path, _MINIMAL_SPEC_YAML)
    r_low = RefinerResult(spec=spec, yaml_text="", predicted_fixes=["stage_failed:analyst"])
    r_high = RefinerResult(spec=spec, yaml_text="", predicted_fixes=["stage_failed:analyst", "output_invalid:writer"])

    best = _pick_best_proposal([r_low, r_high], diags, spec)
    assert best is r_high


def test_pick_best_proposal_returns_none_for_empty_list(tmp_path):
    from armature.synthesis.improve import _pick_best_proposal
    spec = _load(tmp_path, _MINIMAL_SPEC_YAML)
    assert _pick_best_proposal([], [], spec) is None


def test_pick_best_proposal_returns_single_when_one_candidate(tmp_path):
    from armature.synthesis.improve import _pick_best_proposal
    spec = _load(tmp_path, _MINIMAL_SPEC_YAML)
    r = RefinerResult(spec=spec, yaml_text="", predicted_fixes=[])
    result = _pick_best_proposal([r], [], spec)
    assert result is r


# ── #6: latency-aware selection (RED) ─────────────────────────────────────────


def test_latency_risk_zero_when_unchanged(tmp_path):
    from armature.synthesis.improve import _latency_risk
    spec = _load(tmp_path, _MINIMAL_SPEC_YAML)
    assert _latency_risk(spec, spec) == 0.0


def test_latency_risk_stage_added(tmp_path):
    from armature.synthesis.improve import _latency_risk
    old = _load(tmp_path, _MINIMAL_SPEC_YAML)
    new_yaml = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, description: Analyze.}
  - id: writer
    role: {name: Writer, type: worker, description: Write.}
"""
    new = _load(tmp_path, new_yaml)
    assert _latency_risk(old, new) == 1.0


def test_latency_risk_stage_removed(tmp_path):
    from armature.synthesis.improve import _latency_risk
    two = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, description: Analyze.}
  - id: writer
    role: {name: Writer, type: worker, description: Write.}
"""
    old = _load(tmp_path, two)
    new = _load(tmp_path, _MINIMAL_SPEC_YAML)
    assert _latency_risk(old, new) == -1.0


def test_latency_risk_tier_escalation(tmp_path):
    from armature.synthesis.improve import _latency_risk
    base = """\
name: test-wf
version: "1.0"
model_tiers:
  small: {provider: openrouter, model: qwen/qwen3.6-27b}
  large: {provider: openrouter, model: moonshotai/kimi-k2.6}
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, model_tier: small, description: Analyze.}
"""
    escalated = base.replace("model_tier: small", "model_tier: large")
    old = _load(tmp_path, base)
    new = _load(tmp_path, escalated)
    assert _latency_risk(old, new) == 1.0


def test_latency_risk_tier_demotion(tmp_path):
    from armature.synthesis.improve import _latency_risk
    base = """\
name: test-wf
version: "1.0"
model_tiers:
  small: {provider: openrouter, model: qwen/qwen3.6-27b}
  large: {provider: openrouter, model: moonshotai/kimi-k2.6}
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, model_tier: large, description: Analyze.}
"""
    demoted = base.replace("model_tier: large", "model_tier: small")
    old = _load(tmp_path, base)
    new = _load(tmp_path, demoted)
    assert _latency_risk(old, new) == -1.0


def test_latency_risk_custom_tier_no_contribution(tmp_path):
    from armature.synthesis.improve import _latency_risk
    base = """\
name: test-wf
version: "1.0"
model_tiers:
  bespoke: {provider: openrouter, model: some/model}
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, model_tier: bespoke, description: Analyze.}
"""
    other = base.replace("model_tier: bespoke", "model_tier: other_custom")
    old = _load(tmp_path, base)
    new = _load(tmp_path, other)
    # Both tiers are custom/unknown rank → no escalation contribution.
    assert _latency_risk(old, new) == 0.0


def test_latency_risk_retry_increase(tmp_path):
    from armature.synthesis.improve import _latency_risk
    base = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, description: Analyze.}
    on_fail: {loop: {stage: analyst, max: 3}}
"""
    more = base.replace("max: 3", "max: 5")
    old = _load(tmp_path, base)
    new = _load(tmp_path, more)
    assert _latency_risk(old, new) == 0.5


def test_latency_risk_timeout_increase(tmp_path):
    from armature.synthesis.improve import _latency_risk
    base = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, description: Analyze.}
    timeout_s: 30
"""
    more = base.replace("timeout_s: 30", "timeout_s: 60")
    old = _load(tmp_path, base)
    new = _load(tmp_path, more)
    assert _latency_risk(old, new) == 0.25


def test_latency_risk_model_tiers_block_redefinition(tmp_path):
    from armature.synthesis.improve import _latency_risk
    base = """\
name: test-wf
version: "1.0"
model_tiers:
  small: {provider: openrouter, model: qwen/qwen3.6-27b, max_tokens: 2048}
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, model_tier: small, description: Analyze.}
"""
    redef = base.replace("max_tokens: 2048", "max_tokens: 8192")
    old = _load(tmp_path, base)
    new = _load(tmp_path, redef)
    assert _latency_risk(old, new) == 0.5


def test_pick_best_prefers_lower_latency_risk_on_coverage_tie(tmp_path):
    from armature.synthesis.improve import _pick_best_proposal
    from armature.state.diagnostics import DiagnosticResult, DiagnosticCode
    diags = [DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="analyst")]
    # Equal coverage (both predict the one failing fix); one adds a stage (risk 1.0),
    # one is description-only (risk 0.0). The low-risk one must win.
    low_risk_spec = _load(tmp_path, _MINIMAL_SPEC_YAML)
    high_risk_yaml = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, description: Analyze.}
  - id: writer
    role: {name: Writer, type: worker, description: Write.}
"""
    high_risk_spec = _load(tmp_path, high_risk_yaml)
    r_low = RefinerResult(spec=low_risk_spec, yaml_text="", predicted_fixes=["stage_failed:analyst"])
    r_high = RefinerResult(spec=high_risk_spec, yaml_text="", predicted_fixes=["stage_failed:analyst"])
    best = _pick_best_proposal([r_low, r_high], diags, low_risk_spec)
    assert best is r_low


def test_pick_best_low_risk_wins_within_epsilon(tmp_path):
    from armature.synthesis.improve import _pick_best_proposal
    from armature.state.diagnostics import DiagnosticResult, DiagnosticCode
    diags = [
        DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="analyst"),
        DiagnosticResult(code=DiagnosticCode.OUTPUT_INVALID, stage_id="writer"),
    ]
    low_risk_spec = _load(tmp_path, _MINIMAL_SPEC_YAML)
    high_risk_yaml = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, description: Analyze.}
  - id: writer
    role: {name: Writer, type: worker, description: Write.}
"""
    high_risk_spec = _load(tmp_path, high_risk_yaml)
    # High-coverage candidate predicts 2 fixes but adds a stage (risk 1.0).
    # Low-coverage candidate predicts 1 fix, risk 0.0. With ε=1, the 1-fix
    # deficit is within tolerance → low-risk wins (H4-v2 thesis).
    r_high_cov_high_risk = RefinerResult(
        spec=high_risk_spec, yaml_text="",
        predicted_fixes=["stage_failed:analyst", "output_invalid:writer"])
    r_low_cov_low_risk = RefinerResult(
        spec=low_risk_spec, yaml_text="", predicted_fixes=["stage_failed:analyst"])
    best = _pick_best_proposal([r_high_cov_high_risk, r_low_cov_low_risk], diags, low_risk_spec)
    assert best is r_low_cov_low_risk


def test_pick_best_high_coverage_wins_beyond_epsilon(tmp_path):
    from armature.synthesis.improve import _pick_best_proposal
    from armature.state.diagnostics import DiagnosticResult, DiagnosticCode
    diags = [
        DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="analyst"),
        DiagnosticResult(code=DiagnosticCode.OUTPUT_INVALID, stage_id="writer"),
        DiagnosticResult(code=DiagnosticCode.LOW_CONFIDENCE, stage_id="judge"),
    ]
    low_risk_spec = _load(tmp_path, _MINIMAL_SPEC_YAML)
    high_risk_yaml = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, description: Analyze.}
  - id: writer
    role: {name: Writer, type: worker, description: Write.}
"""
    high_risk_spec = _load(tmp_path, high_risk_yaml)
    # High-coverage predicts all 3 fixes (risk 1.0); low-coverage predicts 1 (risk 0.0).
    # 3 vs 1 → deficit 2 > ε=1 → high-coverage wins despite latency risk.
    r_high = RefinerResult(
        spec=high_risk_spec, yaml_text="",
        predicted_fixes=["stage_failed:analyst", "output_invalid:writer", "low_confidence:judge"])
    r_low = RefinerResult(spec=low_risk_spec, yaml_text="", predicted_fixes=["stage_failed:analyst"])
    best = _pick_best_proposal([r_high, r_low], diags, low_risk_spec)
    assert best is r_high


def test_pick_best_coverage_tiebreak_when_risks_equal(tmp_path):
    from armature.synthesis.improve import _pick_best_proposal
    from armature.state.diagnostics import DiagnosticResult, DiagnosticCode
    diags = [
        DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="analyst"),
        DiagnosticResult(code=DiagnosticCode.OUTPUT_INVALID, stage_id="writer"),
    ]
    spec = _load(tmp_path, _MINIMAL_SPEC_YAML)
    # Both same spec → equal risk 0 → higher coverage wins (regression guard).
    r_low = RefinerResult(spec=spec, yaml_text="", predicted_fixes=["stage_failed:analyst"])
    r_high = RefinerResult(spec=spec, yaml_text="", predicted_fixes=["stage_failed:analyst", "output_invalid:writer"])
    best = _pick_best_proposal([r_low, r_high], diags, spec)
    assert best is r_high


# ── SelfImproveRunner with n_proposals ───────────────────────────────────────

async def test_runner_generates_k_proposals_when_configured(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False, n_proposals=3)

    llm_call_count = 0

    async def mock_llm(**kwargs):
        nonlocal llm_call_count
        llm_call_count += 1
        return _make_llm_response(_REVISED_SPEC_YAML)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        report = await runner.analyze()

    assert report.n_proposals_generated == 3
    assert llm_call_count == 3


# ── _healthy_stage_ids ────────────────────────────────────────────────────────

def test_healthy_stage_ids_excludes_failing_stages():
    from armature.synthesis.improve import _healthy_stage_ids
    from armature.state.diagnostics import DiagnosticResult, DiagnosticCode

    diags = [DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="analyst")]
    traces = [
        make_trace(stage_id="analyst", success=False),
        make_trace(stage_id="writer", success=True),
    ]
    healthy = _healthy_stage_ids(traces, diags)
    assert "writer" in healthy
    assert "analyst" not in healthy


def test_healthy_stage_ids_empty_when_all_failing():
    from armature.synthesis.improve import _healthy_stage_ids
    from armature.state.diagnostics import DiagnosticResult, DiagnosticCode

    diags = [
        DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="a"),
        DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="b"),
    ]
    traces = [make_trace(stage_id="a"), make_trace(stage_id="b")]
    healthy = _healthy_stage_ids(traces, diags)
    assert healthy == set()


# ── _proposal_regression_risk ─────────────────────────────────────────────────

async def test_proposal_regression_risk_false_when_only_failing_stages_touched(tmp_path):
    from armature.synthesis.improve import _proposal_regression_risk
    from armature.spec.loader import load_spec

    old_yaml = _MINIMAL_SPEC_YAML
    new_yaml = _REVISED_SPEC_YAML

    old_file = tmp_path / "old.yaml"
    new_file = tmp_path / "new.yaml"
    old_file.write_text(old_yaml)
    new_file.write_text(new_yaml)
    old_spec = load_spec(old_file)
    new_spec = load_spec(new_file)

    candidate = RefinerResult(spec=new_spec, yaml_text=new_yaml, predicted_fixes=[])
    risk = _proposal_regression_risk(candidate, old_spec, healthy_stage_ids=set())
    assert risk is False


async def test_proposal_regression_risk_true_when_healthy_stage_touched(tmp_path):
    from armature.synthesis.improve import _proposal_regression_risk
    from armature.spec.loader import load_spec

    _TWO_STAGE_OLD = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze topic.
  - id: writer
    depends_on: [analyst]
    role:
      name: Writer
      type: worker
      description: Write findings.
"""
    _TWO_STAGE_NEW = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze topic.
  - id: writer
    depends_on: [analyst]
    role:
      name: Writer
      type: worker
      description: Write findings in a new way.
"""
    old_file = tmp_path / "old.yaml"
    new_file = tmp_path / "new.yaml"
    old_file.write_text(_TWO_STAGE_OLD)
    new_file.write_text(_TWO_STAGE_NEW)
    old_spec = load_spec(old_file)
    new_spec = load_spec(new_file)

    candidate = RefinerResult(spec=new_spec, yaml_text=_TWO_STAGE_NEW, predicted_fixes=[])
    risk = _proposal_regression_risk(candidate, old_spec, healthy_stage_ids={"writer"})
    assert risk is True


# ── gating in SelfImproveRunner ───────────────────────────────────────────────

async def test_runner_filters_regression_risk_proposals(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    _TWO_STAGE = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic and produce findings.
  - id: writer
    depends_on: [analyst]
    role:
      name: Writer
      type: worker
      description: Write the summary.
"""
    spec_file.write_text(_TWO_STAGE)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", stage_id="analyst", output_valid=False, quorum_score=0.20),
        make_trace(run_id="r2", stage_id="analyst", output_valid=False, quorum_score=0.20),
        make_trace(run_id="r3", stage_id="analyst", output_valid=False, quorum_score=0.20),
        make_trace(run_id="r4", stage_id="writer", success=True, output_valid=True),
    ])

    _SAFE_PROPOSAL = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic in depth with explicit confidence scoring.
  - id: writer
    depends_on: [analyst]
    role:
      name: Writer
      type: worker
      description: Write the summary.
"""

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False, n_proposals=1)
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_SAFE_PROPOSAL)
        report = await runner.analyze()

    assert report.regression_risk_count == 0
    assert report.proposed_spec is not None


async def test_runner_records_regression_risk_count(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    _TWO_STAGE = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic and produce findings.
  - id: writer
    depends_on: [analyst]
    role:
      name: Writer
      type: worker
      description: Write the summary.
"""
    spec_file.write_text(_TWO_STAGE)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", stage_id="analyst", output_valid=False, quorum_score=0.20),
        make_trace(run_id="r2", stage_id="analyst", output_valid=False, quorum_score=0.20),
        make_trace(run_id="r3", stage_id="analyst", output_valid=False, quorum_score=0.20),
        make_trace(run_id="r4", stage_id="writer", success=True, output_valid=True),
    ])

    _RISKY_PROPOSAL = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic and produce findings.
  - id: writer
    depends_on: [analyst]
    role:
      name: Writer
      type: worker
      description: Write the summary with extended detail.
"""

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False, n_proposals=1)
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_RISKY_PROPOSAL)
        report = await runner.analyze()

    assert report.regression_risk_count == 1


async def test_runner_fallsback_to_risky_candidates_when_all_risky(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    _TWO_STAGE = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic and produce findings.
  - id: writer
    depends_on: [analyst]
    role:
      name: Writer
      type: worker
      description: Write the summary.
"""
    spec_file.write_text(_TWO_STAGE)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", stage_id="analyst", output_valid=False, quorum_score=0.20),
        make_trace(run_id="r2", stage_id="analyst", output_valid=False, quorum_score=0.20),
        make_trace(run_id="r3", stage_id="analyst", output_valid=False, quorum_score=0.20),
        make_trace(run_id="r4", stage_id="writer", success=True, output_valid=True),
    ])

    # Both proposals touch "writer" (healthy) — all risky, should fallback
    _RISKY_PROPOSAL = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic and produce findings.
  - id: writer
    depends_on: [analyst]
    role:
      name: Writer
      type: worker
      description: Write the summary with extended detail.
"""

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False, n_proposals=2)
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_RISKY_PROPOSAL)
        report = await runner.analyze()

    # All proposals were risky — fallback to full candidates set, still produces a proposal
    assert report.regression_risk_count == 2
    assert report.proposed_spec is not None


# ── editable_surfaces hard gate (#3) ──────────────────────────────────────────

_MODEL_TIERS_BLOCK = """\
model_tiers:
  small:
    provider: openrouter
    model: qwen/qwen3.6-27b
  large:
    provider: openrouter
    model: moonshotai/kimi-k2.6
"""

_BASE_TIER_SPEC = f"""\
name: test-wf
version: "1.0"
{_MODEL_TIERS_BLOCK}stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      model_tier: small
      description: Analyze the topic.
"""

# Revised spec swaps the stage's model_tier (small -> large) — touches model_tiers.
_REVISED_TIER_SPEC = f"""\
name: test-wf
version: "1.0"
{_MODEL_TIERS_BLOCK}stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      model_tier: large
      description: Analyze the topic.
"""

# A spec that explicitly allows model_tiers.
_BASE_TIER_SPEC_ALLOW = f"""\
name: test-wf
version: "1.0"
{_MODEL_TIERS_BLOCK}self_improvement:
  editable_surfaces: [descriptions, model_tiers]
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      model_tier: small
      description: Analyze the topic.
"""


async def _seed_low_hqs(store):
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])


def test_touched_surfaces_detects_description_change(tmp_path):
    from armature.synthesis.improve import _touched_surfaces
    from armature.spec.loader import load_spec
    f = tmp_path / "s.yaml"
    f.write_text(_BASE_TIER_SPEC)
    g = tmp_path / "r.yaml"
    g.write_text(_BASE_TIER_SPEC.replace("Analyze the topic.", "Analyze the topic thoroughly."))
    assert _touched_surfaces(load_spec(f), load_spec(g)) == {"descriptions"}


def test_touched_surfaces_detects_model_tier_change(tmp_path):
    from armature.synthesis.improve import _touched_surfaces
    from armature.spec.loader import load_spec
    f = tmp_path / "s.yaml"; f.write_text(_BASE_TIER_SPEC)
    g = tmp_path / "r.yaml"; g.write_text(_REVISED_TIER_SPEC)
    assert _touched_surfaces(load_spec(f), load_spec(g)) == {"model_tiers"}


def test_touched_surfaces_detects_schema_change(tmp_path):
    from armature.synthesis.improve import _touched_surfaces
    from armature.spec.loader import load_spec
    base = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, description: Analyze.}
"""
    rev = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role: {name: Analyst, type: researcher, description: Analyze.}
    output_schema: {type: object, required: [x]}
"""
    f = tmp_path / "s.yaml"; f.write_text(base)
    g = tmp_path / "r.yaml"; g.write_text(rev)
    assert _touched_surfaces(load_spec(f), load_spec(g)) == {"schemas"}


def test_touched_surfaces_empty_when_unchanged(tmp_path):
    from armature.synthesis.improve import _touched_surfaces
    from armature.spec.loader import load_spec
    f = tmp_path / "s.yaml"; f.write_text(_BASE_TIER_SPEC)
    assert _touched_surfaces(load_spec(f), load_spec(f)) == set()


async def test_locked_surface_rejected_single_proposal(tmp_path):
    """A tier change with MODEL_TIERS locked (default) is rejected: not applied, not pending."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_BASE_TIER_SPEC)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await _seed_low_hqs(store)

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True)
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_TIER_SPEC)
        report = await runner.analyze()

    assert report.applied is False
    assert report.requires_review is False
    assert report.rejected_locked_surfaces == ["model_tiers"]
    assert spec_file.read_text() == _BASE_TIER_SPEC  # spec unchanged
    assert not (spec_file.parent / "wf.pending.yaml").exists()  # no pending file
    assert report.proposed_spec is not None  # proposal still surfaced for inspection


async def test_allowed_surface_applied_single_proposal(tmp_path):
    """A tier change with MODEL_TIERS explicitly allowed is auto-applied."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_BASE_TIER_SPEC_ALLOW)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await _seed_low_hqs(store)

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True)
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_TIER_SPEC)
        report = await runner.analyze()

    assert report.applied is True
    assert report.rejected_locked_surfaces == []
    assert "model_tier: large" in spec_file.read_text()


async def test_locked_surface_rejected_in_multi_proposal(tmp_path):
    """Multi-proposal: a candidate touching a locked surface is dropped before selection."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_BASE_TIER_SPEC)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await _seed_low_hqs(store)

    # Two candidates: one touches model_tiers (locked), one touches only descriptions (allowed).
    clean_candidate = _BASE_TIER_SPEC.replace("Analyze the topic.", "Analyze the topic with care.")
    locked_candidate = _REVISED_TIER_SPEC
    responses = [_make_llm_response(locked_candidate), _make_llm_response(clean_candidate)]

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True, n_proposals=2)
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock, side_effect=responses):
        report = await runner.analyze()

    # The clean (descriptions-only) candidate is applied; the locked one is rejected.
    assert report.applied is True
    assert report.rejected_proposals == 1
    assert report.rejected_locked_surfaces == ["model_tiers"]
    assert "with care" in spec_file.read_text()


async def test_all_proposals_locked_yields_no_application(tmp_path):
    """If every candidate touches a locked surface, nothing is applied and all are rejected."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_BASE_TIER_SPEC)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await _seed_low_hqs(store)

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True, n_proposals=2)
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_TIER_SPEC)
        report = await runner.analyze()

    assert report.applied is False
    assert report.proposed_spec is None
    assert report.rejected_proposals == 2
    assert spec_file.read_text() == _BASE_TIER_SPEC


# ── _build_refiner_suggestions (#2) ──────────────────────────────────────────

def test_build_refiner_suggestions_none_when_no_feedback():
    from armature.synthesis.improve import _build_refiner_suggestions
    assert _build_refiner_suggestions(None, []) is None
    assert _build_refiner_suggestions({}, []) is None


def test_build_refiner_suggestions_includes_missed_predictions():
    from armature.synthesis.improve import _build_refiner_suggestions
    prev = {"missed_predictions": ["output_invalid:analyst"], "unexpected_regressions": [],
            "verified_fixes": [], "drift_score": 0.0}
    s = _build_refiner_suggestions(prev, [])
    assert s is not None
    assert "output_invalid:analyst" in s
    assert "missed" in s.lower() or "still failing" in s.lower()


def test_build_refiner_suggestions_includes_unexpected_regressions():
    from armature.synthesis.improve import _build_refiner_suggestions
    prev = {"missed_predictions": [], "unexpected_regressions": ["low_confidence:judge"],
            "verified_fixes": [], "drift_score": 0.0}
    s = _build_refiner_suggestions(prev, [])
    assert s is not None
    assert "low_confidence:judge" in s
    assert "regress" in s.lower() or "unexpected" in s.lower()


def test_build_refiner_suggestions_flags_high_drift():
    from armature.synthesis.improve import _build_refiner_suggestions
    prev = {"missed_predictions": [], "unexpected_regressions": [], "verified_fixes": [], "drift_score": 0.5}
    s = _build_refiner_suggestions(prev, [])
    assert s is not None
    assert "drift" in s.lower() or "oscillat" in s.lower()


def test_build_refiner_suggestions_includes_post_run_improvement_suggestions():
    from armature.synthesis.improve import _build_refiner_suggestions
    prev = {}
    tr = make_trace(stage_id="self_analyst")
    tr.outputs = {"improvement_suggestions": "Tighten the judge prompt to require evidence."}
    s = _build_refiner_suggestions(prev, [tr])
    assert s is not None
    assert "Tighten the judge prompt" in s


# ── _build_refiner_suggestions optimizer-proposals (#7 reverse) ──────────────

def _opt_proposal(*, accepted: bool, score: float, rationale: str):
    from armature.optimizer.history import ProposalRecord
    return ProposalRecord(
        proposal_id="pid",
        workflow_name="test-wf",
        proposed_diff="--- a\n+++ b\n",
        rationale=rationale,
        confidence=0.8,
        accepted=accepted,
        score=score,
        feedback="ok",
    )


def test_build_refiner_suggestions_includes_optimizer_proposals():
    from armature.synthesis.improve import _build_refiner_suggestions
    props = [
        _opt_proposal(accepted=True, score=0.88, rationale="Add guided_json to analyst."),
        _opt_proposal(accepted=False, score=0.30, rationale="Bump model tier — too costly."),
    ]
    s = _build_refiner_suggestions(None, [], props)
    assert s is not None
    assert "Prior A/B-tested proposals" in s
    assert "armature optimize" in s
    assert "[ACCEPTED score=0.88]" in s
    assert "[REJECTED score=0.30]" in s
    assert "Add guided_json to analyst." in s
    assert "Bump model tier" in s


def test_build_refiner_suggestions_no_optimizer_section_when_empty():
    from armature.synthesis.improve import _build_refiner_suggestions
    # No prior-cycle feedback, no post-run suggestions, no optimizer proposals → None.
    assert _build_refiner_suggestions(None, [], []) is None
    assert _build_refiner_suggestions(None, [], None) is None


async def test_load_optimizer_proposals_absent_when_no_db(tmp_path):
    from armature.synthesis.improve import _load_optimizer_proposals
    db = tmp_path / "missing.db"
    result = await _load_optimizer_proposals(db, "test-wf")
    assert result == []
    # improve must never create the DB file — only optimize writes it.
    assert not db.exists()


async def test_load_optimizer_proposals_absent_when_none(tmp_path):
    from armature.synthesis.improve import _load_optimizer_proposals
    assert await _load_optimizer_proposals(None, "test-wf") == []


async def test_load_optimizer_proposals_reads_store(tmp_path):
    from armature.synthesis.improve import _load_optimizer_proposals
    from armature.optimizer.history import ProposalStore, ProposalRecord
    db = tmp_path / "proposals.db"
    store = ProposalStore(db)
    await store.init()
    await store.record(ProposalRecord(
        proposal_id="p1", workflow_name="test-wf",
        proposed_diff="d1", rationale="r1", confidence=0.7,
        accepted=True, score=0.9, feedback="f1",
    ))
    await store.record(ProposalRecord(
        proposal_id="p2", workflow_name="other-wf",  # different stem → must NOT match
        proposed_diff="d2", rationale="r2", confidence=0.6,
        accepted=False, score=0.2, feedback="f2",
    ))
    result = await _load_optimizer_proposals(db, "test-wf")
    assert len(result) == 1
    assert result[0].workflow_name == "test-wf"
    assert result[0].rationale == "r1"


async def test_analyze_feeds_optimizer_proposals_to_refiner(tmp_path):
    """A seeded ProposalStore flows into the refiner prompt as refiner_suggestions."""
    from armature.optimizer.history import ProposalStore, ProposalRecord
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    proposal_db = tmp_path / "proposals.db"
    store = TraceStore(db)
    await _seed_low_hqs(store)

    # Seed optimize's A/B history for this workflow stem ("wf").
    pstore = ProposalStore(proposal_db)
    await pstore.init()
    await pstore.record(ProposalRecord(
        proposal_id="p1", workflow_name="wf", proposed_diff="d",
        rationale="Switch analyst to guided_json.", confidence=0.8,
        accepted=False, score=0.25, feedback="rejected",
    ))

    runner = SelfImproveRunner(
        spec_file, db, auto_apply=False, log_path=tmp_path / "improve.log.jsonl",
        proposal_db_path=proposal_db,
    )

    captured = {}

    from armature.spec.loader import load_spec
    revised_spec = load_spec(spec_file)

    async def fake_refine(*, spec_yaml, diagnostics, hqs, refiner_suggestions=None,
                          editable_surfaces=None):
        captured["refiner_suggestions"] = refiner_suggestions
        return RefinerResult(spec=revised_spec, yaml_text="", predicted_fixes=[])

    with patch("armature.synthesis.improve.SpecRefiner") as MockRefiner:
        MockRefiner.return_value.refine = fake_refine
        await runner.analyze()

    s = captured.get("refiner_suggestions")
    assert s is not None
    assert "Prior A/B-tested proposals" in s
    assert "Switch analyst to guided_json." in s
    assert "[REJECTED score=0.25]" in s


async def test_analyze_feeds_missed_predictions_back_to_refiner(tmp_path):
    """A prior cycle's missed_predictions appear in the next cycle's refiner prompt."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    log_file = tmp_path / "improve.log.jsonl"
    store = TraceStore(db)
    await _seed_low_hqs(store)

    # Pre-write a prior log entry with a missed prediction.
    prev_entry = {
        "timestamp": "2026-07-20T00:00:00+00:00",
        "workflow_name": "test-wf",
        "n_traces": 3,
        "hqs_before": 0.40,
        "target_hqs": 0.90,
        "needs_improvement": True,
        "applied": True,
        "diagnostics": [],
        "diagnostics_keys": ["output_invalid:analyst"],
        "predicted_fixes": ["output_invalid:analyst"],
        "predicted_regressions": [],
        "verified_fixes": [],
        "missed_predictions": ["output_invalid:analyst"],
        "unexpected_regressions": [],
        "drift_score": 0.0,
        "regression_risk_count": 0,
        "n_proposals_generated": 1,
    }
    log_file.write_text(json.dumps(prev_entry) + "\n")

    runner = SelfImproveRunner(spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False, log_path=log_file)

    captured = []
    async def mock_llm(**kwargs):
        captured.extend(kwargs.get("messages", []))
        return _make_llm_response(_REVISED_SPEC_YAML)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        report = await runner.analyze()

    user_msgs = [m["content"] for m in captured if m.get("role") == "user"]
    assert user_msgs, "expected at least one user message captured"
    assert "output_invalid:analyst" in user_msgs[0]
    assert "missed" in user_msgs[0].lower() or "still failing" in user_msgs[0].lower()


# ── #6: latency-aware selection — integration (RED) ───────────────────────────

_REVISED_DESC_ONLY_WITH_PRED = (
    _REVISED_SPEC_YAML
    + '\n---PREDICTIONS---\n{"predicted_fixes": ["output_invalid:analyst"], "predicted_regressions": []}'
)

_REVISED_NEW_STAGE_WITH_PRED = """\
name: test-wf
version: "1.0"
stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      description: Analyze the topic.
  - id: writer
    role:
      name: Writer
      type: worker
      description: Write the findings.
""" + '\n---PREDICTIONS---\n{"predicted_fixes": ["output_invalid:analyst"], "predicted_regressions": []}'


async def test_multi_proposal_selects_low_latency_candidate(tmp_path):
    """Among equal-coverage candidates, the low-latency-risk one is applied."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])
    runner = SelfImproveRunner(
        spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=True, n_proposals=2,
    )
    responses = [_REVISED_NEW_STAGE_WITH_PRED, _REVISED_DESC_ONLY_WITH_PRED]

    async def mock_llm(**kwargs):
        return _make_llm_response(responses.pop(0) if responses else _REVISED_DESC_ONLY_WITH_PRED)

    with patch("armature.synthesis.improve.llm_completion", side_effect=mock_llm):
        report = await runner.analyze()

    # Both candidates predict the same single fix (equal coverage). The
    # description-only candidate has latency_risk 0; the stage-adding one has
    # risk 1.0. ε=1 → low-risk wins → it is auto-applied (description-only changes
    # need no review). The new-stage candidate must NOT be the one applied.
    assert report.applied is True
    assert spec_file.read_text().strip() == _REVISED_SPEC_YAML.strip()
    assert not (tmp_path / "wf.pending.yaml").exists()
    # The selected candidate's latency_risk surfaces on the report.
    assert report.latency_risk == 0.0


async def test_single_proposal_report_carries_latency_risk(tmp_path):
    """Single-proposal path surfaces the proposal's latency_risk on the report."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(_MINIMAL_SPEC_YAML)
    db = tmp_path / "traces.db"
    store = TraceStore(db)
    await seed_store(store, [
        make_trace(run_id="r1", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r2", quorum_score=0.20, output_valid=False),
        make_trace(run_id="r3", quorum_score=0.20, output_valid=False),
    ])
    runner = SelfImproveRunner(
        spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False,
    )
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_NEW_STAGE_WITH_PRED)
        report = await runner.analyze()

    # The proposal adds a stage → latency_risk 1.0.
    assert report.latency_risk == 1.0


async def test_log_entry_records_latency_risk(tmp_path):
    """_write_log records latency_risk on the cycle entry."""
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
        spec_file, db, target_hqs=0.90, min_traces=1, auto_apply=False, log_path=log_file,
    )
    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(_REVISED_NEW_STAGE_WITH_PRED)
        await runner.analyze()

    lines = [l for l in log_file.read_text().splitlines() if l.strip()]
    entry = json.loads(lines[-1])
    assert entry["latency_risk"] == 1.0


async def test_improvement_report_latency_risk_default():
    """ImprovementReport.latency_risk defaults to 0.0."""
    report = ImprovementReport(
        workflow_name="wf", spec_path=Path("/tmp/wf.yaml"),
        n_traces=1, hqs_before=0.8, needs_improvement=False,
        applied=False, diagnostics=[],
    )
    assert report.latency_risk == 0.0

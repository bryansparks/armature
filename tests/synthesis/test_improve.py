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

def _spec_with_trigger(*, target_hqs=None, min_traces=None):
    from armature.spec.models import HarnessSpec
    si_yaml = ""
    if target_hqs is not None or min_traces is not None:
        parts = ["self_improvement:"]
        if target_hqs is not None:
            parts.append(f"  target_hqs: {target_hqs}")
        if min_traces is not None:
            parts.append(f"  min_traces: {min_traces}")
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
    target, min_t = resolve_trigger_overrides(
        None, None, spec, default_target_hqs=0.90, default_min_traces=3
    )
    assert target == 0.90
    assert min_t == 3


def test_resolve_trigger_overrides_spec_field_wins_over_default():
    spec = _spec_with_trigger(target_hqs=0.95, min_traces=10)
    target, min_t = resolve_trigger_overrides(
        None, None, spec, default_target_hqs=0.90, default_min_traces=3
    )
    assert target == 0.95
    assert min_t == 10


def test_resolve_trigger_overrides_cli_flag_wins_over_spec():
    spec = _spec_with_trigger(target_hqs=0.95, min_traces=10)
    target, min_t = resolve_trigger_overrides(
        0.80, 5, spec, default_target_hqs=0.90, default_min_traces=3
    )
    assert target == 0.80
    assert min_t == 5


def test_resolve_trigger_overrides_spec_partial_override():
    # spec sets only target_hqs; min_traces falls through to default
    spec = _spec_with_trigger(target_hqs=0.75)
    target, min_t = resolve_trigger_overrides(
        None, None, spec, default_target_hqs=0.90, default_min_traces=3
    )
    assert target == 0.75
    assert min_t == 3


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

def test_pick_best_proposal_selects_highest_coverage():
    from armature.synthesis.improve import _pick_best_proposal
    from armature.state.diagnostics import DiagnosticResult, DiagnosticCode
    from unittest.mock import MagicMock

    diags = [
        DiagnosticResult(code=DiagnosticCode.STAGE_FAILED, stage_id="analyst"),
        DiagnosticResult(code=DiagnosticCode.OUTPUT_INVALID, stage_id="writer"),
    ]

    r_low = RefinerResult(spec=MagicMock(), yaml_text="", predicted_fixes=["stage_failed:analyst"])
    r_high = RefinerResult(spec=MagicMock(), yaml_text="", predicted_fixes=["stage_failed:analyst", "output_invalid:writer"])

    best = _pick_best_proposal([r_low, r_high], diags)
    assert best is r_high


def test_pick_best_proposal_returns_none_for_empty_list():
    from armature.synthesis.improve import _pick_best_proposal
    assert _pick_best_proposal([], []) is None


def test_pick_best_proposal_returns_single_when_one_candidate():
    from armature.synthesis.improve import _pick_best_proposal
    from unittest.mock import MagicMock
    r = RefinerResult(spec=MagicMock(), yaml_text="", predicted_fixes=[])
    result = _pick_best_proposal([r], [])
    assert result is r


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

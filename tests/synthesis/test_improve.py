"""Tests for SelfImproveRunner and SpecRefiner."""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from armature.synthesis.improve import SelfImproveRunner, SpecRefiner, ImprovementReport
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
    spec, raw = result
    assert spec.name == "test-wf"


async def test_spec_refiner_strips_invalid_changes():
    """If refiner adds stages (violating constraints), result is still None or valid."""
    refiner = SpecRefiner(model="claude-sonnet-4-6")
    bad_yaml = "not: a: valid: spec: at all"

    with patch("armature.synthesis.improve.llm_completion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _make_llm_response(bad_yaml)
        result = await refiner.refine(spec_yaml=_MINIMAL_SPEC_YAML, diagnostics=[], ihr=None)

    assert result is None


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_llm_response(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp

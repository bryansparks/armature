import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from armature.optimizer.runner import OptimizerRunner, OptimizationResult, ABTestResult
from armature.state.traces import HqsResult, TraceRecord

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _trace(*, run_id, stage_id="worker1", role_type="worker", success=True,
           output_valid=True, quorum_score=None, escalation_count=0,
           error_type=None):
    return TraceRecord(
        run_id=run_id, workflow_name="echo-workflow", stage_id=stage_id,
        role_type=role_type, model="claude-sonnet", success=success,
        output_valid=output_valid, quorum_score=quorum_score,
        escalation_count=escalation_count, error_type=error_type,
        inputs={}, outputs={"brief": "x"},
    )


def make_mock_harness_result(accept: bool = True):
    return {
        "analyze_traces": {
            "top_failure": "JSON parse errors",
            "failure_count": 5,
            "affected_stage": "worker1",
        },
        "propose_fix": {
            "proposed_diff": "- output_mode: text\n+ output_mode: guided_json",
            "rationale": "Add guided JSON",
            "confidence": 0.85,
        },
        "evaluate_proposal": {
            "accept": accept,
            "score": 0.88 if accept else 0.3,
            "feedback": "Good change" if accept else "Too risky",
        },
    }


async def test_optimizer_returns_result(tmp_path):
    runner = OptimizerRunner(
        target_spec_path=FIXTURES / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    mock_traces = [object()] * 5  # 5 placeholder items
    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = make_mock_harness_result(accept=True)
            result = await runner.optimize()
    assert isinstance(result, OptimizationResult)
    assert result.accepted is True
    assert result.confidence == pytest.approx(0.85)


async def test_optimizer_returns_rejected_result(tmp_path):
    runner = OptimizerRunner(
        target_spec_path=FIXTURES / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    mock_traces = [object()] * 5  # 5 placeholder items
    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = make_mock_harness_result(accept=False)
            result = await runner.optimize()
    assert result.accepted is False


async def test_optimizer_no_traces_returns_none(tmp_path):
    runner = OptimizerRunner(
        target_spec_path=FIXTURES / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",  # empty DB, never init'd
    )
    result = await runner.optimize()
    assert result is None  # Not enough trace data


def make_hqs(run_id: str, hqs: float) -> HqsResult:
    return HqsResult(
        run_id=run_id,
        hqs=hqs,
        output_valid_rate=1.0,
        success_rate=1.0,
        avg_quorum_score=hqs,
        latency_score=1.0,
        n_traces=3,
    )


async def test_ab_test_proposed_wins(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    # original scores ~0.70, proposed ~0.90
    original_hqss = [make_hqs(f"r{i}", 0.70) for i in range(3)]
    proposed_hqss = [make_hqs(f"p{i}", 0.90) for i in range(3)]
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.side_effect = original_hqss + proposed_hqss
        result = await runner.a_b_test(
            proposed_spec_path=fixtures / "echo-workflow.yaml",
            inputs_sample=[{"x": 1}, {"x": 2}, {"x": 3}],
            n_runs=1,
        )
    assert isinstance(result, ABTestResult)
    assert result.winner == "proposed"
    assert result.delta == pytest.approx(0.20, abs=1e-6)


async def test_ab_test_tie(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    all_hqss = [make_hqs(f"r{i}", 0.80) for i in range(6)]
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.side_effect = all_hqss
        result = await runner.a_b_test(
            proposed_spec_path=fixtures / "echo-workflow.yaml",
            inputs_sample=[{"x": 1}, {"x": 2}, {"x": 3}],
            n_runs=1,
        )
    assert result.winner == "tie"


async def test_ab_test_original_wins(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    original_hqss = [make_hqs(f"r{i}", 0.85) for i in range(3)]
    proposed_hqss = [make_hqs(f"p{i}", 0.60) for i in range(3)]
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.side_effect = original_hqss + proposed_hqss
        result = await runner.a_b_test(
            proposed_spec_path=fixtures / "echo-workflow.yaml",
            inputs_sample=[{"x": 1}, {"x": 2}, {"x": 3}],
            n_runs=1,
        )
    assert result.winner == "original"
    assert result.delta == pytest.approx(-0.25, abs=1e-6)


async def test_ab_test_all_none_scores_returns_tie(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.return_value = None  # all calls return None
        result = await runner.a_b_test(
            proposed_spec_path=fixtures / "echo-workflow.yaml",
            inputs_sample=[{"x": 1}, {"x": 2}],
            n_runs=1,
        )
    assert result.winner == "tie"
    assert result.delta == pytest.approx(0.0, abs=1e-6)
    assert result.original_hqs == pytest.approx(0.0, abs=1e-6)
    assert result.proposed_hqs == pytest.approx(0.0, abs=1e-6)


async def test_ab_test_empty_inputs_returns_tie(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        result = await runner.a_b_test(
            proposed_spec_path=fixtures / "echo-workflow.yaml",
            inputs_sample=[],
            n_runs=5,
        )
    assert result.winner == "tie"
    assert result.n_inputs == 0
    mock_score.assert_not_called()


async def test_metric_fn_scores_passed_to_workflow(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        metric_fn=lambda outputs: float(outputs.get("confidence", 0.0)),
    )
    from armature.state.traces import TraceRecord
    fake_traces = [
        TraceRecord(
            run_id=f"r{i}", workflow_name="echo-workflow", stage_id="s",
            role_type="worker", model="m", latency_ms=100.0,
            success=True, output_valid=True,
            outputs={"confidence": 0.8 + i * 0.05},
        )
        for i in range(5)
    ]
    captured_inputs: list[dict] = []

    async def capture_workflow(inputs):
        captured_inputs.append(inputs)
        return make_mock_harness_result(accept=True)

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=fake_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          side_effect=capture_workflow):
            await runner.optimize()

    assert len(captured_inputs) == 1
    ctx = captured_inputs[0]
    assert "metric_mean" in ctx
    assert ctx["metric_mean"] == pytest.approx(0.9, abs=1e-6)  # (0.80+0.85+0.90+0.95+1.00)/5
    assert "metric_scores_json" in ctx
    scores = json.loads(ctx["metric_scores_json"])
    assert len(scores) == 5
    assert scores == pytest.approx([0.80, 0.85, 0.90, 0.95, 1.00], abs=1e-6)


async def test_metric_fn_none_omits_keys(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        # no metric_fn
    )
    mock_traces = [object()] * 5
    captured_inputs: list[dict] = []

    async def capture_workflow(inputs):
        captured_inputs.append(inputs)
        return make_mock_harness_result(accept=True)

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          side_effect=capture_workflow):
            await runner.optimize()

    ctx = captured_inputs[0]
    assert "metric_mean" not in ctx
    assert "metric_scores_json" not in ctx


async def test_metric_fn_exception_does_not_crash(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"

    def bad_metric(outputs):
        raise ValueError("metric blew up")

    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        metric_fn=bad_metric,
    )
    from armature.state.traces import TraceRecord
    fake_traces = [
        TraceRecord(
            run_id="r0", workflow_name="echo-workflow", stage_id="s",
            role_type="worker", model="m", latency_ms=100.0,
            success=True, output_valid=True,
        )
        for _ in range(5)
    ]
    captured_inputs: list[dict] = []

    async def capture_workflow(inputs):
        captured_inputs.append(inputs)
        return make_mock_harness_result(accept=True)

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=fake_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          side_effect=capture_workflow):
            result = await runner.optimize()

    assert result is not None
    assert len(captured_inputs) == 1
    ctx = captured_inputs[0]
    assert "metric_mean" not in ctx
    assert "metric_scores_json" not in ctx


import json as _json
from armature.state.improvement_store import ImprovementRecord, ImprovementStore


async def test_optimize_injects_unified_improvement_history(tmp_path):
    """optimize() feeds both engines' records as one improvement_history_json."""
    fixtures = Path(__file__).parent.parent / "fixtures"
    # echo-workflow.yaml fixture's stem is "echo-workflow"
    improvement_db = tmp_path / "improvements.db"

    store = ImprovementStore(improvement_db)
    await store.init()
    # A prior optimize record (A/B-rejected) + a prior improve record (verified fix).
    await store.record(ImprovementRecord(
        record_id="opt1", workflow_stem="echo-workflow", source="optimize",
        proposed_diff="- model: small\n+ model: medium", rationale="Improve quality",
        confidence=0.6, accepted=False, score=0.3, feedback="Introduced regression",
    ))
    await store.record(ImprovementRecord(
        record_id="imp1", workflow_stem="echo-workflow", source="improve",
        verified_fixes=["output_invalid:analyst"], missed_predictions=["stage_failed:writer"],
        applied=True, hqs_before=0.6, drift_score=0.0,
    ))

    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        improvement_db_path=improvement_db,
    )
    mock_traces = [object()] * 5
    captured_inputs: list[dict] = []

    async def capture_workflow(inputs):
        captured_inputs.append(inputs)
        return make_mock_harness_result(accept=True)

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          side_effect=capture_workflow):
            await runner.optimize()

    ctx = captured_inputs[0]
    assert "improvement_history_json" in ctx
    history = _json.loads(ctx["improvement_history_json"])
    assert len(history) == 2
    sources = {h["source"] for h in history}
    assert sources == {"optimize", "improve"}
    # Most recent first — imp1 recorded after opt1
    assert history[0]["record_id"] == "imp1"
    assert history[1]["record_id"] == "opt1"
    # Both engines' field sets are present
    assert history[0]["verified_fixes"] == ["output_invalid:analyst"]
    assert history[1]["accepted"] is False
    assert history[1]["proposed_diff"] == "- model: small\n+ model: medium"
    # The two legacy injection keys are gone — one unified key only.
    assert "proposal_history_json" not in ctx
    assert "improve_history_json" not in ctx


async def test_optimize_records_result_to_improvement_store(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    improvement_db = tmp_path / "improvements.db"

    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        improvement_db_path=improvement_db,
    )
    mock_traces = [object()] * 5

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          return_value=make_mock_harness_result(accept=True)):
            result = await runner.optimize()

    assert result is not None
    store = ImprovementStore(improvement_db)
    history = await store.load_history("echo-workflow")
    assert len(history) == 1
    assert history[0].source == "optimize"
    assert history[0].accepted is True
    assert history[0].proposed_diff == result.proposed_diff


async def test_optimize_no_improvement_db_still_works(tmp_path):
    """improvement_db_path is optional — existing behavior unchanged."""
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        # no improvement_db_path
    )
    mock_traces = [object()] * 5
    captured_inputs: list[dict] = []

    async def capture_workflow(inputs):
        captured_inputs.append(inputs)
        return make_mock_harness_result(accept=True)

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          side_effect=capture_workflow):
            result = await runner.optimize()

    assert result is not None
    assert "improvement_history_json" not in captured_inputs[0]


from armature.optimizer.runner import LoopResult


async def test_run_loop_runs_n_iterations(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        improvement_db_path=tmp_path / "improvements.db",
    )
    call_count = 0

    async def mock_optimize():
        nonlocal call_count
        call_count += 1
        return OptimizationResult(
            accepted=True,
            proposed_diff=f"diff-{call_count}",
            rationale="test",
            confidence=0.8,
            score=0.8,
            feedback="ok",
        )

    with patch.object(runner, "optimize", new_callable=AsyncMock, side_effect=mock_optimize):
        loop_result = await runner.run_loop(n_iterations=3)

    assert call_count == 3
    assert isinstance(loop_result, LoopResult)
    assert len(loop_result.iterations) == 3
    assert loop_result.accepted_count == 3


async def test_run_loop_stops_early_on_none(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    results = [
        OptimizationResult(accepted=True, proposed_diff="d", rationale="r",
                           confidence=0.8, score=0.8, feedback="ok"),
        None,   # not enough traces on second pass — stop
        OptimizationResult(accepted=True, proposed_diff="d2", rationale="r",
                           confidence=0.8, score=0.8, feedback="ok"),
    ]
    with patch.object(runner, "optimize", new_callable=AsyncMock, side_effect=results):
        loop_result = await runner.run_loop(n_iterations=3)

    assert len(loop_result.iterations) == 1   # stopped after None; None itself not appended
    assert loop_result.accepted_count == 1


async def test_run_loop_zero_iterations(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    with patch.object(runner, "optimize", new_callable=AsyncMock) as mock_opt:
        loop_result = await runner.run_loop(n_iterations=0)
    mock_opt.assert_not_called()
    assert loop_result.iterations == []
    assert loop_result.accepted_count == 0


# ---------------------------------------------------------------------------
# apply_diff tests
# ---------------------------------------------------------------------------

import shutil


def test_apply_diff_patches_file(tmp_path):
    spec = tmp_path / "workflow.yaml"
    spec.write_text("name: original\nstages: []\n")

    diff = (
        "--- original\n"
        "+++ proposed\n"
        "@@ -1,2 +1,2 @@\n"
        "-name: original\n"
        "+name: updated\n"
        " stages: []\n"
    )

    ok, msg = OptimizerRunner.apply_diff(spec, diff)
    assert ok, f"apply_diff failed: {msg}"
    assert "name: updated" in spec.read_text()
    assert (tmp_path / "workflow.yaml.orig").exists()


def test_apply_diff_bad_diff_returns_false(tmp_path):
    spec = tmp_path / "workflow.yaml"
    spec.write_text("name: original\nstages: []\n")

    ok, msg = OptimizerRunner.apply_diff(spec, "this is not a valid diff")
    assert not ok
    assert spec.read_text() == "name: original\nstages: []\n"  # unchanged


def test_apply_diff_no_patch_binary_returns_false(tmp_path):
    spec = tmp_path / "workflow.yaml"
    spec.write_text("name: x\n")

    with patch("shutil.which", return_value=None):
        ok, msg = OptimizerRunner.apply_diff(spec, "--- a\n+++ b\n")
    assert not ok
    assert "patch" in msg.lower()


async def test_run_loop_auto_apply_calls_apply_diff(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    accepted_result = OptimizationResult(
        accepted=True, proposed_diff="diff-text",
        rationale="r", confidence=0.8, score=0.8, feedback="ok",
    )

    with patch.object(runner, "optimize", new_callable=AsyncMock, return_value=accepted_result):
        with patch.object(OptimizerRunner, "apply_diff", return_value=(True, "Applied")) as mock_apply:
            loop_result = await runner.run_loop(n_iterations=1, auto_apply=True)

    mock_apply.assert_called_once_with(runner._target_spec_path, "diff-text")
    assert "Applied" in loop_result.iterations[0].feedback


async def test_run_loop_no_auto_apply_skips_apply_diff(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    accepted_result = OptimizationResult(
        accepted=True, proposed_diff="diff-text",
        rationale="r", confidence=0.8, score=0.8, feedback="ok",
    )

    with patch.object(runner, "optimize", new_callable=AsyncMock, return_value=accepted_result):
        with patch.object(OptimizerRunner, "apply_diff") as mock_apply:
            await runner.run_loop(n_iterations=1, auto_apply=False)

    mock_apply.assert_not_called()


# ── #7-A: shared diagnosis (RED) ──────────────────────────────────────────────


async def test_optimizer_injects_diagnostics_json(tmp_path):
    """optimize() feeds DiagnosticAnalyzer codes into the meta-workflow as diagnostics_json."""
    runner = OptimizerRunner(
        target_spec_path=FIXTURES / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    # 5 traces, some failing → stage_failed + output_invalid diagnostics.
    mock_traces = [
        _trace(run_id="r1", success=False, error_type="RuntimeError", output_valid=False),
        _trace(run_id="r2", success=False, error_type="RuntimeError", output_valid=False),
        _trace(run_id="r3", success=True, output_valid=True, quorum_score=0.95),
        _trace(run_id="r4", success=True, output_valid=True, quorum_score=0.95),
        _trace(run_id="r5", success=True, output_valid=True, quorum_score=0.95),
    ]
    captured: dict = {}
    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = make_mock_harness_result(accept=True)

            async def capture(inputs):
                captured["inputs"] = inputs
                return make_mock_harness_result(accept=True)
            mock_run.side_effect = capture
            await runner.optimize()

    inputs = captured["inputs"]
    assert "diagnostics_json" in inputs
    diags = json.loads(inputs["diagnostics_json"])
    assert isinstance(diags, list)
    codes = {d["code"] for d in diags}
    assert "stage_failed" in codes
    assert "output_invalid" in codes
    # Each entry carries the structured fields improve uses.
    assert all("stage_id" in d and "details" in d for d in diags)


async def test_optimizer_diagnostics_json_empty_when_clean_traces(tmp_path):
    """All-clean traces → diagnostics_json is an empty list (still injected)."""
    runner = OptimizerRunner(
        target_spec_path=FIXTURES / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    mock_traces = [_trace(run_id=f"r{i}", quorum_score=0.95) for i in range(5)]
    captured: dict = {}
    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock) as mock_run:
            async def capture(inputs):
                captured["inputs"] = inputs
                return make_mock_harness_result(accept=True)
            mock_run.side_effect = capture
            await runner.optimize()

    assert json.loads(captured["inputs"]["diagnostics_json"]) == []


async def test_optimizer_diagnostics_failure_does_not_block(tmp_path):
    """If DiagnosticAnalyzer raises, optimize() still returns a result (advisory)."""
    runner = OptimizerRunner(
        target_spec_path=FIXTURES / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    mock_traces = [_trace(run_id=f"r{i}", quorum_score=0.95) for i in range(5)]
    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch("armature.optimizer.runner.DiagnosticAnalyzer") as mock_diag_cls:
            mock_diag_cls.return_value.analyze.side_effect = RuntimeError("boom")
            with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = make_mock_harness_result(accept=True)
                result = await runner.optimize()

    assert isinstance(result, OptimizationResult)
    assert result.accepted is True
    # diagnostics_json is simply absent — optimization is not blocked.
    inputs = mock_run.call_args.args[0] if mock_run.call_args.args else mock_run.call_args[0][0]
    assert "diagnostics_json" not in inputs

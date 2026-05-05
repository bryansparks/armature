import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from armature.optimizer.runner import OptimizerRunner, OptimizationResult, ABTestResult
from armature.state.traces import IhrResult

FIXTURES = Path(__file__).parent.parent / "fixtures"


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


def make_ihr(run_id: str, ihr: float) -> IhrResult:
    return IhrResult(
        run_id=run_id,
        ihr=ihr,
        output_valid_rate=1.0,
        success_rate=1.0,
        avg_quorum_score=ihr,
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
    original_ihrs = [make_ihr(f"r{i}", 0.70) for i in range(3)]
    proposed_ihrs = [make_ihr(f"p{i}", 0.90) for i in range(3)]
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.side_effect = original_ihrs + proposed_ihrs
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
    all_ihrs = [make_ihr(f"r{i}", 0.80) for i in range(6)]
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.side_effect = all_ihrs
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
    original_ihrs = [make_ihr(f"r{i}", 0.85) for i in range(3)]
    proposed_ihrs = [make_ihr(f"p{i}", 0.60) for i in range(3)]
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.side_effect = original_ihrs + proposed_ihrs
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
    assert result.original_ihr == pytest.approx(0.0, abs=1e-6)
    assert result.proposed_ihr == pytest.approx(0.0, abs=1e-6)


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
from armature.optimizer.history import ProposalRecord, ProposalStore


async def test_optimize_injects_proposal_history(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    proposal_db = tmp_path / "proposals.db"

    # Pre-populate history with two prior proposals
    store = ProposalStore(proposal_db)
    await store.init()
    await store.record(ProposalRecord(
        proposal_id="old1", workflow_name="echo-workflow",
        proposed_diff="- text\n+ guided_json", rationale="Fix parse errors",
        confidence=0.9, accepted=True, score=0.88, feedback="Improved output validity",
    ))
    await store.record(ProposalRecord(
        proposal_id="old2", workflow_name="echo-workflow",
        proposed_diff="- model: small\n+ model: medium", rationale="Improve quality",
        confidence=0.6, accepted=False, score=0.3, feedback="Introduced regression",
    ))

    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        proposal_db_path=proposal_db,
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
    assert "proposal_history_json" in ctx
    history = _json.loads(ctx["proposal_history_json"])
    assert len(history) == 2
    # Most recent first — old2 was recorded after old1
    assert history[0]["proposal_id"] == "old2"
    assert history[1]["proposal_id"] == "old1"


async def test_optimize_records_result_to_proposal_store(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    proposal_db = tmp_path / "proposals.db"

    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        proposal_db_path=proposal_db,
    )
    mock_traces = [object()] * 5

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          return_value=make_mock_harness_result(accept=True)):
            result = await runner.optimize()

    assert result is not None
    store = ProposalStore(proposal_db)
    history = await store.load_history("echo-workflow")
    assert len(history) == 1
    assert history[0].accepted is True
    assert history[0].proposed_diff == result.proposed_diff


async def test_optimize_no_proposal_db_still_works(tmp_path):
    """proposal_db_path is optional — existing behavior unchanged."""
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        # no proposal_db_path
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
    assert "proposal_history_json" not in captured_inputs[0]

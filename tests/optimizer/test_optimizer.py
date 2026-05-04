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

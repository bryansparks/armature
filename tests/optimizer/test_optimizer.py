import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from armature.optimizer.runner import OptimizerRunner, OptimizationResult

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

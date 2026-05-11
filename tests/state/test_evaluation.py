"""Tests for EvaluationRunner and EvaluationStore.

EvaluationStore persists per-stage quality scores.
EvaluationRunner scores stage outputs against declarative criteria using an LLM.
Stage.evaluate carries the criteria list from the spec.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from armature.spec.models import HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig
from armature.state.traces import TraceStore, TraceRecord


def _make_spec(stages):
    return HarnessSpec(name="wf", stages=stages, validate=False)


def _make_tiers():
    return ModelTiers(small=ModelTierConfig(provider="anthropic", model="claude-haiku-4-5-20251001"))


async def _seed_trace(trace_store: TraceStore, stage_id: str, run_id: str = "r1", outputs: dict = None):
    await trace_store.init()
    await trace_store.record(TraceRecord(
        run_id=run_id, workflow_name="wf", stage_id=stage_id,
        role_type="worker", model="m", latency_ms=100,
        success=True, output_valid=True, quorum_score=0.9,
        inputs={"x": "input"}, outputs=outputs or {"result": "some output"},
    ))


# ── Stage.evaluate spec field ─────────────────────────────────────────────────

def test_stage_evaluate_defaults_to_empty():
    """Stage.evaluate defaults to an empty list."""
    from armature.spec.models import ToolCallConfig
    stage = Stage(id="s", tool_call=ToolCallConfig(name="t"))
    assert stage.evaluate == []


def test_stage_with_evaluate_criteria():
    """Stage accepts a non-empty evaluate list."""
    from armature.spec.models import ToolCallConfig
    stage = Stage(
        id="s",
        tool_call=ToolCallConfig(name="t"),
        evaluate=["output is valid JSON", "confidence score is present"],
    )
    assert len(stage.evaluate) == 2


def test_stage_evaluate_in_harness_spec():
    """HarnessSpec with evaluate criteria passes validation."""
    from armature.spec.models import ToolCallConfig
    spec = _make_spec([
        Stage(id="worker", tool_call=ToolCallConfig(name="t"),
              evaluate=["output contains a summary"]),
    ])
    from armature.spec.validator import validate_spec
    errors = validate_spec(spec, strict=False)
    assert not any(e.code == "NO_EXECUTION_TYPE" for e in errors)


# ── EvaluationStore ───────────────────────────────────────────────────────────

async def test_evaluation_store_record_and_load(tmp_path):
    """record() stores an EvaluationResult; load_for_run() retrieves it."""
    from armature.state.evaluator import EvaluationStore, EvaluationResult

    store = EvaluationStore(tmp_path / "evaluations.db")
    await store.init()

    result = EvaluationResult(
        run_id="r1",
        workflow_name="wf",
        stage_id="judge",
        score=0.85,
        criteria_passed=["output is valid JSON"],
        criteria_failed=[],
        notes="All criteria met.",
    )
    await store.record(result)

    loaded = await store.load_for_run("r1")
    assert len(loaded) == 1
    assert loaded[0].stage_id == "judge"
    assert loaded[0].score == pytest.approx(0.85)


async def test_evaluation_store_load_filters_by_run(tmp_path):
    """load_for_run() only returns results for the requested run_id."""
    from armature.state.evaluator import EvaluationStore, EvaluationResult

    store = EvaluationStore(tmp_path / "evaluations.db")
    await store.init()

    for run_id in ["r1", "r2"]:
        await store.record(EvaluationResult(
            run_id=run_id, workflow_name="wf", stage_id="worker",
            score=0.9, criteria_passed=["ok"], criteria_failed=[], notes="",
        ))

    loaded = await store.load_for_run("r1")
    assert len(loaded) == 1
    assert loaded[0].run_id == "r1"


async def test_evaluation_store_empty_returns_empty(tmp_path):
    """No results stored → empty list, no error."""
    from armature.state.evaluator import EvaluationStore

    store = EvaluationStore(tmp_path / "evaluations.db")
    await store.init()

    assert await store.load_for_run("nonexistent") == []


# ── EvaluationRunner ──────────────────────────────────────────────────────────

_EVAL_LLM_RESPONSE = """{
  "criteria": [
    {"criterion": "output contains a decision", "passed": true, "reason": "decision field present"},
    {"criterion": "confidence is above 0.7", "passed": false, "reason": "confidence was 0.5"}
  ],
  "score": 0.5,
  "notes": "One criterion failed."
}"""


async def test_evaluation_runner_skips_stages_without_criteria(tmp_path):
    """Stages with empty evaluate list produce no EvaluationResult."""
    from armature.state.evaluator import EvaluationRunner, EvaluationStore
    from armature.spec.models import ToolCallConfig

    spec = _make_spec([
        Stage(id="worker", tool_call=ToolCallConfig(name="t")),  # no evaluate
    ])
    trace_store = TraceStore(tmp_path / "traces.db")
    await _seed_trace(trace_store, "worker")

    eval_store = EvaluationStore(tmp_path / "evaluations.db")
    await eval_store.init()

    runner = EvaluationRunner(model="claude-haiku-4-5-20251001", evaluation_store=eval_store)

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = _EVAL_LLM_RESPONSE
        return resp

    with patch("armature.state.evaluator.litellm_completion", side_effect=mock_completion):
        results = await runner.evaluate_run(run_id="r1", spec=spec, trace_store=trace_store)

    assert results == []


async def test_evaluation_runner_scores_stage_with_criteria(tmp_path):
    """evaluate_run() returns one EvaluationResult per stage that has criteria."""
    from armature.state.evaluator import EvaluationRunner, EvaluationStore
    from armature.spec.models import ToolCallConfig

    spec = _make_spec([
        Stage(id="worker", tool_call=ToolCallConfig(name="t"),
              evaluate=["output contains a decision", "confidence is above 0.7"]),
    ])
    trace_store = TraceStore(tmp_path / "traces.db")
    await _seed_trace(trace_store, "worker")

    eval_store = EvaluationStore(tmp_path / "evaluations.db")
    await eval_store.init()

    runner = EvaluationRunner(model="claude-haiku-4-5-20251001", evaluation_store=eval_store)

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = _EVAL_LLM_RESPONSE
        return resp

    with patch("armature.state.evaluator.litellm_completion", side_effect=mock_completion):
        results = await runner.evaluate_run(run_id="r1", spec=spec, trace_store=trace_store)

    assert len(results) == 1
    assert results[0].stage_id == "worker"
    assert results[0].score == pytest.approx(0.5)
    assert "output contains a decision" in results[0].criteria_passed
    assert "confidence is above 0.7" in results[0].criteria_failed


async def test_evaluation_runner_passes_criteria_to_llm(tmp_path):
    """The LLM prompt contains the stage's evaluate criteria."""
    from armature.state.evaluator import EvaluationRunner, EvaluationStore
    from armature.spec.models import ToolCallConfig

    spec = _make_spec([
        Stage(id="worker", tool_call=ToolCallConfig(name="t"),
              evaluate=["output is grammatically correct"]),
    ])
    trace_store = TraceStore(tmp_path / "traces.db")
    await _seed_trace(trace_store, "worker")

    eval_store = EvaluationStore(tmp_path / "evaluations.db")
    await eval_store.init()

    captured = {}
    runner = EvaluationRunner(model="claude-haiku-4-5-20251001", evaluation_store=eval_store)

    async def mock_completion(**kwargs):
        captured["messages"] = kwargs.get("messages", [])
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = '{"criteria": [{"criterion": "output is grammatically correct", "passed": true, "reason": "ok"}], "score": 1.0, "notes": ""}'
        return resp

    with patch("armature.state.evaluator.litellm_completion", side_effect=mock_completion):
        await runner.evaluate_run(run_id="r1", spec=spec, trace_store=trace_store)

    prompt_text = " ".join(m["content"] for m in captured["messages"])
    assert "grammatically correct" in prompt_text


async def test_evaluation_runner_passes_stage_output_to_llm(tmp_path):
    """The LLM prompt contains the stage's actual output."""
    from armature.state.evaluator import EvaluationRunner, EvaluationStore
    from armature.spec.models import ToolCallConfig

    spec = _make_spec([
        Stage(id="worker", tool_call=ToolCallConfig(name="t"),
              evaluate=["output has a result"]),
    ])
    trace_store = TraceStore(tmp_path / "traces.db")
    await _seed_trace(trace_store, "worker", outputs={"decision": "approved", "confidence": 0.95})

    eval_store = EvaluationStore(tmp_path / "evaluations.db")
    await eval_store.init()

    captured = {}
    runner = EvaluationRunner(model="claude-haiku-4-5-20251001", evaluation_store=eval_store)

    async def mock_completion(**kwargs):
        captured["messages"] = kwargs.get("messages", [])
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = '{"criteria": [{"criterion": "output has a result", "passed": true, "reason": "ok"}], "score": 1.0, "notes": ""}'
        return resp

    with patch("armature.state.evaluator.litellm_completion", side_effect=mock_completion):
        await runner.evaluate_run(run_id="r1", spec=spec, trace_store=trace_store)

    prompt_text = " ".join(m["content"] for m in captured["messages"])
    assert "approved" in prompt_text or "decision" in prompt_text


async def test_evaluation_runner_stores_results_in_store(tmp_path):
    """evaluate_run() persists EvaluationResult to EvaluationStore."""
    from armature.state.evaluator import EvaluationRunner, EvaluationStore
    from armature.spec.models import ToolCallConfig

    spec = _make_spec([
        Stage(id="worker", tool_call=ToolCallConfig(name="t"),
              evaluate=["output is present"]),
    ])
    trace_store = TraceStore(tmp_path / "traces.db")
    await _seed_trace(trace_store, "worker")

    eval_store = EvaluationStore(tmp_path / "evaluations.db")
    await eval_store.init()

    runner = EvaluationRunner(model="claude-haiku-4-5-20251001", evaluation_store=eval_store)

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = '{"criteria": [{"criterion": "output is present", "passed": true, "reason": "ok"}], "score": 1.0, "notes": ""}'
        return resp

    with patch("armature.state.evaluator.litellm_completion", side_effect=mock_completion):
        await runner.evaluate_run(run_id="r1", spec=spec, trace_store=trace_store)

    stored = await eval_store.load_for_run("r1")
    assert len(stored) == 1
    assert stored[0].score == pytest.approx(1.0)


async def test_evaluation_runner_handles_llm_failure_gracefully(tmp_path):
    """LLM failure → EvaluationResult with score 0.0, no exception raised."""
    from armature.state.evaluator import EvaluationRunner, EvaluationStore
    from armature.spec.models import ToolCallConfig

    spec = _make_spec([
        Stage(id="worker", tool_call=ToolCallConfig(name="t"),
              evaluate=["output is valid"]),
    ])
    trace_store = TraceStore(tmp_path / "traces.db")
    await _seed_trace(trace_store, "worker")

    eval_store = EvaluationStore(tmp_path / "evaluations.db")
    await eval_store.init()

    runner = EvaluationRunner(model="claude-haiku-4-5-20251001", evaluation_store=eval_store)

    async def mock_completion(**kwargs):
        raise RuntimeError("API unavailable")

    with patch("armature.state.evaluator.litellm_completion", side_effect=mock_completion):
        results = await runner.evaluate_run(run_id="r1", spec=spec, trace_store=trace_store)

    assert len(results) == 1
    assert results[0].score == pytest.approx(0.0)
    assert results[0].stage_id == "worker"


async def test_evaluation_runner_handles_no_trace_for_stage(tmp_path):
    """Stage has evaluate criteria but no trace was recorded — skipped, no error."""
    from armature.state.evaluator import EvaluationRunner, EvaluationStore
    from armature.spec.models import ToolCallConfig

    spec = _make_spec([
        Stage(id="worker", tool_call=ToolCallConfig(name="t"),
              evaluate=["output is valid"]),
    ])
    trace_store = TraceStore(tmp_path / "traces.db")
    await trace_store.init()  # no trace recorded

    eval_store = EvaluationStore(tmp_path / "evaluations.db")
    await eval_store.init()

    runner = EvaluationRunner(model="claude-haiku-4-5-20251001", evaluation_store=eval_store)

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = '{"criteria": [], "score": 1.0, "notes": ""}'
        return resp

    with patch("armature.state.evaluator.litellm_completion", side_effect=mock_completion):
        results = await runner.evaluate_run(run_id="r1", spec=spec, trace_store=trace_store)

    assert results == []


async def test_evaluation_runner_multiple_stages(tmp_path):
    """Multiple stages with criteria produce one result each."""
    from armature.state.evaluator import EvaluationRunner, EvaluationStore
    from armature.spec.models import ToolCallConfig

    spec = _make_spec([
        Stage(id="research", tool_call=ToolCallConfig(name="t"),
              evaluate=["brief is substantive"]),
        Stage(id="judge", tool_call=ToolCallConfig(name="t"), depends_on=["research"],
              evaluate=["decision is one of approve or reject"]),
    ])
    trace_store = TraceStore(tmp_path / "traces.db")
    await _seed_trace(trace_store, "research", outputs={"brief": "CO2 is rising"})
    await _seed_trace(trace_store, "judge", outputs={"decision": "approve"})

    eval_store = EvaluationStore(tmp_path / "evaluations.db")
    await eval_store.init()

    runner = EvaluationRunner(model="claude-haiku-4-5-20251001", evaluation_store=eval_store)
    call_count = [0]

    async def mock_completion(**kwargs):
        call_count[0] += 1
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = '{"criteria": [{"criterion": "x", "passed": true, "reason": "ok"}], "score": 1.0, "notes": ""}'
        return resp

    with patch("armature.state.evaluator.litellm_completion", side_effect=mock_completion):
        results = await runner.evaluate_run(run_id="r1", spec=spec, trace_store=trace_store)

    assert len(results) == 2
    stage_ids = {r.stage_id for r in results}
    assert stage_ids == {"research", "judge"}
    assert call_count[0] == 2  # one LLM call per stage

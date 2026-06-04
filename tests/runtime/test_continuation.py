"""Tests for continuation: block — prior run context injection in Harness."""
import pytest
from unittest.mock import AsyncMock, patch
from armature.runtime.engine import Harness
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType,
    ContinuationConfig, ContinuationKey,
)
from armature.state.traces import TraceStore, TraceRecord


def _spec_with_continuation(inject_as="prior_run", carry_forward=None):
    if carry_forward is None:
        carry_forward = [ContinuationKey(key="s1.summary")]
    return HarnessSpec(
        name="cont-wf",
        version="1.0",
        continuation=ContinuationConfig(
            carry_forward=carry_forward,
            inject_as=inject_as,
        ),
        stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="do"))],
    )


async def _seed_prior_run(db, workflow_name, stage_id, outputs):
    """Write a trace record directly so the second-run tests have prior data."""
    store = TraceStore(db)
    await store.init()
    await store.record(TraceRecord(
        run_id="prior-run-id",
        workflow_name=workflow_name,
        stage_id=stage_id,
        role_type="worker",
        model="test-model",
        outputs=outputs,
    ))


async def test_first_run_has_no_prior_run_in_context(tmp_path):
    spec = _spec_with_continuation()
    harness = Harness(spec=spec, traces_db=tmp_path / "traces.db")
    captured = {}

    async def mock_exec(stage, context):
        captured.update(context)
        return {"summary": "first result"}

    with patch.object(harness, "_execute_stage", new_callable=AsyncMock) as m:
        m.side_effect = mock_exec
        await harness.run({})

    assert "prior_run" not in captured


async def test_second_run_injects_prior_run_context(tmp_path):
    spec = _spec_with_continuation()
    db = tmp_path / "traces.db"
    await _seed_prior_run(db, "cont-wf", "s1", {"summary": "last run summary"})

    harness = Harness(spec=spec, traces_db=db)
    captured = {}

    async def mock_exec(stage, context):
        captured.update(context)
        return {"summary": "second run"}

    with patch.object(harness, "_execute_stage", new_callable=AsyncMock) as m:
        m.side_effect = mock_exec
        await harness.run({})

    assert "prior_run" in captured
    assert captured["prior_run"]["summary"] == "last run summary"


async def test_carry_forward_respects_inject_as_rename(tmp_path):
    spec = _spec_with_continuation(inject_as="last_cycle")
    db = tmp_path / "traces.db"
    await _seed_prior_run(db, "cont-wf", "s1", {"summary": "cycle one"})

    harness = Harness(spec=spec, traces_db=db)
    captured = {}

    async def mock_exec(stage, context):
        captured.update(context)
        return {"summary": "cycle two"}

    with patch.object(harness, "_execute_stage", new_callable=AsyncMock) as m:
        m.side_effect = mock_exec
        await harness.run({})

    assert "last_cycle" in captured
    assert "prior_run" not in captured


async def test_carry_forward_outputs_stored_at_2000_chars(tmp_path):
    """Stages in carry_forward list should store outputs up to 2000 chars, not 200."""
    spec = _spec_with_continuation(carry_forward=[ContinuationKey(key="s1.long_text")])
    db = tmp_path / "traces.db"
    long_value = "x" * 1500  # between 200 (old cap) and 2000 (new cap)

    # Mock LLMNode.execute directly so trace writing still runs in the engine
    async def mock_llm_execute(context):
        return {
            "long_text": long_value,
            "_input_tokens": 0, "_output_tokens": 0,
            "_escalation_count": 0, "_tools_declared": [], "_tools_called": [],
        }

    harness = Harness(spec=spec, traces_db=db)
    with patch("armature.nodes.llm.LLMNode.execute", new_callable=AsyncMock) as m, \
         patch("armature.nodes.llm.LLMNode._resolve_model", return_value="test-model"):
        m.side_effect = mock_llm_execute
        await harness.run({})

    store = TraceStore(db)
    await store.init()
    run_id = await store.latest_run_id("cont-wf")
    outputs = await store.get_run_outputs(run_id)
    assert len(outputs["s1"]["long_text"]) == 1500

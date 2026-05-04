import pytest
from pathlib import Path
from armature.state.traces import TraceStore, TraceRecord

@pytest.fixture
async def store(tmp_path):
    s = TraceStore(tmp_path / "traces.db")
    await s.init()
    return s

async def test_record_and_query(store):
    trace = TraceRecord(
        run_id="run1",
        workflow_name="my-flow",
        stage_id="s1",
        role_type="worker",
        model="ollama/qwen2.5:7b",
        input_tokens=50,
        output_tokens=20,
        latency_ms=210.5,
        success=True,
        output_valid=True,
    )
    await store.record(trace)
    results = await store.query(workflow_name="my-flow")
    assert len(results) == 1
    assert results[0].run_id == "run1"
    assert results[0].latency_ms == pytest.approx(210.5)

async def test_high_quality_filter(store):
    await store.record(TraceRecord(
        run_id="r1", workflow_name="w", stage_id="s", role_type="judge",
        model="claude-opus-4-7", input_tokens=100, output_tokens=50,
        latency_ms=500, success=True, output_valid=True, quorum_score=0.92,
    ))
    await store.record(TraceRecord(
        run_id="r2", workflow_name="w", stage_id="s", role_type="judge",
        model="claude-opus-4-7", input_tokens=100, output_tokens=50,
        latency_ms=500, success=True, output_valid=True, quorum_score=0.55,
    ))
    hq = await store.high_quality_traces("w", min_score=0.85)
    assert len(hq) == 1
    assert hq[0].run_id == "r1"

async def test_empty_db_returns_empty(store):
    results = await store.query()
    assert results == []

async def test_init_is_idempotent(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    await store.init()  # second call must not raise
    results = await store.query()
    assert results == []

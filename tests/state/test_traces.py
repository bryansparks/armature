import pytest
from pathlib import Path
from armature.state.traces import TraceStore, TraceRecord, IhrResult


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


async def _populate_run(store, run_id: str, n: int, **kwargs) -> None:
    for i in range(n):
        await store.record(TraceRecord(
            run_id=run_id,
            workflow_name=kwargs.get("workflow_name", "wf"),
            stage_id=f"s{i}",
            role_type="worker",
            model="test/model",
            latency_ms=kwargs.get("latency_ms", 500.0),
            success=kwargs.get("success", True),
            output_valid=kwargs.get("output_valid", True),
            quorum_score=kwargs.get("quorum_score", None),
        ))


async def test_compute_ihr_perfect(store):
    await _populate_run(store, "r1", 4,
        latency_ms=0.0, success=True, output_valid=True, quorum_score=1.0)
    result = await store.compute_ihr("r1")
    assert isinstance(result, IhrResult)
    assert result.run_id == "r1"
    assert result.ihr == pytest.approx(1.0, abs=1e-6)


async def test_compute_ihr_no_quorum_defaults_half(store):
    await _populate_run(store, "r2", 2,
        latency_ms=0.0, success=True, output_valid=True, quorum_score=None)
    result = await store.compute_ihr("r2")
    # latency_score=1.0, output_valid_rate=1.0, success_rate=1.0, quorum=0.5
    expected = 0.40 * 1.0 + 0.30 * 1.0 + 0.20 * 0.5 + 0.10 * 1.0
    assert result.ihr == pytest.approx(expected, abs=1e-6)


async def test_compute_ihr_partial_failures(store):
    await store.record(TraceRecord(
        run_id="r3", workflow_name="wf", stage_id="s1", role_type="worker",
        model="m", latency_ms=1000.0, success=True, output_valid=True, quorum_score=0.8))
    await store.record(TraceRecord(
        run_id="r3", workflow_name="wf", stage_id="s2", role_type="worker",
        model="m", latency_ms=3000.0, success=False, output_valid=False, quorum_score=0.4))
    result = await store.compute_ihr("r3")
    avg_latency = 2000.0
    latency_score = max(0.0, 1.0 - avg_latency / 5000.0)
    expected = (0.40 * 0.5    # output_valid_rate: 1/2
              + 0.30 * 0.5    # success_rate: 1/2
              + 0.20 * 0.6    # avg_quorum: (0.8+0.4)/2
              + 0.10 * latency_score)
    assert result.ihr == pytest.approx(expected, abs=1e-6)
    assert result.n_traces == 2


async def test_compute_ihr_unknown_run_returns_none(store):
    result = await store.compute_ihr("nonexistent")
    assert result is None


async def test_query_by_run_returns_only_that_run(store):
    await _populate_run(store, "runA", 3, workflow_name="wf")
    await _populate_run(store, "runB", 2, workflow_name="wf")
    records = await store.query_by_run("runA")
    assert len(records) == 3
    assert all(r.run_id == "runA" for r in records)


async def test_compute_ihr_mixed_quorum_ignores_none_traces(store):
    # 2 traces with quorum, 1 without — None is excluded from average
    await store.record(TraceRecord(
        run_id="r4", workflow_name="wf", stage_id="s1", role_type="worker",
        model="m", latency_ms=0.0, success=True, output_valid=True, quorum_score=0.9))
    await store.record(TraceRecord(
        run_id="r4", workflow_name="wf", stage_id="s2", role_type="worker",
        model="m", latency_ms=0.0, success=True, output_valid=True, quorum_score=0.7))
    await store.record(TraceRecord(
        run_id="r4", workflow_name="wf", stage_id="s3", role_type="worker",
        model="m", latency_ms=0.0, success=True, output_valid=True, quorum_score=None))
    result = await store.compute_ihr("r4")
    # avg_quorum = (0.9 + 0.7) / 2 = 0.8  (None trace excluded)
    expected = 0.40 * 1.0 + 0.30 * 1.0 + 0.20 * 0.8 + 0.10 * 1.0
    assert result.ihr == pytest.approx(expected, abs=1e-6)
    assert result.avg_quorum_score == pytest.approx(0.8, abs=1e-6)


async def test_error_type_stored_and_retrieved(store):
    await store.record(TraceRecord(
        run_id="r5", workflow_name="wf", stage_id="s1", role_type="worker",
        model="m", latency_ms=100.0, success=False, output_valid=False,
        error_type="RateLimitError",
    ))
    results = await store.query(workflow_name="wf")
    assert results[0].error_type == "RateLimitError"


async def test_escalation_count_in_ihr(store):
    for i in range(3):
        await store.record(TraceRecord(
            run_id="r6", workflow_name="wf", stage_id=f"s{i}", role_type="worker",
            model="m", latency_ms=0.0, success=True, output_valid=True,
            escalation_count=i,  # 0, 1, 2
        ))
    result = await store.compute_ihr("r6")
    assert result.avg_escalation_count == pytest.approx(1.0)  # (0+1+2)/3


async def test_spec_version_stored_and_retrieved(store):
    await store.record(TraceRecord(
        run_id="r7", workflow_name="wf", stage_id="s1", role_type="worker",
        model="m", latency_ms=50.0, success=True, output_valid=True,
        spec_version="abc123def456",
    ))
    results = await store.query(workflow_name="wf")
    assert results[0].spec_version == "abc123def456"


async def test_trace_store_enables_wal_mode(tmp_path):
    import aiosqlite
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    async with aiosqlite.connect(tmp_path / "traces.db") as db:
        cursor = await db.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
    assert row[0] == "wal"


async def test_multiple_runs_coexist_in_shared_db(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    await _populate_run(store, "runX", 2, workflow_name="shared-wf")
    await _populate_run(store, "runY", 3, workflow_name="shared-wf")
    x = await store.query_by_run("runX")
    y = await store.query_by_run("runY")
    assert len(x) == 2
    assert len(y) == 3
    all_traces = await store.query(workflow_name="shared-wf")
    assert len(all_traces) == 5

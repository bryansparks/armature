import pytest
from pathlib import Path
from armature.state.traces import TraceStore, TraceRecord, HqsResult


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


async def test_compute_hqs_perfect(store):
    await _populate_run(store, "r1", 4,
        latency_ms=0.0, success=True, output_valid=True, quorum_score=1.0)
    result = await store.compute_hqs("r1")
    assert isinstance(result, HqsResult)
    assert result.run_id == "r1"
    assert result.hqs == pytest.approx(1.0, abs=1e-6)


async def test_compute_hqs_no_quorum_defaults_half(store):
    await _populate_run(store, "r2", 2,
        latency_ms=0.0, success=True, output_valid=True, quorum_score=None)
    result = await store.compute_hqs("r2")
    # latency_score=1.0, output_valid_rate=1.0, success_rate=1.0, quorum=0.5
    expected = 0.40 * 1.0 + 0.30 * 1.0 + 0.20 * 0.5 + 0.10 * 1.0
    assert result.hqs == pytest.approx(expected, abs=1e-6)


async def test_compute_hqs_partial_failures(store):
    await store.record(TraceRecord(
        run_id="r3", workflow_name="wf", stage_id="s1", role_type="worker",
        model="m", latency_ms=1000.0, success=True, output_valid=True, quorum_score=0.8))
    await store.record(TraceRecord(
        run_id="r3", workflow_name="wf", stage_id="s2", role_type="worker",
        model="m", latency_ms=3000.0, success=False, output_valid=False, quorum_score=0.4))
    result = await store.compute_hqs("r3")
    avg_latency = 2000.0
    latency_score = max(0.0, 1.0 - avg_latency / 5000.0)
    hfr = 1.0  # both traces have escalation_count=0
    expected = (0.35 * 0.5    # output_valid_rate: 1/2
              + 0.25 * 0.5    # success_rate: 1/2
              + 0.20 * 0.6    # avg_quorum: (0.8+0.4)/2
              + 0.10 * latency_score
              + 0.10 * hfr)   # hfr: arXiv:2605.30621v1
    assert result.hqs == pytest.approx(expected, abs=1e-6)
    assert result.n_traces == 2


async def test_compute_hqs_unknown_run_returns_none(store):
    result = await store.compute_hqs("nonexistent")
    assert result is None


async def test_query_by_run_returns_only_that_run(store):
    await _populate_run(store, "runA", 3, workflow_name="wf")
    await _populate_run(store, "runB", 2, workflow_name="wf")
    records = await store.query_by_run("runA")
    assert len(records) == 3
    assert all(r.run_id == "runA" for r in records)


async def test_compute_hqs_mixed_quorum_ignores_none_traces(store):
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
    result = await store.compute_hqs("r4")
    # avg_quorum = (0.9 + 0.7) / 2 = 0.8  (None trace excluded)
    expected = 0.40 * 1.0 + 0.30 * 1.0 + 0.20 * 0.8 + 0.10 * 1.0
    assert result.hqs == pytest.approx(expected, abs=1e-6)
    assert result.avg_quorum_score == pytest.approx(0.8, abs=1e-6)


async def test_error_type_stored_and_retrieved(store):
    await store.record(TraceRecord(
        run_id="r5", workflow_name="wf", stage_id="s1", role_type="worker",
        model="m", latency_ms=100.0, success=False, output_valid=False,
        error_type="RateLimitError",
    ))
    results = await store.query(workflow_name="wf")
    assert results[0].error_type == "RateLimitError"


async def test_escalation_count_in_hqs(store):
    for i in range(3):
        await store.record(TraceRecord(
            run_id="r6", workflow_name="wf", stage_id=f"s{i}", role_type="worker",
            model="m", latency_ms=0.0, success=True, output_valid=True,
            escalation_count=i,  # 0, 1, 2
        ))
    result = await store.compute_hqs("r6")
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


# ── Phase B: inputs_hash and policy_version trace fields ──────────────────────

async def test_trace_record_has_inputs_hash_field():
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
    )
    assert hasattr(trace, "inputs_hash")


async def test_trace_record_has_policy_version_field():
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
    )
    assert hasattr(trace, "policy_version")


async def test_inputs_hash_default_is_empty_string():
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
    )
    assert trace.inputs_hash == ""


async def test_policy_version_default_is_empty_string():
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
    )
    assert trace.policy_version == ""


async def test_inputs_hash_can_be_set():
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
        inputs_hash="abc123",
    )
    assert trace.inputs_hash == "abc123"


async def test_policy_version_can_be_set():
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
        policy_version="deadbeef1234",
    )
    assert trace.policy_version == "deadbeef1234"


async def test_inputs_hash_persisted_and_retrieved(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
        inputs_hash="sha256abc123",
    )
    await store.record(trace)
    results = await store.query(workflow_name="wf")
    assert results[0].inputs_hash == "sha256abc123"


async def test_policy_version_persisted_and_retrieved(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
        policy_version="v1policy",
    )
    await store.record(trace)
    results = await store.query(workflow_name="wf")
    assert results[0].policy_version == "v1policy"


async def test_existing_db_upgraded_with_new_columns(tmp_path):
    """Simulate a DB created without the new columns, then upgraded via init()."""
    import aiosqlite
    db_path = tmp_path / "old.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, workflow_name TEXT NOT NULL,
                stage_id TEXT NOT NULL, role_type TEXT NOT NULL, model TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0.0, success INTEGER NOT NULL DEFAULT 1,
                output_valid INTEGER NOT NULL DEFAULT 1, quorum_score REAL,
                timestamp TEXT NOT NULL, inputs_json TEXT DEFAULT '{}',
                outputs_json TEXT DEFAULT '{}', error_type TEXT,
                escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT ''
            )
        """)
        await db.commit()

    store = TraceStore(db_path)
    await store.init()  # should add inputs_hash and policy_version columns

    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
        inputs_hash="newhash", policy_version="newver",
    )
    await store.record(trace)
    results = await store.query(workflow_name="wf")
    assert results[0].inputs_hash == "newhash"
    assert results[0].policy_version == "newver"


# ── Phase B: Context Provenance (RED) ────────────────────────────────────────

async def test_inputs_provenance_field_defaults_to_empty(tmp_path):
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
    )
    assert trace.inputs_provenance == {}


async def test_inputs_provenance_persisted_and_retrieved(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    prov = {"query": "user_input", "context": "stage:summarizer"}
    trace = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="test-model",
        inputs_provenance=prov,
    )
    await store.record(trace)
    results = await store.query(workflow_name="wf")
    assert results[0].inputs_provenance == prov


async def test_inputs_provenance_round_trips_nested_values(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    prov = {
        "result": "stage:analyst",
        "memories": "memory:analyst.summary",
        "user_query": "user_input",
    }
    trace = TraceRecord(
        run_id="r2", workflow_name="wf", stage_id="s2",
        role_type="worker", model="test-model",
        inputs_provenance=prov,
    )
    await store.record(trace)
    results = await store.query(workflow_name="wf")
    assert results[0].inputs_provenance == prov


async def test_get_run_outputs_returns_stage_keyed_dict(store):
    await store.record(TraceRecord(
        run_id="run-out", workflow_name="wf", stage_id="stage_a", role_type="worker",
        model="m", outputs={"summary": "all good", "count": "3"},
    ))
    await store.record(TraceRecord(
        run_id="run-out", workflow_name="wf", stage_id="stage_b", role_type="worker",
        model="m", outputs={"recommendation": "ship it"},
    ))
    result = await store.get_run_outputs("run-out")
    assert set(result.keys()) == {"stage_a", "stage_b"}
    assert result["stage_a"]["summary"] == "all good"
    assert result["stage_b"]["recommendation"] == "ship it"


async def test_old_db_without_provenance_column_returns_empty_dict(tmp_path):
    import aiosqlite
    db_path = tmp_path / "old_noprov.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, workflow_name TEXT NOT NULL,
                stage_id TEXT NOT NULL, role_type TEXT NOT NULL, model TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0.0, success INTEGER NOT NULL DEFAULT 1,
                output_valid INTEGER NOT NULL DEFAULT 1, quorum_score REAL,
                timestamp TEXT NOT NULL, inputs_json TEXT DEFAULT '{}',
                outputs_json TEXT DEFAULT '{}', error_type TEXT,
                escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '',
                inputs_hash TEXT DEFAULT '', policy_version TEXT DEFAULT ''
            )
        """)
        await db.execute(
            "INSERT INTO traces (run_id, workflow_name, stage_id, role_type, model, timestamp) "
            "VALUES ('r1','wf','s1','worker','model','2025-01-01T00:00:00+00:00')"
        )
        await db.commit()

    store = TraceStore(db_path)
    await store.init()
    results = await store.query(workflow_name="wf")
    assert results[0].inputs_provenance == {}


# ---------------------------------------------------------------------------
# sandbox_image_digest — Phase 3
# ---------------------------------------------------------------------------

def test_trace_record_has_sandbox_image_digest_field():
    """TraceRecord must have sandbox_image_digest defaulting to None."""
    rec = TraceRecord(run_id="r", workflow_name="w", stage_id="s", role_type="worker", model="m")
    assert rec.sandbox_image_digest is None


def test_trace_record_sandbox_image_digest_round_trips():
    """TraceRecord correctly stores a sandbox_image_digest value."""
    rec = TraceRecord(
        run_id="r", workflow_name="w", stage_id="s", role_type="worker", model="m",
        sandbox_image_digest="sha256:abc123",
    )
    assert rec.sandbox_image_digest == "sha256:abc123"


async def test_trace_store_persists_and_retrieves_sandbox_image_digest(tmp_path):
    """TraceStore round-trips sandbox_image_digest through the database."""
    store = TraceStore(tmp_path / "digest_test.db")
    await store.init()

    await store.record(TraceRecord(
        run_id="run-digest", workflow_name="wf", stage_id="stage_a",
        role_type="worker", model="m",
        sandbox_image_digest="sha256:deadbeef",
    ))
    results = await store.query(workflow_name="wf")
    assert len(results) == 1
    assert results[0].sandbox_image_digest == "sha256:deadbeef"


async def test_trace_store_returns_none_digest_when_not_set(tmp_path):
    """TraceStore returns sandbox_image_digest=None when field was not written."""
    store = TraceStore(tmp_path / "no_digest.db")
    await store.init()

    await store.record(TraceRecord(
        run_id="run-no-digest", workflow_name="wf", stage_id="stage_a",
        role_type="worker", model="m",
    ))
    results = await store.query(workflow_name="wf")
    assert results[0].sandbox_image_digest is None


async def test_old_db_without_digest_column_migrates_cleanly(tmp_path):
    """TraceStore.init() adds sandbox_image_digest column to existing DBs without error."""
    import aiosqlite
    db_path = tmp_path / "old_nodigest.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, workflow_name TEXT NOT NULL,
                stage_id TEXT NOT NULL, role_type TEXT NOT NULL, model TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0.0, success INTEGER NOT NULL DEFAULT 1,
                output_valid INTEGER NOT NULL DEFAULT 1, quorum_score REAL,
                timestamp TEXT NOT NULL, inputs_json TEXT DEFAULT '{}',
                outputs_json TEXT DEFAULT '{}'
            )
        """)
        await db.execute(
            "INSERT INTO traces (run_id, workflow_name, stage_id, role_type, model, timestamp) "
            "VALUES ('r1','wf','s1','worker','model','2025-01-01T00:00:00+00:00')"
        )
        await db.commit()

    store = TraceStore(db_path)
    await store.init()  # must not raise
    results = await store.query(workflow_name="wf")
    assert results[0].sandbox_image_digest is None

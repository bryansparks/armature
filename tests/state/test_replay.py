"""Tests for audit replay: TraceStore query_by_run + armature replay CLI command."""
import asyncio
from pathlib import Path
from typer.testing import CliRunner
from armature.state.traces import TraceRecord, TraceStore
from armature.cli import app

runner = CliRunner()


def make_trace(run_id: str, stage_id: str, success: bool = True) -> TraceRecord:
    return TraceRecord(
        run_id=run_id,
        workflow_name="test-wf",
        stage_id=stage_id,
        role_type="worker",
        model="gpt-4o-mini",
        success=success,
        output_valid=success,
        latency_ms=120.0,
        inputs={"q": "hello"},
        outputs={"answer": "world"},
    )


# ── TraceStore query_by_run ────────────────────────────────────────────────────

async def test_query_by_run_unknown_returns_empty(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    result = await store.query_by_run("nonexistent-run")
    assert result == []


async def test_query_by_run_returns_traces_in_asc_order(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    t1 = make_trace("run-abc", "stage-1")
    t2 = make_trace("run-abc", "stage-2")
    await store.record(t1)
    await store.record(t2)
    results = await store.query_by_run("run-abc")
    assert len(results) == 2
    assert results[0].stage_id == "stage-1"
    assert results[1].stage_id == "stage-2"


async def test_compute_hqs_matches_formula(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    t1 = make_trace("run-xyz", "stage-a", success=True)
    t2 = make_trace("run-xyz", "stage-b", success=True)
    await store.record(t1)
    await store.record(t2)
    hqs_result = await store.compute_hqs("run-xyz")
    assert hqs_result is not None
    assert hqs_result.n_traces == 2
    assert hqs_result.success_rate == 1.0
    assert hqs_result.output_valid_rate == 1.0
    assert 0.0 <= hqs_result.hqs <= 1.0


# ── replay CLI command ─────────────────────────────────────────────────────────

def test_replay_command_exists():
    """armature replay should be a recognized command."""
    result = runner.invoke(app, ["replay", "--help"])
    assert result.exit_code == 0


def test_replay_unknown_run_id_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["replay", "no-such-run"])
    assert result.exit_code != 0


def test_replay_known_run_id_prints_stage_table(tmp_path):
    db = tmp_path / "traces.db"

    async def _setup():
        store = TraceStore(db)
        await store.init()
        await store.record(make_trace("run-test", "gather"))
        await store.record(make_trace("run-test", "summarize"))

    asyncio.run(_setup())

    result = runner.invoke(app, ["replay", "run-test", "--traces", str(db)])
    assert result.exit_code == 0
    assert "gather" in result.output
    assert "summarize" in result.output

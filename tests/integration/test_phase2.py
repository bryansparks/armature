import pytest
from pathlib import Path
from armature.runtime.engine import Harness
from armature.state.traces import TraceStore

FIXTURES = Path(__file__).parent.parent / "fixtures"


async def test_trace_store_populated_after_run(tmp_path):
    """Engine records traces for all stage types including script stages."""
    harness = Harness.from_spec(
        FIXTURES / "echo-workflow.yaml",
        vars={"message": "trace-test"},
    )
    harness._session._path = tmp_path / "session.jsonl"
    trace_db = tmp_path / "traces.db"
    harness._traces = TraceStore(trace_db)

    result = await harness.run({"message": "trace-test"})

    assert "echo" in result
    assert result["echo"]["exit_code"] == 0

    store = TraceStore(trace_db)
    await store.init()
    traces = await store.query()
    assert len(traces) >= 1
    assert all(t.role_type == "script" for t in traces)
    assert all(t.success is True for t in traces)


async def test_subagent_fan_out_end_to_end(tmp_path):
    """Parent workflow fans out to a child workflow via SubagentNode."""
    from armature.spec.models import HarnessSpec, Stage

    parent_spec = HarnessSpec(
        name="parent-flow",
        version="1.0",
        stages=[
            Stage(
                id="child_run",
                subagent_spec=str(FIXTURES / "child-workflow.yaml"),
            )
        ],
    )
    harness = Harness(spec=parent_spec, session_dir=tmp_path)
    result = await harness.run({"greeting": "integration-test"})

    assert "child_run" in result
    child_result = result["child_run"]
    assert "respond" in child_result
    assert "integration-test" in child_result["respond"]["stdout"]


async def test_service_run_via_http(tmp_path):
    """HTTP service runs a workflow and returns a structured result."""
    pytest.importorskip("fastapi", reason="fastapi not installed; install with pip install armature[service]")
    from httpx import AsyncClient, ASGITransport
    from armature.service.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/run",
            json={
                "spec_path": str(FIXTURES / "echo-workflow.yaml"),
                "inputs": {"message": "phase2-integration"},
                "session_dir": str(tmp_path),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert body["result"]["echo"]["exit_code"] == 0


async def test_trace_records_spec_version(tmp_path):
    """Traces include the spec_version fingerprint."""
    harness = Harness.from_spec(FIXTURES / "echo-workflow.yaml")
    harness._session._path = tmp_path / "session.jsonl"
    trace_db = tmp_path / "traces.db"
    harness._traces = TraceStore(trace_db)

    await harness.run({"message": "version-test"})

    store = TraceStore(trace_db)
    await store.init()
    traces = await store.query()
    assert all(t.spec_version is not None for t in traces)
    assert all(len(t.spec_version) == 12 for t in traces)


async def test_run_id_present_in_context_for_all_stages(tmp_path):
    """run_id is injected into the context and accessible by all stages."""
    from armature.spec.models import HarnessSpec, Stage, Adapter

    captured_ctx = []

    class TrackingHarness(Harness):
        async def _execute_stage(self, stage, context):
            captured_ctx.append(dict(context))
            return await super()._execute_stage(stage, context)

    spec = HarnessSpec(
        name="ctx-test",
        stages=[Stage(id="s1", adapter="echo_cmd")],
        adapters={"echo_cmd": Adapter(name="echo_cmd", type="script", cmd="echo ok")},
    )
    harness = TrackingHarness(spec=spec, session_dir=tmp_path)
    await harness.run({})

    assert all("run_id" in ctx for ctx in captured_ctx)


async def test_two_script_stages_both_recorded_in_traces(tmp_path):
    """Each script stage produces its own trace record."""
    harness = Harness.from_spec(FIXTURES / "echo-workflow.yaml")
    harness._session._path = tmp_path / "session.jsonl"
    trace_db = tmp_path / "traces.db"
    harness._traces = TraceStore(trace_db)

    await harness.run({"message": "two-stage-trace"})

    store = TraceStore(trace_db)
    await store.init()
    traces = await store.query()
    stage_ids = {t.stage_id for t in traces}
    assert "echo" in stage_ids
    assert "verify" in stage_ids


async def test_run_summary_event_emitted(tmp_path):
    """The run_summary on_event callback fires at end with execution statistics."""
    events = []

    def on_event(name, data):
        events.append((name, data))

    harness = Harness.from_spec(
        FIXTURES / "echo-workflow.yaml",
        vars={"message": "event-test"},
    )
    harness._session._path = tmp_path / "session.jsonl"
    harness._on_event = on_event

    await harness.run({"message": "event-test"})

    event_names = [e[0] for e in events]
    assert "run_summary" in event_names
    summary_data = next(d for n, d in events if n == "run_summary")
    assert "stages_total" in summary_data
    assert summary_data["stages_total"] == 2
    assert "elapsed_s" in summary_data
    assert summary_data["elapsed_s"] >= 0

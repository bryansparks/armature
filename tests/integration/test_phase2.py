import pytest
from pathlib import Path
from armature.runtime.engine import Harness
from armature.state.traces import TraceStore

FIXTURES = Path(__file__).parent.parent / "fixtures"


async def test_trace_store_populated_after_run(tmp_path):
    """Engine records traces for all stage types including script stages.

    Run a script-only workflow and verify:
    - The harness completes without error
    - The TraceStore wired to tmp_path records one trace with role_type="script"
    """
    harness = Harness.from_spec(
        FIXTURES / "echo-workflow.yaml",
        vars={"message": "trace-test"},
    )
    # Redirect both the session log and trace DB to tmp_path so the test is
    # hermetic and doesn't touch ~/.armature.
    harness._session._path = tmp_path / "session.jsonl"
    trace_db = tmp_path / "traces.db"
    harness._traces = TraceStore(trace_db)

    result = await harness.run({"message": "trace-test"})

    assert "echo" in result
    assert result["echo"]["exit_code"] == 0

    # Script stages now record traces with role_type="script".
    store = TraceStore(trace_db)
    await store.init()
    traces = await store.query()
    assert len(traces) >= 1, "Script stages should produce at least one trace record each"
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
    # SubagentNode.execute returns child.run(context) which is {stage_id: result}
    assert "respond" in child_result
    assert "integration-test" in child_result["respond"]["stdout"]


async def test_service_run_via_http(tmp_path):
    """HTTP service runs a workflow and returns a structured result."""
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

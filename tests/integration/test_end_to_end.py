import pytest
from pathlib import Path
from armature.runtime.engine import Harness

FIXTURES = Path(__file__).parent.parent / "fixtures"


async def test_echo_workflow_runs_end_to_end(tmp_path):
    harness = Harness.from_spec(
        FIXTURES / "echo-workflow.yaml",
        vars={"message": "hello-world"},
    )
    harness._session._path = tmp_path / "session.jsonl"

    result = await harness.run({"message": "hello-world"})

    assert "echo" in result
    assert result["echo"]["exit_code"] == 0
    assert "hello-world" in result["echo"]["stdout"] or "received" in result["echo"]["stdout"]


async def test_session_log_written(tmp_path):
    harness = Harness.from_spec(
        FIXTURES / "echo-workflow.yaml",
        vars={"message": "test"},
    )
    harness._session._path = tmp_path / "session.jsonl"
    await harness.run({"message": "test"})

    events = await harness._session.read_all()
    event_types = [e.type for e in events]
    assert "run_start" in event_types
    assert "stage_start" in event_types
    assert "run_complete" in event_types

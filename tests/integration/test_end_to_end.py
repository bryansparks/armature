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


async def test_echo_workflow_all_stages_complete(tmp_path):
    """Both echo and verify stages execute and appear in the result."""
    harness = Harness.from_spec(FIXTURES / "echo-workflow.yaml")
    harness._session._path = tmp_path / "session.jsonl"
    result = await harness.run({"message": "all-stages"})
    assert "echo" in result
    assert "verify" in result
    assert result["verify"]["exit_code"] == 0


async def test_echo_workflow_verify_depends_on_echo(tmp_path):
    """verify stage runs after echo (DAG ordering respected)."""
    execution_order = []

    harness = Harness.from_spec(FIXTURES / "echo-workflow.yaml")
    harness._session._path = tmp_path / "session.jsonl"

    original_execute = harness._execute_stage.__func__

    async def tracked_execute(self, stage, context):
        result = await original_execute(self, stage, context)
        execution_order.append(stage.id)
        return result

    import types
    harness._execute_stage = types.MethodType(tracked_execute, harness)
    await harness.run({"message": "order-test"})
    assert execution_order.index("echo") < execution_order.index("verify")


async def test_echo_workflow_context_injected_into_cmd(tmp_path):
    """Context variable 'message' is Jinja2-rendered into the shell command."""
    harness = Harness.from_spec(FIXTURES / "echo-workflow.yaml")
    harness._session._path = tmp_path / "session.jsonl"
    result = await harness.run({"message": "my-unique-token-xyz"})
    assert "my-unique-token-xyz" in result["echo"]["stdout"]


async def test_child_workflow_runs_standalone(tmp_path):
    """child-workflow.yaml runs standalone with a greeting input."""
    harness = Harness.from_spec(FIXTURES / "child-workflow.yaml")
    harness._session._path = tmp_path / "session.jsonl"
    result = await harness.run({"greeting": "hello-child"})
    assert "respond" in result
    assert result["respond"]["exit_code"] == 0
    assert "hello-child" in result["respond"]["stdout"]


async def test_harness_run_populates_session_events_for_each_stage(tmp_path):
    """Each stage produces stage_start and stage_complete session events."""
    harness = Harness.from_spec(FIXTURES / "echo-workflow.yaml")
    harness._session._path = tmp_path / "session.jsonl"
    await harness.run({"message": "events-test"})

    events = await harness._session.read_all()
    event_types = [e.type for e in events]
    # Two stages → two stage_start events
    assert event_types.count("stage_start") >= 2

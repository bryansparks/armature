import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from armature.runtime.engine import Harness
from armature.spec.models import HarnessSpec, Stage, Role, RoleType
from armature.state.traces import TraceStore

def make_minimal_spec() -> HarnessSpec:
    return HarnessSpec(
        name="test",
        version="1.0",
        stages=[
            Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="test"))
        ]
    )

async def test_harness_from_spec():
    spec = make_minimal_spec()
    harness = Harness(spec=spec)
    assert harness.name == "test"

async def test_harness_run_returns_result():
    spec = make_minimal_spec()
    harness = Harness(spec=spec)

    with patch.object(harness, "_execute_stage", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"content": "stage output"}
        result = await harness.run({"topic": "test"})

    assert result is not None
    mock_exec.assert_called_once()

async def test_harness_initializes_trace_store(tmp_path):
    spec = make_minimal_spec()
    harness = Harness(spec=spec, session_dir=tmp_path)
    assert hasattr(harness, "_traces")
    assert isinstance(harness._traces, TraceStore)


def test_harness_from_file(tmp_path):
    spec_file = tmp_path / "test.yaml"
    spec_file.write_text("""
name: file-test
version: "1.0"
stages:
  - id: s1
    role:
      name: r
      type: worker
      description: test
""")
    harness = Harness.from_spec(spec_file)
    assert harness.name == "file-test"

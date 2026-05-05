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


from armature.hooks.lifecycle import HookDecision, ToolBlocked
from armature.spec.models import (
    HarnessSpec, Stage, Adapter, SafetyCondition, ToolSafetyRule,
)


def make_adapter_spec(adapter_name: str = "run_shell", cmd: str = "echo hi") -> HarnessSpec:
    return HarnessSpec(
        name="adapter-test",
        stages=[Stage(id="s1", adapter=adapter_name)],
        adapters={adapter_name: Adapter(name=adapter_name, type="script", cmd=cmd)},
    )


async def test_pre_tool_hook_is_called_for_adapter(tmp_path):
    spec = make_adapter_spec(cmd="echo hello")
    harness = Harness(spec=spec, session_dir=tmp_path)

    calls = []

    async def capture_hook(phase, tool_name, args, ctx):
        calls.append(tool_name)
        return HookDecision.ALLOW

    from armature.hooks.lifecycle import HookPhase
    harness._hooks.register(HookPhase.PRE_TOOL, capture_hook)

    await harness.run({})
    assert calls == ["run_shell"]


async def test_pre_tool_hook_block_raises_tool_blocked(tmp_path):
    spec = make_adapter_spec(cmd="rm -rf /tmp/test")
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def block_hook(phase, tool_name, args, ctx):
        return HookDecision.BLOCK

    from armature.hooks.lifecycle import HookPhase
    harness._hooks.register(HookPhase.PRE_TOOL, block_hook)

    with pytest.raises(ToolBlocked) as exc_info:
        await harness.run({})
    assert "run_shell" in str(exc_info.value)


async def test_post_tool_hook_is_called_after_adapter(tmp_path):
    spec = make_adapter_spec(cmd="echo done")
    harness = Harness(spec=spec, session_dir=tmp_path)

    post_calls = []

    async def post_hook(phase, tool_name, result, ctx):
        post_calls.append((tool_name, result.get("exit_code")))

    from armature.hooks.lifecycle import HookPhase
    harness._hooks.register(HookPhase.POST_TOOL, post_hook)

    await harness.run({})
    assert post_calls == [("run_shell", 0)]


async def test_safety_rules_from_spec_block_adapter(tmp_path):
    spec = HarnessSpec(
        name="guarded",
        stages=[Stage(id="s1", adapter="danger")],
        adapters={"danger": Adapter(name="danger", type="script", cmd="sudo apt-get install vim")},
        safety_rules=[
            ToolSafetyRule(
                tool="danger",
                condition=SafetyCondition(field="cmd", op="contains", value="sudo"),
                action="block",
                message="sudo not permitted",
            )
        ],
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    with pytest.raises(ToolBlocked) as exc_info:
        await harness.run({})
    assert "sudo not permitted" in str(exc_info.value)


async def test_safety_rules_allow_safe_adapter(tmp_path):
    spec = HarnessSpec(
        name="guarded-allow",
        stages=[Stage(id="s1", adapter="safe_cmd")],
        adapters={"safe_cmd": Adapter(name="safe_cmd", type="script", cmd="echo hello")},
        safety_rules=[
            ToolSafetyRule(
                tool="safe_cmd",
                condition=SafetyCondition(field="cmd", op="contains", value="sudo"),
                action="block",
                message="sudo not permitted",
            )
        ],
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    result = await harness.run({})
    assert result["s1"]["exit_code"] == 0

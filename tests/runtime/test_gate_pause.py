"""A run under the server pauses at a human gate and resumes from its checkpoint with the decision."""
import json

import pytest

from armature.nodes.gate import (
    CHECKPOINT_FILE,
    GATE_DIR_ENV,
    GATE_FILE,
    GateSuspended,
    gate_digest,
)
from armature.permissions.permissions import PermissionLevel
from armature.registry.registry import ToolDescriptor
from armature.runtime.engine import Harness
from armature.spec.models import (
    GateApproval,
    HarnessSpec,
    ModelTierConfig,
    ModelTiers,
    Stage,
    ToolCallConfig,
)


def _harness(stages, tmp_path):
    spec = HarnessSpec(name="wf", stages=stages,
                       model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")))
    return Harness(spec=spec, session_dir=tmp_path / "session")


def _tool(harness, name, calls):
    async def handler(args):
        calls.append(name)
        return {"from": name}
    harness._registry.register(ToolDescriptor(name=name, description=name, permission=PermissionLevel.READ_ONLY,
                                              handler=handler, parameters={}))


def _stages():
    return [
        Stage(id="build", tool_call=ToolCallConfig(name="build")),
        Stage(id="lint", tool_call=ToolCallConfig(name="lint")),
        Stage(id="approve", gate="human", present="Ship {{ build.from }}?", depends_on=["build"],
              approval=GateApproval(min_role="team_lead", expires_hours=4)),
        Stage(id="ship", tool_call=ToolCallConfig(name="ship"), depends_on=["approve", "lint"]),
    ]


async def test_pauses_after_its_wave_and_resumes_with_the_answer(tmp_path, monkeypatch):
    gate_dir = tmp_path / "gate"
    gate_dir.mkdir()
    monkeypatch.setenv(GATE_DIR_ENV, str(gate_dir))
    calls: list[str] = []
    first = _harness(_stages(), tmp_path)
    for name in ("build", "lint", "ship"):
        _tool(first, name, calls)
    with pytest.raises(GateSuspended):
        await first.run({})
    request = json.loads((gate_dir / GATE_FILE).read_text())
    assert sorted(calls) == ["build", "lint"]
    assert request["step_id"] == request["key"] == "approve"
    assert request["present"] == "Ship build?"
    assert "min_role" not in request and "expires_hours" not in request
    assert set(request["checkpoint"]) == {"build", "lint"}

    checkpoint = {**request["checkpoint"], "approve": {"approved": True, "feedback": None}}
    (gate_dir / CHECKPOINT_FILE).write_text(json.dumps(checkpoint))
    (gate_dir / GATE_FILE).unlink()
    calls.clear()
    second = _harness(_stages(), tmp_path)
    for name in ("build", "lint", "ship"):
        _tool(second, name, calls)
    results = await second.run({})
    assert calls == ["ship"]
    assert results["approve"] == {"approved": True, "feedback": None}
    assert not (gate_dir / GATE_FILE).exists()


async def test_fail_as_value_does_not_swallow_the_pause(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE_DIR_ENV, str(tmp_path))
    harness = _harness([Stage(id="approve", gate="human", fail_as_value=True)], tmp_path)
    with pytest.raises(GateSuspended):
        await harness.run({})
    assert json.loads((tmp_path / GATE_FILE).read_text())["present"] == "Review required."


async def test_skipped_gate_does_not_pause(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE_DIR_ENV, str(tmp_path))
    harness = _harness([Stage(id="approve", gate="human", condition="{{ false }}")], tmp_path)
    results = await harness.run({})
    assert results["approve"] == {"_skipped": True}
    assert not (tmp_path / GATE_FILE).exists()


async def test_a_child_workflow_gate_fails_instead_of_pausing(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE_DIR_ENV, str(tmp_path))
    spec = HarnessSpec(name="child", stages=[Stage(id="approve", gate="human")],
                       model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")))
    child = Harness(spec=spec, session_dir=tmp_path / "child", parent_step="call")
    with pytest.raises(RuntimeError, match="called workflow"):
        await child.run({})


async def test_run_suspend_event_replaces_run_end(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE_DIR_ENV, str(tmp_path))
    harness = _harness([Stage(id="approve", gate="human")], tmp_path)
    sent = []

    class Recorder:
        enabled, endpoint, token = True, "unix:/x", "t"

        def emit(self, kind, step_id=None, /, **payload):
            sent.append((kind, step_id, payload))

        def redact(self, payload):
            return payload

        def close(self, timeout=1.0):
            pass

    monkeypatch.setattr(Harness, "_open_run_emitter", lambda self, inputs: _async(Recorder()))
    with pytest.raises(GateSuspended):
        await harness.run({})
    kinds = [kind for kind, _, _ in sent]
    assert "run_end" not in kinds and kinds[-1] == "run_suspend"
    assert (sent[-1][2]["step_id"], sent[-1][2]["key"]) == ("approve", "approve")
    assert ("step_start", "approve") in [(kind, step) for kind, step, _ in sent]


async def _async(value):
    return value


def test_kill_descendants_stops_what_this_process_started():
    import subprocess

    from armature.nodes.gate import kill_descendants
    child = subprocess.Popen(["sleep", "30"])
    kill_descendants()
    assert child.wait(5) == -9


async def test_the_engine_key_comes_on_a_pipe_that_closes_and_signs_the_pause(tmp_path, monkeypatch):
    import os

    from armature.events.env import child_env
    from armature.events.sign import ENGINE_FD_ENV, engine_pipe, sign_engine
    monkeypatch.setenv(GATE_DIR_ENV, str(tmp_path))
    pipe = engine_pipe("run-1:secret")
    monkeypatch.setenv(ENGINE_FD_ENV, str(pipe))
    assert ENGINE_FD_ENV not in child_env()
    harness = _harness([Stage(id="approve", gate="human")], tmp_path)
    assert ENGINE_FD_ENV not in os.environ
    with pytest.raises(OSError):
        os.fstat(pipe)
    sent = []

    class Recorder:
        enabled, endpoint, token = True, "unix:/x", "t"

        def emit(self, kind, step_id=None, /, **payload):
            sent.append((kind, payload))

        def redact(self, payload):
            return payload

        def close(self, timeout=1.0):
            pass

    monkeypatch.setattr(Harness, "_open_run_emitter", lambda self, inputs: _async(Recorder()))
    with pytest.raises(GateSuspended):
        await harness.run({})
    payload = sent[-1][1]
    request = json.loads((tmp_path / GATE_FILE).read_text())
    assert payload["digest"] == gate_digest(request)
    signed = {"kind": "run_suspend", "run_id": "run-1", "step_id": "approve", "key": "approve",
              "digest": payload["digest"], "ts": payload["ts"]}
    assert payload["sig"] == sign_engine("secret", signed)


async def test_the_digest_covers_the_request_as_the_host_reads_it(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    monkeypatch.setenv(GATE_DIR_ENV, str(tmp_path))
    harness = _harness([Stage(id="build", tool_call=ToolCallConfig(name="build")),
                        Stage(id="approve", gate="human", depends_on=["build"])], tmp_path)

    async def odd(args):
        return {"at": datetime(2026, 1, 2, tzinfo=UTC), "pair": (1, 2), 3: "int key"}
    harness._registry.register(ToolDescriptor(name="build", description="b", permission=PermissionLevel.READ_ONLY,
                                              handler=odd, parameters={}))
    with pytest.raises(GateSuspended):
        await harness.run({})
    assert harness._gate_digest == gate_digest(json.loads((tmp_path / GATE_FILE).read_text()))


def _capture_engine_keys(monkeypatch):
    seen = []
    original = Harness.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        seen.append((kwargs.get("parent_step"), self._engine_key))
    monkeypatch.setattr(Harness, "__init__", init)
    return seen


async def test_called_and_subagent_workflows_share_the_parent_engine_key(tmp_path, monkeypatch):
    from pathlib import Path

    from armature.nodes.subagent import SubagentNode
    from armature.spec.calls import MappingCallResolver
    from armature.spec.models import CallRef
    callee = "# armature: stored-workflow\nname: child\nstages:\n  - id: only\n    adapter: hi\n" \
             "adapters:\n  hi: {name: hi, type: script, cmd: 'echo hi'}\n"
    resolver = MappingCallResolver({("child", 1): callee}, {"child": 1})
    spec = HarnessSpec(name="wf", stages=[Stage(id="call", call=CallRef(workflow="child"))],
                       model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")))
    seen = _capture_engine_keys(monkeypatch)
    parent = Harness(spec=spec, session_dir=tmp_path / "s", call_resolver=resolver, engine_key=("run-1", "k"))
    await parent.run({})
    assert seen[-1] == ("call", ("run-1", "k"))
    fixture = Path(__file__).parent.parent / "fixtures" / "child-workflow.yaml"
    node = SubagentNode(stage=Stage(id="sub", subagent_spec=str(fixture)), session_dir=tmp_path,
                        engine_key=("run-1", "k"))
    await node.execute({"greeting": "hi"})
    assert seen[-1][1] == ("run-1", "k")


def test_a_script_step_and_a_keyless_sudo_run_never_read_the_run_s_stdin(monkeypatch):
    import asyncio
    import os

    from armature.events.sign import read_engine_key
    from armature.nodes.script import ScriptNode
    from armature.spec.models import Adapter
    saved = os.dup(0)
    read_end, write_end = os.pipe()
    os.write(write_end, b"left on stdin")
    os.close(write_end)
    os.dup2(read_end, 0)
    os.close(read_end)
    try:
        result = asyncio.run(ScriptNode(adapter=Adapter(name="c", type="script", cmd="cat")).execute({}))
        assert result["stdout"] == ""
        monkeypatch.setenv("SUDO_USER", "armature-runner")
        monkeypatch.delenv("ARMATURE_ENGINE_FD", raising=False)
        assert read_engine_key() is None
        assert os.read(0, 100) == b""
    finally:
        os.dup2(saved, 0)
        os.close(saved)

import asyncio
import json
import subprocess
import time

import pytest

from armature.runtime.engine import Harness
from armature.spec.models import (
    Adapter,
    HarnessSpec,
    ModelTierConfig,
    ModelTiers,
    Role,
    RoleType,
    SandboxConfig,
    SandboxMode,
    Stage,
)
from tests.events.support import DatagramReader


def _spec(**sandbox) -> HarnessSpec:
    return HarnessSpec(
        name="stub-test",
        stages=[
            Stage(id="resolve", adapter="resolve"),
            Stage(id="gate", gate="human", depends_on=["resolve"]),
            Stage(id="review", adapter="review", depends_on=["gate"], fan_out=2,
                  partition_source="{{ resolve.files }}", partition_key="file"),
            Stage(id="lead", role=Role(name="Lead", type=RoleType.WORKER, description="decide", model_tier="small"),
                  depends_on=["review"]),
            Stage(id="post", adapter="post", depends_on=["lead"]),
        ],
        adapters={
            "resolve": Adapter(name="resolve", type="script", cmd="echo resolve"),
            "review": Adapter(name="review", type="script", cmd="echo review"),
            "post": Adapter(name="post", type="script", cmd="echo post"),
        },
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        **sandbox,
    )


@pytest.fixture
def no_subprocess(monkeypatch):
    started = []

    def refuse(*args, **kwargs):
        started.append(args)
        raise AssertionError(f"stub mode started a subprocess: {args!r}")

    async def refuse_async(*args, **kwargs):
        refuse(*args)

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse_async)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_async)
    return started


def _stub(monkeypatch, tmp_path, outputs, default=None):
    path = tmp_path / "stubs.json"
    path.write_text(json.dumps(outputs))
    monkeypatch.setenv("ARMATURE_STUB_OUTPUTS", str(path))
    if default is not None:
        monkeypatch.setenv("ARMATURE_STUB_DEFAULT", default)
    else:
        monkeypatch.delenv("ARMATURE_STUB_DEFAULT", raising=False)


def _no_model(monkeypatch):
    async def refuse(**kwargs):
        raise AssertionError("stub mode called a model")
    monkeypatch.setattr("armature.nodes.llm.litellm_completion", refuse)


async def test_stub_outputs_flow_through_depends_on_and_fan_out_without_a_subprocess(
        monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {
        "resolve": {"files": ["a.py", "b.py"]},
        "review": [{"finding": "a"}, {"finding": "b"}],
        "post": {"posted": False},
    })
    harness = Harness(spec=_spec(sandbox=SandboxConfig(mode=SandboxMode.DOCKER)), session_dir=tmp_path,
                      traces_db=tmp_path / "t.db")
    results = await harness.run({})
    assert results["resolve"] == {"files": ["a.py", "b.py"]}
    assert results["gate"]["approved"] is True
    assert results["review"] == [{"finding": "a"}, {"finding": "b"}]
    assert results["lead"] == {"stub": True}
    assert results["post"] == {"posted": False}
    assert no_subprocess == []


async def test_a_recorded_llm_output_is_used(monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": []}, "review": [], "lead": {"verdict": "approve"},
                                  "post": {}})
    results = await Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})
    assert results["lead"] == {"verdict": "approve"}


async def test_a_missing_stage_fails_with_stub_missing(monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": []}, "review": []})
    with pytest.raises(ValueError, match="STUB_MISSING: stage 'post'"):
        await Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})


async def test_default_empty_fills_missing_stages(monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": ["a.py"]}}, default="empty")
    results = await Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})
    assert results["review"] == [{}]
    assert results["post"] == {}


async def test_stub_mode_emits_events(monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": ["a.py"]}, "review": [{}], "post": {}})
    reader = DatagramReader()
    monkeypatch.setenv("ARMATURE_EVENTS", reader.endpoint)
    monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "run-token")
    try:
        harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
        await harness.run({})
        harness._events.flush(5)
        deadline = time.monotonic() + 5
        while not any(e["kind"] == "run_end" for e in reader.events) and time.monotonic() < deadline:
            time.sleep(0.01)
        kinds = [(e["kind"], e["step_id"]) for e in reader.events]
    finally:
        reader.close()
    assert kinds[0] == ("run_start", None)
    for step in ("resolve", "gate", "review", "lead", "post"):
        assert ("step_end", step) in kinds
    assert reader.events[-1]["payload"]["status"] == "succeeded"


async def test_stub_mode_loads_no_tool_modules_and_reads_no_files(monkeypatch, tmp_path, no_subprocess):
    import pathlib

    from armature.spec.models import ToolModule
    _no_model(monkeypatch)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read")
    _stub(monkeypatch, tmp_path, {"resolve": {"files": [str(secret)]}, "review": [{}], "post": {}})
    spec = _spec()
    spec.tools = [ToolModule(module="armature_no_such_tool_module")]
    review = next(s for s in spec.stages if s.id == "review")
    review.inject_file_as = "content"
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    read = []
    original = pathlib.Path.read_text

    def recording(self, *args, **kwargs):
        read.append(str(self))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, "read_text", recording)
    await harness.run({})
    assert str(secret) not in read

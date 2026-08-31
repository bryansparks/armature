"""Integration tests for per-stage context governance in the engine."""
import pytest

from armature.runtime import engine as engine_mod
from armature.runtime.engine import Harness, _apply_context_never
from armature.spec.models import HarnessSpec


class _FakeLLMNode:
    """Captures the context and mission_context the engine hands each stage."""
    instances: list["_FakeLLMNode"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.context = None
        _FakeLLMNode.instances.append(self)

    def _resolve_model(self) -> str:
        return "fake"

    async def execute(self, context):
        self.context = context
        return {"content": "ok"}


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    _FakeLLMNode.instances = []
    monkeypatch.setattr(engine_mod, "LLMNode", _FakeLLMNode)


def _governed_spec() -> HarnessSpec:
    return HarnessSpec.model_validate({
        "name": "gov",
        "mission": "Be careful.",
        "model_tiers": {"small": {"provider": "mock", "model": "m"}},
        "role_type_defaults": {"worker": "small"},
        "contracts": {"inputs": [{"name": "raw_pii"}]},
        "context_layers": [{"name": "principles", "content": "Be terse.", "precedence": 10}],
        "context_policy": {"must": ["principles"]},
        "stages": [
            {"id": "researcher",
             "role": {"name": "R", "type": "worker", "description": "dig"}},
            {"id": "analyst",
             "role": {"name": "A", "type": "worker", "description": "analyze"},
             "depends_on": ["researcher"],
             "context_policy": {"never": ["researcher", "raw_pii"]}},
        ],
    })


async def _run(spec, tmp_path, inputs=None):
    harness = Harness(spec=spec, session_dir=tmp_path, validate=False,
                      traces_db=tmp_path / "t.db")
    return harness, await harness.run(inputs or {})


def _node(stage_id: str) -> _FakeLLMNode:
    return [n for n in _FakeLLMNode.instances if n.kwargs["stage"].id == stage_id][0]


async def test_stage_never_filters_context_keys_and_breadcrumbs(tmp_path):
    await _run(_governed_spec(), tmp_path, {"raw_pii": "SECRET"})
    analyst = _node("analyst")
    assert "researcher" not in analyst.context       # closed stage output
    assert "raw_pii" not in analyst.context          # closed runtime input
    assert "researcher" not in analyst.kwargs["mission_context"]  # no breadcrumb leak


async def test_default_must_injects_layers_into_prompt(tmp_path):
    await _run(_governed_spec(), tmp_path, {"raw_pii": "x"})
    researcher = _node("researcher")
    assert "[Workflow Mission]\nBe careful." in researcher.kwargs["mission_context"]
    assert "[Context Layer: principles]\nBe terse." in researcher.kwargs["mission_context"]
    analyst = _node("analyst")
    assert "[Context Layer: principles]" in analyst.kwargs["mission_context"]


async def test_mission_only_context_block_is_byte_identical(tmp_path):
    """End-to-end pin (through the engine, not just the _build_context_block
    unit): a mission-only, ungoverned single-stage spec produces EXACTLY the
    old mission-block string — the branch's load-bearing back-compat claim."""
    spec = HarnessSpec.model_validate({
        "name": "solo-mission",
        "mission": "Be careful.",
        "model_tiers": {"small": {"provider": "mock", "model": "m"}},
        "role_type_defaults": {"worker": "small"},
        "stages": [{"id": "solo",
                    "role": {"name": "S", "type": "worker", "description": "d"}}],
    })
    await _run(spec, tmp_path)
    solo = _node("solo")
    assert solo.kwargs["mission_context"] == "[Workflow Mission]\nBe careful."


async def test_effective_policy_recorded_on_traces(tmp_path):
    harness, _ = await _run(_governed_spec(), tmp_path, {"raw_pii": "x"})
    traces = await harness._traces.query_by_run(harness._run_id)
    by_stage = {t.stage_id: t for t in traces}
    assert "principles" in by_stage["researcher"].context_policy["must"]
    assert set(by_stage["analyst"].context_policy["never"]) == {"researcher", "raw_pii"}


async def test_ungoverned_spec_records_null_policy(tmp_path):
    spec = HarnessSpec.model_validate({
        "name": "plain",
        "model_tiers": {"small": {"provider": "mock", "model": "m"}},
        "role_type_defaults": {"worker": "small"},
        "stages": [{"id": "solo",
                    "role": {"name": "S", "type": "worker", "description": "d"}}],
    })
    harness, _ = await _run(spec, tmp_path)
    traces = await harness._traces.query_by_run(harness._run_id)
    assert traces[0].context_policy is None


async def test_never_applies_to_tool_call_stages(tmp_path, monkeypatch):
    from armature.nodes import tool_call as tc_mod

    captured = {}

    class _FakeToolNode:
        def __init__(self, *, stage, registry):
            self.stage = stage

        async def execute(self, context):
            captured.update(context)
            return {"ok": True}

    monkeypatch.setattr(tc_mod, "ToolCallNode", _FakeToolNode)
    spec = HarnessSpec.model_validate({
        "name": "toolgov",
        "model_tiers": {"small": {"provider": "mock", "model": "m"}},
        "role_type_defaults": {"worker": "small"},
        "stages": [
            {"id": "leak",
             "role": {"name": "L", "type": "worker", "description": "d"}},
            {"id": "sink", "depends_on": ["leak"],
             "tool_call": {"name": "noop", "args": {}},
             "context_policy": {"never": ["leak"]}},
        ],
    })
    await _run(spec, tmp_path)
    assert "leak" not in captured


async def test_sibling_wave_filtering_does_not_mutate_shared_context(tmp_path):
    """b1 and b2 run in the same wave off shared cumulative context; b1's
    `never` must not leak into — or shrink — the context b2 sees."""
    spec = HarnessSpec.model_validate({
        "name": "siblings",
        "model_tiers": {"small": {"provider": "mock", "model": "m"}},
        "role_type_defaults": {"worker": "small"},
        "stages": [
            {"id": "a",
             "role": {"name": "A", "type": "worker", "description": "produce"}},
            {"id": "b1", "depends_on": ["a"],
             "role": {"name": "B1", "type": "worker", "description": "d"},
             "context_policy": {"never": ["a"]}},
            {"id": "b2", "depends_on": ["a"],
             "role": {"name": "B2", "type": "worker", "description": "d"}},
        ],
    })
    await _run(spec, tmp_path)
    b1 = _node("b1")
    b2 = _node("b2")
    assert "a" not in b1.context
    assert "a" in b2.context


def test_apply_context_never_drops_keys():
    out = _apply_context_never(frozenset({"b", "_memory"}),
                               {"a": 1, "b": 2, "_memory": {}})
    assert out == {"a": 1}


def test_apply_context_never_empty_is_identity():
    ctx = {"a": 1}
    assert _apply_context_never(frozenset(), ctx) is ctx


def test_apply_context_never_redacts_transcript_entries():
    transcript = [
        {"stage_id": "secret", "response": "s3cret"},
        {"stage_id": "ok", "response": "fine"},
    ]
    out = _apply_context_never(frozenset({"secret"}),
                               {"_transcript": transcript, "x": 1})
    assert [e["stage_id"] for e in out["_transcript"]] == ["ok"]


def test_apply_context_never_closed_transcript_dropped_whole():
    out = _apply_context_never(
        frozenset({"_transcript"}),
        {"_transcript": [{"stage_id": "a"}], "x": 1},
    )
    assert "_transcript" not in out

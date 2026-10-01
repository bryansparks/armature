"""Cost metering: the engine aggregates _cost_usd from LLM stage results."""
import pytest

from armature.runtime import engine as engine_mod
from armature.runtime.engine import Harness
from armature.spec.models import HarnessSpec


class _CostedLLM:
    """Fake LLM node returning engine-shaped results carrying _cost_usd."""
    instances: list["_CostedLLM"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _CostedLLM.instances.append(self)

    def _resolve_model(self) -> str:
        return "fake"

    async def execute(self, context):
        return {"content": "ok", "_input_tokens": 1, "_output_tokens": 1,
                "_cost_usd": 0.01, "_escalation_count": 0, "_tools_called": []}


@pytest.fixture(autouse=True)
def _costed_llm(monkeypatch):
    _CostedLLM.instances = []
    monkeypatch.setattr(engine_mod, "LLMNode", _CostedLLM)


def _spec() -> HarnessSpec:
    return HarnessSpec.model_validate({
        "name": "metered",
        "model_tiers": {"small": {"provider": "mock", "model": "m"}},
        "role_type_defaults": {"worker": "small"},
        "stages": [
            {"id": "s1", "role": {"name": "W", "type": "worker", "description": "d"}},
            {"id": "s2", "role": {"name": "W", "type": "worker", "description": "d"},
             "depends_on": ["s1"]},
        ],
    })


async def test_engine_aggregates_cost_across_stages(tmp_path):
    harness = Harness(spec=_spec(), session_dir=tmp_path, validate=False,
                      traces_db=tmp_path / "t.db")
    await harness.run({})
    assert abs(harness.total_cost_usd - 0.02) < 1e-9      # two stages, 0.01 each


async def test_engine_cost_defaults_to_zero_before_any_run(tmp_path):
    harness = Harness(spec=_spec(), session_dir=tmp_path, validate=False,
                      traces_db=tmp_path / "t.db")
    assert harness.total_cost_usd == 0.0


# ── The litellm boundary (final-review C1): _response_cost must read the
# attribute litellm actually sets — response._hidden_params["response_cost"].
# A wrong read meters $0.00 on every real run while all tests stay green.


def test_response_cost_reads_litellm_hidden_params():
    from types import SimpleNamespace
    from armature.nodes.llm import _response_cost

    resp = SimpleNamespace(_hidden_params={"response_cost": 0.0123})
    assert abs(_response_cost(resp) - 0.0123) < 1e-12


def test_response_cost_zero_without_pricing():
    from types import SimpleNamespace
    from armature.nodes.llm import _response_cost

    assert _response_cost(SimpleNamespace(_hidden_params={})) == 0.0
    assert _response_cost(SimpleNamespace()) == 0.0


# ── Tier-escalation spend (final-review I4): failed tier attempts spent
# money — the exhausted path must meter what was observed, not 0.0.


async def test_exhausted_tiers_meter_spend_from_failed_attempts(monkeypatch):
    from types import SimpleNamespace
    from armature.nodes import llm as llm_mod
    from armature.nodes.llm import LLMNode
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        OutputMode,
    )

    async def _fake_call(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="not json", tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            _hidden_params={"response_cost": 0.01},
        )

    monkeypatch.setattr(llm_mod, "_call_with_retry", _fake_call)
    spec = HarnessSpec(
        name="wf",
        model_tiers=ModelTiers(
            small=ModelTierConfig(provider="openai", model="a"),
            medium=ModelTierConfig(provider="openai", model="b"),
        ),
        stages=[Stage(
            id="s",
            role=Role(name="R", type=RoleType.WORKER, description="d",
                      model_tier="small"),
            output_mode=OutputMode.GUIDED_JSON,
            output_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            depends_on=[],
        )],
    )
    node = LLMNode(stage=spec.stages[0], tiers=spec.model_tiers)
    result = await node.execute({})
    assert result.get("_parse_error") is True
    # both tier attempts failed and both spent $0.01 — metered, not dropped
    assert abs(result["_cost_usd"] - 0.02) < 1e-9


# ── Subagent child spend (final-review I2): a child Harness's LLM cost is
# observed spend of the parent run — the engine must meter it.


async def test_engine_meters_subagent_child_spend(tmp_path):
    child = tmp_path / "child.yml"
    child.write_text(
        "name: costed-child\n"
        "model_tiers:\n"
        "  small: {provider: mock, model: m}\n"
        "role_type_defaults:\n"
        "  worker: small\n"
        "stages:\n"
        "  - id: work\n"
        "    role: {name: W, type: worker, description: d}\n"
        "    depends_on: []\n"
    )
    parent = HarnessSpec.model_validate({
        "name": "parent",
        "stages": [{"id": "sub", "subagent_spec": str(child)}],
    })
    harness = Harness(spec=parent, session_dir=tmp_path, validate=False,
                      traces_db=tmp_path / "t.db")
    await harness.run({})
    assert abs(harness.total_cost_usd - 0.01) < 1e-9
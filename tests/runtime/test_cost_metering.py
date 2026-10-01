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
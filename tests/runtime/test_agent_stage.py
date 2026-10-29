"""An agent: stage dispatches to a Claude Code session, fans out, and meters cost and llm calls."""
import pytest

from armature.runtime.engine import Harness
from armature.spec.models import Contract, HarnessSpec, Stage
from tests.nodes.test_agent import result_line, stub  # noqa: F401
from tests.events.test_claude_stream import Recorder


def spec(**stage):
    return HarnessSpec(name="wf", stages=[Stage(id="review", depends_on=[], agent={
        "model": "sonnet", "prompt": "Review as {{ specialist.name }}."}, **stage)])


async def test_agent_stage_runs_and_meters_cost(stub, tmp_path):
    stub.plan({"lines": [result_line("looks fine", cost=0.2)]})
    harness = Harness(spec=spec(), session_dir=tmp_path, validate=False)
    result = await harness.run({})
    assert result["review"] == {"content": "looks fine"}
    assert harness.total_cost_usd == pytest.approx(0.2)
    assert harness._llm_call_count == 1


async def test_agent_fan_out_runs_one_session_per_item(stub, tmp_path):
    stub.plan({"lines": [result_line("ok", cost=0.1)]})
    harness = Harness(spec=spec(fan_out=3, partition_source="{{ specialists }}", partition_key="specialist"),
                      session_dir=tmp_path, validate=False)
    result = await harness.run({"specialists": [{"name": "security"}, {"name": "correctness"}, {"name": "beauty"}]})
    assert result["review"] == [{"content": "ok"}] * 3
    prompts = sorted(call["stdin"].split("\n")[0] for call in stub.calls())
    assert prompts == ["Review as beauty.", "Review as correctness.", "Review as security."]
    assert harness.total_cost_usd == pytest.approx(0.3)


async def test_agent_fan_out_tags_events_with_the_item_role(stub, tmp_path):
    stub.plan({"lines": [result_line("ok")]})
    recorder = Recorder()
    recorder.endpoint, recorder.token = "", ""
    harness = Harness(spec=spec(fan_out=2, partition_source="{{ specialists }}", partition_key="specialist"),
                      session_dir=tmp_path, validate=False, emitter=recorder)
    await harness.run({"specialists": [{"name": "security"}, {"name": "beauty"}]})
    roles = {payload.get("role") for kind, step, payload in recorder.events if kind == "session_end"}
    assert roles == {"security", "beauty"}


async def test_max_llm_calls_stops_an_agent_stage(stub, tmp_path):
    limited = spec()
    limited.contracts = Contract(max_llm_calls=1)
    limited.stages.append(Stage(id="again", depends_on=["review"], agent={"model": "m", "prompt": "p"}))
    harness = Harness(spec=limited, session_dir=tmp_path, validate=False)
    with pytest.raises(Exception, match="max_llm_calls"):
        await harness.run({})
    assert len(stub.calls()) == 1


async def test_fan_out_cannot_overshoot_max_llm_calls(stub, tmp_path):
    stub.plan({"lines": [result_line("ok")], "sleep": 0.5})
    limited = spec(fan_out=3, partition_source="{{ specialists }}", partition_key="specialist")
    limited.contracts = Contract(max_llm_calls=2)
    harness = Harness(spec=limited, session_dir=tmp_path, validate=False)
    result = await harness.run({"specialists": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})
    assert len(stub.calls()) == 2
    assert sum("_fan_out_error" in r for r in result["review"]) == 1
    assert harness._llm_call_count == 2

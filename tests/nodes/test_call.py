import pytest

from armature.nodes.call import CallNode, final_stage_id
from armature.spec.calls import CallNotFound, MappingCallResolver
from armature.spec.models import CallRef, HarnessSpec, Stage

CALLEE = r"""# armature: stored-workflow
name: greeter
contracts:
  inputs:
    - {name: who, type: string, required: true}
adapters:
  hello: {name: hello, type: script,
          cmd: "python3 -c 'import json, os; print(\"hello\", json.loads(os.environ[\"ARMATURE_CONTEXT\"])[\"who\"])'"}
stages:
  - id: first
    adapter: hello
  - id: last
    adapter: hello
    depends_on: [first]
"""


def _node(tmp_path, **with_):
    stage = Stage(id="greet", call=CallRef(workflow="greeter"), **{"with": with_})
    return CallNode(stage=stage, resolver=MappingCallResolver({("greeter", 4): CALLEE}, {"greeter": 4}),
                    session_dir=tmp_path)


async def test_call_node_returns_final_stage_output(tmp_path):
    node = _node(tmp_path, who="{{ name }}")
    result = await node.execute({"name": "ada", "unrelated": "x"})
    assert result["stdout"].strip() == "hello ada"
    assert node.resolved_version == 4
    assert (tmp_path / "stage_greet" / "child_0").is_dir()


async def test_call_node_uses_fan_out_index_for_session_dir(tmp_path):
    await _node(tmp_path, who="b").execute({"_fan_out_item_index": 2})
    assert (tmp_path / "stage_greet" / "child_2").is_dir()


async def test_call_node_missing_required_input(tmp_path):
    with pytest.raises(ValueError, match="Required input 'who'"):
        await _node(tmp_path).execute({})


async def test_call_node_unresolved_workflow(tmp_path):
    stage = Stage(id="g", call=CallRef(workflow="ghost"))
    with pytest.raises(CallNotFound):
        await CallNode(stage=stage, resolver=MappingCallResolver({})).execute({})


def test_call_node_requires_call():
    with pytest.raises(ValueError, match="has no call"):
        CallNode(stage=Stage(id="x", gate="human"), resolver=MappingCallResolver({}))


def test_final_stage_ignores_post_run():
    spec = HarnessSpec(name="w", stages=[
        Stage(id="a", gate="human"), Stage(id="b", gate="human"),
        Stage(id="z", gate="human", post_run=True),
    ])
    assert final_stage_id(spec) == "b"

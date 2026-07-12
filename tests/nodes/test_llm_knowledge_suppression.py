from unittest.mock import MagicMock, patch
import json


def _make_node(navigation_tools, tool_names, knowledge_key="_knowledge"):
    from armature.nodes.llm import LLMNode
    from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig
    role = Role(name="Agent", type=RoleType.WORKER, description="test",
                model_tier="small", tools=tool_names)
    stage = Stage(id="agent", role=role)
    tiers = ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))
    return LLMNode(
        stage=stage, tiers=tiers, registry=None,
        navigation_tools=navigation_tools, knowledge_key=knowledge_key,
    )


async def test_suppression_removes_knowledge_when_memory_tool_declared():
    node = _make_node(navigation_tools=True, tool_names=["memory.search_records"])
    captured = {}

    async def mock_completion(**kwargs):
        captured.update(kwargs)
        r = MagicMock(); r.choices = [MagicMock()]
        r.choices[0].message.content = "ok"
        r.choices[0].message.tool_calls = None
        r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
        return r

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({"topic": "x", "_knowledge": [{"entity": "e", "fact": "f"}]})
    user_msg = captured["messages"][-1]["content"]
    ctx = json.loads(user_msg)
    assert "_knowledge" not in ctx
    assert "topic" in ctx


async def test_suppression_off_when_navigation_tools_false():
    node = _make_node(navigation_tools=False, tool_names=["memory.search_records"])
    captured = {}

    async def mock_completion(**kwargs):
        captured.update(kwargs)
        r = MagicMock(); r.choices = [MagicMock()]
        r.choices[0].message.content = "ok"
        r.choices[0].message.tool_calls = None
        r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
        return r

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({"topic": "x", "_knowledge": [{"entity": "e", "fact": "f"}]})
    ctx = json.loads(captured["messages"][-1]["content"])
    assert "_knowledge" in ctx   # kept — suppression inactive


async def test_suppression_off_when_no_memory_tool_declared():
    node = _make_node(navigation_tools=True, tool_names=[])
    captured = {}

    async def mock_completion(**kwargs):
        captured.update(kwargs)
        r = MagicMock(); r.choices = [MagicMock()]
        r.choices[0].message.content = "ok"
        r.choices[0].message.tool_calls = None
        r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
        return r

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({"topic": "x", "_knowledge": [{"entity": "e", "fact": "f"}]})
    ctx = json.loads(captured["messages"][-1]["content"])
    assert "_knowledge" in ctx   # kept — stage declares no memory.* tool


async def test_suppression_uses_custom_knowledge_key():
    node = _make_node(navigation_tools=True, tool_names=["memory.read_track"],
                     knowledge_key="_facts")
    captured = {}

    async def mock_completion(**kwargs):
        captured.update(kwargs)
        r = MagicMock(); r.choices = [MagicMock()]
        r.choices[0].message.content = "ok"
        r.choices[0].message.tool_calls = None
        r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
        return r

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({"topic": "x", "_facts": [{"entity": "e", "fact": "f"}]})
    ctx = json.loads(captured["messages"][-1]["content"])
    assert "_facts" not in ctx
"""Tests for LLMNode tool-call dispatch loop and per-stage tool filtering."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from armature.nodes.llm import LLMNode
from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig, OutputMode
from armature.registry.registry import ToolRegistry, ToolDescriptor
from armature.permissions.permissions import PermissionLevel


# ── Helpers ─────────────────────────────────────────────────────────────────

def make_tool_stage(tool_names: list[str] | None = None, provider: str = "openai") -> tuple[Stage, ModelTiers]:
    role = Role(
        name="Agent",
        type=RoleType.WORKER,
        description="test agent",
        model_tier="small",
        tools=tool_names or [],
    )
    stage = Stage(id="agent", role=role)
    tiers = ModelTiers(small=ModelTierConfig(provider=provider, model="gpt-4o-mini"))
    return stage, tiers


def make_registry(*tool_names: str, optional_param: bool = False) -> ToolRegistry:
    reg = ToolRegistry()
    for name in tool_names:
        params: dict = {"query": {"type": "string"}}
        if optional_param:
            params["top_k"] = {"type": "integer", "optional": True}

        async def handler(args, _name=name):
            return {"result": f"{_name}_result"}

        reg.register(ToolDescriptor(
            name=name,
            description=f"Tool {name}",
            permission=PermissionLevel.READ_ONLY,
            handler=handler,
            parameters=params,
        ))
    return reg


def make_plain_response(content: str) -> MagicMock:
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    r.usage.prompt_tokens = 10
    r.usage.completion_tokens = 5
    return r


def make_tool_call_response(tool_name: str, args: dict, call_id: str = "tc_1") -> MagicMock:
    r = MagicMock()
    r.choices = [MagicMock()]
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(args)
    r.choices[0].message.tool_calls = [tc]
    r.choices[0].message.content = None
    r.usage.prompt_tokens = 10
    r.usage.completion_tokens = 5
    return r


# ── Per-stage tool filtering ─────────────────────────────────────────────────

async def test_empty_role_tools_sends_no_tool_specs():
    stage, tiers = make_tool_stage(tool_names=[])
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("hello")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert "tools" not in captured
    assert "tool_choice" not in captured
    assert result["content"] == "hello"


async def test_role_tools_filters_to_declared_names_only():
    stage, tiers = make_tool_stage(tool_names=["search"])
    reg = make_registry("search", "shell", "file_write")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("done")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    names = [t["function"]["name"] for t in captured["tools"]]
    assert names == ["search"]
    assert "shell" not in names
    assert "file_write" not in names


async def test_unknown_tool_name_silently_skipped():
    stage, tiers = make_tool_stage(tool_names=["search", "does_not_exist"])
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("ok")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    names = [t["function"]["name"] for t in captured["tools"]]
    assert names == ["search"]


# ── Tool-call dispatch loop ──────────────────────────────────────────────────

async def test_tool_call_dispatched_and_result_returned():
    stage, tiers = make_tool_stage(tool_names=["search"])
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    responses = iter([
        make_tool_call_response("search", {"query": "armature"}, "tc_1"),
        make_plain_response("The answer is 42"),
    ])

    with patch("armature.nodes.llm.litellm_completion", side_effect=lambda **kw: next(responses)):
        result = await node.execute({})

    assert result["content"] == "The answer is 42"


async def test_tool_result_message_appended_to_conversation():
    stage, tiers = make_tool_stage(tool_names=["search"])
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    call_num = 0
    final_messages: list = []

    async def mock_completion(**kwargs):
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            return make_tool_call_response("search", {"query": "test"}, "tc_1")
        final_messages.extend(kwargs["messages"])
        return make_plain_response("done")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    roles = [m["role"] for m in final_messages]
    assert "assistant" in roles
    assert "tool" in roles


async def test_multi_turn_tool_loop():
    stage, tiers = make_tool_stage(tool_names=["search"])
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    responses = iter([
        make_tool_call_response("search", {"query": "first"}, "tc_1"),
        make_tool_call_response("search", {"query": "second"}, "tc_2"),
        make_plain_response("Final answer"),
    ])
    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        return next(responses)

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert call_count == 3
    assert result["content"] == "Final answer"


async def test_max_tool_iterations_caps_loop():
    stage, tiers = make_tool_stage(tool_names=["search"])
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)
    node._max_tool_iterations = 3

    call_count = 0

    async def always_tool_call(**kwargs):
        nonlocal call_count
        call_count += 1
        return make_tool_call_response("search", {"query": "loop"}, f"tc_{call_count}")

    with patch("armature.nodes.llm.litellm_completion", side_effect=always_tool_call):
        result = await node.execute({})

    # 1 initial + 3 loop iterations = 4 total calls
    assert call_count == 4
    assert "_parse_error" not in result


async def test_iterations_reset_per_tier_attempt():
    """Tool iteration budget resets on tier escalation, not shared across tiers."""
    role = Role(name="A", type=RoleType.WORKER, description="d", model_tier="small", tools=["search"])
    stage = Stage(id="s", role=role, output_mode=OutputMode.GUIDED_JSON)
    tiers = ModelTiers(
        small=ModelTierConfig(provider="openai", model="gpt-4o-mini"),
        frontier=ModelTierConfig(provider="openai", model="gpt-4o"),
    )
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)
    node._max_tool_iterations = 2

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        # First 3 calls: tool calls (hits limit on small tier, escalates)
        # Next call: valid JSON on frontier tier
        if call_count <= 3:
            return make_tool_call_response("search", {"query": "x"}, f"tc_{call_count}")
        return make_plain_response('{"ok": true}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert result.get("ok") is True


async def test_tool_dispatch_error_feeds_error_back():
    stage, tiers = make_tool_stage(tool_names=["search"])

    reg = ToolRegistry()
    async def failing_handler(args):
        raise RuntimeError("connection refused")
    reg.register(ToolDescriptor(
        name="search", description="search", permission=PermissionLevel.READ_ONLY,
        handler=failing_handler, parameters={"query": {"type": "string"}},
    ))
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    call_num = 0
    captured_tool_msg: dict = {}

    async def mock_completion(**kwargs):
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            return make_tool_call_response("search", {"query": "test"}, "tc_1")
        for m in kwargs["messages"]:
            if m["role"] == "tool":
                captured_tool_msg.update(json.loads(m["content"]))
        return make_plain_response("recovered after error")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert result["content"] == "recovered after error"
    assert "error" in captured_tool_msg


async def test_safety_rule_blocks_react_tool_call():
    """A block rule must fire on the LLM ReAct dispatch path (Claim 1).

    The ReAct loop catches the ToolBlocked raised by dispatch and feeds it back
    as a tool error result; the handler must never run.
    """
    from armature.hooks.lifecycle import HookRegistry, SafetyHookBuilder
    from armature.spec.models import SafetyCondition, ToolSafetyRule

    hooks = HookRegistry()
    SafetyHookBuilder.register(hooks, [ToolSafetyRule(
        tool="search", action="block",
        condition=SafetyCondition(field="query", op="contains", value="SENSITIVE"),
        message="sensitive query not permitted",
    )])

    reg = ToolRegistry(hooks=hooks)
    handler_called = {"n": 0}

    async def search_handler(args):
        handler_called["n"] += 1
        return {"result": "should not reach here"}

    reg.register(ToolDescriptor(
        name="search", description="search",
        permission=PermissionLevel.READ_ONLY, handler=search_handler,
        parameters={"query": {"type": "string"}},
    ))

    stage, tiers = make_tool_stage(tool_names=["search"])
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    call_num = 0
    captured_tool_msg: dict = {}

    async def mock_completion(**kwargs):
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            return make_tool_call_response("search", {"query": "SENSITIVE"}, "tc_1")
        for m in kwargs["messages"]:
            if m["role"] == "tool":
                captured_tool_msg.update(json.loads(m["content"]))
        return make_plain_response("recovered after block")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert result["content"] == "recovered after block"
    assert handler_called["n"] == 0, "blocked tool handler must not run"
    assert "error" in captured_tool_msg
    assert "sensitive query not permitted" in captured_tool_msg["error"]


# ── Ollama / _NO_STRUCTURED_OUTPUT provider ──────────────────────────────────

async def test_ollama_skips_native_tool_kwargs():
    stage, tiers = make_tool_stage(tool_names=["search"], provider="ollama")
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("answer")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    assert "tools" not in captured
    assert "tool_choice" not in captured


# ── Tool spec required field ─────────────────────────────────────────────────

async def test_required_excludes_optional_params():
    stage, tiers = make_tool_stage(tool_names=["search"])
    reg = make_registry("search", optional_param=True)
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("ok")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    tool_fn = captured["tools"][0]["function"]
    assert "query" in tool_fn["parameters"]["required"]
    assert "top_k" not in tool_fn["parameters"]["required"]


async def test_all_required_when_no_optional_flag():
    stage, tiers = make_tool_stage(tool_names=["search"])
    reg = make_registry("search", optional_param=False)
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("ok")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    required = captured["tools"][0]["function"]["parameters"]["required"]
    assert "query" in required


# ── tool_calling override flag ────────────────────────────────────────────────

async def test_tool_calling_true_enables_native_tools_for_ollama():
    """tool_calling=True overrides the provider-based Ollama exclusion."""
    stage, _ = make_tool_stage(tool_names=["search"], provider="ollama")
    tiers = ModelTiers(small=ModelTierConfig(provider="ollama", model="llama3", tool_calling=True))
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("answer")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    assert "tools" in captured
    assert "tool_choice" in captured


async def test_tool_calling_false_disables_native_tools_for_openai():
    """tool_calling=False suppresses tool injection even for supported providers."""
    stage, tiers = make_tool_stage(tool_names=["search"], provider="openai")
    tiers = ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini", tool_calling=False))
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("answer")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    assert "tools" not in captured
    assert "tool_choice" not in captured


async def test_tool_calling_none_uses_provider_heuristic_openai():
    """tool_calling=None (default) uses provider detection — OpenAI gets native tools."""
    stage, tiers = make_tool_stage(tool_names=["search"], provider="openai")
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("answer")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    assert "tools" in captured


async def test_tool_calling_none_uses_provider_heuristic_ollama():
    """tool_calling=None (default) uses provider detection — Ollama gets no native tools."""
    stage, _ = make_tool_stage(tool_names=["search"], provider="ollama")
    tiers = ModelTiers(small=ModelTierConfig(provider="ollama", model="llama3"))
    reg = make_registry("search")
    node = LLMNode(stage=stage, tiers=tiers, registry=reg)

    captured = {}
    async def mock_completion(**kwargs):
        captured.update(kwargs)
        return make_plain_response("answer")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    assert "tools" not in captured


async def test_supports_tool_calling_helper_explicit_true():
    """_supports_tool_calling returns True when explicitly set, regardless of provider."""
    from armature.spec.models import ModelTierConfig as MTC
    stage, tiers = make_tool_stage(tool_names=["search"])
    node = LLMNode(stage=stage, tiers=tiers)
    ollama_cfg = MTC(provider="ollama", model="x", tool_calling=True)
    assert node._supports_tool_calling(ollama_cfg) is True


async def test_supports_tool_calling_helper_explicit_false():
    """_supports_tool_calling returns False when explicitly set, regardless of provider."""
    from armature.spec.models import ModelTierConfig as MTC
    stage, tiers = make_tool_stage(tool_names=["search"])
    node = LLMNode(stage=stage, tiers=tiers)
    openai_cfg = MTC(provider="openai", model="gpt-4o-mini", tool_calling=False)
    assert node._supports_tool_calling(openai_cfg) is False

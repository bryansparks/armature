import pytest
from armature.registry.registry import ToolRegistry, ToolDescriptor, PermissionLevel


def test_register_and_lookup():
    registry = ToolRegistry()
    async def my_tool(args): return {"result": "ok"}

    registry.register(ToolDescriptor(
        name="my_tool",
        description="Does something",
        permission=PermissionLevel.READ_ONLY,
        handler=my_tool,
    ))
    desc = registry.get("my_tool")
    assert desc is not None
    assert desc.name == "my_tool"


def test_unknown_tool_returns_none():
    registry = ToolRegistry()
    assert registry.get("nonexistent") is None


def test_list_descriptors_for_llm():
    registry = ToolRegistry()
    async def tool_a(args): return {}
    registry.register(ToolDescriptor(
        name="tool_a", description="Tool A",
        permission=PermissionLevel.READ_ONLY, handler=tool_a,
    ))
    descriptors = registry.descriptors()
    assert any(d["name"] == "tool_a" for d in descriptors)


async def test_dispatch_tool():
    registry = ToolRegistry()
    async def echo(args): return {"echo": args.get("msg")}
    registry.register(ToolDescriptor(
        name="echo", description="Echoes input",
        permission=PermissionLevel.READ_ONLY, handler=echo,
    ))
    result = await registry.dispatch("echo", {"msg": "hello"})
    assert result["echo"] == "hello"


async def test_dispatch_unknown_tool_raises_key_error():
    registry = ToolRegistry()
    with pytest.raises(KeyError, match="nonexistent"):
        await registry.dispatch("nonexistent", {})


async def test_dispatch_destructive_tool_raises_permission_error():
    registry = ToolRegistry()
    async def dangerous(args): return {}
    registry.register(ToolDescriptor(
        name="dangerous", description="Dangerous",
        permission=PermissionLevel.DESTRUCTIVE, handler=dangerous,
    ))
    with pytest.raises(PermissionError, match="dangerous"):
        await registry.dispatch("dangerous", {})


def test_register_overwrites_existing_tool():
    """Re-registering a tool name replaces the prior descriptor."""
    registry = ToolRegistry()
    async def v1(args): return {"version": 1}
    async def v2(args): return {"version": 2}
    registry.register(ToolDescriptor(name="t", description="v1", permission=PermissionLevel.READ_ONLY, handler=v1))
    registry.register(ToolDescriptor(name="t", description="v2", permission=PermissionLevel.READ_ONLY, handler=v2))
    assert registry.get("t").description == "v2"


def test_descriptors_includes_parameters():
    registry = ToolRegistry()
    async def parameterized(args): return {}
    registry.register(ToolDescriptor(
        name="p", description="Has params",
        permission=PermissionLevel.READ_ONLY,
        handler=parameterized,
        parameters={"query": {"type": "string"}},
    ))
    descs = registry.descriptors()
    p_desc = next(d for d in descs if d["name"] == "p")
    assert p_desc["parameters"] == {"query": {"type": "string"}}


def test_descriptors_empty_registry():
    registry = ToolRegistry()
    assert registry.descriptors() == []


# ── dispatch chokepoint: safety hooks fire on every dispatch (Claim 1) ──────────

async def test_dispatch_runs_pre_tool_hook_and_blocks():
    """A block rule on the hook registry must stop dispatch before the handler runs."""
    from armature.hooks.lifecycle import HookRegistry, SafetyHookBuilder, ToolBlocked
    from armature.spec.models import SafetyCondition, ToolSafetyRule

    hooks = HookRegistry()
    SafetyHookBuilder.register(hooks, [ToolSafetyRule(
        tool="echo", action="block",
        condition=SafetyCondition(field="msg", op="contains", value="forbidden"),
        message="blocked by rule",
    )])

    registry = ToolRegistry(hooks=hooks)
    ran = {"called": False}
    async def echo(args):
        ran["called"] = True
        return {"echo": args.get("msg")}
    registry.register(ToolDescriptor(
        name="echo", description="Echoes input",
        permission=PermissionLevel.READ_ONLY, handler=echo,
    ))

    with pytest.raises(ToolBlocked):
        await registry.dispatch("echo", {"msg": "forbidden value"})
    assert ran["called"] is False, "handler must not run when a block rule fires"


async def test_dispatch_runs_post_tool_hook_after_success():
    """A post-tool hook must fire after a successful dispatch."""
    from armature.hooks.lifecycle import HookRegistry, HookPhase, HookDecision

    hooks = HookRegistry()
    seen = {"tool": None, "result": None}
    async def post_hook(phase, tool_name, result, ctx):
        seen["tool"] = tool_name
        seen["result"] = result
    hooks.register(HookPhase.POST_TOOL, post_hook)

    registry = ToolRegistry(hooks=hooks)
    async def echo(args):
        return {"echo": args.get("msg")}
    registry.register(ToolDescriptor(
        name="echo", description="Echoes input",
        permission=PermissionLevel.READ_ONLY, handler=echo,
    ))

    result = await registry.dispatch("echo", {"msg": "hi"})
    assert result["echo"] == "hi"
    assert seen["tool"] == "echo"
    assert seen["result"] == {"echo": "hi"}


async def test_dispatch_without_hooks_behaves_as_before():
    """A registry with no hooks attached must dispatch exactly as before."""
    registry = ToolRegistry()
    async def echo(args):
        return {"echo": args.get("msg")}
    registry.register(ToolDescriptor(
        name="echo", description="Echoes input",
        permission=PermissionLevel.READ_ONLY, handler=echo,
    ))
    result = await registry.dispatch("echo", {"msg": "ok"})
    assert result["echo"] == "ok"


async def test_dispatch_attach_hooks_after_construction():
    """attach_hooks() wires enforcement into an already-constructed registry."""
    from armature.hooks.lifecycle import HookRegistry, SafetyHookBuilder, ToolBlocked
    from armature.spec.models import SafetyCondition, ToolSafetyRule

    registry = ToolRegistry()
    async def echo(args):
        return {"echo": args.get("msg")}
    registry.register(ToolDescriptor(
        name="echo", description="Echoes input",
        permission=PermissionLevel.READ_ONLY, handler=echo,
    ))

    # No hooks yet -> allowed.
    assert (await registry.dispatch("echo", {"msg": "ok"}))["echo"] == "ok"

    hooks = HookRegistry()
    SafetyHookBuilder.register(hooks, [ToolSafetyRule(
        tool="echo", action="block",
        condition=SafetyCondition(field="msg", op="contains", value="bad"),
        message="blocked",
    )])
    registry.attach_hooks(hooks)

    with pytest.raises(ToolBlocked):
        await registry.dispatch("echo", {"msg": "bad"})

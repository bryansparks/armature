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

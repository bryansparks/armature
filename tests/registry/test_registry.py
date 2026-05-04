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

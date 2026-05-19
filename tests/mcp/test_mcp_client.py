"""Tests for MCP server integration — MCPServerConfig and MCPRegistrar."""
import pytest
from types import SimpleNamespace
from armature.registry.registry import ToolRegistry


# ── Fake session ─────────────────────────────────────────────────────────────

def _make_fake_tool(name: str, description: str = "", schema: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, inputSchema=schema or {})


class FakeMCPSession:
    """Minimal fake MCP session for unit testing."""

    def __init__(self, tools: list[SimpleNamespace] | None = None):
        self._tools = tools or []
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self) -> None:
        pass

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, tool_name: str, arguments: dict) -> SimpleNamespace:
        self.calls.append((tool_name, arguments))
        return SimpleNamespace(content=[SimpleNamespace(text=f"result:{tool_name}")])


async def _fake_factory(tools: list[SimpleNamespace]):
    """Returns a session_factory closure with the given tools."""
    session = FakeMCPSession(tools)
    async def factory(config):
        return session
    return factory, session


# ── Phase 3-a: MCPServerConfig model ─────────────────────────────────────────

def test_mcp_server_config_importable():
    from armature.spec.models import MCPServerConfig
    assert MCPServerConfig is not None


def test_mcp_server_config_stdio_fields():
    from armature.spec.models import MCPServerConfig
    cfg = MCPServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "server"])
    assert cfg.name == "fs"
    assert cfg.transport == "stdio"
    assert cfg.command == "npx"
    assert cfg.args == ["-y", "server"]


def test_mcp_server_config_http_fields():
    from armature.spec.models import MCPServerConfig
    cfg = MCPServerConfig(name="search", transport="http", url="http://localhost:8001/mcp")
    assert cfg.transport == "http"
    assert cfg.url == "http://localhost:8001/mcp"


def test_mcp_server_config_defaults():
    from armature.spec.models import MCPServerConfig
    cfg = MCPServerConfig(name="x", transport="stdio", command="cmd")
    assert cfg.args == []
    assert cfg.headers == {}
    assert cfg.env == {}
    assert cfg.timeout_s == 30.0


def test_harness_spec_has_mcp_servers():
    from armature.spec.models import HarnessSpec
    spec = HarnessSpec(name="wf", stages=[])
    assert spec.mcp_servers == []


def test_harness_spec_accepts_mcp_servers():
    from armature.spec.models import HarnessSpec, MCPServerConfig
    cfg = MCPServerConfig(name="fs", transport="stdio", command="npx")
    spec = HarnessSpec(name="wf", stages=[], mcp_servers=[cfg])
    assert len(spec.mcp_servers) == 1
    assert spec.mcp_servers[0].name == "fs"


# ── Phase 3-a: MCPRegistrar ───────────────────────────────────────────────────

def test_mcp_registrar_importable():
    from armature.mcp.client import MCPRegistrar
    assert MCPRegistrar is not None


async def test_register_all_with_fake_session_registers_tools():
    from armature.spec.models import MCPServerConfig
    from armature.mcp.client import MCPRegistrar

    tools = [_make_fake_tool("read_file", "Read a file"), _make_fake_tool("write_file", "Write a file")]
    factory, session = await _fake_factory(tools)

    registry = ToolRegistry()
    cfg = MCPServerConfig(name="fs", transport="stdio", command="npx")
    await MCPRegistrar.register_all([cfg], registry, session_factory=factory)

    assert registry.get("fs.read_file") is not None
    assert registry.get("fs.write_file") is not None


async def test_register_all_uses_qualified_names():
    from armature.spec.models import MCPServerConfig
    from armature.mcp.client import MCPRegistrar

    tools = [_make_fake_tool("search")]
    factory, _ = await _fake_factory(tools)

    registry = ToolRegistry()
    cfg = MCPServerConfig(name="web", transport="http", url="http://localhost/mcp")
    await MCPRegistrar.register_all([cfg], registry, session_factory=factory)

    assert registry.get("web.search") is not None
    assert registry.get("search") is None  # unqualified name not registered


async def test_register_all_multiple_servers():
    from armature.spec.models import MCPServerConfig
    from armature.mcp.client import MCPRegistrar

    tools_a = [_make_fake_tool("read_file")]
    tools_b = [_make_fake_tool("query")]

    sessions_iter = iter([FakeMCPSession(tools_a), FakeMCPSession(tools_b)])
    async def factory(config):
        return next(sessions_iter)

    registry = ToolRegistry()
    configs = [
        MCPServerConfig(name="fs", transport="stdio", command="npx"),
        MCPServerConfig(name="db", transport="stdio", command="uvx"),
    ]
    await MCPRegistrar.register_all(configs, registry, session_factory=factory)

    assert registry.get("fs.read_file") is not None
    assert registry.get("db.query") is not None


async def test_registered_mcp_tool_is_callable():
    from armature.spec.models import MCPServerConfig
    from armature.mcp.client import MCPRegistrar

    tools = [_make_fake_tool("echo")]
    factory, session = await _fake_factory(tools)

    registry = ToolRegistry()
    cfg = MCPServerConfig(name="util", transport="stdio", command="echo")
    await MCPRegistrar.register_all([cfg], registry, session_factory=factory)

    result = await registry.dispatch("util.echo", {"text": "hello"})
    assert result is not None


async def test_mcp_tool_handler_calls_session_call_tool():
    from armature.spec.models import MCPServerConfig
    from armature.mcp.client import MCPRegistrar

    tools = [_make_fake_tool("greet")]
    factory, session = await _fake_factory(tools)

    registry = ToolRegistry()
    cfg = MCPServerConfig(name="svc", transport="stdio", command="cmd")
    await MCPRegistrar.register_all([cfg], registry, session_factory=factory)

    await registry.dispatch("svc.greet", {"name": "world"})
    assert len(session.calls) == 1
    assert session.calls[0][0] == "greet"
    assert session.calls[0][1] == {"name": "world"}


async def test_register_all_tool_description_from_session():
    from armature.spec.models import MCPServerConfig
    from armature.mcp.client import MCPRegistrar

    tools = [_make_fake_tool("read_file", description="Reads a file from disk")]
    factory, _ = await _fake_factory(tools)

    registry = ToolRegistry()
    cfg = MCPServerConfig(name="fs", transport="stdio", command="npx")
    await MCPRegistrar.register_all([cfg], registry, session_factory=factory)

    descriptor = registry.get("fs.read_file")
    assert "Reads a file from disk" in descriptor.description


async def test_empty_servers_list_registers_nothing():
    from armature.mcp.client import MCPRegistrar

    registry = ToolRegistry()
    await MCPRegistrar.register_all([], registry)
    assert registry.descriptors() == []

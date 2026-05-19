"""MCP (Model Context Protocol) client integration for Armature.

Discovers tools from MCP servers and registers them in the ToolRegistry
under qualified names: ``{server_name}.{tool_name}``.

Real MCP sessions (stdio/http/sse) require the ``mcp`` optional package.
Pass ``session_factory`` to inject fake sessions for testing.
"""
from __future__ import annotations
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from armature.spec.models import MCPServerConfig
    from armature.registry.registry import ToolRegistry


class MCPRegistrar:
    @staticmethod
    async def register_all(
        servers: "list[MCPServerConfig]",
        registry: "ToolRegistry",
        *,
        session_factory: Callable | None = None,
    ) -> list[Any]:
        """Connect to each MCP server, discover its tools, and register them.

        Returns the list of open sessions so the caller can manage lifecycle.
        """
        sessions: list[Any] = []
        factory = session_factory or MCPRegistrar._default_factory

        for config in servers:
            session = await factory(config)
            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                qualified = f"{config.name}.{tool.name}"
                handler = MCPRegistrar._make_handler(session, tool.name)
                from armature.registry.registry import ToolDescriptor
                from armature.permissions.permissions import PermissionLevel
                registry.register(ToolDescriptor(
                    name=qualified,
                    description=tool.description or qualified,
                    permission=PermissionLevel.READ_ONLY,
                    handler=handler,
                    parameters=getattr(tool, "inputSchema", {}),
                ))
            sessions.append(session)

        return sessions

    @staticmethod
    def _make_handler(session: Any, tool_name: str) -> Callable:
        async def handler(args: dict) -> dict:
            result = await session.call_tool(tool_name, arguments=args)
            if hasattr(result, "content"):
                parts = [
                    c.text if hasattr(c, "text") else str(c)
                    for c in result.content
                ]
                return {"output": "\n".join(parts)}
            return {"output": str(result)}
        return handler

    @staticmethod
    async def _default_factory(config: "MCPServerConfig") -> Any:
        """Create a real MCP session. Requires the ``mcp`` package."""
        try:
            from mcp import ClientSession
        except ImportError as exc:
            raise ImportError(
                "MCP server support requires the 'mcp' package. "
                "Install it with: pip install 'armature[mcp]'"
            ) from exc

        if config.transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(
                command=config.command or "",
                args=config.args,
                env=config.env or None,
            )
            read, write = await stdio_client(params).__aenter__()
            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
            return session

        if config.transport in ("http", "sse"):
            from mcp.client.sse import sse_client
            headers = config.headers or {}
            read, write = await sse_client(config.url or "", headers=headers).__aenter__()
            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
            return session

        raise ValueError(f"Unknown MCP transport: {config.transport!r}")

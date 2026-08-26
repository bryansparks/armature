"""Vendored echo tool for the echo-tool example package.

Registered by the engine when the spec declares `tools: - module: echo_tool`.
Returns its `msg` argument as the stage's `content` output, which the package
results writer extracts into `artifacts/echo.md`.
"""
from armature.registry.registry import ToolDescriptor, PermissionLevel


def register(registry):
    async def echo(args):
        return {"content": args.get("msg", "")}

    registry.register(ToolDescriptor(
        name="echo",
        description="Echo the msg argument into the stage output.",
        permission=PermissionLevel.READ_ONLY,
        handler=echo,
        parameters={"msg": {"type": "string", "description": "Message to echo."}},
    ))
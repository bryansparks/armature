from __future__ import annotations
from typing import Callable, Any
from pydantic import BaseModel
from armature.permissions.permissions import PermissionLevel, Reversibility, requires_approval


class ToolDescriptor(BaseModel):
    name: str
    description: str
    permission: PermissionLevel
    handler: Callable
    parameters: dict[str, Any] = {}
    reversibility: Reversibility = Reversibility.FULL
    postcondition: "Callable[[dict, Any], bool] | None" = None

    model_config = {"arbitrary_types_allowed": True}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDescriptor] = {}

    def register(self, descriptor: ToolDescriptor) -> None:
        self._tools[descriptor.name] = descriptor

    def get(self, name: str) -> ToolDescriptor | None:
        return self._tools.get(name)

    def descriptors(self) -> list[dict[str, Any]]:
        return [
            {"name": d.name, "description": d.description, "parameters": d.parameters}
            for d in self._tools.values()
        ]

    async def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        desc = self.get(name)
        if desc is None:
            raise KeyError(f"Tool not found: {name}")
        if requires_approval(desc.permission):
            raise PermissionError(
                f"Tool '{name}' requires explicit approval (permission: {desc.permission}). "
                "Register a pre-tool hook to gate DESTRUCTIVE tools."
            )
        result = await desc.handler(args)
        if desc.postcondition is not None and not desc.postcondition(args, result):
            from armature.hooks.lifecycle import PostconditionFailed
            raise PostconditionFailed(name, result)
        return result

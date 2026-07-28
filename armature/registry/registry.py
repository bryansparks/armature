from __future__ import annotations
from typing import Callable, Any, TYPE_CHECKING
from pydantic import BaseModel
from armature.permissions.permissions import PermissionLevel, Reversibility, requires_approval

if TYPE_CHECKING:
    from armature.hooks.lifecycle import HookRegistry


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
    def __init__(self, hooks: "HookRegistry | None" = None):
        self._tools: dict[str, ToolDescriptor] = {}
        self._hooks: "HookRegistry | None" = None
        if hooks is not None:
            self.attach_hooks(hooks)

    def attach_hooks(self, hooks: "HookRegistry") -> None:
        """Attach a HookRegistry so ``dispatch`` enforces pre/post-tool hooks.

        This is the single chokepoint for ``safety_rules`` / ``strict_mode``
        enforcement: every tool invocation — direct ``tool_call:`` stages, LLM
        ReAct tool calls, and adapter/script stages alike — flows through
        ``dispatch`` (or, for adapters, the engine's explicit ``run_pre_tool``
        call), so a block rule registered here fires on every path.
        """
        self._hooks = hooks

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
        # Safety rules first: a block rule must fire before the DESTRUCTIVE floor
        # or the handler, so spec authors can gate any tool (including WORKSPACE
        # tools like `shell`) with `safety_rules`. ToolBlocked propagates.
        if self._hooks is not None:
            from armature.hooks.lifecycle import HookDecision
            decision = await self._hooks.run_pre_tool(name, args, {})
            if decision == HookDecision.BLOCK:
                from armature.hooks.lifecycle import ToolBlocked
                raise ToolBlocked(name, args.get("cmd", ""), "blocked by safety rule")
        if requires_approval(desc.permission):
            raise PermissionError(
                f"Tool '{name}' requires explicit approval (permission: {desc.permission}). "
                "Register a pre-tool hook to gate DESTRUCTIVE tools."
            )
        result = await desc.handler(args)
        if self._hooks is not None:
            await self._hooks.run_post_tool(name, result, {})
        if desc.postcondition is not None and not desc.postcondition(args, result):
            from armature.hooks.lifecycle import PostconditionFailed
            raise PostconditionFailed(name, result)
        return result

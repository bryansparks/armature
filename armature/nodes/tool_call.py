from __future__ import annotations
from typing import Any
from armature.nodes.base import BaseNode
from armature.spec.models import Stage


class ToolCallNode(BaseNode):
    """Directly invokes a registered tool without involving an LLM.

    The stage's tool_call.args are Jinja2-rendered against the current context
    so upstream stage outputs can be forwarded as arguments. The tool's return
    value becomes the stage result, stored in context under the stage id.
    """

    def __init__(self, stage: Stage, registry: Any):
        if stage.tool_call is None:
            raise ValueError(f"Stage '{stage.id}' has no tool_call config")
        self._stage = stage
        self._registry = registry

    async def execute(self, context: dict[str, Any]) -> Any:
        cfg = self._stage.tool_call
        args = _render_args(cfg.args, context)
        return await self._registry.dispatch(cfg.name, args)


def _render_args(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Jinja2-render string values in args against context. Non-strings pass through."""
    if not any(isinstance(v, str) and "{{" in v for v in args.values()):
        return args
    from jinja2 import ChainableUndefined, Environment, BaseLoader
    env = Environment(loader=BaseLoader(), undefined=ChainableUndefined)
    rendered = {}
    for k, v in args.items():
        if isinstance(v, str) and "{{" in v:
            rendered[k] = env.from_string(v).render(**context)
        else:
            rendered[k] = v
    return rendered

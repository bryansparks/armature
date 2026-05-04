from __future__ import annotations
from typing import Any, Callable, Awaitable


class LoopExecutor:
    def __init__(
        self,
        handler: Callable[[dict], Awaitable[Any]],
        until: Callable[[Any], bool],
        max_iterations: int = 10,
    ):
        self._handler = handler
        self._until = until
        self._max = max_iterations

    async def run(self, initial_ctx: dict[str, Any]) -> Any:
        ctx = dict(initial_ctx)
        result = None
        for iteration in range(self._max):
            result = await self._handler(ctx)
            if isinstance(result, dict):
                ctx.update(result)
            if self._until(result):
                break
        return result

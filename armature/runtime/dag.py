from __future__ import annotations
import asyncio
from collections import defaultdict, deque
from typing import Callable, Any


def topological_order(deps: dict[str, list[str]]) -> list[str]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = defaultdict(int)
    all_nodes: set[str] = set(deps.keys())

    for node, predecessors in deps.items():
        for pred in predecessors:
            all_nodes.add(pred)
            adjacency[pred].append(node)
            in_degree[node] += 1

    for node in all_nodes:
        if node not in in_degree:
            in_degree[node] = 0

    queue = deque(n for n in all_nodes if in_degree[n] == 0)
    order = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for successor in adjacency[node]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if len(order) != len(all_nodes):
        raise ValueError("DAG has a cycle — cannot determine execution order")
    return order


class DAGExecutor:
    """Execute a DAG of async handlers, running independent stages concurrently.

    Stages whose dependencies have all completed form a "ready wave" and are
    launched together via asyncio.gather. Results are merged into a shared
    context dict after each wave, so downstream stages see all upstream outputs.
    """

    def __init__(
        self,
        handlers: dict[str, Callable],
        deps: dict[str, list[str]],
    ):
        self._handlers = handlers
        self._deps = deps

    async def run(self, initial_ctx: dict[str, Any]) -> dict[str, Any]:
        results: dict[str, Any] = dict(initial_ctx)

        stage_ids = set(self._handlers.keys())

        # remaining[s] = set of dependency stage ids still to complete
        remaining: dict[str, set[str]] = {
            sid: {d for d in self._deps.get(sid, []) if d in stage_ids}
            for sid in stage_ids
        }
        completed: set[str] = set()

        while len(completed) < len(stage_ids):
            # Stages whose all dependencies are satisfied
            wave = [
                sid for sid in stage_ids
                if sid not in completed and not remaining[sid] - completed
            ]

            if not wave:
                raise ValueError(
                    "DAG deadlock — all remaining stages have unresolvable dependencies"
                )

            # Run this wave concurrently; raise immediately on first failure
            async def _run_one(sid: str) -> tuple[str, Any]:
                return sid, await self._handlers[sid](results)

            wave_results = await asyncio.gather(*[_run_one(sid) for sid in wave])

            for sid, result in wave_results:
                results[sid] = result
                completed.add(sid)

        return results

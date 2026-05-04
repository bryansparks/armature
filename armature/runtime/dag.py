from __future__ import annotations
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
    def __init__(
        self,
        handlers: dict[str, Callable],
        deps: dict[str, list[str]],
    ):
        self._handlers = handlers
        self._deps = deps

    async def run(self, initial_ctx: dict[str, Any]) -> dict[str, Any]:
        order = topological_order(self._deps)
        results: dict[str, Any] = dict(initial_ctx)

        for stage_id in order:
            if stage_id not in self._handlers:
                continue
            handler = self._handlers[stage_id]
            stage_result = await handler(results)
            results[stage_id] = stage_result

        return results

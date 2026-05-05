from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Any
from armature.nodes.base import BaseNode
from armature.spec.models import Stage
from armature.spec.loader import load_spec


def _partition(items: list, n: int) -> list[list]:
    size, rem = divmod(len(items), n)
    chunks, start = [], 0
    for i in range(n):
        end = start + size + (1 if i < rem else 0)
        chunks.append(items[start:end])
        start = end
    return chunks


def _fan_in(results: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    if strategy == "first":
        return results[0] if results else {}
    if strategy == "merge":
        merged: dict[str, Any] = {}
        for r in results:
            merged.update(r)
        return merged
    return {"results": results}


class SubagentNode(BaseNode):
    def __init__(self, stage: Stage, session_dir: Path | None = None):
        if not stage.subagent_spec:
            raise ValueError(f"Stage '{stage.id}' has no subagent_spec")
        self._stage = stage
        self._session_dir = session_dir

    async def _run_child(self, context: dict[str, Any], child_index: int) -> dict[str, Any]:
        from armature.runtime.engine import Harness

        spec_path = Path(self._stage.subagent_spec)
        if not spec_path.exists():
            raise FileNotFoundError(f"Subagent spec not found: {spec_path}")

        child_dir: Path | None = None
        if self._session_dir is not None:
            child_dir = self._session_dir / f"child_{child_index}"
            child_dir.mkdir(parents=True, exist_ok=True)

        child = Harness(
            spec=load_spec(spec_path, vars=context),
            session_dir=child_dir,
        )
        return await child.run(context)

    def _build_contexts(self, context: dict[str, Any], n: int) -> list[dict[str, Any]]:
        key = self._stage.partition_key
        if key and key in context and isinstance(context[key], list):
            chunks = _partition(context[key], n)
            return [{**context, key: chunk} for chunk in chunks]
        return [dict(context) for _ in range(n)]

    async def execute(self, context: dict[str, Any]) -> Any:
        n = self._stage.fan_out
        if n is None:
            return await self._run_child(context, 0)
        if n == 1:
            result = await self._run_child(context, 0)
            return _fan_in([result], self._stage.fan_in)

        contexts = self._build_contexts(context, n)
        tasks = [self._run_child(ctx, i) for i, ctx in enumerate(contexts)]
        results = await asyncio.gather(*tasks)
        return _fan_in(list(results), self._stage.fan_in)

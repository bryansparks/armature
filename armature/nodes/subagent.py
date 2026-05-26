from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Any
from armature.nodes.base import BaseNode
from armature.spec.models import Stage
from armature.spec.loader import load_spec


async def litellm_completion(**kwargs) -> Any:
    import litellm
    return await litellm.acompletion(**kwargs)


def _partition(items: list, n: int) -> list[list[Any]]:
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
    if strategy == "consensus":
        return {"results": results, "_needs_consensus": True}
    return {"results": results}


async def _consensus_judge(results: list[dict[str, Any]], stage: Stage) -> dict[str, Any]:
    """Call a judge LLM to synthesize conflicting parallel subagent outputs."""
    results_text = json.dumps(results, indent=2, default=str)
    model = "openai/gpt-4o-mini"
    if stage.role is not None:
        pass  # subagent stages don't have a role — use default

    response = await litellm_completion(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a consensus judge. You receive multiple parallel agent outputs "
                    "and must synthesize them into a single best answer. "
                    "Return a JSON object with the synthesized result."
                ),
            },
            {
                "role": "user",
                "content": f"Parallel outputs to synthesize:\n{results_text}",
            },
        ],
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"consensus_output": raw, "source_results": results}


class SubagentNode(BaseNode):
    def __init__(self, stage: Stage, session_dir: Path | None = None):
        if not stage.subagent_spec:
            raise ValueError(f"Stage '{stage.id}' has no subagent_spec")
        self._stage = stage
        self._session_dir = session_dir

    def _resolve_child_context(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self._stage.isolated:
            return context
        sig = self._stage.signature
        if sig is None or not sig.input:
            return {}
        return {k: context[k] for k in sig.input if k in context}

    async def _run_child(self, context: dict[str, Any], child_index: int) -> dict[str, Any]:
        from armature.runtime.engine import Harness

        spec_path = Path(self._stage.subagent_spec)
        if not spec_path.exists():
            raise FileNotFoundError(f"Subagent spec not found: {spec_path}")

        child_dir: Path | None = None
        if self._session_dir is not None:
            child_dir = self._session_dir / f"child_{child_index}"
            child_dir.mkdir(parents=True, exist_ok=True)

        child_context = self._resolve_child_context(context)
        child = Harness(
            spec=load_spec(spec_path, vars=child_context),
            session_dir=child_dir,
        )
        return await child.run(child_context)

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
        if n < 1:
            raise ValueError(f"Stage '{self._stage.id}': fan_out must be >= 1, got {n}")
        if n == 1:
            result = await self._run_child(context, 0)
            return _fan_in([result], self._stage.fan_in)

        contexts = self._build_contexts(context, n)
        tasks = [self._run_child(ctx, i) for i, ctx in enumerate(contexts)]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        merged = _fan_in(list(results), self._stage.fan_in)
        if merged.get("_needs_consensus"):
            return await _consensus_judge(list(results), self._stage)
        return merged

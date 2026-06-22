"""Adapter evaluation harness.

Benchmarks an adapter by running a target workflow stage twice: once with the
adapter active (via a custom adapter registry) and once without, then compares
quality signals extracted from the traces.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from armature.adapters.registry import AdapterRegistry
from armature.runtime.engine import Harness
from armature.spec.loader import load_spec


@dataclass
class AdapterEvalResult:
    adapter_name: str
    adapter_version: str
    with_adapter_score: float | None
    without_adapter_score: float | None
    examples: int

    @property
    def delta(self) -> float | None:
        if self.with_adapter_score is None or self.without_adapter_score is None:
            return None
        return self.with_adapter_score - self.without_adapter_score


async def evaluate_adapter(
    registry: AdapterRegistry,
    name: str,
    version: str | None,
    spec_path: Path,
    inputs: dict[str, Any] | None = None,
    stage_id: str | None = None,
) -> AdapterEvalResult:
    """Run a spec with and without the adapter active and compare scores.

    Args:
        registry: AdapterRegistry containing the adapter to evaluate.
        name: Adapter name.
        version: Adapter version; None resolves to latest.
        spec_path: Path to the workflow spec.
        inputs: Runtime inputs for the workflow.
        stage_id: Specific stage to score; if None, uses the first judge stage
                  or the deepest leaf stage.
    """
    spec = load_spec(spec_path)
    resolved = registry.get(name, version)

    if stage_id is None:
        stage_id = _pick_evaluation_stage(spec)

    # Run with the adapter active.
    harness_with = Harness(
        spec,
        use_cache=False,
        adapter_registry=registry,
    )
    results_with = await harness_with.run(inputs or {})
    with_score = _extract_stage_score(harness_with, results_with, stage_id)

    # Run without the adapter (default empty registry).
    harness_without = Harness(spec, use_cache=False)
    results_without = await harness_without.run(inputs or {})
    without_score = _extract_stage_score(harness_without, results_without, stage_id)

    return AdapterEvalResult(
        adapter_name=resolved.metadata.name,
        adapter_version=resolved.metadata.version,
        with_adapter_score=with_score,
        without_adapter_score=without_score,
        examples=1,
    )


def _pick_evaluation_stage(spec) -> str:
    """Choose a stage to evaluate: first judge, else last non-post-run stage."""
    for stage in spec.stages:
        if stage.role and stage.role.type.value == "judge" and not stage.post_run:
            return stage.id
    for stage in reversed(spec.stages):
        if not stage.post_run:
            return stage.id
    raise ValueError("No suitable evaluation stage found in spec")


def _extract_stage_score(
    harness: Harness, results: dict[str, Any], stage_id: str
) -> float | None:
    """Best-effort extraction of a 0-1 quality score from a completed run."""
    # Prefer the trace store quorum_score if available.
    try:
        import asyncio

        traces = asyncio.run(_last_trace_for_stage(harness, stage_id))
        if traces:
            return traces[0].quorum_score
    except Exception:
        pass
    # Fall back to parsing a 'score' or 'confidence' field in the stage output.
    from armature.runtime.engine import _resolve_dot_path

    result = results.get(stage_id, {})
    if isinstance(result, dict):
        for key in ("score", "confidence", "quality", "accept"):
            val = _resolve_dot_path(result, key)
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, bool):
                return 1.0 if val else 0.0
    return None


async def _last_trace_for_stage(harness: Harness, stage_id: str):
    await harness._ensure_traces()
    traces = await harness._traces.query(
        workflow_name=harness.name,
        stage_id=stage_id,
        limit=1,
    )
    return traces

from __future__ import annotations
from armature.state.traces import TraceStore


class BootstrapStore:
    """Retrieves high-quality few-shot examples from TraceStore for prompt injection."""

    def __init__(self, traces: TraceStore):
        self._traces = traces

    async def examples_for_stage(
        self,
        workflow_name: str,
        stage_id: str,
        min_score: float = 0.85,
        max_examples: int = 5,
    ) -> list[dict]:
        """Return (inputs, outputs) pairs from traces meeting the quality threshold."""
        records = await self._traces.query(
            workflow_name=workflow_name,
            stage_id=stage_id,
            min_quorum_score=min_score,
            limit=max_examples,
        )
        return [{"inputs": r.inputs, "outputs": r.outputs} for r in records]

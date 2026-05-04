from __future__ import annotations
from typing import Any
import httpx
from armature.hooks.lifecycle import HookPhase, HookRegistry


async def submit_trace(args: dict[str, Any]) -> dict[str, Any]:
    """
    Submit a trace record to Alembic for SLM fine-tuning data.
    Args: { trace: dict (TraceRecord.model_dump()), score: float, alembic_url: str }
    Returns: { submitted: bool, trace_id: str }
    """
    alembic_url = args.get("alembic_url", "http://localhost:8001")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{alembic_url}/traces/submit",
            json={"trace": args["trace"], "score": args.get("score", 0.0)},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return {"submitted": True, "trace_id": data.get("trace_id", "")}


def register_alembic_hook(
    hook_registry: HookRegistry,
    threshold: float = 0.85,
    alembic_url: str = "http://localhost:8001",
) -> None:
    """
    Register a POST_STAGE hook that submits high-quality traces to Alembic.
    Only submits when quorum_score >= threshold (requires Quorum to have run).
    """
    async def _alembic_post_stage(phase, stage_id, result, ctx):
        score = ctx.get("_quorum_score")
        if score is not None and score >= threshold:
            trace_data = {k: v for k, v in result.items() if not k.startswith("_")}
            try:
                await submit_trace({
                    "trace": {"stage_id": stage_id, "outputs": trace_data, "run_id": ctx.get("run_id", "")},
                    "score": score,
                    "alembic_url": alembic_url,
                })
            except Exception:
                pass  # Never block execution on Alembic submission failure

    hook_registry.register(HookPhase.POST_STAGE, _alembic_post_stage)

from __future__ import annotations
from typing import Any


async def deliberate(args: dict[str, Any]) -> dict[str, Any]:
    """
    Calls Quorum's QuorumEngine for deliberation.
    Args: { topic: str, brief: str (optional), agents: list[str] (optional) }
    Returns: { decision: str, confidence: float, dissents: list[str], trace: dict }
    """
    try:
        from quorum import Quorum, QuorumConfig  # type: ignore
    except ImportError:
        raise ImportError(
            "Quorum is not installed. Install it with: pip install quorum\n"
            "Or clone from: ~/projects/quorum"
        )

    config = QuorumConfig(
        objective=args.get("topic", args.get("objective", "")),
        documents=[args.get("brief", "")],
        agent_roles=args.get("agents", ["analyst", "strategist", "risk_assessor"]),
    )
    engine = Quorum(config=config)
    result = await engine.run_async()
    return {
        "decision": result.decision,
        "confidence": result.confidence,
        "dissents": result.dissenting_opinions,
        "trace": result.transcript,
    }

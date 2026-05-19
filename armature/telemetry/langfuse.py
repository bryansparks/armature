"""LangFuse observability adapter for Armature.

Auto-activates when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set
and the ``langfuse`` package is installed. Accepts an optional ``client``
argument for testing without a real LangFuse server.
"""
from __future__ import annotations
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from armature.hooks.lifecycle import HookRegistry


class LangFuseAdapter:
    @staticmethod
    def is_configured() -> bool:
        return bool(
            os.environ.get("LANGFUSE_PUBLIC_KEY")
            and os.environ.get("LANGFUSE_SECRET_KEY")
        )

    @staticmethod
    def is_available() -> bool:
        try:
            import langfuse  # noqa: F401
            return True
        except ImportError:
            return False

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _make_client(self) -> Any:
        if self._client is not None:
            return self._client
        from langfuse import Langfuse
        return Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )

    def attach(
        self,
        hooks: "HookRegistry",
        run_id: str,
        workflow_name: str,
        spec_version: str,
    ) -> None:
        from armature.hooks.lifecycle import HookPhase, HookDecision

        client = self._make_client()
        trace = client.trace(name=workflow_name, id=run_id, metadata={"spec_version": spec_version})
        spans: dict[str, Any] = {}

        async def pre_stage(phase: Any, stage_id: str, args: dict, ctx: dict) -> HookDecision:
            spans[stage_id] = trace.span(name=stage_id)
            return HookDecision.ALLOW

        async def post_stage(phase: Any, stage_id: str, result: Any, ctx: dict) -> None:
            span = spans.pop(stage_id, None)
            if span is not None:
                span.end(output=result)

        hooks.register(HookPhase.PRE_STAGE, pre_stage)
        hooks.register(HookPhase.POST_STAGE, post_stage)

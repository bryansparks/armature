"""LangSmith observability adapter for Armature.

Auto-activates when LANGSMITH_API_KEY is set and the ``langsmith``
package is installed. Accepts an optional ``client`` argument for
testing without a real LangSmith server.
"""
from __future__ import annotations
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from armature.hooks.lifecycle import HookRegistry


class LangSmithAdapter:
    @staticmethod
    def is_configured() -> bool:
        return bool(os.environ.get("LANGSMITH_API_KEY"))

    @staticmethod
    def is_available() -> bool:
        try:
            import langsmith  # noqa: F401
            return True
        except ImportError:
            return False

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _make_client(self) -> Any:
        if self._client is not None:
            return self._client
        from langsmith import Client
        project = os.environ.get("LANGSMITH_PROJECT", "armature")
        return Client(api_key=os.environ["LANGSMITH_API_KEY"], project_name=project)

    def attach(
        self,
        hooks: "HookRegistry",
        run_id: str,
        workflow_name: str,
    ) -> None:
        from armature.hooks.lifecycle import HookPhase, HookDecision

        client = self._make_client()
        runs: dict[str, Any] = {}

        async def pre_stage(phase: Any, stage_id: str, args: dict, ctx: dict) -> HookDecision:
            run = client.create_run(
                name=stage_id,
                run_type="chain",
                inputs={"context_keys": list(ctx.keys())},
                project_name=os.environ.get("LANGSMITH_PROJECT", "armature"),
                parent_run_id=run_id,
            )
            runs[stage_id] = run
            return HookDecision.ALLOW

        async def post_stage(phase: Any, stage_id: str, result: Any, ctx: dict) -> None:
            run = runs.pop(stage_id, None)
            if run is not None:
                client.update_run(run.id, outputs=result if isinstance(result, dict) else {"result": result})

        hooks.register(HookPhase.PRE_STAGE, pre_stage)
        hooks.register(HookPhase.POST_STAGE, post_stage)

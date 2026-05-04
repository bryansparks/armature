from __future__ import annotations
import uuid
from pathlib import Path
from typing import Any
from armature.spec.models import HarnessSpec, Stage
from armature.spec.loader import load_spec
from armature.runtime.dag import DAGExecutor
from armature.runtime.context import ContextManager
from armature.runtime.prompt import PromptAssembler
from armature.nodes.llm import LLMNode
from armature.nodes.script import ScriptNode
from armature.nodes.gate import HumanGateNode
from armature.registry.registry import ToolRegistry
from armature.registry.builtins import register_builtins
from armature.hooks.lifecycle import HookRegistry, HookDecision
from armature.state.session import SessionLog, SessionEvent
from armature.state.artifacts import ArtifactStore


class Harness:
    def __init__(
        self,
        spec: HarnessSpec,
        session_dir: Path | None = None,
    ):
        self._spec = spec
        self._run_id = str(uuid.uuid4())[:8]
        base_dir = Path(session_dir or f"~/.armature/runs/{self._run_id}").expanduser()
        self._session = SessionLog(base_dir / "session.jsonl")
        self._artifacts = ArtifactStore(base_dir / "artifacts")
        self._registry = ToolRegistry()
        register_builtins(self._registry)
        self._hooks = HookRegistry()
        self._context = ContextManager()
        self._assembler = PromptAssembler()

    @property
    def name(self) -> str:
        return self._spec.name

    @classmethod
    def from_spec(cls, path: Path | str, vars: dict | None = None) -> "Harness":
        spec = load_spec(path, vars=vars)
        return cls(spec=spec)

    async def _execute_stage(self, stage: Stage, context: dict[str, Any]) -> Any:
        await self._session.append(SessionEvent(type="stage_start", data={"stage": stage.id}))

        decision = await self._hooks.run_pre_stage(stage.id, context)
        if decision == HookDecision.BLOCK:
            raise PermissionError(f"Stage '{stage.id}' blocked by lifecycle hook")

        if stage.gate == "human":
            node = HumanGateNode(stage=stage)
            result = await node.execute(context)
        elif stage.adapter:
            adapter = self._spec.adapters.get(stage.adapter)
            if adapter is None:
                raise ValueError(f"Adapter '{stage.adapter}' not defined in spec")
            node = ScriptNode(adapter=adapter)
            result = await node.execute(context)
        elif stage.role:
            node = LLMNode(
                stage=stage,
                tiers=self._spec.model_tiers,
                assembler=self._assembler,
                registry=self._registry,
            )
            result = await node.execute(context)
        else:
            raise ValueError(f"Stage '{stage.id}' has no role, adapter, or gate")

        await self._hooks.run_post_stage(stage.id, result, context)
        await self._session.append(SessionEvent(
            type="stage_complete", data={"stage": stage.id, "result": str(result)[:500]}
        ))
        return result

    async def run(self, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(inputs or {})
        context["run_id"] = self._run_id

        await self._session.append(SessionEvent(
            type="run_start", data={"run_id": self._run_id, "workflow": self._spec.name}
        ))

        deps = {s.id: s.depends_on for s in self._spec.stages}

        async def make_handler(stage: Stage):
            async def handler(ctx):
                return await self._execute_stage(stage, ctx)
            return handler

        handlers = {s.id: await make_handler(s) for s in self._spec.stages}
        executor = DAGExecutor(handlers, deps)
        results = await executor.run(context)

        await self._session.append(SessionEvent(type="run_complete", data={"run_id": self._run_id}))
        return results

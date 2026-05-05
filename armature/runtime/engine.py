from __future__ import annotations
import uuid
import time
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
from armature.state.traces import TraceStore, TraceRecord
from armature.telemetry import get_tracer


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
        if self._spec.safety_rules:
            from armature.hooks.lifecycle import SafetyHookBuilder
            SafetyHookBuilder.register(self._hooks, self._spec.safety_rules)
        self._context = ContextManager()
        self._assembler = PromptAssembler()
        self._session_dir = base_dir
        self._traces = TraceStore(base_dir / "traces.db")

    @property
    def name(self) -> str:
        return self._spec.name

    @classmethod
    def from_spec(cls, path: Path | str, vars: dict | None = None) -> "Harness":
        spec = load_spec(path, vars=vars)
        return cls(spec=spec)

    async def _ensure_traces(self) -> None:
        if not hasattr(self, "_traces_initialized"):
            await self._traces.init()
            self._traces_initialized = True

    async def _execute_stage(self, stage: Stage, context: dict[str, Any]) -> Any:
        await self._session.append(SessionEvent(type="stage_start", data={"stage": stage.id}))

        decision = await self._hooks.run_pre_stage(stage.id, context)
        if decision == HookDecision.BLOCK:
            raise PermissionError(f"Stage '{stage.id}' blocked by lifecycle hook")

        t0 = time.monotonic()
        tracer = get_tracer()

        _stage_succeeded = False
        try:
            with tracer.start_as_current_span(
                f"armature.stage.{stage.id}",
                attributes={"stage": stage.id, "workflow": self._spec.name},
            ) as span:
                try:
                    if stage.gate == "human":
                        node = HumanGateNode(stage=stage)
                        result = await node.execute(context)
                    elif stage.subagent_spec:
                        from armature.nodes.subagent import SubagentNode
                        node = SubagentNode(stage=stage, session_dir=self._session_dir)
                        result = await node.execute(context)
                    elif stage.adapter:
                        adapter = self._spec.adapters.get(stage.adapter)
                        if adapter is None:
                            raise ValueError(f"Adapter '{stage.adapter}' not defined in spec")
                        node = ScriptNode(adapter=adapter)
                        tool_args = {"cmd": adapter.cmd or ""}
                        decision = await self._hooks.run_pre_tool(stage.adapter, tool_args, context)
                        if decision == HookDecision.BLOCK:
                            # Fallback for programmatic hooks that return BLOCK without raising.
                            # SafetyHookBuilder hooks raise ToolBlocked directly (carrying rule.message).
                            from armature.hooks.lifecycle import ToolBlocked
                            raise ToolBlocked(stage.adapter, adapter.cmd or "", "blocked by safety rule")
                        result = await node.execute(context)
                        await self._hooks.run_post_tool(stage.adapter, result, context)
                    elif stage.role:
                        node = LLMNode(
                            stage=stage,
                            tiers=self._spec.model_tiers,
                            assembler=self._assembler,
                            registry=self._registry,
                        )
                        result = await node.execute(context)
                        await self._ensure_traces()
                        latency = (time.monotonic() - t0) * 1000
                        span.set_attribute("latency_ms", latency)
                        output_valid = "_parse_error" not in result
                        await self._traces.record(TraceRecord(
                            run_id=self._run_id,
                            workflow_name=self._spec.name,
                            stage_id=stage.id,
                            role_type=stage.role.type.value,
                            model=node._resolve_model(),
                            input_tokens=result.pop("_input_tokens", 0),
                            output_tokens=result.pop("_output_tokens", 0),
                            latency_ms=latency,
                            success=True,
                            output_valid=output_valid,
                            inputs={k: str(v)[:200] for k, v in context.items()},
                            outputs={k: str(v)[:200] for k, v in result.items()},
                        ))
                    else:
                        raise ValueError(f"Stage '{stage.id}' has no role, adapter, or gate")

                    _stage_succeeded = True
                    span.set_attribute("success", True)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_attribute("success", False)
                    raise
        except Exception:
            if not _stage_succeeded:
                raise  # real stage failure — propagate
            # else: OTel span.__exit__ raised — swallow, execution continues

        await self._hooks.run_post_stage(stage.id, result, context)
        await self._session.append(SessionEvent(
            type="stage_complete", data={"stage": stage.id, "result": str(result)[:500]}
        ))
        return result

    async def _execute_stage_with_recovery(
        self, stage: Stage, context: dict[str, Any]
    ) -> Any:
        if stage.on_fail is None or stage.on_fail.loop is None:
            return await self._execute_stage(stage, context)

        loop_cfg = stage.on_fail.loop
        last_exc: Exception | None = None
        retry_ctx = dict(context)

        for attempt in range(loop_cfg.max + 1):
            try:
                return await self._execute_stage(stage, retry_ctx)
            except Exception as exc:
                from armature.hooks.lifecycle import ToolBlocked
                if isinstance(exc, ToolBlocked):
                    raise  # policy violation — retrying won't change the outcome
                last_exc = exc
                if attempt < loop_cfg.max:
                    retry_ctx = {
                        **retry_ctx,
                        "_retry_attempt": attempt + 1,
                        "_last_error": str(exc),
                    }

        raise last_exc  # type: ignore[misc]

    async def run(self, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(inputs or {})
        context["run_id"] = self._run_id

        tracer = get_tracer()
        with tracer.start_as_current_span(
            f"armature.run.{self._spec.name}",
            attributes={"workflow": self._spec.name, "run_id": self._run_id},
        ):
            await self._session.append(SessionEvent(
                type="run_start", data={"run_id": self._run_id, "workflow": self._spec.name}
            ))

            deps = {s.id: s.depends_on for s in self._spec.stages}

            async def make_handler(stage: Stage):
                async def handler(ctx):
                    return await self._execute_stage_with_recovery(stage, ctx)
                return handler

            handlers = {s.id: await make_handler(s) for s in self._spec.stages}
            executor = DAGExecutor(handlers, deps)
            results = await executor.run(context)

            await self._session.append(SessionEvent(type="run_complete", data={"run_id": self._run_id}))
            return results

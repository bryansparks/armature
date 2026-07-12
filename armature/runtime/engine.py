from __future__ import annotations
import asyncio
import hashlib
import json
import uuid
import time
from pathlib import Path
from typing import Any, Callable
from armature.spec.models import HarnessSpec, Stage
from armature.spec.loader import load_spec
from armature.runtime.dag import DAGExecutor
from armature.runtime.context import ContextManager
from armature.runtime.prompt import PromptAssembler
from armature.nodes.llm import LLMNode
from armature.nodes.script import ScriptNode
from armature.nodes.gate import HumanGateNode
from armature.nodes.provider_errors import classify_provider_error
from armature.registry.registry import ToolRegistry
from armature.registry.builtins import register_builtins
from armature.hooks.lifecycle import HookRegistry, HookDecision, make_default_behavior_registry, RogueSignalCounter
from armature.state.session import SessionLog, SessionEvent
from armature.state.artifacts import ArtifactStore
from armature.state.traces import TraceStore, TraceRecord
from armature.telemetry import get_tracer


def new_run_id() -> str:
    """A fresh run id.

    12 hex chars = 48 bits of entropy. The previous 8-hex (32-bit) id collided
    at soak scale (~450 runs, birthday bound ~65k) and tripped
    checkpoint_resume_correctness, which mis-reports a run_id REUSE. There is
    no reuse path — `armature run --force` shells out fresh per rep and
    `__init__` calls this — so widening the space to 48 bits makes collisions
    negligible (~1e-10 at 600 runs) and keeps run_ids INTRINSICALLY distinct,
    not a CLI option. Full uuid (122 bits) would also work but 12 chars keeps
    display strings short.
    """
    return uuid.uuid4().hex[:12]


_QUORUM_SCORE_KEYS = ("score", "quality_score", "confidence")


def _extract_quorum_score(role_type: str, result: dict) -> float | None:
    """Extract a 0–1 quality score from a judge stage's output for HQS tracking.

    Scans result for score/quality_score/confidence (in that priority).
    Only judge stages produce quorum scores; all other role types return None.
    """
    if role_type != "judge":
        return None
    for key in _QUORUM_SCORE_KEYS:
        val = result.get(key)
        if isinstance(val, (int, float)) and 0.0 <= float(val) <= 1.0:
            return float(val)
    return None


def _resolve_dot_path(data: dict, path: str) -> Any:
    """Resolve a dot-separated path like 'decide_round.report' against a nested dict."""
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _set_nested_key(data: dict, path: str, value: Any) -> None:
    """Set a nested key like 'decide_round.report' in a dict, creating intermediaries."""
    keys = path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def _merge_carry_forward(target: dict, carry: dict) -> None:
    """Deep-merge carry-forward dict into target, preserving nested structure."""
    for key, value in carry.items():
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            _merge_carry_forward(target[key], value)
        else:
            target[key] = value


_GLOBAL_TRACES_DB = Path("~/.armature/traces.db")


def _carry_output_cap(stage_id: str, spec: "HarnessSpec") -> int:
    if spec.continuation:
        carry_ids = {e.key.split(".")[0] for e in spec.continuation.carry_forward}
        if stage_id in carry_ids:
            return 2000
    return 200


def _build_mission_block(
    mission: str,
    context: dict,
    spec_stage_ids: set[str],
    max_preview_chars: int = 200,
) -> str:
    """Build the mission + prior-stage breadcrumb block for LLM system prompts."""
    parts = []
    if mission:
        parts.append(f"[Workflow Mission]\n{mission.strip()}")
    prior = []
    for sid in spec_stage_ids:
        if sid in context:
            preview = json.dumps(context[sid], default=str)[:max_preview_chars]
            prior.append(f"• {sid} → {preview}")
    if prior:
        parts.append("[Prior stages]\n" + "\n".join(prior))
    return "\n\n".join(parts)


class Harness:
    def __init__(
        self,
        spec: HarnessSpec,
        session_dir: Path | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        *,
        validate: bool = True,
        traces_db: Path | str | None = None,
        use_cache: bool = True,
        adapter_registry=None,
    ):
        if validate:
            from armature.spec.validator import validate_spec
            validate_spec(spec)

        self._spec = spec
        self._spec_version = hashlib.sha256(
            self._spec.model_dump_json().encode()
        ).hexdigest()[:12]
        self._run_id = new_run_id()
        base_dir = Path(session_dir or f"~/.armature/runs/{self._run_id}").expanduser()
        self._session = SessionLog(base_dir / "session.jsonl")
        self._artifacts = ArtifactStore(base_dir / "artifacts")
        self._registry = ToolRegistry()
        register_builtins(self._registry)
        self._load_tool_modules()
        from armature.sandbox.docker import DockerSandboxProvider
        from armature.spec.models import SandboxMode
        self._sandbox_provider = DockerSandboxProvider()
        self._sandbox_provider.wrap_registry(
            self._registry,
            self._spec.sandbox,
            Path(self._spec.sandbox.host_workspace).expanduser().resolve(),
        )
        self._sandbox_image_digest: str | None = None
        if self._spec.sandbox.mode == SandboxMode.DOCKER:
            try:
                import subprocess as _sp
                _proc = _sp.run(
                    [self._spec.sandbox.runtime, "inspect", "--format", "{{.Id}}", self._spec.sandbox.image],
                    capture_output=True, text=True, timeout=10,
                )
                if _proc.returncode == 0:
                    self._sandbox_image_digest = _proc.stdout.strip() or None
            except Exception:
                pass  # docker not available or image not pulled; proceed without digest
        self._policy_version = hashlib.sha256(
            str([r.model_dump() for r in self._spec.safety_rules]).encode()
        ).hexdigest()[:12]
        self._hooks = HookRegistry()
        self._rogue_counter = RogueSignalCounter()
        from armature.hooks.lifecycle import SafetyHookBuilder
        SafetyHookBuilder.register(
            self._hooks,
            self._spec.safety_rules,
            tool_registry=self._registry,
            strict_mode=(self._spec.safety_mode == "strict"),
            counter=self._rogue_counter,
        )
        self._attach_observability_adapters()
        self._context = ContextManager()
        self._assembler = PromptAssembler()
        self._session_dir = base_dir
        resolved_traces = Path(traces_db).expanduser() if traces_db else _GLOBAL_TRACES_DB.expanduser()
        self._traces = TraceStore(resolved_traces)
        self._on_event = on_event
        self._on_token = None   # async (chunk: str)->None; set by service layer for streaming
        self._transcript: list[dict[str, Any]] = []
        if use_cache:
            from armature.cache.llm_cache import LLMCache
            self._llm_cache: "LLMCache | None" = LLMCache(resolved_traces.parent / "llm_cache.sqlite")
        else:
            self._llm_cache = None

        if adapter_registry is not None:
            self._adapter_registry = adapter_registry
        else:
            from armature.adapters.registry import AdapterRegistry
            self._adapter_registry = AdapterRegistry()

        self._behaviors = make_default_behavior_registry()

        mem_cfg = self._spec.memory
        if mem_cfg and mem_cfg.enabled:
            from armature.state.memory import MemoryStore
            if mem_cfg.db:
                mem_path = Path(mem_cfg.db).expanduser()
            else:
                mem_path = Path(f"~/.armature/memory/{self._spec.name}.db").expanduser()
            self._memory_store: "MemoryStore | None" = MemoryStore(mem_path)
            self._memory_config = mem_cfg

            if mem_cfg.extract_knowledge:
                from armature.state.knowledge import KnowledgeStore
                from armature.state.extractor import KnowledgeExtractor
                knowledge_path = mem_path.with_name(mem_path.stem + "_knowledge.db")
                self._knowledge_store = KnowledgeStore(knowledge_path)
                embedder = None
                try:
                    from armature.state.embedder import LocalEmbedder
                    if LocalEmbedder.is_available():
                        embedder = LocalEmbedder()
                except Exception:
                    embedder = None
                self._knowledge_extractor = KnowledgeExtractor(
                    model=self._spec.model_tiers.small.model
                    if self._spec.model_tiers.small else "gpt-4o-mini",
                    knowledge_store=self._knowledge_store,
                    embedder=embedder,
                    reconcile=mem_cfg.reconcile,
                    reconcile_llm=mem_cfg.reconcile_llm,
                )
            else:
                self._knowledge_store = None
                self._knowledge_extractor = None
        else:
            self._memory_store = None
            self._memory_config = None
            self._knowledge_store = None
            self._knowledge_extractor = None

        # ── Memory pyramid (Phase 2): register navigation tools ──
        self._navigation_embedder = None
        if self._memory_config is not None and self._memory_config.navigation_tools:
            # search_records/get_records need a KnowledgeStore even when extraction is off
            if self._knowledge_store is None:
                from armature.state.knowledge import KnowledgeStore
                knowledge_path = mem_path.with_name(mem_path.stem + "_knowledge.db")
                self._knowledge_store = KnowledgeStore(knowledge_path)
            try:
                from armature.state.embedder import LocalEmbedder
                if LocalEmbedder.is_available():
                    self._navigation_embedder = LocalEmbedder()
            except Exception:
                self._navigation_embedder = None
            from armature.registry.memory_tools import register_memory_tools
            register_memory_tools(
                self._registry,
                memory_store=self._memory_store,
                knowledge_store=self._knowledge_store,
                trace_store=self._traces,
                embedder=self._navigation_embedder,
                workflow_name=self._spec.name,
                run_id=self._run_id,
            )

        if self._spec.checkpoint:
            from armature.runtime.checkpoint import CheckpointStore
            self._checkpoint: "CheckpointStore | None" = CheckpointStore(
                base_dir / "checkpoint.json"
            )
        else:
            self._checkpoint = None
        self._checkpoint_prior: dict[str, Any] = {}
        self._checkpoint_loop_iters: dict[str, Any] = {}
        self._llm_call_count: int = 0
        self._mcp_sessions: list[Any] = []

    def _load_tool_modules(self) -> None:
        import importlib
        for tool_mod in self._spec.tools:
            mod = importlib.import_module(tool_mod.module)
            if not hasattr(mod, "register"):
                raise AttributeError(
                    f"Tool module '{tool_mod.module}' must expose a "
                    "`register(registry: ToolRegistry) -> None` function"
                )
            mod.register(self._registry)

    async def _attach_mcp_servers(self) -> None:
        if not self._spec.mcp_servers:
            return
        from armature.mcp.client import MCPRegistrar
        self._mcp_sessions = await MCPRegistrar.register_all(
            self._spec.mcp_servers, self._registry
        )

    def _attach_observability_adapters(self) -> None:
        from armature.telemetry.langfuse import LangFuseAdapter
        from armature.telemetry.langsmith import LangSmithAdapter

        if LangFuseAdapter.is_configured() and LangFuseAdapter.is_available():
            LangFuseAdapter().attach(
                self._hooks,
                run_id=self._run_id,
                workflow_name=self._spec.name,
                spec_version=self._spec_version,
            )

        if LangSmithAdapter.is_configured() and LangSmithAdapter.is_available():
            LangSmithAdapter().attach(
                self._hooks,
                run_id=self._run_id,
                workflow_name=self._spec.name,
            )

    @property
    def transcript(self) -> list[dict[str, Any]]:
        return self._transcript

    @property
    def name(self) -> str:
        return self._spec.name

    @classmethod
    def from_spec(
        cls,
        path: Path | str,
        vars: dict | None = None,
        use_cache: bool = True,
        adapter_registry=None,
    ) -> "Harness":
        spec = load_spec(path, vars=vars)
        return cls(spec=spec, use_cache=use_cache, adapter_registry=adapter_registry)

    async def _ensure_traces(self) -> None:
        if not hasattr(self, "_traces_initialized"):
            await self._traces.init()
            self._traces_initialized = True

    async def _ensure_cache(self) -> None:
        if self._llm_cache is not None and not hasattr(self, "_cache_initialized"):
            await self._llm_cache.init()
            self._cache_initialized = True

    def _get_provenance(self) -> dict[str, str]:
        return getattr(self, "_provenance", {})

    async def _execute_stage(self, stage: Stage, context: dict[str, Any]) -> Any:
        await self._session.append(SessionEvent(type="stage_start", data={"stage": stage.id}))

        if self._on_event:
            if stage.gate:
                stage_kind = "gate"
            elif stage.tool_call:
                stage_kind = "tool_call"
            elif stage.adapter:
                stage_kind = "script"
            else:
                stage_kind = "llm"
            role_label = f"{stage.role.name} ({stage.role.type.value})" if stage.role else None
            self._on_event("stage_start", {"stage": stage.id, "kind": stage_kind, "role": role_label})

        self._sandbox_provider.set_stage_image(stage.sandbox_image)

        decision = await self._hooks.run_pre_stage(stage.id, context)
        if decision == HookDecision.BLOCK:
            raise PermissionError(f"Stage '{stage.id}' blocked by lifecycle hook")

        t0 = time.monotonic()
        tracer = get_tracer()

        _llm_node: "LLMNode | None" = None
        _stage_type = "unknown"

        _stage_succeeded = False
        try:
            with tracer.start_as_current_span(
                f"armature.stage.{stage.id}",
                attributes={"stage": stage.id, "workflow": self._spec.name},
            ) as span:
                try:
                    if stage.gate == "human":
                        _stage_type = "gate"
                        node = HumanGateNode(stage=stage)
                        result = await node.execute(context)
                        await self._ensure_traces()
                        gate_latency = (time.monotonic() - t0) * 1000
                        approved = result.get("approved", True) if isinstance(result, dict) else True
                        await self._traces.record(TraceRecord(
                            run_id=self._run_id,
                            workflow_name=self._spec.name,
                            stage_id=stage.id,
                            role_type="gate",
                            model="",
                            latency_ms=gate_latency,
                            success=bool(approved),
                            output_valid=True,
                            spec_version=self._spec_version,
                            policy_version=self._policy_version,
                            inputs={k: str(v)[:200] for k, v in context.items()},
                            outputs={k: str(v)[:200] for k, v in (result.items() if isinstance(result, dict) else {})},
                            inputs_provenance=dict(self._get_provenance()),
                        ))
                    elif stage.subagent_spec:
                        _stage_type = "subagent"
                        from armature.nodes.subagent import SubagentNode
                        node = SubagentNode(stage=stage, session_dir=self._session_dir)
                        result = await node.execute(context)
                    elif stage.tool_call:
                        _stage_type = "tool_call"
                        from armature.nodes.tool_call import ToolCallNode
                        node = ToolCallNode(stage=stage, registry=self._registry)
                        result = await node.execute(context)
                    elif stage.adapter:
                        _stage_type = "script"
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
                        await self._ensure_traces()
                        script_latency = (time.monotonic() - t0) * 1000
                        self._get_provenance().update({k: f"stage:{stage.id}" for k in result})
                        await self._traces.record(TraceRecord(
                            run_id=self._run_id,
                            workflow_name=self._spec.name,
                            stage_id=stage.id,
                            role_type="script",
                            model="",
                            latency_ms=script_latency,
                            success=result.get("exit_code", 0) == 0,
                            output_valid=True,
                            spec_version=self._spec_version,
                            inputs_hash=hashlib.sha256(
                                json.dumps(context, sort_keys=True, default=str).encode()
                            ).hexdigest()[:32],
                            policy_version=self._policy_version,
                            inputs={k: str(v)[:200] for k, v in context.items()},
                            outputs={k: str(v)[:200] for k, v in result.items()},
                            inputs_provenance=dict(self._get_provenance()),
                            sandbox_image_digest=self._sandbox_image_digest,
                        ))
                    elif stage.role:
                        _stage_type = "llm"
                        limit = self._spec.contracts.max_llm_calls
                        if limit > 0 and self._llm_call_count >= limit:
                            raise RuntimeError(
                                f"max_llm_calls limit ({limit}) reached — "
                                f"stage '{stage.id}' cannot execute"
                            )
                        self._llm_call_count += 1
                        await self._ensure_cache()
                        mission_ctx = _build_mission_block(
                            self._spec.mission,
                            context,
                            {s.id for s in self._spec.stages},
                        )
                        _llm_node = LLMNode(
                            stage=stage,
                            tiers=self._spec.model_tiers,
                            role_type_defaults=self._spec.role_type_defaults,
                            assembler=self._assembler,
                            registry=self._registry,
                            transcript=self._transcript,
                            skill_library=self._spec.skill_library,
                            cache=self._llm_cache,
                            mission_context=mission_ctx,
                            on_token=self._on_token if stage.response_stage else None,
                            adapter_registry=self._adapter_registry,
                            navigation_tools=bool(
                                self._memory_config is not None
                                and self._memory_config.navigation_tools
                            ),
                            knowledge_key=(
                                self._memory_config.inject_knowledge_as
                                if self._memory_config is not None
                                else "_knowledge"
                            ),
                        )
                        result = await _llm_node.execute(context)
                        await self._ensure_traces()
                        latency = (time.monotonic() - t0) * 1000
                        span.set_attribute("latency_ms", latency)
                        output_valid = "_parse_error" not in result
                        self._get_provenance().update({k: f"stage:{stage.id}" for k in result
                                                      if not k.startswith("_")})
                        _escalation_count = result.pop("_escalation_count", 0)
                        await self._traces.record(TraceRecord(
                            run_id=self._run_id,
                            workflow_name=self._spec.name,
                            stage_id=stage.id,
                            role_type=stage.role.type.value,
                            model=_llm_node._resolve_model(),
                            input_tokens=result.pop("_input_tokens", 0),
                            output_tokens=result.pop("_output_tokens", 0),
                            latency_ms=latency,
                            success=True,
                            output_valid=output_valid,
                            quorum_score=_extract_quorum_score(stage.role.type.value, result),
                            escalation_count=_escalation_count,
                            tools_declared=result.pop("_tools_declared", []),
                            tools_called=result.pop("_tools_called", []),
                            spec_version=self._spec_version,
                            inputs_hash=hashlib.sha256(
                                json.dumps(context, sort_keys=True, default=str).encode()
                            ).hexdigest()[:32],
                            policy_version=self._policy_version,
                            inputs={k: str(v)[:200] for k, v in context.items()},
                            outputs={k: str(v)[:(_carry_output_cap(stage.id, self._spec))] for k, v in result.items()},
                            inputs_provenance=dict(self._get_provenance()),
                            sandbox_image_digest=self._sandbox_image_digest,
                            agent_id=getattr(stage.role, "x_source", None),
                            agent_version=getattr(stage.role, "x_agent_version", None),
                            active_skill_ids=list(getattr(stage.role, "skills", []) or []),
                        ))
                        if _escalation_count > 0 and stage.output_mode == "guided_json" and self._on_event:
                            self._on_event("tier_escalation_warning", {
                                "stage": stage.id,
                                "escalation_count": _escalation_count,
                                "message": (
                                    f"Stage '{stage.id}' required {_escalation_count} tier escalation(s) "
                                    f"for guided_json output. Consider using a higher model tier."
                                ),
                            })
                    else:
                        raise ValueError(
                            f"Stage '{stage.id}' has no role, adapter, gate, or tool_call"
                        )

                    _stage_succeeded = True
                    span.set_attribute("success", True)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_attribute("success", False)
                    if _stage_type in ("llm", "script"):
                        try:
                            await self._ensure_traces()
                            _fail_latency = (time.monotonic() - t0) * 1000
                            _fail_role = stage.role.type.value if _stage_type == "llm" else "script"
                            _fail_model = (_llm_node._resolve_model() if _llm_node else "unknown") if _stage_type == "llm" else ""
                            await self._traces.record(TraceRecord(
                                run_id=self._run_id,
                                workflow_name=self._spec.name,
                                stage_id=stage.id,
                                role_type=_fail_role,
                                model=_fail_model,
                                latency_ms=_fail_latency,
                                success=False,
                                output_valid=False,
                                error_type=type(exc).__name__,
                                error_kind=classify_provider_error(exc),
                                spec_version=self._spec_version,
                                inputs_hash=hashlib.sha256(
                                    json.dumps(context, sort_keys=True, default=str).encode()
                                ).hexdigest()[:32],
                                policy_version=self._policy_version,
                                inputs={k: str(v)[:200] for k, v in context.items()},
                                inputs_provenance=dict(self._get_provenance()),
                                sandbox_image_digest=self._sandbox_image_digest,
                            ))
                        except Exception:
                            pass  # telemetry must never block execution
                    raise
        except Exception:
            if not _stage_succeeded:
                raise  # real stage failure — propagate
            # else: OTel span.__exit__ raised — swallow, execution continues

        await self._hooks.run_post_stage(stage.id, result, context)

        if self._memory_store is not None and self._memory_config is not None:
            quality = _extract_quorum_score(stage.role.type.value if stage.role else "worker", result) or 0.5
            for cap in self._memory_config.capture:
                if cap.stage == stage.id:
                    value = result.get(cap.key) if isinstance(result, dict) else None
                    if value is not None:
                        try:
                            await self._memory_store.record(
                                workflow_name=self._spec.name,
                                stage_id=stage.id,
                                capture_key=cap.key,
                                value=value,
                                max_entries=cap.max_entries,
                                quality=quality,
                            )
                        except Exception:
                            pass  # memory capture must never block execution

        await self._session.append(SessionEvent(
            type="stage_complete", data={"stage": stage.id, "result": str(result)[:500]}
        ))

        if self._on_event:
            elapsed = round(time.monotonic() - t0, 1)
            self._on_event("stage_complete", {"stage": stage.id, "elapsed_s": elapsed})

        return result

    async def _execute_fan_out_stage(
        self, stage: Stage, context: dict[str, Any]
    ) -> list[Any]:
        """Run a fan-out stage: execute the LLM/tool once per item in partition_source.

        The partition_source Jinja2 expression must resolve to a Python list.
        Each item is bound to partition_key in a per-item context copy.
        If inject_file_as is set, each item is treated as a file path whose
        content is read and added to context under that key.
        Concurrency is bounded by the fan_out integer (default 20).
        Per-item exceptions are caught and reported as empty results so a single
        failing file never aborts the entire fan-out.
        """
        import asyncio
        from pathlib import Path as _Path
        from jinja2.nativetypes import NativeEnvironment
        from jinja2 import ChainableUndefined

        env = NativeEnvironment(undefined=ChainableUndefined)
        items = env.from_string(stage.partition_source).render(**context)

        if not isinstance(items, (list, tuple)):
            hint = ""
            # Try to give actionable context: parse "{{ stage_id.key }}" and check
            # whether that upstream stage returned null/missing for that key.
            src = stage.partition_source.strip().strip("{}").strip()
            if "." in src:
                src_stage, _, src_key = src.partition(".")
                src_stage = src_stage.strip()
                src_key = src_key.strip()
                upstream = context.get(src_stage)
                if isinstance(upstream, dict) and upstream.get(src_key) is None:
                    hint = (
                        f" Stage '{src_stage}' returned null for '{src_key}' — "
                        f"check output_valid in traces: the model may have failed "
                        f"to produce a valid guided_json response."
                    )
                elif upstream is None:
                    hint = f" Stage '{src_stage}' has no output in context."
            raise ValueError(
                f"Stage '{stage.id}' partition_source resolved to "
                f"{type(items).__name__}, expected list.{hint}"
            )

        max_concurrent = stage.fan_out or 20
        semaphore = asyncio.Semaphore(max_concurrent)
        partition_key = stage.partition_key or "item"

        async def _run_one(item: Any) -> Any:
            async with semaphore:
                per_ctx = {**context, partition_key: item}
                if stage.inject_file_as is not None:
                    try:
                        per_ctx[stage.inject_file_as] = _Path(str(item)).read_text(errors="replace")
                    except Exception:
                        per_ctx[stage.inject_file_as] = ""
                try:
                    return await self._execute_stage(stage, per_ctx)
                except Exception as exc:
                    return {"_fan_out_error": str(exc), "vulnerabilities": []}

        raw = await asyncio.gather(*[_run_one(item) for item in items])
        results = list(raw)

        strategy = stage.fan_in
        if strategy == "merge":
            merged: dict[str, Any] = {}
            for r in results:
                if isinstance(r, dict):
                    merged.update(r)
            return merged
        if strategy == "first":
            return results[0] if results else {}
        # "list" (default): return the raw list
        return results

    @staticmethod
    def _eval_until(expr: str, result: Any, context: dict[str, Any]) -> bool:
        """Evaluate a Jinja2 `until` expression against stage result + context.

        The result's keys (if it's a dict) are merged into the template context
        so authors can write `until: "{{ status == 'done' }}"` directly.
        """
        from jinja2 import Environment, BaseLoader, ChainableUndefined
        env = Environment(loader=BaseLoader(), undefined=ChainableUndefined)
        eval_ctx = {**context}
        if isinstance(result, dict):
            eval_ctx.update(result)
        else:
            eval_ctx["_result"] = result
        rendered = env.from_string(expr).render(**eval_ctx).strip().lower()
        return rendered in ("true", "1", "yes")

    async def _run_with_retry(self, stage: Stage, context: dict[str, Any]) -> Any:
        """Execute stage with optional on_fail.loop retry, until condition, and backoff."""
        if stage.on_fail is None or stage.on_fail.loop is None:
            return await self._execute_stage(stage, context)

        loop_cfg = stage.on_fail.loop
        last_exc: Exception | None = None
        last_result: Any = None
        retry_ctx = dict(context)

        for attempt in range(loop_cfg.max + 1):
            try:
                result = await self._execute_stage(stage, retry_ctx)
                last_result = result
                last_exc = None
            except Exception as exc:
                from armature.hooks.lifecycle import ToolBlocked
                if isinstance(exc, ToolBlocked):
                    raise  # policy violation — retrying won't change the outcome
                last_exc = exc

            # Stop if successful and until condition satisfied (or no until condition)
            if last_exc is None:
                if loop_cfg.until is None or self._eval_until(loop_cfg.until, last_result, retry_ctx):
                    return last_result

            # Need another attempt — update context and apply backoff
            if attempt < loop_cfg.max:
                update: dict[str, Any] = {"_retry_attempt": attempt + 1}
                if last_exc is not None:
                    update["_last_error"] = str(last_exc)
                elif last_result is not None:
                    update["_last_result"] = last_result
                retry_ctx = {**retry_ctx, **update}
                if self._on_event:
                    self._on_event("retry_attempt", {
                        "stage": stage.id,
                        "attempt": attempt + 1,
                        "max": loop_cfg.max,
                        "reason": str(last_exc) if last_exc else "until condition not satisfied",
                    })
                if loop_cfg.backoff_s is not None:
                    delay = min(
                        loop_cfg.backoff_s * (2 ** attempt),
                        loop_cfg.backoff_max_s,
                    )
                    await asyncio.sleep(delay)

        # Loop exhausted
        if last_exc is not None:
            raise last_exc
        return last_result  # until never satisfied — return best result

    async def _run_with_loop(self, stage: Stage, context: dict[str, Any]) -> Any:
        """Execute stage with deliberate iteration (not retry-on-failure)."""
        # If the entire loop completed in a prior run, return the cached result.
        if self._checkpoint is not None and stage.id in self._checkpoint_prior:
            if self._on_event:
                self._on_event("stage_resumed", {"stage": stage.id})
            return self._checkpoint_prior[stage.id]

        loop_cfg = stage.loop
        var_name = loop_cfg.iteration_var
        result: Any = None
        iteration_ctx = dict(context)

        for iteration_num in range(1, loop_cfg.max_iterations + 1):
            is_last = (iteration_num == loop_cfg.max_iterations)

            iteration_info: dict[str, Any] = {
                "num": iteration_num,
                "is_first": iteration_num == 1,
                "is_last": is_last,
                "carry_forward": {},
            }

            if result is not None:
                carry: dict[str, Any] = {}
                if loop_cfg.carry_forward is not None:
                    for path in loop_cfg.carry_forward:
                        value = _resolve_dot_path(result if isinstance(result, dict) else {}, path)
                        if value is not None:
                            _set_nested_key(carry, path, value)
                else:
                    carry = result if isinstance(result, dict) else {"_result": result}
                iteration_info["carry_forward"] = carry

            update: dict[str, Any] = {var_name: iteration_info}
            if isinstance(iteration_info["carry_forward"], dict):
                _merge_carry_forward(update, iteration_info["carry_forward"])
            iteration_ctx = {**iteration_ctx, **update}

            result = await self._execute_stage_with_recovery(
                stage, iteration_ctx, _loop_iteration=iteration_num
            )

            until_met = False
            if loop_cfg.until is not None:
                try:
                    until_met = self._eval_until(loop_cfg.until, result, iteration_ctx)
                except Exception as exc:
                    raise RuntimeError(
                        f"Stage '{stage.id}' loop.until raised an error: {exc}"
                    ) from exc

            if self._on_event:
                self._on_event("loop_iteration", {
                    "stage": stage.id,
                    "iteration": iteration_num,
                    "max": loop_cfg.max_iterations,
                    "until_met": until_met,
                })

            if until_met:
                # Write final result under the plain stage.id key so downstream
                # stages and checkpoint resume can find it.
                if self._checkpoint is not None:
                    self._checkpoint_write(stage.id, result)
                return result

            if not is_last and loop_cfg.backoff_s is not None:
                delay = min(
                    loop_cfg.backoff_s * (2 ** (iteration_num - 1)),
                    loop_cfg.backoff_max_s,
                )
                await asyncio.sleep(delay)

        # Write final result under the plain stage.id key so downstream stages
        # can reference it via {{ stage_id.field }} and checkpoint resume works.
        if self._checkpoint is not None:
            self._checkpoint_write(stage.id, result)
        return result

    def _effective_output_limit(self, stage: Stage) -> int | None:
        if stage.output_max_chars is not None:
            return stage.output_max_chars
        return self._spec.contracts.output_max_chars

    def _maybe_truncate(self, stage: Stage, result: Any) -> Any:
        limit = self._effective_output_limit(stage)
        if limit is None:
            return result
        from armature.runtime.truncation import truncate_result
        return truncate_result(result, limit)

    def _checkpoint_write(
        self, stage_id: str, result: Any, _loop_iteration: int | None = None
    ) -> None:
        if self._checkpoint is None:
            return
        _cp_key = stage_id if _loop_iteration is None else f"{stage_id}__iter_{_loop_iteration}"
        combined = {**self._checkpoint_prior, **self._checkpoint_loop_iters}
        self._checkpoint.write(_cp_key, result, combined)
        if _loop_iteration is not None:
            self._checkpoint_loop_iters[_cp_key] = result
        else:
            self._checkpoint_prior[stage_id] = result

    async def _execute_stage_with_recovery(
        self, stage: Stage, context: dict[str, Any],
        _loop_iteration: int | None = None,
    ) -> Any:
        # Return cached result from a prior run (checkpoint resume).
        # For loop iterations, use iteration-scoped keys so that iteration N
        # doesn't short-circuit iteration N+1.
        _cp_key = stage.id if _loop_iteration is None else f"{stage.id}__iter_{_loop_iteration}"
        _cp_store = self._checkpoint_loop_iters if _loop_iteration is not None else self._checkpoint_prior

        if self._checkpoint is not None and _cp_key in _cp_store:
            if self._on_event:
                event_data: dict[str, Any] = {"stage": stage.id}
                if _loop_iteration is not None:
                    event_data["iteration"] = _loop_iteration
                self._on_event("stage_resumed", event_data)
            return _cp_store[_cp_key]

        # Evaluate skip_if (negative gate) and condition (positive gate).
        # condition is the inverse: stage runs only when truthy, skips when falsy.
        # Both are evaluated; if either triggers a skip, the stage is skipped.
        from jinja2 import ChainableUndefined, Environment, BaseLoader
        _jinja_env = Environment(loader=BaseLoader(), undefined=ChainableUndefined)

        if stage.skip_if is not None:
            rendered = _jinja_env.from_string(stage.skip_if).render(**context).strip().lower()
            if rendered in ("true", "1", "yes"):
                if self._on_event:
                    self._on_event("stage_skipped", {"stage": stage.id, "reason": "skip_if"})
                return {"_skipped": True}

        if stage.condition is not None:
            rendered = _jinja_env.from_string(stage.condition).render(**context).strip().lower()
            if rendered not in ("true", "1", "yes"):
                if self._on_event:
                    self._on_event("stage_skipped", {"stage": stage.id, "reason": "condition"})
                return {"_skipped": True}

        if stage.partition_source is not None:
            try:
                result = await self._execute_fan_out_stage(stage, context)
            except Exception as exc:
                if stage.fail_as_value:
                    failed = {
                        "_failed": True,
                        "_failed_reason": str(exc),
                        "_failed_type": type(exc).__name__,
                    }
                    if self._on_event:
                        self._on_event("stage_failed", {
                            "stage": stage.id,
                            "type": type(exc).__name__,
                            "reason": str(exc),
                        })
                    self._checkpoint_write(stage.id, failed, _loop_iteration)
                    return failed
                raise
            result = self._maybe_truncate(stage, result)
            self._checkpoint_write(stage.id, result, _loop_iteration)
            return result

        try:
            coro = self._run_with_retry(stage, context)
            if stage.timeout_s is not None:
                result = await asyncio.wait_for(coro, timeout=stage.timeout_s)
            else:
                result = await coro
            result = self._maybe_truncate(stage, result)
            self._checkpoint_write(stage.id, result, _loop_iteration)
            return result
        except asyncio.TimeoutError:
            if stage.fail_as_value:
                failed = {
                    "_failed": True,
                    "_failed_reason": f"timed out after {stage.timeout_s}s",
                    "_failed_type": "TimeoutError",
                }
                if self._on_event:
                    self._on_event("stage_failed", {
                        "stage": stage.id,
                        "type": "TimeoutError",
                        "reason": failed["_failed_reason"],
                    })
                self._checkpoint_write(stage.id, failed, _loop_iteration)
                return failed
            raise TimeoutError(f"Stage '{stage.id}' timed out after {stage.timeout_s}s")
        except Exception as exc:
            if stage.fail_as_value:
                failed = {
                    "_failed": True,
                    "_failed_reason": str(exc),
                    "_failed_type": type(exc).__name__,
                }
                if self._on_event:
                    self._on_event("stage_failed", {
                        "stage": stage.id,
                        "type": type(exc).__name__,
                        "reason": str(exc),
                    })
                self._checkpoint_write(stage.id, failed, _loop_iteration)
                return failed
            raise

    def _validate_inputs(self, context: dict[str, Any]) -> None:
        for inp in self._spec.contracts.inputs:
            name = inp.get("name")
            if name is None:
                continue
            if inp.get("required", False) and (name not in context or context[name] is None):
                raise ValueError(f"Required input '{name}' missing from context")

    def _validate_outputs(self, results: dict[str, Any]) -> None:
        for out in self._spec.contracts.outputs:
            stage_id = out.get("stage")
            key = out.get("key")
            if stage_id is None or key is None:
                continue
            if not out.get("required", False):
                continue
            stage_result = results.get(stage_id)
            if not isinstance(stage_result, dict) or stage_result.get(key) is None:
                raise ValueError(
                    f"Required output '{key}' from stage '{stage_id}' missing or None in results"
                )

    async def run(
        self,
        inputs: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        await self._attach_mcp_servers()

        context = dict(inputs or {})
        context["run_id"] = self._run_id
        if self._spec.continuation:
            _prior = await self._load_prior_context()
            if _prior is not None:
                context[self._spec.continuation.inject_as] = _prior
        self._validate_inputs(context)
        self._provenance: dict[str, str] = {k: "user_input" for k in (inputs or {})}

        # Load prior checkpoint results so downstream stages can reference them.
        # Split into stage-level keys (for context and n_resumed) and
        # iteration-scoped keys (for mid-loop resume only).
        if self._checkpoint is not None and not force:
            raw_checkpoint: dict[str, Any] = self._checkpoint.load()
            self._checkpoint_prior = {
                k: v for k, v in raw_checkpoint.items()
                if "__iter_" not in k
            }
            self._checkpoint_loop_iters = {
                k: v for k, v in raw_checkpoint.items()
                if "__iter_" in k
            }
            context.update(self._checkpoint_prior)
        else:
            self._checkpoint_prior = {}
            self._checkpoint_loop_iters = {}
            if self._checkpoint is not None and force:
                self._checkpoint.clear()

        if self._memory_store is not None:
            await self._memory_store.init()
            mem_cfg = self._memory_config  # type: ignore[union-attr]
            if mem_cfg.fresh:
                memories: dict = {}
                stale_keys: set = set()
            else:
                memories, stale_keys = await self._memory_store.load(self._spec.name)
            context[mem_cfg.inject_as] = memories
            self._provenance[mem_cfg.inject_as] = "memory"
            if stale_keys:
                context["_stale_memory_keys"] = [
                    f"{stage_id}.{capture_key}" for stage_id, capture_key in sorted(stale_keys)
                ]
                self._provenance["_stale_memory_keys"] = "stale_memory"

            if self._knowledge_store is not None:
                await self._knowledge_store.init()
                knowledge = await self._knowledge_store.search(
                    self._spec.name, query=self._spec.name, top_k=10
                )
                context[mem_cfg.inject_knowledge_as] = [
                    {"entity": k.entity, "fact": k.fact, "confidence": k.confidence}
                    for k in knowledge
                ]

        tracer = get_tracer()
        with tracer.start_as_current_span(
            f"armature.run.{self._spec.name}",
            attributes={"workflow": self._spec.name, "run_id": self._run_id},
        ):
            await self._session.append(SessionEvent(
                type="run_start", data={"run_id": self._run_id, "workflow": self._spec.name}
            ))

            normal_stages = [s for s in self._spec.stages if not s.post_run]
            post_run_stages = [s for s in self._spec.stages if s.post_run]

            deps = {s.id: s.depends_on for s in normal_stages}

            async def make_handler(stage: Stage):
                async def handler(ctx):
                    if stage.loop is not None:
                        return await self._run_with_loop(stage, ctx)
                    return await self._execute_stage_with_recovery(stage, ctx)
                return handler

            _run_t0 = time.monotonic()
            handlers = {s.id: await make_handler(s) for s in normal_stages}
            executor = DAGExecutor(handlers, deps)
            timeout_s = self._spec.contracts.timeout_hours * 3600
            try:
                results = await asyncio.wait_for(
                    executor.run(context), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Workflow '{self._spec.name}' exceeded "
                    f"timeout_hours={self._spec.contracts.timeout_hours}"
                )
            self._validate_outputs(results)

            await self._session.append(SessionEvent(type="run_complete", data={"run_id": self._run_id}))

            if self._on_event:
                stage_ids = [s.id for s in self._spec.stages]
                n_resumed = sum(1 for sid in stage_ids if sid in self._checkpoint_prior)
                n_skipped = sum(
                    1 for sid in stage_ids
                    if isinstance(results.get(sid), dict) and results[sid].get("_skipped")
                )
                n_failed = sum(
                    1 for sid in stage_ids
                    if isinstance(results.get(sid), dict) and results[sid].get("_failed")
                )
                n_ran = len(stage_ids) - n_resumed - n_skipped
                self._on_event("run_summary", {
                    "run_id": self._run_id,
                    "workflow": self._spec.name,
                    "elapsed_s": round(time.monotonic() - _run_t0, 2),
                    "stages_total": len(stage_ids),
                    "stages_ran": n_ran,
                    "stages_skipped": n_skipped,
                    "stages_resumed": n_resumed,
                    "stages_failed": n_failed,
                    "rogue_signals": self._rogue_counter.count,
                })

            # Post-run knowledge extraction (non-blocking)
            if self._knowledge_extractor is not None and self._memory_store is not None:
                try:
                    updated_memories, _ = await self._memory_store.load(self._spec.name)
                    await self._knowledge_extractor.extract(
                        updated_memories,
                        workflow_name=self._spec.name,
                        run_id=self._run_id,
                    )
                except Exception:
                    pass  # extraction must never block execution

            # Compute failure-signature diagnostics from this run's traces
            from armature.state.diagnostics import DiagnosticAnalyzer
            try:
                run_traces = await self._traces.query_by_run(self._run_id)
                diagnostics = DiagnosticAnalyzer(run_traces).analyze()
            except Exception:
                diagnostics = []

            # Execute post_run stages sequentially with enriched context
            if post_run_stages:
                post_ctx = {
                    **context,
                    **results,
                    "_transcript": self._transcript,
                    "_diagnostics": [d.model_dump() for d in diagnostics],
                }
                for stage in post_run_stages:
                    stage_result = await self._execute_stage_with_recovery(stage, post_ctx)
                    results[stage.id] = stage_result
                    post_ctx[stage.id] = stage_result

            # Evaluate trace-triggered behaviors against recent traces
            try:
                recent_traces = await self._traces.query(
                    workflow_name=self._spec.name, limit=50
                )
                self._behaviors.evaluate(recent_traces)
            except Exception:
                pass  # behaviors must never block execution

            return results

    async def _load_prior_context(self) -> dict | None:
        cfg = self._spec.continuation
        if not cfg or not cfg.carry_forward:
            return None
        await self._ensure_traces()
        prior_run_id = await self._traces.latest_run_id(self._spec.name)
        if prior_run_id is None or prior_run_id == self._run_id:
            return None
        run_outputs = await self._traces.get_run_outputs(prior_run_id)
        result: dict = {}
        for entry in cfg.carry_forward:
            parts = entry.key.split(".", 1)
            if len(parts) != 2:
                continue
            stage_id, output_key = parts
            if stage_id in run_outputs and output_key in run_outputs[stage_id]:
                result[output_key] = run_outputs[stage_id][output_key]
        return result or None

    async def evaluate(
        self,
        run_id: str | None = None,
        model: str | None = None,
    ) -> "list[Any]":
        """Score stage outputs against their declarative evaluate criteria.

        run_id defaults to the most recent run. Returns a list of EvaluationResult,
        one per stage that has evaluate criteria and a recorded trace.
        """
        from armature.state.evaluator import EvaluationRunner, EvaluationStore

        rid = run_id or self._run_id
        eval_model = model or (
            self._spec.model_tiers.small.model if self._spec.model_tiers.small else "gpt-4o-mini"
        )
        eval_store = EvaluationStore(self._session_dir / "evaluations.db")
        await eval_store.init()

        runner = EvaluationRunner(model=eval_model, evaluation_store=eval_store)
        await self._ensure_traces()
        return await runner.evaluate_run(run_id=rid, spec=self._spec, trace_store=self._traces)

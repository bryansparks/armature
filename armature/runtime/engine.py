from __future__ import annotations
import asyncio
import hashlib
import json
import os
import uuid
import time
from pathlib import Path
from contextvars import ContextVar
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable
from armature.spec.models import AgentStep, HarnessSpec, Stage
from armature.spec.loader import load_spec
from armature.spec.calls import BundleCallResolver, CallResolver
from armature.spec.context import (
    MISSION_LAYER_NAME, WORK_UNIT_LAYER_NAME, EffectiveContextPolicy,
    ordered_layers, resolve_effective_policy,
)
from armature.runtime.dag import DAGExecutor
from armature.runtime.context import ContextManager
from armature.runtime.prompt import PromptAssembler
from armature.nodes.llm import LLMNode
from armature.nodes.script import ScriptNode
from armature.nodes.gate import (
    CHECKPOINT_FILE,
    GATE_DIR_ENV,
    GateSuspended,
    HumanGateNode,
    gate_digest,
    gate_message,
    kill_descendants,
    write_gate,
)
from armature.nodes.stub import StubNode, StubOutputs
from armature.nodes.provider_errors import classify_provider_error
from armature.registry.registry import ToolRegistry
from armature.registry.builtins import register_builtins
from armature.hooks.lifecycle import HookRegistry, HookDecision, make_default_behavior_registry, RogueSignalCounter
from armature.state.session import SessionLog, SessionEvent
from armature.state.artifacts import ArtifactStore
from armature.state.traces import TraceStore, TraceRecord
from armature.telemetry import get_tracer
from armature.events import Emitter, NullEmitter
from armature.events.engine import forward_engine_event
from armature.events.sign import read_engine_key, sign_engine
from armature.events.env import child_env
from armature.events.register import register_run, registration_body


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

HEARTBEAT_S = 60.0

# (harness id, stage id, branch index, branch count) for the fan-out branch running in this task.
_FAN_OUT_BRANCH: ContextVar[tuple[int, str, int, int] | None] = ContextVar(
    "armature_fan_out_branch", default=None
)


def _carry_output_cap(stage_id: str, spec: "HarnessSpec") -> int:
    if spec.continuation:
        carry_ids = {e.key.split(".")[0] for e in spec.continuation.carry_forward}
        if stage_id in carry_ids:
            return 2000
    return 200


def _build_context_block(
    layers: list[tuple[str, str]],
    context: dict,
    spec_stage_ids: set[str],
    max_preview_chars: int = 200,
) -> str:
    """Build the must'd-layers + prior-stage breadcrumb block for LLM system prompts.

    `layers` is one (header, content) pair per must'd context layer, in render
    order. The mission pseudo-layer renders as `[Workflow Mission]` — exact
    back-compat with the old mission block; user layers as
    `[Context Layer: {name}]`.
    """
    parts = []
    for header, content in layers:
        text = content.strip()
        if text:
            parts.append(f"{header}\n{text}")
    prior = []
    for sid in spec_stage_ids:
        if sid in context:
            preview = json.dumps(context[sid], default=str)[:max_preview_chars]
            prior.append(f"• {sid} → {preview}")
    if prior:
        parts.append("[Prior stages]\n" + "\n".join(prior))
    return "\n\n".join(parts)


def _apply_context_never(never: frozenset[str], context: dict[str, Any]) -> dict[str, Any]:
    """Copy `context` minus every key a NEVER closes.

    `_transcript` (post_run context) is special-cased: entries whose stage_id
    is never'd are dropped individually, so a post_run analyst still sees
    the rest of the transcript — unless `_transcript` itself is closed.
    """
    if not never:
        return context
    filtered = {k: v for k, v in context.items() if k not in never}
    if "_transcript" in never:
        return filtered
    transcript = context.get("_transcript")
    if isinstance(transcript, list):
        filtered["_transcript"] = [
            e for e in transcript
            if not (isinstance(e, dict) and e.get("stage_id") in never)
        ]
    return filtered


def _agent_role(stage: Stage, context: dict[str, Any]) -> str:
    """What the run log groups an agent session under: its fan-out item's name, else the stage id."""
    branch = _FAN_OUT_BRANCH.get()
    if branch is None or branch[1] != stage.id:
        return stage.id
    item = context.get(stage.partition_key or "item")
    if isinstance(item, dict):
        item = item.get("name") or item.get("role")
    return item if isinstance(item, str) and item else stage.id


class Harness:
    @property
    def _on_event(self) -> Callable[[str, dict[str, Any]], None] | None:
        if self._events.enabled:
            return self._forward_event
        return self._user_on_event

    @_on_event.setter
    def _on_event(self, callback: Callable[[str, dict[str, Any]], None] | None) -> None:
        self._user_on_event = callback

    def _forward_event(self, name: str, data: dict[str, Any]) -> None:
        try:
            forward_engine_event(self._emit, name, data)
        except Exception:
            pass
        if self._user_on_event:
            self._user_on_event(name, data)

    def _emit(self, kind: str, step_id: str | None = None, /, **payload: Any) -> None:
        if not self._events.enabled:
            return
        branch = _FAN_OUT_BRANCH.get()
        if branch is not None and branch[0] == id(self) and branch[1] == step_id:
            payload["branch"], payload["branches"] = branch[2], branch[3]
        if self._event_parent_step is not None:
            payload["parent_step"] = self._event_parent_step
        self._events.emit(kind, step_id, **payload)

    def _emit_step_failed(self, stage: Stage, exc: BaseException) -> None:
        self._emit("step_failed", stage.id, type=type(exc).__name__, reason=str(exc))

    async def _open_run_emitter(self, inputs: dict[str, Any] | None) -> Emitter | None:
        """This run's own emitter, or None. Sets whether the run is nested in another."""
        if os.environ.get("ARMATURE_EVENTS") and os.environ.get("ARMATURE_EVENTS_TOKEN"):
            # ARMATURE_STEP_ID means a step of another run started this one.
            outer_step = os.environ.get("ARMATURE_STEP_ID") or None
            if outer_step is not None:
                self._event_parent_step = outer_step
                self._events_run_id = os.environ.get("ARMATURE_RUN_ID") or None
            return Emitter.from_env()
        server = os.environ.get("ARMATURE_SERVER")
        runner_key = os.environ.get("ARMATURE_RUNNER_KEY")
        if not (server and runner_key):
            return None
        registered = await asyncio.to_thread(
            register_run, server, runner_key, registration_body(self._spec.name, inputs)
        )
        if registered is None:
            return None
        self._events_run_id, emitter = registered
        return emitter

    def _script_env(self, step_id: str) -> dict[str, str]:
        env = {"ARMATURE_RUN_ID": self._events_run_id or self._run_id, "ARMATURE_STEP_ID": step_id}
        if self._events.enabled:
            env["ARMATURE_EVENTS"] = self._events.endpoint
            env["ARMATURE_EVENTS_TOKEN"] = self._events.token
        return env

    def _script_inputs(self, stage, adapter, context: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
        """The child env and context for a script stage, with its rendered with: and $PACK_DIR."""
        env = self._script_env(stage.id)
        if adapter.pack_dir is not None:
            env["PACK_DIR"] = adapter.pack_dir
        if stage.with_ is None and adapter.params is None:
            return env, context
        from armature.packs.params import render_with, with_errors
        values = render_with(stage.with_ or {}, context)
        if adapter.params is not None:
            problems = with_errors(adapter.params, values, allow_templates=False)
            if problems:
                raise ValueError(f"Stage '{stage.id}' with: does not match adapter '{adapter.name}': "
                                 + "; ".join(problems))
        env["ARMATURE_WITH"] = json.dumps(values, default=str)
        return env, {**context, "with": values}

    async def _tool_env(self, stage, adapter) -> dict[str, str]:
        """A built-in pack step's grant to call the server's tools, for that step's child only."""
        from armature.packs import is_builtin
        from armature.packs.tool import TOKEN_ENV, URL_ENV, request_grant
        url, engine = os.environ.get(URL_ENV), self._engine_key
        if not is_builtin(adapter.pack_dir) or not url or not engine or not self._events.enabled:
            return {}
        tools = [adapter.name.replace("/", ".", 1)]
        return {TOKEN_ENV: await asyncio.to_thread(request_grant, url, self._events.token, stage.id, tools, engine)}

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
        emitter: Emitter | None = None,
        parent_step: str | None = None,
        call_resolver: CallResolver | None = None,
        engine_key: tuple[str, str] | None = None,
    ):
        # A child run in this process signs with the key its parent hands it, under the root run's id. A root run
        # reads its key first, before this process starts anything, so the pipe holding it is closed when it does.
        if engine_key is None:
            engine_key = read_engine_key()
            engine_key = engine_key if parent_step is None and emitter is None else None
        self._engine_key = engine_key
        self._gate_digest: str | None = None
        self._call_resolver = call_resolver if call_resolver is not None else BundleCallResolver.for_spec(spec)
        if validate:
            from armature.spec.validator import validate_spec
            validate_spec(spec, call_resolver=self._call_resolver)

        self._spec = spec
        self._stubs = StubOutputs.from_env()
        self._total_cost_usd = 0.0
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
        if self._spec.sandbox.mode == SandboxMode.DOCKER and self._stubs is None:
            try:
                import subprocess as _sp
                _proc = _sp.run(
                    [self._spec.sandbox.runtime, "inspect", "--format", "{{.Id}}", self._spec.sandbox.image],
                    capture_output=True, text=True, timeout=10, env=child_env(),
                )
                if _proc.returncode == 0:
                    self._sandbox_image_digest = _proc.stdout.strip() or None
            except Exception:
                pass  # docker not available or image not pulled; proceed without digest
        self._policy_version = hashlib.sha256(
            str([r.model_dump() for r in self._spec.safety_rules]).encode()
        ).hexdigest()[:12]
        self._context_policies: dict[str, EffectiveContextPolicy] = {
            s.id: resolve_effective_policy(spec, s) for s in spec.stages
        }
        self._record_context_policy = bool(
            spec.context_layers or spec.context_policy
            or any(s.context_policy for s in spec.stages)
        )
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
        # Route every tool dispatch through the safety hooks. This is the
        # chokepoint that makes safety_rules/strict_mode fire on the
        # tool_call: and LLM ReAct paths (which call registry.dispatch
        # directly), not just the adapter path.
        self._registry.attach_hooks(self._hooks)
        self._attach_observability_adapters()
        self._context = ContextManager()
        self._assembler = PromptAssembler()
        self._session_dir = base_dir
        resolved_traces = Path(traces_db).expanduser() if traces_db else _GLOBAL_TRACES_DB.expanduser()
        self._traces = TraceStore(resolved_traces)
        self._given_emitter = emitter
        self._events: Emitter = emitter if emitter is not None else NullEmitter()
        self._parent_step = parent_step
        self._event_parent_step = parent_step
        self._events_run_id: str | None = None
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
        # Shared embedder between the knowledge extractor and navigation tools.
        # Constructed once (in the extract block or the nav block) and reused.
        self._embedder: "LocalEmbedder | None" = None
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
                try:
                    from armature.state.embedder import LocalEmbedder
                    if LocalEmbedder.is_available():
                        self._embedder = LocalEmbedder()
                except Exception:
                    self._embedder = None
                self._knowledge_extractor = KnowledgeExtractor(
                    model=self._spec.model_tiers.small.model
                    if self._spec.model_tiers.small else "gpt-4o-mini",
                    knowledge_store=self._knowledge_store,
                    embedder=self._embedder,
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

        # ── Memory navigation tools (read-only over L0/L1) ──
        if self._memory_config is not None and self._memory_config.navigation_tools:
            # Ensure a knowledge store exists even if extract_knowledge is false;
            # navigation tools query L1 when available.
            if self._knowledge_store is None:
                from armature.state.knowledge import KnowledgeStore
                knowledge_path = mem_path.with_name(mem_path.stem + "_knowledge.db")
                self._knowledge_store = KnowledgeStore(knowledge_path)
            # Reuse the extractor's embedder if present; else try to construct one.
            if self._embedder is None:
                try:
                    from armature.state.embedder import LocalEmbedder
                    if LocalEmbedder.is_available():
                        self._embedder = LocalEmbedder()
                except Exception:
                    self._embedder = None
            from armature.registry.memory_tools import register_memory_tools
            register_memory_tools(
                self._registry,
                memory_store=self._memory_store,
                knowledge_store=self._knowledge_store,
                trace_store=self._traces,
                embedder=self._embedder,
                workflow_name=self._memory_workflow_name,
                run_id=self._run_id,
            )

        # Only the run a job started pauses at a gate. A called or child workflow shares its process and env.
        gate_dir = os.environ.get(GATE_DIR_ENV)
        top_level = parent_step is None and emitter is None
        self._gate_dir = Path(gate_dir) if gate_dir and top_level else None
        self._nested_gate = bool(gate_dir) and not top_level
        if self._spec.checkpoint or self._gate_dir is not None:
            from armature.runtime.checkpoint import CheckpointStore, SessionLock
            checkpoint_dir = self._gate_dir or base_dir
            self._checkpoint: "CheckpointStore | None" = CheckpointStore(
                checkpoint_dir / CHECKPOINT_FILE
            )
            self._session_lock: "SessionLock | None" = SessionLock(
                checkpoint_dir / "session.lock"
            )
        else:
            self._checkpoint = None
            self._session_lock = None
        self._checkpoint_prior: dict[str, Any] = {}
        self._checkpoint_loop_iters: dict[str, Any] = {}
        self._llm_call_count: int = 0
        self._mcp_sessions: list[Any] = []

    def _load_tool_modules(self) -> None:
        if self._stubs is not None:
            return
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
        if not self._spec.mcp_servers or self._stubs is not None:
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

    @property
    def _memory_workflow_name(self) -> str:
        """Workflow name used to namespace memory records (L0/L1).

        Defaults to the spec's `name`, but `memory.workflow_name` can override it
        so a variant spec shares another workflow's accumulated memory without
        changing its trace/workflow identity.
        """
        if self._memory_config is not None:
            return self._memory_config.workflow_name or self._spec.name
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

    async def _record_trace(self, trace: TraceRecord) -> None:
        """Record a trace for a stage that succeeded, without being able to fail it.

        The success-path write sits inside the stage's try, so an exception here
        is indistinguishable from the stage itself failing: the work is thrown
        away and a fan-out branch silently returns _fan_out_error instead. A
        dropped row costs a dashboard entry; a raised one costs the whole stage.
        """
        try:
            await self._ensure_traces()
            await self._traces.record(trace)
        except Exception:
            pass

    async def _ensure_cache(self) -> None:
        if self._llm_cache is not None and not hasattr(self, "_cache_initialized"):
            await self._llm_cache.init()
            self._cache_initialized = True

    def _get_provenance(self) -> dict[str, str]:
        return getattr(self, "_provenance", {})

    def _must_layer_pairs(self, gov: EffectiveContextPolicy) -> list[tuple[str, str]]:
        """(header, content) per must'd layer, in precedence render order.

        The mission pseudo-layer renders as `[Workflow Mission]` — exact
        back-compat with the old mission block; user layers as
        `[Context Layer: {name}]`.
        """
        pairs: list[tuple[str, str]] = []
        for layer in ordered_layers(self._spec, getattr(self, "_work_record", None)):
            if layer.name not in gov.must or layer.content is None:
                continue
            if layer.name == MISSION_LAYER_NAME:
                header = "[Workflow Mission]"
            elif layer.name == WORK_UNIT_LAYER_NAME:
                header = "[Work Unit]"
            else:
                header = f"[Context Layer: {layer.name}]"
            pairs.append((header, layer.content))
        return pairs

    async def _execute_stage(self, stage: Stage, context: dict[str, Any]) -> Any:
        await self._session.append(SessionEvent(type="stage_start", data={"stage": stage.id}))

        if self._on_event:
            if stage.gate:
                stage_kind = "gate"
            elif stage.tool_call:
                stage_kind = "tool_call"
            elif stage.adapter:
                stage_kind = "script"
            elif stage.call:
                stage_kind = "call"
            elif isinstance(stage.agent, AgentStep):
                stage_kind = "agent"
            else:
                stage_kind = "llm"
            role_label = f"{stage.role.name} ({stage.role.type.value})" if stage.role else None
            self._on_event("stage_start", {"stage": stage.id, "kind": stage_kind, "role": role_label})

        self._sandbox_provider.set_stage_image(stage.sandbox_image)

        decision = await self._hooks.run_pre_stage(stage.id, context)
        if decision == HookDecision.BLOCK:
            raise PermissionError(f"Stage '{stage.id}' blocked by lifecycle hook")

        # Context governance: a NEVER at any level closes context keys for
        # every downstream dispatch (LLM, gate, tool_call, adapter, subagent)
        # and for the breadcrumb block — closed content cannot leak via previews.
        gov = self._context_policies.get(stage.id)
        if gov is not None and gov.never:
            context = _apply_context_never(gov.never, context)

        t0 = time.monotonic()
        tracer = get_tracer()

        _llm_node: "LLMNode | None" = None
        _agent_node = None
        _stage_type = "unknown"

        _stage_succeeded = False
        try:
            with tracer.start_as_current_span(
                f"armature.stage.{stage.id}",
                attributes={"stage": stage.id, "workflow": self._spec.name},
            ) as span:
                try:
                    if self._stubs is not None:
                        _stage_type = "stub"
                        branch = _FAN_OUT_BRANCH.get()
                        index = branch[2] if branch is not None and branch[:2] == (id(self), stage.id) else None
                        result = await StubNode(stage, self._stubs, index).execute(context)
                    elif stage.gate == "human":
                        _stage_type = "gate"
                        node = HumanGateNode(stage=stage)
                        result = await node.execute(context)
                        gate_latency = (time.monotonic() - t0) * 1000
                        approved = result.get("approved", True) if isinstance(result, dict) else True
                        await self._record_trace(TraceRecord(
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
                            context_policy=(
                                gov.as_dict()
                                if gov is not None and self._record_context_policy else None
                            ),
                            inputs={k: str(v)[:200] for k, v in context.items()},
                            outputs={k: str(v)[:200] for k, v in (result.items() if isinstance(result, dict) else {})},
                            inputs_provenance=dict(self._get_provenance()),
                        ))
                    elif stage.subagent_spec:
                        _stage_type = "subagent"
                        from armature.nodes.subagent import SubagentNode
                        node = SubagentNode(
                            stage=stage, session_dir=self._session_dir,
                            emitter=self._events, parent_step=stage.id,
                            call_resolver=self._call_resolver, engine_key=self._engine_key,
                        )
                        try:
                            result = await node.execute(context)
                        finally:
                            # Child-run spend is observed spend of this run
                            # (design §2.3), failed attempts included.
                            self._total_cost_usd += node.total_cost_usd
                    elif stage.call:
                        _stage_type = "call"
                        from armature.nodes.call import CallNode
                        node = CallNode(
                            stage=stage, resolver=self._call_resolver, session_dir=self._session_dir,
                            emitter=self._events, parent_step=stage.id, engine_key=self._engine_key,
                        )
                        try:
                            result = await node.execute(context)
                        finally:
                            self._total_cost_usd += node.total_cost_usd
                        self._emit("log", stage.id, event="call", workflow=stage.call.workflow,
                                   version=node.resolved_version)
                    elif isinstance(stage.agent, AgentStep):
                        _stage_type = "agent"
                        from armature.nodes.agent import AgentNode
                        limit = self._spec.contracts.max_llm_calls
                        if limit > 0 and self._llm_call_count >= limit:
                            raise RuntimeError(
                                f"max_llm_calls limit ({limit}) reached — "
                                f"stage '{stage.id}' cannot execute"
                            )
                        # Reserved now so concurrent fan-out branches see it, then corrected to the sessions run.
                        self._llm_call_count += 1
                        _agent_node = AgentNode(
                            stage, root=self._session_dir / "agents", step_id=stage.id,
                            emitter=(SimpleNamespace(emit=self._emit, redact=self._events.redact)
                                     if self._events.enabled else None),
                            fields={"role": _agent_role(stage, context)},
                        )
                        try:
                            result = await _agent_node.execute(context)
                        finally:
                            self._total_cost_usd += _agent_node.total_cost_usd
                            self._llm_call_count += _agent_node.sessions - 1
                        self._get_provenance().update({k: f"stage:{stage.id}" for k in result})
                        await self._record_trace(TraceRecord(
                            run_id=self._run_id,
                            workflow_name=self._spec.name,
                            stage_id=stage.id,
                            role_type="agent",
                            model=_agent_node.model,
                            latency_ms=(time.monotonic() - t0) * 1000,
                            success=True,
                            output_valid=True,
                            spec_version=self._spec_version,
                            policy_version=self._policy_version,
                            context_policy=(
                                gov.as_dict()
                                if gov is not None and self._record_context_policy else None
                            ),
                            inputs={k: str(v)[:200] for k, v in context.items()},
                            outputs={k: str(v)[:(_carry_output_cap(stage.id, self._spec))] for k, v in result.items()},
                            inputs_provenance=dict(self._get_provenance()),
                            active_skill_ids=list(stage.agent.skills),
                        ))
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
                        script_env, script_context = self._script_inputs(stage, adapter, context)
                        script_env.update(await self._tool_env(stage, adapter))
                        node = ScriptNode(
                            adapter=adapter, env=script_env,
                            emit=self._emit if self._events.enabled else None, step_id=stage.id,
                        )
                        tool_args = {"cmd": adapter.cmd or ""}
                        decision = await self._hooks.run_pre_tool(stage.adapter, tool_args, context)
                        if decision == HookDecision.BLOCK:
                            # Fallback for programmatic hooks that return BLOCK without raising.
                            # SafetyHookBuilder hooks raise ToolBlocked directly (carrying rule.message).
                            from armature.hooks.lifecycle import ToolBlocked
                            raise ToolBlocked(stage.adapter, adapter.cmd or "", "blocked by safety rule")
                        result = await node.execute(script_context)
                        await self._hooks.run_post_tool(stage.adapter, result, context)
                        script_latency = (time.monotonic() - t0) * 1000
                        self._get_provenance().update({k: f"stage:{stage.id}" for k in result})
                        await self._record_trace(TraceRecord(
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
                            context_policy=(
                                gov.as_dict()
                                if gov is not None and self._record_context_policy else None
                            ),
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
                        mission_ctx = _build_context_block(
                            self._must_layer_pairs(
                                gov if gov is not None
                                else EffectiveContextPolicy(must=(), never=frozenset())
                            ),
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
                        latency = (time.monotonic() - t0) * 1000
                        span.set_attribute("latency_ms", latency)
                        output_valid = "_parse_error" not in result
                        self._get_provenance().update({k: f"stage:{stage.id}" for k in result
                                                      if not k.startswith("_")})
                        _escalation_count = result.pop("_escalation_count", 0)
                        _cost_usd = result.pop("_cost_usd", 0.0)
                        self._total_cost_usd += _cost_usd
                        _model = _llm_node._resolve_model()
                        _input_tokens = result.pop("_input_tokens", 0)
                        _output_tokens = result.pop("_output_tokens", 0)
                        self._emit(
                            "cost", stage.id, model=_model, input_tokens=_input_tokens,
                            output_tokens=_output_tokens, cost_usd=_cost_usd,
                        )
                        await self._record_trace(TraceRecord(
                            run_id=self._run_id,
                            workflow_name=self._spec.name,
                            stage_id=stage.id,
                            role_type=stage.role.type.value,
                            model=_model,
                            input_tokens=_input_tokens,
                            output_tokens=_output_tokens,
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
                            context_policy=(
                                gov.as_dict()
                                if gov is not None and self._record_context_policy else None
                            ),
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
                    if _stage_type in ("llm", "script", "agent"):
                        try:
                            await self._ensure_traces()
                            _fail_latency = (time.monotonic() - t0) * 1000
                            _fail_role = stage.role.type.value if _stage_type == "llm" else _stage_type
                            if _stage_type == "llm":
                                _fail_model = _llm_node._resolve_model() if _llm_node else "unknown"
                            else:
                                _fail_model = _agent_node.model if _agent_node else ""
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
                                context_policy=(
                                    gov.as_dict()
                                    if gov is not None and self._record_context_policy else None
                                ),
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
                                workflow_name=self._memory_workflow_name,
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
            self._on_event("stage_complete", {"stage": stage.id, "elapsed_s": elapsed, "output": result})

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
        from jinja2 import ChainableUndefined
        from armature.runtime.templating import native_env

        items = native_env(ChainableUndefined).from_string(stage.partition_source).render(**context)

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

        async def _run_one(item_index: int, item: Any) -> Any:
            async with semaphore:
                per_ctx = {**context, partition_key: item}
                if stage.subagent_spec is not None or stage.call is not None:
                    # Reserved context key (same convention as the loop
                    # machinery's _iteration) letting SubagentNode give each
                    # engine-fanned child a distinct session directory.
                    per_ctx["_fan_out_item_index"] = item_index
                if stage.inject_file_as is not None and self._stubs is None:
                    try:
                        per_ctx[stage.inject_file_as] = _Path(str(item)).read_text(errors="replace")
                    except Exception:
                        per_ctx[stage.inject_file_as] = ""
                branch = _FAN_OUT_BRANCH.set((id(self), stage.id, item_index, len(items)))
                try:
                    return await self._execute_stage(stage, per_ctx)
                except Exception as exc:
                    self._emit_step_failed(stage, exc)
                    return {"_fan_out_error": str(exc), "vulnerabilities": []}
                finally:
                    _FAN_OUT_BRANCH.reset(branch)

        raw = await asyncio.gather(*[_run_one(i, item) for i, item in enumerate(items)])
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
        from jinja2 import ChainableUndefined
        from armature.runtime.templating import text_env
        env = text_env(ChainableUndefined)
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
            self._resumed(stage, self._checkpoint_prior[stage.id], {"stage": stage.id})
            return self._checkpoint_prior[stage.id]

        loop_cfg = stage.loop
        var_name = loop_cfg.iteration_var
        result: Any = None
        iteration_ctx = dict(context)
        loop_t0 = time.monotonic()

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

            elapsed = time.monotonic() - loop_t0
            if not is_last and loop_cfg.timeout_s is not None and elapsed >= loop_cfg.timeout_s:
                reason = (f"loop.timeout_s={loop_cfg.timeout_s}s spent after {iteration_num} "
                          f"round(s) ({elapsed:.1f}s) before until was met")
                if isinstance(result, dict):
                    result = {**result, "_loop_stopped": reason}
                if self._on_event:
                    self._on_event("loop_stopped", {
                        "stage": stage.id, "iteration": iteration_num, "reason": reason,
                    })
                break

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

    def _resumed(self, stage: Stage, result: Any, event_data: dict[str, Any]) -> None:
        """Report a stage a checkpoint answered. A run the server resumes already has its finished steps, so only
        a gate's answer is news there."""
        if not self._on_event:
            return
        if self._gate_dir is None:
            self._on_event("stage_resumed", event_data)
        elif stage.gate == "human":
            self._on_event("stage_complete", {**event_data, "elapsed_s": 0, "output": result})

    def _suspend_payload(self, paused: GateSuspended) -> dict[str, Any]:
        payload = {"step_id": paused.stage.id, "key": paused.key, "digest": self._gate_digest,
                   "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
        if self._engine_key is not None:
            run_id, key = self._engine_key
            payload["sig"] = sign_engine(key, {"kind": "run_suspend", "run_id": run_id, **payload})
        return payload

    def _write_gate(self, paused: GateSuspended) -> None:
        # Through JSON first, so the digest covers exactly what the host will read back.
        request = json.loads(json.dumps({
            "step_id": paused.stage.id, "key": paused.key, "present": paused.present,
            "checkpoint": {**self._checkpoint_prior, **self._checkpoint_loop_iters},
        }, default=str))
        write_gate(self._gate_dir, request)
        self._gate_digest = gate_digest(request)

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
        heartbeat = (
            asyncio.create_task(self._heartbeat(stage.id)) if self._events.enabled else None
        )
        try:
            return await self._execute_stage_recovering(stage, context, _loop_iteration)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()

    async def _heartbeat(self, stage_id: str) -> None:
        t0 = time.monotonic()
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            self._emit("progress", stage_id, stage=stage_id, elapsed_s=round(time.monotonic() - t0, 1))

    async def _execute_stage_recovering(
        self, stage: Stage, context: dict[str, Any],
        _loop_iteration: int | None = None,
    ) -> Any:
        # Return cached result from a prior run (checkpoint resume).
        # For loop iterations, use iteration-scoped keys so that iteration N
        # doesn't short-circuit iteration N+1.
        _cp_key = stage.id if _loop_iteration is None else f"{stage.id}__iter_{_loop_iteration}"
        _cp_store = self._checkpoint_loop_iters if _loop_iteration is not None else self._checkpoint_prior

        if self._checkpoint is not None and _cp_key in _cp_store:
            event_data: dict[str, Any] = {"stage": stage.id}
            if _loop_iteration is not None:
                event_data["iteration"] = _loop_iteration
            self._resumed(stage, _cp_store[_cp_key], event_data)
            return _cp_store[_cp_key]

        # Evaluate skip_if (negative gate) and condition (positive gate).
        # condition is the inverse: stage runs only when truthy, skips when falsy.
        # Both are evaluated; if either triggers a skip, the stage is skipped.
        from jinja2 import ChainableUndefined
        from armature.runtime.templating import text_env
        _jinja_env = text_env(ChainableUndefined)

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

        if stage.gate == "human" and self._stubs is None and (self._gate_dir is not None or self._nested_gate):
            if self._nested_gate:
                raise RuntimeError(f"Stage '{stage.id}' is a human gate in a called workflow. Only the workflow "
                                   "a job runs can pause at one.")
            gov = self._context_policies.get(stage.id)
            shown = _apply_context_never(gov.never, context) if gov is not None and gov.never else context
            if self._on_event:
                self._on_event("stage_start", {"stage": stage.id, "kind": "gate", "role": None})
            raise GateSuspended(stage, _cp_key, gate_message(stage, shown))

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
                self._emit_step_failed(stage, exc)
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
            timeout_exc = TimeoutError(f"Stage '{stage.id}' timed out after {stage.timeout_s}s")
            self._emit_step_failed(stage, timeout_exc)
            raise timeout_exc
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
            self._emit_step_failed(stage, exc)
            raise

    def _validate_inputs(self, context: dict[str, Any]) -> dict[str, Any]:
        """Coerce declared inputs in ``context`` to their types and apply defaults; raises InputError."""
        from armature.spec.contract_io import resolve_inputs
        context.update(resolve_inputs(self._spec.contracts.inputs, context))
        return context

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

    @property
    def total_cost_usd(self) -> float:
        """Sum of this run's own observed LLM costs (provider-reported per
        response). Zero when nothing metered — never a provider balance check."""
        return getattr(self, "_total_cost_usd", 0.0)

    async def run(
        self,
        inputs: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Execute the workflow.

        With checkpoint mode on, the session lock is held for the duration
        of the run so a concurrent run against the same session directory
        fails loudly (SessionDirInUse) instead of silently duplicating
        un-checkpointed stage effects. See docs/CHECKPOINT-AND-RESUME.md
        → Effect-delivery contract.
        """
        owned: Emitter | None = None
        if self._given_emitter is None:
            self._event_parent_step = self._parent_step
            self._events_run_id = None
            owned = await self._open_run_emitter(inputs)
            self._events = owned if owned is not None else NullEmitter()
        lifecycle = self._events.enabled and self._event_parent_step is None
        if not lifecycle and owned is None:
            return await self._run_guarded(inputs, force=force)
        if lifecycle:
            self._emit("run_start", workflow=self._spec.name, spec_version=self._spec_version)
        t0 = time.monotonic()
        status, error, paused = "failed", None, None
        try:
            results = await self._run_guarded(inputs, force=force)
            status = "succeeded"
            return results
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except GateSuspended as exc:
            paused = exc
            raise
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if lifecycle and paused is not None:
                self._emit("run_suspend", **self._suspend_payload(paused), elapsed_s=round(time.monotonic() - t0, 2))
            elif lifecycle:
                self._emit(
                    "run_end", status=status, error=error,
                    elapsed_s=round(time.monotonic() - t0, 2),
                )
            if owned is not None:
                await asyncio.to_thread(owned.close)

    async def _run_guarded(
        self,
        inputs: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        if self._session_lock is None:
            return await self._run_locked(inputs, force=force)
        with self._session_lock:
            try:
                return await self._run_locked(inputs, force=force)
            except GateSuspended as paused:
                # The checkpoint already holds every finished stage, so the request goes out after it.
                if self._gate_dir is not None:
                    kill_descendants()
                    self._write_gate(paused)
                raise

    async def _run_locked(
        self,
        inputs: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        await self._attach_mcp_servers()
        self._total_cost_usd = 0.0

        context = dict(inputs or {})
        context["run_id"] = self._run_id
        if self._spec.continuation:
            _prior = await self._load_prior_context()
            if _prior is not None:
                context[self._spec.continuation.inject_as] = _prior
        self._validate_inputs(context)
        self._provenance: dict[str, str] = {k: "user_input" for k in (inputs or {})}

        # Work record injected by a mission executor (mission_source: work_unit).
        # It drives the mission + work_unit pseudo-layers (design §4); its
        # absence falls back to the spec's static mission. Per-run state, so
        # the per-spec policy map built in __init__ is recomputed here.
        self._work_record = (context.get("work_unit")
                             if isinstance(context.get("work_unit"), dict) else None)
        if self._work_record is not None:
            self._provenance["work_unit"] = "work_record"
        self._context_policies = {
            s.id: resolve_effective_policy(self._spec, s, self._work_record)
            for s in self._spec.stages
        }

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
                memories, stale_keys = await self._memory_store.load(self._memory_workflow_name)
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
                    self._memory_workflow_name, query=self._spec.name, top_k=10
                )
                context[mem_cfg.inject_knowledge_as] = [
                    {"entity": k.entity, "fact": k.fact, "confidence": k.confidence}
                    for k in knowledge
                ]
                if mem_cfg.navigation_tools:
                    context["_memory_index"] = await self._knowledge_store.index_summary(
                        self._memory_workflow_name
                    )
                    self._provenance["_memory_index"] = "memory_index"

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
            if self._knowledge_extractor is not None and self._memory_store is not None and self._stubs is None:
                try:
                    updated_memories, _ = await self._memory_store.load(self._memory_workflow_name)
                    await self._knowledge_extractor.extract(
                        updated_memories,
                        workflow_name=self._memory_workflow_name,
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

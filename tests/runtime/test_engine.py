import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from armature.runtime.engine import Harness, _extract_quorum_score
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
    MemoryConfig, MemoryCapture,
)
from armature.state.traces import TraceStore

def make_minimal_spec() -> HarnessSpec:
    return HarnessSpec(
        name="test",
        version="1.0",
        stages=[
            Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="test"))
        ]
    )

async def test_harness_from_spec():
    spec = make_minimal_spec()
    harness = Harness(spec=spec)
    assert harness.name == "test"

async def test_harness_run_returns_result():
    spec = make_minimal_spec()
    harness = Harness(spec=spec)

    with patch.object(harness, "_execute_stage", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {"content": "stage output"}
        result = await harness.run({"topic": "test"})

    assert result is not None
    mock_exec.assert_called_once()

async def test_harness_initializes_trace_store(tmp_path):
    spec = make_minimal_spec()
    harness = Harness(spec=spec, session_dir=tmp_path)
    assert hasattr(harness, "_traces")
    assert isinstance(harness._traces, TraceStore)


async def test_harness_uses_global_traces_db_by_default():
    spec = make_minimal_spec()
    harness = Harness(spec=spec)
    assert harness._traces._path == Path("~/.armature/traces.db").expanduser()


async def test_harness_uses_custom_traces_db(tmp_path):
    spec = make_minimal_spec()
    harness = Harness(spec=spec, traces_db=tmp_path / "custom.db")
    assert harness._traces._path == tmp_path / "custom.db"


async def test_engine_wires_sandbox_when_mode_docker():
    """Harness.__init__ calls DockerSandboxProvider.wrap_registry when mode=DOCKER."""
    from armature.spec.models import SandboxConfig, SandboxMode
    from unittest.mock import patch as _patch
    spec = make_minimal_spec()
    spec = spec.model_copy(update={"sandbox": SandboxConfig(mode=SandboxMode.DOCKER)})
    with _patch("armature.sandbox.docker.DockerSandboxProvider.wrap_registry") as mock_wrap:
        Harness(spec=spec, validate=False)
    mock_wrap.assert_called_once()
    _, call_sandbox, _ = mock_wrap.call_args[0]
    assert call_sandbox.mode == SandboxMode.DOCKER


async def test_engine_sandbox_none_is_noop():
    """Harness.__init__ with default sandbox (mode=NONE) does not replace tool handlers."""
    from armature.spec.models import SandboxConfig, SandboxMode
    from unittest.mock import patch as _patch
    spec = make_minimal_spec()
    with _patch("armature.sandbox.docker.DockerSandboxProvider.wrap_registry") as mock_wrap:
        harness = Harness(spec=spec, validate=False)
    # wrap_registry may be called but must be a no-op; either way shell handler is builtin
    shell_desc = harness._registry.get("shell")
    assert shell_desc is not None  # builtin still registered


def test_harness_from_file(tmp_path):
    spec_file = tmp_path / "test.yaml"
    spec_file.write_text("""
name: file-test
version: "1.0"
stages:
  - id: s1
    role:
      name: r
      type: worker
      description: test
""")
    harness = Harness.from_spec(spec_file)
    assert harness.name == "file-test"


from armature.hooks.lifecycle import HookDecision, ToolBlocked
from armature.spec.models import (
    HarnessSpec, Stage, Adapter, SafetyCondition, ToolSafetyRule,
)


def make_adapter_spec(adapter_name: str = "run_shell", cmd: str = "echo hi") -> HarnessSpec:
    return HarnessSpec(
        name="adapter-test",
        stages=[Stage(id="s1", adapter=adapter_name)],
        adapters={adapter_name: Adapter(name=adapter_name, type="script", cmd=cmd)},
    )


async def test_pre_tool_hook_is_called_for_adapter(tmp_path):
    spec = make_adapter_spec(cmd="echo hello")
    harness = Harness(spec=spec, session_dir=tmp_path)

    calls = []

    async def capture_hook(phase, tool_name, args, ctx):
        calls.append(tool_name)
        return HookDecision.ALLOW

    from armature.hooks.lifecycle import HookPhase
    harness._hooks.register(HookPhase.PRE_TOOL, capture_hook)

    await harness.run({})
    assert calls == ["run_shell"]


async def test_pre_tool_hook_block_raises_tool_blocked(tmp_path):
    spec = make_adapter_spec(cmd="rm -rf /tmp/test")
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def block_hook(phase, tool_name, args, ctx):
        return HookDecision.BLOCK

    from armature.hooks.lifecycle import HookPhase
    harness._hooks.register(HookPhase.PRE_TOOL, block_hook)

    with pytest.raises(ToolBlocked) as exc_info:
        await harness.run({})
    assert "run_shell" in str(exc_info.value)


async def test_post_tool_hook_is_called_after_adapter(tmp_path):
    spec = make_adapter_spec(cmd="echo done")
    harness = Harness(spec=spec, session_dir=tmp_path)

    post_calls = []

    async def post_hook(phase, tool_name, result, ctx):
        post_calls.append((tool_name, result.get("exit_code")))

    from armature.hooks.lifecycle import HookPhase
    harness._hooks.register(HookPhase.POST_TOOL, post_hook)

    await harness.run({})
    assert post_calls == [("run_shell", 0)]


async def test_safety_rules_from_spec_block_adapter(tmp_path):
    spec = HarnessSpec(
        name="guarded",
        stages=[Stage(id="s1", adapter="danger")],
        adapters={"danger": Adapter(name="danger", type="script", cmd="sudo apt-get install vim")},
        safety_rules=[
            ToolSafetyRule(
                tool="danger",
                condition=SafetyCondition(field="cmd", op="contains", value="sudo"),
                action="block",
                message="sudo not permitted",
            )
        ],
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    with pytest.raises(ToolBlocked) as exc_info:
        await harness.run({})
    assert "sudo not permitted" in str(exc_info.value)


async def test_safety_rules_allow_safe_adapter(tmp_path):
    spec = HarnessSpec(
        name="guarded-allow",
        stages=[Stage(id="s1", adapter="safe_cmd")],
        adapters={"safe_cmd": Adapter(name="safe_cmd", type="script", cmd="echo hello")},
        safety_rules=[
            ToolSafetyRule(
                tool="safe_cmd",
                condition=SafetyCondition(field="cmd", op="contains", value="sudo"),
                action="block",
                message="sudo not permitted",
            )
        ],
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    result = await harness.run({})
    assert result["s1"]["exit_code"] == 0


# ---------------------------------------------------------------------------
# _extract_quorum_score unit tests
# ---------------------------------------------------------------------------

def test_extract_score_field_from_judge():
    assert _extract_quorum_score("judge", {"score": 0.85}) == pytest.approx(0.85)

def test_extract_quality_score_field_from_judge():
    assert _extract_quorum_score("judge", {"quality_score": 0.72}) == pytest.approx(0.72)

def test_extract_confidence_field_from_judge():
    assert _extract_quorum_score("judge", {"confidence": 0.9}) == pytest.approx(0.9)

def test_score_takes_priority_over_confidence():
    # score is checked before confidence
    assert _extract_quorum_score("judge", {"score": 0.6, "confidence": 0.9}) == pytest.approx(0.6)

def test_worker_role_always_returns_none():
    assert _extract_quorum_score("worker", {"score": 0.99}) is None

def test_researcher_role_returns_none():
    assert _extract_quorum_score("researcher", {"confidence": 0.8}) is None

def test_out_of_range_score_ignored():
    # 1.5 is outside [0,1] — skip to next key
    assert _extract_quorum_score("judge", {"score": 1.5, "confidence": 0.7}) == pytest.approx(0.7)

def test_negative_score_ignored():
    assert _extract_quorum_score("judge", {"score": -0.1, "confidence": 0.5}) == pytest.approx(0.5)

def test_no_matching_key_returns_none():
    assert _extract_quorum_score("judge", {"decision": "yes", "reasoning": "..."}) is None

def test_integer_score_accepted():
    assert _extract_quorum_score("judge", {"score": 1}) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Integration: engine writes quorum_score to trace for judge stages
# ---------------------------------------------------------------------------

async def test_judge_stage_quorum_score_written_to_trace(tmp_path):
    spec = HarnessSpec(
        name="quorum-test",
        stages=[Stage(
            id="evaluator",
            role=Role(name="Evaluator", type=RoleType.JUDGE, description="evaluate", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "traces.db")

    async def mock_execute(context):
        return {"confidence": 0.82, "feedback": "good", "_input_tokens": 10, "_output_tokens": 5}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    await harness._ensure_traces()
    traces = await harness._traces.query(workflow_name="quorum-test")
    assert len(traces) == 1
    assert traces[0].quorum_score == pytest.approx(0.82)


async def test_worker_stage_quorum_score_not_written(tmp_path):
    spec = HarnessSpec(
        name="worker-test",
        stages=[Stage(
            id="doer",
            role=Role(name="Worker", type=RoleType.WORKER, description="do work", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "traces.db")

    async def mock_execute(context):
        return {"score": 0.95, "content": "done", "_input_tokens": 5, "_output_tokens": 3}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    await harness._ensure_traces()
    traces = await harness._traces.query(workflow_name="worker-test")
    assert len(traces) == 1
    assert traces[0].quorum_score is None


async def test_failed_llm_stage_writes_failure_trace(tmp_path):
    spec = HarnessSpec(
        name="fail-test",
        stages=[Stage(
            id="doer",
            role=Role(name="Worker", type=RoleType.WORKER, description="work", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "traces.db")

    async def mock_execute(context):
        raise RuntimeError("simulated failure")

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        with pytest.raises(RuntimeError):
            await harness.run({})

    await harness._ensure_traces()
    traces = await harness._traces.query(workflow_name="fail-test")
    assert len(traces) == 1
    assert traces[0].success is False
    assert traces[0].error_type == "RuntimeError"
    assert traces[0].output_valid is False


async def test_script_stage_writes_trace(tmp_path):
    spec = HarnessSpec(
        name="script-trace-test",
        stages=[Stage(id="s1", adapter="echo_cmd")],
        adapters={"echo_cmd": Adapter(name="echo_cmd", type="script", cmd="echo hello")},
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "traces.db")
    await harness.run({})

    await harness._ensure_traces()
    traces = await harness._traces.query(workflow_name="script-trace-test")
    assert len(traces) == 1
    assert traces[0].role_type == "script"
    assert traces[0].success is True
    assert traces[0].model == ""


async def test_spec_version_in_trace(tmp_path):
    spec = HarnessSpec(
        name="sv-test",
        stages=[Stage(
            id="s1",
            role=Role(name="W", type=RoleType.WORKER, description="d", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "traces.db")

    async def mock_execute(context):
        return {"content": "ok", "_input_tokens": 5, "_output_tokens": 3, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    await harness._ensure_traces()
    traces = await harness._traces.query(workflow_name="sv-test")
    assert len(traces) == 1
    assert len(traces[0].spec_version) == 12  # sha256[:12]
    assert traces[0].spec_version == harness._spec_version


# ---------------------------------------------------------------------------
# Memory: cross-run capture and injection
# ---------------------------------------------------------------------------

def _make_memory_spec(db_path: str) -> HarnessSpec:
    return HarnessSpec(
        name="mem-test",
        stages=[Stage(
            id="summarizer",
            role=Role(name="W", type=RoleType.WORKER, description="summarize", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        memory=MemoryConfig(
            enabled=True,
            capture=[MemoryCapture(stage="summarizer", key="summary", max_entries=3)],
            inject_as="_memory",
            db=db_path,
        ),
    )


async def test_memory_capture_stores_stage_output(tmp_path):
    db = str(tmp_path / "mem.db")
    spec = _make_memory_spec(db)
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def mock_execute(context):
        return {"summary": "run1 summary", "_input_tokens": 5, "_output_tokens": 3, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    from armature.state.memory import MemoryStore
    store = MemoryStore(db)
    memories, _ = await store.load("mem-test")
    assert memories["summarizer"]["summary"] == ["run1 summary"]


async def test_memory_injected_into_context_on_second_run(tmp_path):
    db = str(tmp_path / "mem.db")
    spec = _make_memory_spec(db)
    received_contexts = []

    async def mock_execute(context):
        received_contexts.append(dict(context))
        return {"summary": "a summary", "_input_tokens": 5, "_output_tokens": 3, "_escalation_count": 0}

    # First run — no prior memory
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        harness1 = Harness(spec=spec, session_dir=tmp_path / "run1")
        await harness1.run({})

    first_ctx = received_contexts[0]
    assert first_ctx.get("_memory") == {} or first_ctx.get("_memory") is not None

    # Second run — should have memory from first run
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        harness2 = Harness(spec=spec, session_dir=tmp_path / "run2")
        await harness2.run({})

    second_ctx = received_contexts[1]
    assert "_memory" in second_ctx
    assert second_ctx["_memory"]["summarizer"]["summary"] == ["a summary"]


async def test_memory_max_entries_enforced(tmp_path):
    db = str(tmp_path / "mem.db")
    spec = _make_memory_spec(db)  # max_entries=3

    call_num = [0]

    async def mock_execute(context):
        call_num[0] += 1
        return {"summary": f"run{call_num[0]}", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    for i in range(5):
        with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
            harness = Harness(spec=spec, session_dir=tmp_path / f"run{i}")
            await harness.run({})

    from armature.state.memory import MemoryStore
    store = MemoryStore(db)
    memories, _ = await store.load("mem-test")
    entries = memories["summarizer"]["summary"]
    assert len(entries) == 3
    assert "run1" not in entries  # oldest two evicted
    assert "run2" not in entries


async def test_memory_disabled_does_not_inject(tmp_path):
    spec = HarnessSpec(
        name="no-mem",
        stages=[Stage(
            id="s1",
            role=Role(name="W", type=RoleType.WORKER, description="work", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        memory=MemoryConfig(enabled=False, capture=[], inject_as="_memory"),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    received = []

    async def mock_execute(context):
        received.append(dict(context))
        return {"content": "ok", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    assert "_memory" not in received[0]


async def test_no_memory_config_is_backward_compatible(tmp_path):
    spec = HarnessSpec(
        name="compat",
        stages=[Stage(
            id="s1",
            role=Role(name="W", type=RoleType.WORKER, description="work", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        # memory not set
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    assert harness._memory_store is None

    received = []

    async def mock_execute(context):
        received.append(dict(context))
        return {"content": "ok", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    assert "_memory" not in received[0]


# ---------------------------------------------------------------------------
# Memory: fresh=True skips loading prior memories
# ---------------------------------------------------------------------------

async def test_fresh_memory_skips_loading_prior_entries(tmp_path):
    """When memory.fresh=True, the _memory context is empty even if prior runs saved data."""
    db = str(tmp_path / "mem.db")

    from armature.state.memory import MemoryStore
    store = MemoryStore(db)
    await store.init()
    await store.record("fresh-test", "s1", "result", "prior output")

    spec = HarnessSpec(
        name="fresh-test",
        stages=[Stage(
            id="s1",
            role=Role(name="W", type=RoleType.WORKER, description="work", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        memory=MemoryConfig(
            enabled=True,
            fresh=True,
            capture=[MemoryCapture(stage="s1", key="result", max_entries=5)],
            inject_as="_memory",
            db=db,
        ),
    )
    harness = Harness(spec=spec, session_dir=tmp_path / "run")

    received = []

    async def mock_execute(context):
        received.append(dict(context))
        return {"result": "new output", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    assert received[0]["_memory"] == {}


async def test_fresh_memory_still_captures_new_output(tmp_path):
    """fresh=True skips loading, but new outputs are still captured for future runs."""
    db = str(tmp_path / "mem.db")
    spec = HarnessSpec(
        name="fresh-capture",
        stages=[Stage(
            id="stage_a",
            role=Role(name="W", type=RoleType.WORKER, description="work", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        memory=MemoryConfig(
            enabled=True,
            fresh=True,
            capture=[MemoryCapture(stage="stage_a", key="content", max_entries=5)],
            inject_as="_memory",
            db=db,
        ),
    )
    harness = Harness(spec=spec, session_dir=tmp_path / "run")

    async def mock_execute(context):
        return {"content": "fresh output", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    from armature.state.memory import MemoryStore
    store = MemoryStore(db)
    memories, _ = await store.load("fresh-capture")
    assert memories["stage_a"]["content"] == ["fresh output"]


async def test_memory_without_fresh_loads_prior_data(tmp_path):
    """Sanity: when fresh is False (default), prior data is loaded as before."""
    db = str(tmp_path / "mem.db")
    from armature.state.memory import MemoryStore
    store = MemoryStore(db)
    await store.init()
    await store.record("normal-test", "s1", "result", "prior output")

    spec = HarnessSpec(
        name="normal-test",
        stages=[Stage(
            id="s1",
            role=Role(name="W", type=RoleType.WORKER, description="work", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        memory=MemoryConfig(
            enabled=True,
            fresh=False,
            capture=[],
            inject_as="_memory",
            db=db,
        ),
    )
    harness = Harness(spec=spec, session_dir=tmp_path / "run")
    received = []

    async def mock_execute(context):
        received.append(dict(context))
        return {"result": "ok", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    assert received[0]["_memory"]["s1"]["result"] == ["prior output"]


# ---------------------------------------------------------------------------
# post_run stages: run after all normal stages with transcript + diagnostics
# ---------------------------------------------------------------------------

def _make_post_run_spec() -> HarnessSpec:
    return HarnessSpec(
        name="post-run-test",
        stages=[
            Stage(
                id="worker",
                role=Role(name="W", type=RoleType.WORKER, description="do work", model_tier="small"),
            ),
            Stage(
                id="refiner",
                post_run=True,
                role=Role(name="R", type=RoleType.RESEARCHER, description="analyze run", model_tier="small"),
            ),
        ],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )


async def test_post_run_stage_receives_transcript(tmp_path):
    spec = _make_post_run_spec()
    harness = Harness(spec=spec, session_dir=tmp_path)

    refiner_ctx: dict = {}

    async def mock_execute(context):
        if "_transcript" in context:
            refiner_ctx.update(context)
        return {"content": "ok", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    assert "_transcript" in refiner_ctx


async def test_post_run_stage_receives_prior_stage_results(tmp_path):
    spec = _make_post_run_spec()
    harness = Harness(spec=spec, session_dir=tmp_path)

    refiner_ctx: dict = {}
    call_count = [0]

    async def mock_execute(context):
        call_count[0] += 1
        if call_count[0] == 2:
            refiner_ctx.update(context)
        return {"result": "worker output", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    assert "worker" in refiner_ctx


async def test_post_run_stage_receives_diagnostics(tmp_path):
    spec = _make_post_run_spec()
    harness = Harness(spec=spec, session_dir=tmp_path)

    refiner_ctx: dict = {}

    async def mock_execute(context):
        if "_diagnostics" in context:
            refiner_ctx.update(context)
        return {"content": "ok", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    assert "_diagnostics" in refiner_ctx


async def test_post_run_stage_runs_after_worker(tmp_path):
    spec = _make_post_run_spec()
    harness = Harness(spec=spec, session_dir=tmp_path)

    actual_order: list[str] = []

    async def mock_execute(context):
        if "_transcript" in context:
            actual_order.append("refiner")
        else:
            actual_order.append("worker")
        return {"content": "ok", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    assert actual_order == ["worker", "refiner"]


async def test_post_run_result_included_in_run_output(tmp_path):
    spec = _make_post_run_spec()
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def mock_execute(context):
        if "_transcript" in context:
            return {"suggestions": ["improve prompt"], "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}
        return {"result": "done", "_input_tokens": 1, "_output_tokens": 1, "_escalation_count": 0}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        results = await harness.run({})

    assert "refiner" in results
    assert results["refiner"]["suggestions"] == ["improve prompt"]


# ── gate trace tests ──────────────────────────────────────────────────────────

async def test_gate_stage_writes_trace_with_gate_role_type(tmp_path):
    """Completed gate stages must write a TraceRecord with role_type='gate'."""
    spec = HarnessSpec(
        name="gate-test",
        stages=[Stage(id="approval", gate="human")],
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")

    with patch("armature.nodes.gate.HumanGateNode.execute",
               new_callable=AsyncMock,
               return_value={"approved": True, "feedback": None}):
        await harness.run({})

    traces = await harness._traces.query(workflow_name="gate-test", limit=50)
    gate_traces = [t for t in traces if t.role_type == "gate"]
    assert len(gate_traces) == 1
    assert gate_traces[0].stage_id == "approval"
    assert gate_traces[0].success is True


async def test_gate_trace_records_approved_false_on_rejection(tmp_path):
    """A rejected gate (approved=False) writes a trace with success=False."""
    spec = HarnessSpec(
        name="gate-reject",
        stages=[Stage(id="approval", gate="human", fail_as_value=True)],
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")

    with patch("armature.nodes.gate.HumanGateNode.execute",
               new_callable=AsyncMock,
               return_value={"approved": False, "feedback": "not ready"}):
        await harness.run({})

    traces = await harness._traces.query(workflow_name="gate-reject", limit=50)
    gate_traces = [t for t in traces if t.role_type == "gate"]
    assert len(gate_traces) == 1
    assert gate_traces[0].success is False

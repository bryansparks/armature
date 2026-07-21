from armature.spec.models import (
    HarnessSpec, Stage, Role, Contract, Failure, Adapter,
    ModelTier, ModelTierConfig, RoleType, OutputMode,
    SafetyCondition, ToolSafetyRule, LoopConfig, OnFailConfig,
    ModelTiers, RoleTypeDefaults, FileState, TraceConfig,
    MemoryCapture, MemoryConfig, ToolModule, ToolCallConfig,
    Signature, CompiledAgent,
)
import pytest

def test_role_type_enum():
    assert RoleType.WORKER == "worker"
    assert RoleType.ORCHESTRATOR == "orchestrator"
    assert RoleType.JUDGE == "judge"
    assert RoleType.RESEARCHER == "researcher"

def test_minimal_spec():
    spec = HarnessSpec(
        name="test-workflow",
        version="1.0",
        stages=[
            Stage(
                id="step1",
                role=Role(name="r1", type=RoleType.WORKER, description="Do work"),
            )
        ],
    )
    assert spec.name == "test-workflow"
    assert spec.stages[0].id == "step1"

def test_stage_depends_on():
    spec = HarnessSpec(
        name="chained",
        version="1.0",
        stages=[
            Stage(id="a", role=Role(name="r", type=RoleType.WORKER, description="a")),
            Stage(id="b", depends_on=["a"], role=Role(name="r", type=RoleType.WORKER, description="b")),
        ],
    )
    assert spec.stages[1].depends_on == ["a"]

def test_contract_defaults():
    c = Contract()
    assert c.max_iterations == 20
    assert c.max_llm_calls == 100
    assert c.timeout_hours == 8.0


def test_safety_condition_defaults():
    cond = SafetyCondition(field="cmd", op="contains", value="rm -rf")
    assert cond.field == "cmd"
    assert cond.op == "contains"
    assert cond.value == "rm -rf"


def test_tool_safety_rule_defaults():
    rule = ToolSafetyRule(
        tool="run_shell",
        condition=SafetyCondition(field="cmd", op="contains", value="rm -rf"),
        action="block",
    )
    assert rule.action == "block"
    assert rule.message == ""
    assert rule.tool == "run_shell"


def test_tool_safety_rule_wildcard():
    rule = ToolSafetyRule(
        tool="*",
        condition=SafetyCondition(field="cmd", op="truthy", value=""),
        action="log",
        message="auditing all tool calls",
    )
    assert rule.tool == "*"


def test_harness_spec_safety_rules_default_empty():
    spec = HarnessSpec(
        name="safe-flow",
        stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="d"))],
    )
    assert spec.safety_rules == []


def test_harness_spec_accepts_safety_rules():
    spec = HarnessSpec(
        name="guarded-flow",
        stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="d"))],
        safety_rules=[
            ToolSafetyRule(
                tool="*",
                condition=SafetyCondition(field="cmd", op="contains", value="sudo"),
                action="block",
                message="no sudo",
            )
        ],
    )
    assert len(spec.safety_rules) == 1
    assert spec.safety_rules[0].action == "block"


def test_stage_fan_out_defaults():
    stage = Stage(id="s1", subagent_spec="child.yaml")
    assert stage.fan_out is None
    assert stage.fan_in == "list"
    assert stage.partition_key is None


def test_stage_fan_out_explicit():
    stage = Stage(
        id="s1",
        subagent_spec="child.yaml",
        fan_out=4,
        fan_in="merge",
        partition_key="documents",
    )
    assert stage.fan_out == 4
    assert stage.fan_in == "merge"
    assert stage.partition_key == "documents"


def test_stage_fan_in_first():
    stage = Stage(id="s1", subagent_spec="child.yaml", fan_out=3, fan_in="first")
    assert stage.fan_in == "first"


def test_stage_fan_out_none_means_single():
    stage = Stage(id="s1", subagent_spec="child.yaml", fan_out=None)
    assert stage.fan_out is None


# ── LoopConfig ────────────────────────────────────────────────────────────────

def test_loop_config_defaults():
    lc = LoopConfig(stage="my_stage")
    assert lc.context == "retry"
    assert lc.max == 3
    assert lc.until is None
    assert lc.backoff_s is None
    assert lc.backoff_max_s == 60.0


def test_loop_config_with_backoff():
    lc = LoopConfig(stage="s", backoff_s=2.0, backoff_max_s=30.0, max=5)
    assert lc.backoff_s == 2.0
    assert lc.backoff_max_s == 30.0
    assert lc.max == 5


def test_on_fail_config_with_loop():
    lc = LoopConfig(stage="retry_stage", max=2)
    ofc = OnFailConfig(loop=lc)
    assert ofc.loop is not None
    assert ofc.loop.stage == "retry_stage"


def test_on_fail_config_empty():
    ofc = OnFailConfig()
    assert ofc.loop is None


# ── Failure ───────────────────────────────────────────────────────────────────

def test_failure_defaults():
    f = Failure(condition="exit_code != 0", recovery="retry")
    assert f.max_retries == 3
    assert f.condition == "exit_code != 0"
    assert f.recovery == "retry"


# ── ModelTiers / RoleTypeDefaults ─────────────────────────────────────────────

def test_model_tiers_all_none_by_default():
    mt = ModelTiers()
    assert mt.tiny is None
    assert mt.small is None
    assert mt.frontier is None


def test_model_tiers_partial():
    mt = ModelTiers(
        small=ModelTierConfig(provider="openai", model="gpt-4o-mini"),
        frontier=ModelTierConfig(provider="anthropic", model="claude-opus-4-7"),
    )
    assert mt.medium is None
    assert mt.small.model == "gpt-4o-mini"
    assert mt.frontier.model == "claude-opus-4-7"


def test_role_type_defaults_custom():
    rtd = RoleTypeDefaults(worker="tiny", orchestrator="large")
    assert rtd.worker == "tiny"
    assert rtd.orchestrator == "large"
    assert rtd.judge == "frontier"  # unchanged default


def test_role_type_defaults_default_values():
    rtd = RoleTypeDefaults()
    assert rtd.worker == "small"
    assert rtd.orchestrator == "frontier"
    assert rtd.judge == "frontier"
    assert rtd.researcher == "large"


# ── FileState / TraceConfig ───────────────────────────────────────────────────

def test_file_state_defaults():
    fs = FileState()
    assert fs.enabled is False
    assert "{{run_id}}" in fs.base
    assert fs.workspace == "workspace/"


def test_trace_config_defaults():
    tc = TraceConfig()
    assert tc.enabled is True
    assert tc.metrics == []
    assert "{{run_id}}" in tc.filesystem


# ── MemoryCapture / MemoryConfig ─────────────────────────────────────────────

def test_memory_capture_defaults():
    mc = MemoryCapture(stage="summarize", key="summary")
    assert mc.max_entries == 5
    assert mc.stage == "summarize"
    assert mc.key == "summary"


def test_memory_config_defaults():
    mc = MemoryConfig()
    assert mc.enabled is True
    assert mc.capture == []
    assert mc.inject_as == "_memory"
    assert mc.db is None


def test_memory_config_defaults_preserve_existing_behavior():
    """New fields default so existing specs are unchanged."""
    cfg = MemoryConfig()
    assert cfg.reconcile is True
    assert cfg.reconcile_llm is False


def test_memory_config_round_trips_reconcile_fields():
    import yaml
    raw = """
enabled: true
extract_knowledge: true
reconcile: true
reconcile_llm: false
capture:
  - {stage: researcher, key: brief, max_entries: 5}
"""
    cfg = yaml.safe_load(raw)
    parsed = MemoryConfig(**cfg)
    assert parsed.reconcile is True
    assert parsed.reconcile_llm is False
    assert parsed.capture[0].stage == "researcher"


def test_harness_spec_memory_none_by_default():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"))],
    )
    assert spec.memory is None


def test_harness_spec_memory_configured():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"))],
        memory=MemoryConfig(
            enabled=True,
            capture=[MemoryCapture(stage="s", key="result", max_entries=3)],
            inject_as="_ctx_memory",
        ),
    )
    assert spec.memory is not None
    assert spec.memory.inject_as == "_ctx_memory"
    assert spec.memory.capture[0].max_entries == 3


# ── Stage extras ──────────────────────────────────────────────────────────────

def test_stage_partition_source_and_inject():
    stage = Stage(
        id="process",
        subagent_spec="child.yaml",
        partition_source="{{ documents }}",
        inject_file_as="doc_content",
    )
    assert stage.partition_source == "{{ documents }}"
    assert stage.inject_file_as == "doc_content"


def test_stage_output_schema():
    stage = Stage(
        id="judge",
        role=Role(name="r", type=RoleType.JUDGE, description="d"),
        output_schema={"type": "object", "properties": {"score": {"type": "number"}}},
        output_mode=OutputMode.GUIDED_JSON,
    )
    assert stage.output_mode == OutputMode.GUIDED_JSON
    assert "score" in stage.output_schema["properties"]


def test_stage_fail_as_value_default():
    stage = Stage(id="s", tool_call=ToolCallConfig(name="t"))
    assert stage.fail_as_value is False


def test_stage_timeout_default_none():
    stage = Stage(id="s", tool_call=ToolCallConfig(name="t"))
    assert stage.timeout_s is None


# ── ToolModule / ToolCallConfig ───────────────────────────────────────────────

def test_tool_module():
    tm = ToolModule(module="mypackage.tools")
    assert tm.module == "mypackage.tools"


def test_tool_call_config_defaults():
    tc = ToolCallConfig(name="file_read")
    assert tc.args == {}
    assert tc.name == "file_read"


def test_tool_call_config_with_args():
    tc = ToolCallConfig(name="shell", args={"cmd": "echo hi"})
    assert tc.args["cmd"] == "echo hi"


# ── Signature ─────────────────────────────────────────────────────────────────

def test_signature_defaults():
    sig = Signature()
    assert sig.input == {}
    assert sig.output == {}


def test_signature_with_fields():
    sig = Signature(
        input={"query": "str", "limit": "int"},
        output={"result": "str"},
    )
    assert "query" in sig.input
    assert "result" in sig.output


# ── ModelTierConfig extras ────────────────────────────────────────────────────

def test_model_tier_config_full():
    mtc = ModelTierConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_base="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        temperature=0.7,
        max_tokens=4096,
        tool_calling=True,
    )
    assert mtc.temperature == 0.7
    assert mtc.max_tokens == 4096
    assert mtc.tool_calling is True
    assert mtc.api_key_env == "ANTHROPIC_API_KEY"


# ── SelfImprovementConfig ─────────────────────────────────────────────────────

from armature.spec.models import SelfImprovementConfig, EditableSurface


def test_self_improvement_config_defaults():
    cfg = SelfImprovementConfig()
    assert EditableSurface.DESCRIPTIONS in cfg.editable_surfaces
    assert EditableSurface.RETRY_COUNTS in cfg.editable_surfaces
    assert EditableSurface.TIMEOUTS in cfg.editable_surfaces
    assert EditableSurface.SCHEMAS not in cfg.editable_surfaces
    assert EditableSurface.MODEL_TIERS not in cfg.editable_surfaces


def test_self_improvement_config_explicit():
    cfg = SelfImprovementConfig(editable_surfaces=[EditableSurface.SCHEMAS, EditableSurface.MODEL_TIERS])
    assert cfg.editable_surfaces == [EditableSurface.SCHEMAS, EditableSurface.MODEL_TIERS]


def test_self_improvement_config_trigger_fields_default_none():
    cfg = SelfImprovementConfig()
    assert cfg.target_hqs is None
    assert cfg.min_traces is None


def test_self_improvement_config_accepts_trigger_overrides():
    cfg = SelfImprovementConfig(target_hqs=0.95, min_traces=10)
    assert cfg.target_hqs == 0.95
    assert cfg.min_traces == 10


def test_self_improvement_config_drift_threshold_default_none():
    cfg = SelfImprovementConfig()
    assert cfg.drift_threshold is None


def test_self_improvement_config_accepts_drift_threshold():
    cfg = SelfImprovementConfig(drift_threshold=0.5)
    assert cfg.drift_threshold == 0.5


def test_harness_spec_has_self_improvement_field():
    from armature.spec.models import HarnessSpec
    spec = HarnessSpec(name="wf", stages=[])
    assert hasattr(spec, "self_improvement")
    assert isinstance(spec.self_improvement, SelfImprovementConfig)


def test_compiled_agent_carries_safety_rules():
    agent = CompiledAgent(
        role=Role(name="X", type=RoleType.WORKER, description="d"),
        safety_rules=[ToolSafetyRule(tool="merge_pr", condition=None, action="block")],
    )
    assert len(agent.safety_rules) == 1
    assert agent.safety_rules[0].tool == "merge_pr"
    # round-trips through pydantic
    dumped = agent.model_dump()
    again = CompiledAgent.model_validate(dumped)
    assert again.safety_rules[0].tool == "merge_pr"


def test_compiled_agent_safety_rules_default_empty():
    agent = CompiledAgent(role=Role(name="X", type=RoleType.WORKER, description="d"))
    assert agent.safety_rules == []


def test_memory_config_navigation_tools_default_false():
    from armature.spec.models import MemoryConfig
    cfg = MemoryConfig()
    assert cfg.navigation_tools is False


def test_memory_config_navigation_tools_round_trip():
    from armature.spec.models import MemoryConfig
    cfg = MemoryConfig(navigation_tools=True)
    dumped = cfg.model_dump()
    assert dumped["navigation_tools"] is True
    restored = MemoryConfig(**dumped)
    assert restored.navigation_tools is True


def test_memory_config_navigation_defaults():
    from armature.spec.models import MemoryConfig
    cfg = MemoryConfig()
    assert cfg.navigation_tools is False
    assert cfg.extract_knowledge is False
    assert cfg.reconcile is True


def test_memory_config_navigation_round_trip():
    from armature.spec.models import MemoryConfig
    cfg = MemoryConfig(navigation_tools=True, extract_knowledge=True, reconcile=False)
    dumped = cfg.model_dump()
    restored = MemoryConfig(**dumped)
    assert restored.navigation_tools is True
    assert restored.extract_knowledge is True
    assert restored.reconcile is False

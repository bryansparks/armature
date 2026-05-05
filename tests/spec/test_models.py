from armature.spec.models import (
    HarnessSpec, Stage, Role, Contract, Failure, Adapter,
    ModelTier, ModelTierConfig, RoleType, OutputMode,
    SafetyCondition, ToolSafetyRule,
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

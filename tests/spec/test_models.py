from armature.spec.models import (
    HarnessSpec, Stage, Role, Contract, Failure, Adapter,
    ModelTier, ModelTierConfig, RoleType, OutputMode
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

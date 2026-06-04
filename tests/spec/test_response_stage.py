"""Schema tests for Stage.response_stage field."""
from armature.spec.models import Stage, Role, RoleType


def _minimal_stage(**kwargs) -> dict:
    return {
        "id": "s1",
        "role": {"name": "worker", "type": "worker", "description": "do it"},
        **kwargs,
    }


def test_stage_accepts_response_stage_field():
    stage = Stage(**_minimal_stage(response_stage=True))
    assert stage.response_stage is True


def test_stage_response_stage_defaults_false():
    stage = Stage(**_minimal_stage())
    assert stage.response_stage is False

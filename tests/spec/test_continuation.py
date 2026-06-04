from armature.spec.models import HarnessSpec, Stage, Role, RoleType, ContinuationKey, ContinuationConfig
import pytest


def _minimal_spec(**kwargs):
    return HarnessSpec(
        name="wf",
        version="1.0",
        stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="do"))],
        **kwargs,
    )


def test_continuation_config_defaults():
    cfg = ContinuationConfig()
    assert cfg.carry_forward == []
    assert cfg.inject_as == "prior_run"


def test_continuation_key_dotted_notation():
    k = ContinuationKey(key="monitor.summary")
    assert k.key == "monitor.summary"


def test_harness_spec_accepts_continuation_block():
    spec = _minimal_spec(
        continuation=ContinuationConfig(
            carry_forward=[ContinuationKey(key="s1.output")],
            inject_as="prior_run",
        )
    )
    assert spec.continuation is not None
    assert spec.continuation.carry_forward[0].key == "s1.output"


def test_harness_spec_continuation_defaults_none():
    spec = _minimal_spec()
    assert spec.continuation is None

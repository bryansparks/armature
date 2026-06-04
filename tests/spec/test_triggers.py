import pytest
from pydantic import ValidationError
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType,
    CronTrigger, WebhookTrigger,
)


def _minimal_spec(**kwargs):
    return HarnessSpec(
        name="wf",
        version="1.0",
        stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="do"))],
        **kwargs,
    )


def test_cron_trigger_parsed():
    t = CronTrigger(schedule="0 9 * * *")
    assert t.type == "cron"
    assert t.schedule == "0 9 * * *"


def test_webhook_trigger_parsed():
    t = WebhookTrigger(path="/webhook/my-workflow")
    assert t.type == "webhook"
    assert t.path == "/webhook/my-workflow"


def test_unknown_trigger_type_raises_validation_error():
    with pytest.raises(ValidationError):
        _minimal_spec(triggers=[{"type": "email", "address": "foo@bar.com"}])


def test_harness_spec_accepts_triggers_list():
    spec = _minimal_spec(triggers=[
        {"type": "cron", "schedule": "0 9 * * *"},
        {"type": "webhook", "path": "/webhook/wf"},
    ])
    assert len(spec.triggers) == 2
    assert spec.triggers[0].type == "cron"
    assert spec.triggers[1].type == "webhook"


def test_triggers_defaults_to_empty_list():
    spec = _minimal_spec()
    assert spec.triggers == []

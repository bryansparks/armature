"""Submit gates (the local executor's order) + launch bookkeeping.

Restructured from armature-dispatch tests/unit/test_submit_work.py @ c5fe34f:
the gate refusals and start idempotence are engine policy; the ECS launch
assertions stayed in the deployment repo."""
import pytest

pytest.importorskip("moto")  # transport suite: requires the cloud-dev extra —
                                  # boto3 is NOT a valid marker (litellm pulls it
                                  # transitively); moto is the [cloud-dev] signal

from pathlib import Path  # noqa: E402

from armature.spec.mission import load_mission  # noqa: E402
from armature.state.work import IN_PROGRESS, RETRY_PENDING  # noqa: E402
from armature.transport.s3work import S3WorkStore  # noqa: E402
from armature.transport.workops import (WorkSubmitError, gate_work_unit,  # noqa: E402
                                         start_work_unit)

from .conftest import BUCKET  # noqa: E402

MISSION_DOC = Path(__file__).resolve().parent / "fixtures" / "pretzel.mission.yml"


def _gate(s3, unit_id="research", **kw):
    return gate_work_unit(MISSION_DOC, unit_id, store=S3WorkStore(s3, BUCKET),
                          **kw)


def test_gate_happy_path_returns_mission_record_inputs(s3_bucket):
    s3, _ = s3_bucket
    mission, record, run_inputs = _gate(s3)
    assert mission.name == "pretzel"
    assert record.state.value == "pending" and record.attempts == 0
    assert isinstance(run_inputs, dict)


def test_gate_refuses_bad_names(s3_bucket):
    s3, _ = s3_bucket
    with pytest.raises(WorkSubmitError):
        _gate(s3, unit_id="../evil")


def test_gate_requires_unmet(s3_bucket):
    s3, _ = s3_bucket
    with pytest.raises(WorkSubmitError, match="hero-copy"):
        _gate(s3, unit_id="approve")


def test_gate_unknown_unit(s3_bucket):
    s3, _ = s3_bucket
    with pytest.raises(WorkSubmitError, match="nope"):
        _gate(s3, unit_id="nope")


def test_gate_state_gate_and_attempts_and_budgets(s3_bucket):
    # mirrors test_submit_work_state_gate + test_submit_work_attempts_and_budget_gates,
    # driving state via the store instead of a second submit
    s3, _ = s3_bucket
    store = S3WorkStore(s3, BUCKET)
    mission = load_mission(MISSION_DOC)
    unit = next(u for u in mission.work if u.id == "research")
    rec = store.ensure_unit(mission, unit)
    rec.state = IN_PROGRESS
    store.save(rec)
    with pytest.raises(WorkSubmitError, match="in_progress"):
        _gate(s3)
    rec.attempts = rec.max_attempts
    rec.state = RETRY_PENDING
    store.save(rec)
    with pytest.raises(WorkSubmitError, match="no attempts left"):
        _gate(s3)


def test_gate_unit_budget_spent(s3_bucket):
    # Review Important 3: the budget-before-attempt gates lost coverage in
    # the test move — pin them (they are this branch's own governance
    # argument). Unit budget exhausted: refused before any spend.
    s3, _ = s3_bucket
    store = S3WorkStore(s3, BUCKET)
    mission = load_mission(MISSION_DOC)
    unit = next(u for u in mission.work if u.id == "research")
    rec = store.ensure_unit(mission, unit)
    rec.spent_usd = rec.max_budget_usd        # 0.50 — the unit budget, gone
    store.save(rec)
    with pytest.raises(WorkSubmitError, match="unit budget spent"):
        _gate(s3)


def test_gate_mission_budget_spent(s3_bucket):
    # Review Important 3, mission side: another unit's spend exhausts the
    # mission budget while this unit's own budget is untouched — still
    # refused.
    s3, _ = s3_bucket
    store = S3WorkStore(s3, BUCKET)
    mission = load_mission(MISSION_DOC)
    hero = next(u for u in mission.work if u.id == "hero-copy")
    hrec = store.ensure_unit(mission, hero)
    hrec.spent_usd = mission.budget_usd       # 1.00 — the mission budget, gone
    store.save(hrec)
    with pytest.raises(WorkSubmitError, match="mission budget spent"):
        _gate(s3)                            # research: spent 0, still refused


def test_start_work_unit_same_job_idempotent(s3_bucket):
    # moved verbatim from test_submit_work.py
    s3, _ = s3_bucket
    store = S3WorkStore(s3, BUCKET)
    mission = load_mission(MISSION_DOC)
    unit = next(u for u in mission.work if u.id == "research")
    rec = store.ensure_unit(mission, unit)
    first = start_work_unit(store, mission="pretzel", unit_id="research", job_id="j-1")
    again = start_work_unit(store, mission="pretzel", unit_id="research", job_id="j-1")
    assert first.attempts == 1 and again.attempts == 1


def test_gate_refusal_leaves_live_doc_alone(s3_bucket):
    # Review Focus 4: a submit refused by a gate must not have overwritten
    # the live mission doc — the sweep trusts whatever doc sits in the bucket
    s3, _ = s3_bucket
    store = S3WorkStore(s3, BUCKET)
    _gate(s3)                                     # happy path rides the doc along
    edited = "name: pretzel\nwork: []\n"           # an edited live doc
    store.put_mission_doc("pretzel", edited)
    store.apply("pretzel", "research", IN_PROGRESS, job_id="j-1")
    with pytest.raises(WorkSubmitError, match="in_progress"):
        _gate(s3)                                 # state gate refuses
    live = s3.get_object(Bucket=BUCKET, Key="work/pretzel/mission.yml")["Body"].read()
    assert live.decode() == edited
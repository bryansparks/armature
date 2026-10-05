"""S3WorkStore — byte-compatible with LocalWorkStore's records, keyed by
design §6's work/<mission>/ layout with mission-scoped transitions.

Moved from armature-dispatch tests/unit/test_s3work.py @ c5fe34f (wholesale;
only the import target and the AWS imports changed — boto3/moto are guarded
so this file collects, and skips, without the cloud extra)."""
import pytest

pytest.importorskip("boto3")   # transport suite: requires the cloud extra

from armature.state.work import IN_PROGRESS, PENDING, WorkUnitState  # noqa: E402
from armature.spec.mission import MissionSpec, WorkUnit  # noqa: E402

from armature.transport.s3work import S3WorkStore  # noqa: E402

BUCKET = "dispatch-jobs-123456789012-us-east-1"
MISSION_DOC = """
name: pretzel
objective: prove the store end to end
posture: human-led
budget_usd: 1.0
work:
  - id: research
    title: Research the pretzel audience
    workflow: work-echo
    posture: delegated
  - id: approve
    title: Human approves
    workflow: work-echo
"""


@pytest.fixture
def aws():
    from moto import mock_aws
    import boto3
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        yield s3


@pytest.fixture
def doc():
    return MissionSpec.model_validate({
        "name": "pretzel", "objective": "prove the store",
        "posture": "human-led", "budget_usd": 1.0,
        "work": [
            {"id": "research", "title": "Research", "workflow": "work-echo",
             "posture": "delegated"},
            {"id": "approve", "title": "Approve", "workflow": "work-echo"},
        ],
    })


def test_record_round_trip(aws, doc):
    store = S3WorkStore(aws, BUCKET)
    rec = store.ensure_unit(doc, doc.work[0])
    assert rec.state == PENDING
    assert store.load("pretzel", "research").title == "Research"
    assert store.load("pretzel", "nope") is None


def test_apply_runs_legality_and_audits(aws, doc):
    store = S3WorkStore(aws, BUCKET)
    rec = store.ensure_unit(doc, doc.work[0])
    moved = store.apply("pretzel", "research", IN_PROGRESS,
                        reason="submitted", job_id="j1")
    assert moved.state == IN_PROGRESS and moved.last_job_id == "j1"
    trs = store.list_transitions("pretzel")
    assert [t.seq for t in trs] == [1, 2]           # seed then move
    assert trs[1].from_state == PENDING and trs[1].to_state == IN_PROGRESS
    # illegal move raises, writes nothing
    with pytest.raises(Exception):
        store.apply("pretzel", "research", PENDING)
    assert store.load("pretzel", "research").state == IN_PROGRESS
    # ghost unit: apply refuses, never creates
    with pytest.raises(KeyError):
        store.apply("pretzel", "ghost", IN_PROGRESS)


def test_list_units_excludes_transitions_and_doc(aws, doc):
    store = S3WorkStore(aws, BUCKET)
    store.ensure_unit(doc, doc.work[0])
    store.ensure_unit(doc, doc.work[1])
    units = store.list_units("pretzel")
    assert sorted(u.unit_id for u in units) == ["approve", "research"]


def test_seed_follow_on_is_idempotent(aws):
    from armature.state.closure import FollowOnUnit
    store = S3WorkStore(aws, BUCKET)
    unit = FollowOnUnit(id="deep-dive", title="Deep dive",
                       objective="more", workflow="work-echo")
    first = store.seed_follow_on("pretzel", unit, posture="delegated")
    second = store.seed_follow_on("pretzel", unit, posture="delegated")
    assert first == second
    assert len(store.list_transitions("pretzel")) == 1  # one seeding only
    assert store.load("pretzel", "deep-dive").posture == "delegated"


def test_mission_doc_round_trip_and_malformed(aws):
    store = S3WorkStore(aws, BUCKET)
    assert store.get_mission_doc("pretzel") is None
    store.put_mission_doc("pretzel", MISSION_DOC)
    doc = store.get_mission_doc("pretzel")
    assert doc.name == "pretzel" and doc.budget_usd == 1.0
    store.put_mission_doc("pretzel", "name: pretzel\nwork: not-a-list\n")
    with pytest.raises(ValueError):
        store.get_mission_doc("pretzel")


def test_cross_mission_isolation(aws, doc):
    store = S3WorkStore(aws, BUCKET)
    store.ensure_unit(doc, doc.work[0])
    other = doc.model_copy(deep=True)
    other.name = "second"
    store.ensure_unit(other, other.work[0])
    # same unit id, two missions: separate records and separate seq spaces
    assert store.list_units("pretzel")[0].mission == "pretzel"
    assert store.list_units("second")[0].mission == "second"
    assert len(store.list_transitions("pretzel")) == 1
    assert len(store.list_transitions("second")) == 1
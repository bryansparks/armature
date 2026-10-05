"""Runner-side work-unit plumbing: injection (step 4.7) and settle (7.6).

Moved from armature-dispatch tests/unit/test_runner_work.py @ c5fe34f, split
per plan ruling: the function-level tests of inject/settle moved here (they
exercise armature.transport.settle); the test_main_* end-to-end tests drive
dispatch_runner/__main__.main() — the runner pipeline, which is not moving —
and stay in the dispatch repo."""
import pytest

pytest.importorskip("boto3")   # transport suite: requires the cloud extra

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402
from pathlib import Path  # noqa: E402

from armature.spec.mission import load_mission  # noqa: E402
from armature.state.work import DONE, FAILED, IN_PROGRESS, PENDING, RETRY_PENDING  # noqa: E402

from armature.transport import s3io as s3ops  # noqa: E402
from armature.transport.s3work import S3WorkStore  # noqa: E402

BUCKET = "dispatch-jobs-123456789012-us-east-1"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MISSION_DOC = FIXTURES / "pretzel.mission.yml"


@pytest.fixture
def aws():
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        yield s3


def _pretzel():
    return load_mission(MISSION_DOC)


def _seed_research(store: S3WorkStore, *, attempts: int = 0):
    mission = _pretzel()
    unit = next(u for u in mission.work if u.id == "research")
    record = store.ensure_unit(mission, unit)
    if attempts:
        record.attempts = attempts
        store.save(record)
    return record


# ── runner_injection (pure) ──────────────────────────────────────────────────

def test_runner_injection_shape_and_opt_in(aws, work_echo_pkg):
    from armature.transport.settle import runner_injection
    from armature.packaging.manifest import PackageManifest
    from armature.spec.loader import load_spec
    from ruamel.yaml import YAML
    manifest = PackageManifest.model_validate(
        YAML(typ="safe").load((work_echo_pkg / "package.yaml").read_text()))
    spec = load_spec(work_echo_pkg / manifest.spec)
    store = S3WorkStore(aws, BUCKET)
    record = _seed_research(store)
    record.state = IN_PROGRESS
    wu = runner_injection(_pretzel(), record, spec)
    assert wu == {
        "mission": "pretzel",
        # the local executor (armature cli.py:620) injects the raw mission
        # objective — a YAML | block keeps its trailing newline
        "mission_objective": "Fixture mission — prove the flock transport end to end.\n",
        "unit_id": "research",
        "title": "Research the pretzel audience",
        "objective": "Find who buys giant pretzels.",
        "requires": [],
        "posture": "delegated",
        "state": "in_progress",
        "attempts": 0,
    }


def test_runner_injection_opt_out_and_no_record(aws, work_echo_pkg):
    from armature.transport.settle import runner_injection
    from armature.packaging.manifest import PackageManifest
    from armature.spec.loader import load_spec
    from ruamel.yaml import YAML
    manifest = PackageManifest.model_validate(
        YAML(typ="safe").load((work_echo_pkg / "package.yaml").read_text()))
    spec = load_spec(work_echo_pkg / manifest.spec)
    spec_no_opt = spec.model_copy(update={"mission_source": "static"})
    store = S3WorkStore(aws, BUCKET)
    record = _seed_research(store)
    assert runner_injection(_pretzel(), record, spec_no_opt) is None
    assert runner_injection(None, record, spec) is not None   # doc missing: empty objective
    assert runner_injection(_pretzel(), record, spec)["mission_objective"]
    assert runner_injection(None, record, spec)["mission_objective"] == ""


def test_load_work_record_missing_and_corrupt(aws):
    from armature.transport.settle import load_work_record
    assert load_work_record(aws, BUCKET, "pretzel", "research") is None
    aws.put_object(Bucket=BUCKET, Key="work/pretzel/research.json",
                   Body=b"{not json")
    assert load_work_record(aws, BUCKET, "pretzel", "research") is None  # loud, never raises


# ── settle_work_unit (unit level — closure semantics via constructed specs) ──

def _store_with_research(aws, *, attempts=1, spent=0.0):
    store = S3WorkStore(aws, BUCKET)
    record = _seed_research(store, attempts=attempts)
    record.spent_usd = spent
    store.save(record)
    store.apply("pretzel", "research", IN_PROGRESS, job_id="job-1")
    return store


def _closure_spec(stage="closer"):
    from armature.spec.models import ClosureConfig, HarnessSpec, Stage
    return HarnessSpec(name="w", stages=[Stage(id=stage)],
                        closure=ClosureConfig(stage=stage))


def test_settle_complete_with_closure_done(aws):
    from armature.transport.settle import settle_work_unit
    store = _store_with_research(aws)
    spec = _closure_spec()
    state = settle_work_unit(store, mission="pretzel", unit_id="research",
                             job_id="job-1", status="complete",
                             cost_usd=0.05,
                             results={"closer": {"reason": "done_no_follow_on",
                                                 "notes": "ok"}},
                             spec=spec)
    assert state == "done"
    rec = store.load("pretzel", "research")
    assert rec.spent_usd == pytest.approx(0.05)   # metered on success


def test_settle_complete_no_closure_done(aws):
    from armature.transport.settle import settle_work_unit
    store = _store_with_research(aws)
    state = settle_work_unit(store, mission="pretzel", unit_id="research",
                             job_id="job-1", status="complete",
                             cost_usd=0.0, results={}, spec=_closure_spec())
    assert state == "done"


def test_settle_handed_off_seeds_follow_on(aws):
    from armature.transport.settle import settle_work_unit
    store = _store_with_research(aws)
    state = settle_work_unit(store, mission="pretzel", unit_id="research",
                             job_id="job-1", status="complete", cost_usd=0.0,
                             results={"closer": {
                                 "reason": "handed_off", "notes": "spawn",
                                 "follow_on": [{"id": "deep-dive",
                                                "title": "Deep dive",
                                                "workflow": "work-echo"}]}},
                             spec=_closure_spec())
    assert state == "handed_off"
    seeded = store.load("pretzel", "deep-dive")
    assert seeded is not None and seeded.posture == "delegated"


def test_settle_failure_meters_and_retry_pending(aws):
    from armature.transport.settle import settle_work_unit
    store = _store_with_research(aws)
    state = settle_work_unit(store, mission="pretzel", unit_id="research",
                             job_id="job-1", status="failed",
                             cost_usd=0.02, results={}, spec=_closure_spec())
    assert state == "retry_pending"               # attempts 1 < max 2
    rec = store.load("pretzel", "research")
    assert rec.spent_usd == pytest.approx(0.02)   # metered on failure too


def test_settle_failure_exhausted_attempts_stays_failed(aws):
    from armature.transport.settle import settle_work_unit
    store = _store_with_research(aws, attempts=2)
    state = settle_work_unit(store, mission="pretzel", unit_id="research",
                             job_id="job-1", status="failed",
                             cost_usd=0.0, results={}, spec=_closure_spec())
    assert state == "failed"


def test_settle_malformed_closure_is_loud_failure(aws):
    from armature.transport.settle import settle_work_unit
    store = _store_with_research(aws)
    state = settle_work_unit(store, mission="pretzel", unit_id="research",
                             job_id="job-1", status="complete", cost_usd=0.0,
                             results={"closer": {"reason": "not-a-reason"}},
                             spec=_closure_spec())
    assert state == "retry_pending"


def test_settle_redelivery_never_double_meters(aws):
    # Review Focus 4: a job whose settle already landed is a no-op
    from armature.transport.settle import settle_work_unit
    store = _store_with_research(aws)
    kwargs = dict(mission="pretzel", unit_id="research", job_id="job-1",
                  status="complete", cost_usd=0.05,
                  results={"closer": {"reason": "done_no_follow_on"}},
                  spec=_closure_spec())
    settle_work_unit(store, **kwargs)
    settle_work_unit(store, **kwargs)             # the re-drive
    rec = store.load("pretzel", "research")
    assert rec.spent_usd == pytest.approx(0.05)   # metered exactly once
    assert rec.state == DONE


def test_fail_work_unit_is_the_orphan_verb(aws):
    from armature.transport.settle import fail_work_unit
    store = _store_with_research(aws, attempts=2)   # exhausted
    state = fail_work_unit(store, mission="pretzel", unit_id="research",
                           job_id="job-1", reason="orphaned",
                           cost_usd=0.0)
    assert state == "failed"
    assert store.load("pretzel", "research").spent_usd == 0.0
"""The flock sweep: one readiness-driven pass over every mission.

Moved from armature-dispatch tests/unit/test_sweep.py @ c5fe34f, seam-inverted:
the launch is the tests/transport/launch_stub.py record-only callable; the
ECS launch-env assertions (JOB_ID/DISPATCH_WORK_UNIT on the launched task)
stayed in the deployment repo as launcher-integration tests."""
import io
import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("boto3")   # transport suite: requires the cloud extra

import boto3  # noqa: E402

from armature.spec.mission import load_mission  # noqa: E402
from armature.state.work import IN_PROGRESS  # noqa: E402

from armature.transport import s3io  # noqa: E402
from armature.transport.s3work import S3WorkStore  # noqa: E402
from armature.transport.sweep import list_missions, run_sweep  # noqa: E402

from . import launch_stub  # noqa: E402
from .conftest import BUCKET  # noqa: E402

MISSION_DOC = Path(__file__).resolve().parent / "fixtures" / "pretzel.mission.yml"


def _zip_dir(path: Path) -> bytes:
    """The package zip the deployment repo's s3ops.zip_dir makes (stays there:
    package-UPLOAD mechanics); these repair tests only need the bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(path.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(path).as_posix())
    return buf.getvalue()


@pytest.fixture
def fleet(s3_bucket):
    """moto S3+ecs+sns with the pretzel mission seeded. The fleet no longer
    registers a task def or uploads workflows/work-echo.zip — the injected
    launch stub replaces the launcher; the one test that runs a real moto
    task registers its own task def."""
    s3, _ = s3_bucket
    launch_stub.reset()
    ecs = boto3.client("ecs")
    ecs.create_cluster(clusterName="dispatch")
    store = S3WorkStore(s3, BUCKET)
    mission = load_mission(MISSION_DOC)
    store.put_mission_doc("pretzel", MISSION_DOC.read_text())
    for unit in mission.work:
        store.ensure_unit(mission, unit)
    sns = boto3.client("sns")
    topic = sns.create_topic(Name="alerts")["TopicArn"]
    return {"s3": s3, "ecs": ecs, "sns": sns, "topic": topic,
            "store": store, "mission": mission}


def _sweep(fleet, **kw):
    # ecs=None: the stub's fabricated task arn reads "state unknown" in the
    # repair pass — held, the conservative outcome; tests that exercise task
    # state pass a real ecs client to repair_job directly.
    return run_sweep(fleet["s3"], None, fleet["sns"], BUCKET,
                     launch=launch_stub.launch, topic_arn=fleet["topic"], **kw)


def _by(fleet, report):
    return {(o.mission, o.unit_id): o for o in report.outcomes}


def test_list_missions(fleet):
    assert list_missions(fleet["s3"], BUCKET) == ["pretzel"]


def test_sweep_submits_ready_unit(fleet):
    report = _sweep(fleet)
    out = _by(fleet, report)
    assert out[("pretzel", "research")].action == "submitted"
    assert out[("pretzel", "research")].job_id == "sweep-pretzel~research~1"
    assert out[("pretzel", "hero-copy")].action == "held"      # waiting on research
    assert out[("pretzel", "approve")].action == "held"       # waiting on hero-copy
    rec = fleet["store"].load("pretzel", "research")
    assert rec.state == IN_PROGRESS and rec.attempts == 1
    assert rec.last_job_id == "sweep-pretzel~research~1"
    # the launch callable received the deterministic job id + work ref
    assert launch_stub.CALLS == [{"bucket": BUCKET, "workflow": "work-echo",
                                  "inputs": {}, "job_id": "sweep-pretzel~research~1",
                                  "work_unit": {"mission": "pretzel",
                                                "unit_id": "research"}}]


def test_second_sweep_holds_in_progress_unit(fleet):
    # Review Focus 3: the record move is the idempotence door — a second
    # sweep after the first submit holds (state in_progress), never re-launches
    _sweep(fleet)
    report = _sweep(fleet)
    out = _by(fleet, report)
    assert out[("pretzel", "research")].action == "held"
    assert "in_progress" in " ".join(out[("pretzel", "research")].reasons)
    assert fleet["store"].load("pretzel", "research").attempts == 1


def test_sweep_notifies_human_led(fleet):
    # drive research + hero-copy to done, then approve is ready-but-human-led
    store = fleet["store"]
    store.apply("pretzel", "research", IN_PROGRESS, job_id="j0")
    store.apply("pretzel", "research", "done", reason="fixture", job_id="j0")
    store.apply("pretzel", "hero-copy", IN_PROGRESS, job_id="j0")
    store.apply("pretzel", "hero-copy", "done", reason="fixture", job_id="j0")
    # observe the notification through an SQS subscription (moto delivers)
    sqs = boto3.client("sqs")
    queue = sqs.create_queue(QueueName="notify")["QueueUrl"]
    q_arn = sqs.get_queue_attributes(
        QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    fleet["sns"].subscribe(TopicArn=fleet["topic"], Protocol="sqs",
                           Endpoint=q_arn)
    report = _sweep(fleet)
    out = _by(fleet, report)
    assert out[("pretzel", "approve")].action == "notified"
    assert fleet["store"].load("pretzel", "approve").attempts == 0  # never acted
    msg = sqs.receive_message(QueueUrl=queue)["Messages"][0]["Body"]
    assert "approve" in msg


def test_sweep_dry_run_observes_only(fleet):
    report = _sweep(fleet, dry_run=True)
    out = _by(fleet, report)
    assert out[("pretzel", "research")].action == "held"
    assert any("dry_run" in r for r in out[("pretzel", "research")].reasons)
    assert fleet["store"].load("pretzel", "research").attempts == 0
    assert not launch_stub.CALLS                      # nothing launched at all


def test_sweep_holds_unregistered_follow_on(fleet):
    # Review Focus 5: a closure seeds a follow-on naming an unregistered
    # package — the sweep holds it with a reason, consumes no attempt
    from armature.state.closure import ClosureRecord, FollowOnUnit, apply_closure
    store = fleet["store"]
    # closure semantics run from in_progress (pending → handed_off is illegal)
    store.apply("pretzel", "research", IN_PROGRESS, job_id="j0")
    apply_closure(store, "pretzel", "research",
                  ClosureRecord(reason="handed_off", notes="spawn",
                                follow_on=[FollowOnUnit(
                                    id="deep-dive", title="Deep dive",
                                    workflow="not-registered")]),
                  job_id="j0", posture="delegated")
    report = _sweep(fleet)
    out = _by(fleet, report)
    assert out[("pretzel", "deep-dive")].action == "held"
    assert any("unregistered" in r or "failed" in r
               for r in out[("pretzel", "deep-dive")].reasons)
    assert store.load("pretzel", "deep-dive").attempts == 0


def test_sweep_corrupt_doc_continues_other_missions(fleet):
    # Review Focus 1: a corrupt doc is one loud error line; other missions proceed
    fleet["store"].put_mission_doc("pretzel", "name: pretzel\nwork: [broken\n")
    fleet["store"].put_mission_doc("other", "name: other\nwork: []\n")
    report = _sweep(fleet)
    assert any("pretzel" in e for e in report.errors)
    assert not any("other" in e for e in report.errors)


def test_sweep_corrupt_record_continues_other_missions(fleet):
    # Review Focus 1 sibling: a corrupt unit RECORD (not the doc) must be the
    # same shape — one loud error line, the pass continues with other missions
    fleet["store"].put_mission_doc("other", "name: other\nwork: []\n")
    s3io.upload_bytes(fleet["s3"], BUCKET, "work/pretzel/hero-copy.json",
                      b"not json at all")
    report = _sweep(fleet)                     # must not raise
    assert any("pretzel" in e for e in report.errors)
    assert not any("other" in e for e in report.errors)


def test_sweep_holds_invalid_name_unit(fleet):
    # Review Focus 2: hostile ids are validated at EVERY entry point — the
    # sweep is the one that sees closure-seeded ids no CLI ever checked
    from armature.state.closure import FollowOnUnit
    store = fleet["store"]
    store.seed_follow_on("pretzel", FollowOnUnit(
        id="bad unit", title="Bad", workflow="work-echo"), posture="delegated")
    report = _sweep(fleet)
    out = _by(fleet, report)
    assert out[("pretzel", "bad unit")].action == "held"
    assert any("invalid" in r for r in out[("pretzel", "bad unit")].reasons)
    # no launch call carries the invalid id (research submits legitimately)
    assert all("bad unit" not in c["work_unit"]["unit_id"]
               for c in launch_stub.CALLS)
    assert store.load("pretzel", "bad unit").attempts == 0


def test_submit_unit_holds_when_record_moved_underneath(fleet):
    # never-2PC race: a manual submit moves the record to in_progress after
    # this sweep computed readiness but before its launch bookkeeping — the
    # sweep holds the unit with a reason, never crashes the pass
    from armature.transport.sweep import _submit_unit
    store = fleet["store"]
    stale = store.load("pretzel", "research")   # computed as pending
    store.apply("pretzel", "research", IN_PROGRESS, job_id="manual-1")
    outcome = _submit_unit(BUCKET, store, stale, launch=launch_stub.launch,
                          dry_run=False, cluster="dispatch")
    assert outcome.action == "held"
    assert any("bookkeeping" in r or "failed" in r for r in outcome.reasons)
    rec = store.load("pretzel", "research")
    assert rec.state == IN_PROGRESS and rec.last_job_id == "manual-1"


def test_main_runs_the_sweep(fleet, monkeypatch, capsys):
    # the cron package's only production surface: main() prints the summary
    # JSON that the adapter's parse: json consumes
    from armature.transport import sweep
    monkeypatch.setenv("DISPATCH_BUCKET", BUCKET)
    monkeypatch.setenv("FLOCK_ALERTS_TOPIC_ARN", fleet["topic"])
    monkeypatch.setenv("ARMATURE_SWEEP_LAUNCH", "tests.transport.launch_stub:launch")
    rc = sweep.main()
    captured = capsys.readouterr()
    assert rc == 0
    summary = json.loads(captured.out)
    assert summary["submitted"] == ["sweep-pretzel~research~1"]


def test_main_exit_one_on_errors(fleet, monkeypatch, capsys):
    from armature.transport import sweep
    monkeypatch.setenv("DISPATCH_BUCKET", BUCKET)
    monkeypatch.setenv("ARMATURE_SWEEP_LAUNCH", "tests.transport.launch_stub:launch")
    fleet["store"].put_mission_doc("pretzel", "name: pretzel\nwork: [broken\n")
    rc = sweep.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert "pretzel" in captured.out        # stdout stays parseable JSON
    assert "pretzel" in captured.err        # the errors ride stderr to the alarm


# ── repair pass (design §6 orphan rule) ──────────────────────────────────────

def _in_progress_research(fleet):
    store = fleet["store"]
    record = store.load("pretzel", "research")
    record.attempts = 1
    store.save(record)
    store.apply("pretzel", "research", IN_PROGRESS, job_id="job-1")
    return record


def _stage_job(fleet, *, receipt=None, result=None, pkg_zip=None, task_arn=None):
    """Lay down jobs/job-1/ exactly as the runner would have left it."""
    s3 = fleet["s3"]
    s3io.put_json(s3, BUCKET, "jobs/job-1/task.json", {
        "job_id": "job-1", "package_name": "work-echo",
        "package_version": "1.0", "inputs": {}, "submitted_at": "now",
        "task_arn": task_arn})
    if pkg_zip is not None:
        s3io.upload_bytes(s3, BUCKET, "jobs/job-1/package.zip", pkg_zip)
    if receipt is not None:
        s3io.put_json(s3, BUCKET, "jobs/job-1/results/run-1/receipt.json",
                      receipt)
    if result is not None:
        s3io.put_json(s3, BUCKET, "jobs/job-1/results/run-1/result.json", result)


def _receipt(status="complete", cost_usd=0.0):
    return {"status": status, "cost_usd": cost_usd, "run_id": "run-1"}


def test_repair_receipt_redrive_settles_once(fleet, work_echo_pkg):
    # Review Focus 4: the job finished, the settle never landed (runner
    # crash before 7.6) — the next sweep re-drives the settle idempotently
    from armature.transport.sweep import repair_job
    _in_progress_research(fleet)
    _stage_job(fleet, receipt=_receipt(cost_usd=0.05),
               result={"echoer": {"content": "title=Research the pretzel audience"}},
               pkg_zip=_zip_dir(work_echo_pkg))
    store = fleet["store"]
    outcome = repair_job(fleet["s3"], fleet["ecs"], BUCKET, store,
                         "pretzel", store.load("pretzel", "research"))
    assert outcome.action == "repaired"
    rec = store.load("pretzel", "research")
    assert rec.state.value == "done"
    assert rec.spent_usd == pytest.approx(0.05)
    # and a second repair of the same job double-meters nothing
    outcome2 = repair_job(fleet["s3"], fleet["ecs"], BUCKET, store,
                          "pretzel", store.load("pretzel", "research"))
    assert store.load("pretzel", "research").spent_usd == pytest.approx(0.05)


def test_repair_failed_receipt_goes_retry_pending(fleet, work_echo_pkg):
    from armature.transport.sweep import repair_job
    _in_progress_research(fleet)
    _stage_job(fleet, receipt=_receipt(status="failed", cost_usd=0.01),
               pkg_zip=_zip_dir(work_echo_pkg))
    store = fleet["store"]
    outcome = repair_job(fleet["s3"], fleet["ecs"], BUCKET, store,
                         "pretzel", store.load("pretzel", "research"))
    rec = store.load("pretzel", "research")
    assert rec.state.value == "retry_pending"
    assert rec.spent_usd == pytest.approx(0.01)


def test_repair_orphan_fails_without_receipt(fleet):
    from armature.transport.sweep import repair_job
    _in_progress_research(fleet)
    # no receipt anywhere; task.json names a task ECS no longer knows → gone
    _stage_job(fleet, task_arn="arn:aws:ecs:us-east-1:123456789012:task/dispatch/dead")
    store = fleet["store"]
    outcome = repair_job(fleet["s3"], fleet["ecs"], BUCKET, store,
                         "pretzel", store.load("pretzel", "research"))
    assert outcome.action == "failed"
    rec = store.load("pretzel", "research")
    assert rec.state.value == "retry_pending"      # attempts 1 < max 2
    assert rec.spent_usd == 0.0


def test_repair_running_task_is_none(fleet):
    from armature.transport.sweep import repair_job
    _in_progress_research(fleet)
    # a real, still-running moto task: register the family this test needs
    # (the fleet no longer does), launch it in a default-VPC subnet, stage
    ecs = fleet["ecs"]
    ecs.register_task_definition(
        family="dispatch-runner-work-echo", requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc", cpu="512", memory="1024",
        containerDefinitions=[{"name": "runner", "image": "x/y:1",
                               "essential": True, "memory": 1024}])
    subnet_id = boto3.client("ec2").describe_subnets()["Subnets"][0]["SubnetId"]
    task_arn = ecs.run_task(
        cluster="dispatch",
        taskDefinition="dispatch-runner-work-echo",
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": [subnet_id], "securityGroups": [],
            "assignPublicIp": "ENABLED"}},
        overrides={"containerOverrides": [{"name": "runner", "environment": []}]},
    )["tasks"][0]["taskArn"]
    _stage_job(fleet, task_arn=task_arn)
    store = fleet["store"]
    assert repair_job(fleet["s3"], ecs, BUCKET, store, "pretzel",
                      store.load("pretzel", "research")) is None


def test_repair_unreadable_package_holds(fleet):
    from armature.transport.sweep import repair_job
    _in_progress_research(fleet)
    _stage_job(fleet, receipt=_receipt())          # receipt but no package zip
    # make the package unreadable EVERYWHERE: no jobs/<id>/package.zip and no
    # registered workflows/<name>.zip for _spec_for_job's fallback to find
    fleet["s3"].delete_object(Bucket=BUCKET, Key="workflows/work-echo.zip")
    store = fleet["store"]
    outcome = repair_job(fleet["s3"], fleet["ecs"], BUCKET, store,
                         "pretzel", store.load("pretzel", "research"))
    assert outcome.action == "held"
    assert any("repair failed" in r or "package" in r for r in outcome.reasons)
    assert store.load("pretzel", "research").state == IN_PROGRESS


# ── seam/new-behavior tests (Review Focus 2, 3, 5) ───────────────────────────

def test_main_honors_dispatch_cluster_env(s3_bucket, monkeypatch, capsys):
    # Review Important 1: the cluster name is deployment mechanism — the
    # entrypoint must take it from env, never hardcode "dispatch". On a
    # differently-named cluster the repair pass's describe_tasks misses and
    # every crashed unit is held forever; the orphan rule never fires.
    from armature.transport import sweep
    s3, _ = s3_bucket
    boto3.client("ecs").create_cluster(clusterName="fleetx")
    store = S3WorkStore(s3, BUCKET)
    mission = load_mission(MISSION_DOC)
    store.put_mission_doc("pretzel", MISSION_DOC.read_text())
    unit = next(u for u in mission.work if u.id == "research")
    rec = store.ensure_unit(mission, unit)
    rec.attempts = rec.max_attempts      # at max: orphan-fail lands terminal
    store.save(rec)
    store.apply("pretzel", "research", IN_PROGRESS, job_id="job-1")
    # a task.json naming a task on the deployment's real cluster, dead
    s3io.put_json(s3, BUCKET, "jobs/job-1/task.json",
                  {"job_id": "job-1", "package_name": "work-echo",
                   "package_version": "1.0", "inputs": {},
                   "submitted_at": "now",
                   "task_arn": "arn:aws:ecs:us-east-1:123456789012:"
                               "task/fleetx/dead"})
    monkeypatch.setenv("DISPATCH_BUCKET", BUCKET)
    monkeypatch.setenv("DISPATCH_CLUSTER", "fleetx")
    monkeypatch.setenv("ARMATURE_SWEEP_LAUNCH", "tests.transport.launch_stub:launch")
    assert sweep.main() == 0
    # the orphan rule fired against the right cluster: failed, not held
    assert store.load("pretzel", "research").state.value == "failed"


def test_missing_extra_actionable_error(monkeypatch, s3_bucket):
    # Review Focus 2: without boto3 the entrypoint fails with install
    # instructions, not a bare ModuleNotFoundError
    import sys
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.setenv("DISPATCH_BUCKET", BUCKET)
    monkeypatch.setenv("ARMATURE_SWEEP_LAUNCH", "tests.transport.launch_stub:launch")
    from armature.transport import sweep
    with pytest.raises(ImportError, match=r"armature-agents\[cloud\]"):
        sweep.main()


def test_sweep_holds_when_launch_raises_unexpected(s3_bucket):
    # Review Focus 3: any exception type from the launch backend holds the
    # unit with a reason — the pass never crashes on a foreign error
    s3, _ = s3_bucket
    store = S3WorkStore(s3, BUCKET)
    store.put_mission_doc("pretzel", MISSION_DOC.read_text())
    def boom(*a, **k):
        raise RuntimeError("provider exploded")
    report = run_sweep(s3, None, None, BUCKET, launch=boom)
    out = {(o.mission, o.unit_id): o for o in report.outcomes}
    assert out[("pretzel", "research")].action == "held"
    assert any("provider exploded" in r for r in out[("pretzel", "research")].reasons)
    assert not report.errors
    assert store.load("pretzel", "research").attempts == 0


def test_repair_task_json_without_arn_fails_orphan(s3_bucket, work_echo_pkg):
    # Review Focus 5: task.json present but arn-less reads as "gone" — the
    # orphan rule fails the unit; no crash, no wait
    from armature.transport.sweep import repair_job
    s3, _ = s3_bucket
    store = S3WorkStore(s3, BUCKET)
    mission = load_mission(MISSION_DOC)
    store.put_mission_doc("pretzel", MISSION_DOC.read_text())
    unit = next(u for u in mission.work if u.id == "research")
    rec = store.ensure_unit(mission, unit)
    rec.attempts = 1
    store.save(rec)
    store.apply("pretzel", "research", IN_PROGRESS, job_id="job-1")
    s3io.put_json(s3, BUCKET, "jobs/job-1/task.json",
                  {"job_id": "job-1", "package_name": "work-echo",
                   "package_version": "1.0", "inputs": {}, "submitted_at": "now"})
    outcome = repair_job(s3, boto3.client("ecs"), BUCKET, store, "pretzel",
                         store.load("pretzel", "research"))
    assert outcome.action == "failed"
    assert store.load("pretzel", "research").state.value == "retry_pending"
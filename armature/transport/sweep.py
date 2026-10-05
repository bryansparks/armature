"""The flock sweep — one scheduled pass over all missions (design §6).

Readiness is armature's pure computation; the sweep is the executor that
acts on it: launchable delegated units get a job, ready human-led units get
an SNS notification (notify only — an executor never acts in human-led
scope), everything else is held with its reason. Zero always-on components:
this runs as the flock-sweep cron package and exits.

Overlapping sweeps are safe by construction (never-2PC, design §6):
sweep-minted job ids are deterministic per mission+unit+attempt, and every
record move goes through the same one-door store — a re-sweep of the same
attempt re-drives the same job and never double-consumes an attempt.

Moved from armature-dispatch dispatch/sweep.py @ c5fe34f; the ECS launch is
injected (spec 2026-10-05 §Decisions: mechanism is transport)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

from armature.spec.mission import validate_mission

from armature.transport import s3io
from armature.transport.naming import sweep_job_id, validate_work_names
from armature.transport.settle import fail_work_unit, settle_work_unit
from armature.transport.s3work import S3WorkStore
from armature.transport.workops import start_work_unit


@dataclass
class SweepOutcome:
    mission: str
    unit_id: str
    action: str                  # submitted | notified | held | repaired | failed
    job_id: str | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass
class SweepReport:
    outcomes: list[SweepOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return json.dumps({
            "missions": sorted({o.mission for o in self.outcomes}),
            "submitted": [o.job_id for o in self.outcomes
                         if o.action == "submitted"],
            "notified": [f"{o.mission}/{o.unit_id}" for o in self.outcomes
                         if o.action == "notified"],
            "held": [f"{o.mission}/{o.unit_id}" for o in self.outcomes
                     if o.action == "held"],
            "repaired": [f"{o.mission}/{o.unit_id}" for o in self.outcomes
                          if o.action in ("repaired", "failed")],
            "errors": self.errors,
        }, indent=2)


def list_missions(s3, bucket: str) -> list[str]:
    """Mission names present in the store — one per work/<name>/mission.yml."""
    store = S3WorkStore(s3, bucket)
    found: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{store.prefix}/"):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            if len(parts) == 3 and parts[2] == "mission.yml":
                found.append(parts[1])
    return sorted(set(found))


def _is_ready_but_human_led(decision) -> bool:
    """Notify target: human-led posture is the ONLY thing holding it back."""
    return decision.notify_only and all(
        r.startswith("human-led") for r in decision.reasons)


def _notify(sns, topic_arn: str, mission_name: str, decisions) -> None:
    lines = [f"Armature flock — mission '{mission_name}' has work ready for you:"]
    for d in decisions:
        lines.append(f"- {d.unit_id}: " + "; ".join(d.reasons))
    sns.publish(TopicArn=topic_arn,
                Subject=f"armature flock: {mission_name}",
                Message="\n".join(lines))


def _submit_unit(bucket: str, store: S3WorkStore, record, *, launch, dry_run: bool,
                 cluster: str) -> SweepOutcome:
    """Launch one ready delegated unit via the injected launch callable.

    Bare workflow names resolve to whatever the launch backend has
    registered (the deployment repo's workflows/<name>.zip + task def
    family) — the executor-side registration validate_mission only warns
    about. Path-like refs never resolve in the cloud: held. An unregistered
    package or a launch failure holds the unit WITHOUT consuming an attempt."""
    mission, unit_id = record.mission, record.unit_id
    try:
        validate_work_names(mission, unit_id)
    except ValueError as exc:
        # the sweep sees closure-seeded ids no CLI ever checked — hold
        # before they reach S3 keys, job ids, or task env
        return SweepOutcome(mission=mission, unit_id=unit_id, action="held",
                            reasons=[f"invalid name: {exc}"])
    if record.workflow.endswith((".yml", ".yaml")) or "/" in record.workflow:
        return SweepOutcome(mission=mission, unit_id=unit_id, action="held",
                            reasons=[f"path-like workflow {record.workflow!r} "
                                     "cannot be launched from the cloud"])
    job_id = sweep_job_id(mission, unit_id, record.attempts + 1)
    if dry_run:
        return SweepOutcome(mission=mission, unit_id=unit_id, action="held",
                            job_id=job_id, reasons=["dry_run: would submit"])
    try:
        launch(bucket, record.workflow, dict(record.inputs), job_id=job_id,
               work_unit={"mission": mission, "unit_id": unit_id})
    except Exception as exc:
        # any launch-backend failure (unregistered package, RunTask error,
        # an unexpected provider error) holds the unit WITHOUT consuming an
        # attempt — the launch callable is deployment mechanism (spec
        # §Decisions)
        return SweepOutcome(mission=mission, unit_id=unit_id, action="held",
                            reasons=[f"submit failed: {exc}"])
    try:
        start_work_unit(store, mission=mission, unit_id=unit_id, job_id=job_id)
    except Exception as exc:
        # never-2PC race: another launcher (manual submit, a concurrent
        # sweep) moved the record under us and the legality table refused —
        # hold the unit, never crash the pass
        return SweepOutcome(mission=mission, unit_id=unit_id, action="held",
                            job_id=job_id,
                            reasons=[f"launch bookkeeping failed: "
                                     f"{type(exc).__name__}: {exc}"])
    return SweepOutcome(mission=mission, unit_id=unit_id, action="submitted",
                        job_id=job_id)


def run_sweep(s3, ecs, sns, bucket: str, *, launch, dry_run: bool = False,
              topic_arn: str | None = None,
              cluster: str = "dispatch") -> SweepReport:
    """One pass: repair → readiness → submit / notify / hold, per mission.
    A corrupt mission doc is one loud error line; the pass continues."""
    from armature.state.work import compute_readiness
    report = SweepReport()
    store = S3WorkStore(s3, bucket)
    for mission_name in list_missions(s3, bucket):
        try:
            doc = store.get_mission_doc(mission_name)
        except ValueError as exc:
            report.errors.append(f"mission {mission_name}: corrupt doc: {exc}")
            continue
        if doc is None:
            report.errors.append(f"mission {mission_name}: mission.yml missing")
            continue
        hard = [e for e in validate_mission(doc, strict=False)
                if e.severity != "warning"]
        if hard:
            report.errors.append("mission " + mission_name + ": " + "; ".join(
                f"{e.code}: {e.message}" for e in hard))
            continue
        # one bad record or one transient S3/ECS glitch is one loud error
        # line — never the death of the whole pass (Review Focus 1's shape,
        # extended to every sibling: corrupt records, corrupt task.json,
        # launch races). Legality violations still raise INSIDE the door.
        try:
            # materialize every doc unit (closure-seeded records exist already)
            known = {r.unit_id for r in store.list_units(mission_name)}
            for unit in doc.work:
                if unit.id not in known:
                    store.ensure_unit(doc, unit)
            # repair pass first (design §6 orphan rule): crashed runners and
            # settles that never landed
            for record in store.list_units(mission_name):
                outcome = repair_job(s3, ecs, bucket, store, mission_name,
                                     record, cluster=cluster)
                if outcome is not None:
                    report.outcomes.append(outcome)
            decisions = compute_readiness(doc, store.list_units(mission_name))
            by_id = {r.unit_id: r for r in store.list_units(mission_name)}
            notify_units = []
            for d in decisions:
                record = by_id[d.unit_id]
                if d.launchable:
                    report.outcomes.append(
                        _submit_unit(bucket, store, record, launch=launch,
                                     dry_run=dry_run, cluster=cluster))
                elif _is_ready_but_human_led(d):
                    if dry_run or not topic_arn:
                        report.outcomes.append(SweepOutcome(
                            mission=mission_name, unit_id=d.unit_id, action="held",
                            reasons=(["dry_run: would notify"] if dry_run
                                     else ["notify skipped: no topic arn"]
                                     ) + list(d.reasons)))
                    else:
                        notify_units.append(d)
                else:
                    report.outcomes.append(SweepOutcome(
                        mission=mission_name, unit_id=d.unit_id, action="held",
                        reasons=list(d.reasons)))
            if notify_units:
                try:
                    _notify(sns, topic_arn, mission_name, notify_units)
                except Exception as exc:
                    report.errors.append(
                        f"mission {mission_name}: notify failed: {exc}")
                for d in notify_units:
                    report.outcomes.append(SweepOutcome(
                        mission=mission_name, unit_id=d.unit_id, action="notified",
                        reasons=list(d.reasons)))
        except Exception as exc:
            report.errors.append(f"mission {mission_name}: "
                                 f"{type(exc).__name__}: {exc}")
    return report


# ---- repair pass (design §6 orphan rule; coverage in Task 8) ----------------

def _task_json(s3, bucket: str, job_id: str) -> dict | None:
    try:
        return s3io.get_json(s3, bucket, f"jobs/{job_id}/task.json")
    except s3.exceptions.NoSuchKey:
        return None


def _task_stopped(ecs, task_arn: str, *, cluster: str = "dispatch") -> bool | None:
    """True when STOPPED or unknown-to-ECS; False while running; None when
    DescribeTasks itself failed (hold rather than fail a live run)."""
    try:
        tasks = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"]
    except Exception:
        return None
    if not tasks:
        return True
    return tasks[0]["lastStatus"] == "STOPPED"


def _spec_for_job(s3, bucket: str, job_id: str, task: dict | None):
    """The HarnessSpec the job ran — extracted from its package zip, never
    guessed (design §6: the sweep never invents a closure). Raises ValueError
    when the package cannot be located or read; callers hold the unit."""
    import tempfile
    from pathlib import Path
    from armature.packaging.manifest import PackageManifest
    from armature.spec.loader import load_spec
    from ruamel.yaml import YAML
    candidates = [f"jobs/{job_id}/package.zip"]
    if task and task.get("package_name"):
        candidates.append(f"workflows/{task['package_name']}.zip")
    for key in candidates:
        try:
            with tempfile.TemporaryDirectory() as td:
                pkg_dir = Path(td) / "package"
                s3io.download_and_extract(s3, bucket, key, pkg_dir)
                manifest = PackageManifest.model_validate(
                    YAML(typ="safe").load(
                        (pkg_dir / "package.yaml").read_text()))
                return load_spec(pkg_dir / manifest.spec)
        except Exception:
            continue
    raise ValueError(f"cannot locate a readable package for job {job_id}")


def _results_for_run(s3, bucket: str, job_id: str, run_id: str | None) -> dict:
    if not run_id:
        return {}
    try:
        return s3io.get_json(s3, bucket,
                             f"jobs/{job_id}/results/{run_id}/result.json")
    except s3.exceptions.NoSuchKey:
        return {}


def repair_job(s3, ecs, bucket: str, store: S3WorkStore, mission: str, record, *,
               cluster: str = "dispatch") -> SweepOutcome | None:
    """The orphan rule (design §6): an in_progress unit whose job is gone
    must be failed (never re-launched blind); one whose job finished but
    never settled gets its settle re-driven — idempotently, never
    double-metering. Returns None when the unit is not this pass's
    business."""
    if record.state.value != "in_progress":
        return None
    if record.last_job_id is None:
        return SweepOutcome(mission=mission, unit_id=record.unit_id,
                            action="held", reasons=[
                                "in_progress with no job id — manual repair "
                                "required"])
    job_id = record.last_job_id
    receipt, run_id = s3io.latest_receipt(s3, bucket, job_id)
    if receipt is not None:
        try:
            spec = _spec_for_job(s3, bucket, job_id, _task_json(s3, bucket, job_id))
            results = _results_for_run(s3, bucket, job_id, run_id)
            state = settle_work_unit(store, mission=mission,
                                     unit_id=record.unit_id, job_id=job_id,
                                     status=receipt["status"],
                                     cost_usd=receipt.get("cost_usd"),
                                     results=results, spec=spec)
            return SweepOutcome(mission=mission, unit_id=record.unit_id,
                                action="repaired", job_id=job_id,
                                reasons=[f"settled late: state={state}"])
        except Exception as exc:
            return SweepOutcome(mission=mission, unit_id=record.unit_id,
                                action="held", job_id=job_id,
                                reasons=[f"repair failed: {type(exc).__name__}: {exc}"])
    task = _task_json(s3, bucket, job_id)
    if task is not None and task.get("task_arn"):
        stopped = _task_stopped(ecs, task["task_arn"], cluster=cluster)
        if stopped is False:
            return None                 # still running — its own 7.6 settles it
        if stopped is None:
            return SweepOutcome(mission=mission, unit_id=record.unit_id,
                                action="held", job_id=job_id,
                                reasons=["task state unknown — held for manual review"])
    # task gone (or no task.json at all — at-least-once accepted): orphan
    reason = "orphaned: task gone without a receipt"
    fail_work_unit(store, mission=mission, unit_id=record.unit_id,
                   job_id=job_id, reason=reason, cost_usd=0.0)
    return SweepOutcome(mission=mission, unit_id=record.unit_id,
                        action="failed", job_id=job_id, reasons=[reason])


def main() -> int:
    """Entrypoint for the flock-sweep cron package (`python -m
    armature.transport.sweep`). dry_run defaults from DISPATCH_INPUTS_JSON (the
    cron rule's inputs); the topic ARN is baked into the task def env by the
    compute stack. ARMATURE_SWEEP_LAUNCH names the launch callable
    (module:function) — the ECS launcher is deployment mechanism and is
    injected across the seam; a provider #2 sets its own. Exit 1 on any error
    so the adapter's nonzero exit fails the run and the existing ECS-failure
    alarm path fires; the summary rides stdout (the adapter parses it via
    parse: json) and stderr (which the alarm message surfaces)."""
    try:
        import boto3
    except ImportError as exc:   # pragma: no cover - covered via sys.modules poisoning
        raise ImportError(
            "armature[cloud] requires boto3 — install with: "
            "pip install armature-agents[cloud]") from exc
    dry_run = False
    raw = os.environ.get("DISPATCH_INPUTS_JSON")
    if raw:
        try:
            dry_run = bool(json.loads(raw).get("dry_run", False))
        except (json.JSONDecodeError, AttributeError):
            pass          # the runner owns bad-JSON loudness; never crash here
    launch_path = os.environ.get("ARMATURE_SWEEP_LAUNCH")
    if not launch_path:
        print("flock-sweep: ARMATURE_SWEEP_LAUNCH not set (module:function "
              "naming the launch callable, e.g. dispatch.ops:run_workflow)",
              flush=True)
        return 2
    module_name, _, attr = launch_path.partition(":")
    try:
        launch = getattr(__import__(module_name, fromlist=["_"]), attr)
    except (ImportError, AttributeError) as exc:
        print(f"flock-sweep: cannot import launch callable {launch_path!r}: "
              f"{exc}", flush=True)
        return 2
    topic_arn = os.environ.get("FLOCK_ALERTS_TOPIC_ARN") or None
    bucket = os.environ.get("DISPATCH_BUCKET")
    if not bucket:
        print("flock-sweep: DISPATCH_BUCKET not set", flush=True)
        return 2
    s3, ecs = boto3.client("s3"), boto3.client("ecs")
    report = run_sweep(s3, ecs, boto3.client("sns"), bucket,
                       launch=lambda b, w, i, **kw: launch(b, w, i, s3=s3, ecs=ecs,
                                                          ec2=boto3.client("ec2"), **kw),
                       dry_run=dry_run, topic_arn=topic_arn)
    print(report.summary(), flush=True)
    if report.errors:
        print(report.summary(), file=sys.stderr, flush=True)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
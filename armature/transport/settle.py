"""Runner-side work-unit plumbing: injection (step 4.7) and settle (step 7.6).

The runner moves armature's record, it never interprets it (design §6):
legality and closure semantics run through the same store + armature
functions every executor uses.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from armature.spec.models import HarnessSpec

from armature.transport.s3work import S3WorkStore

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_work_record(s3, bucket: str, mission: str, unit_id: str):
    """The work-unit record from the store, or None when absent/corrupt.

    Missing or corrupt is LOUD but never a gate (design §6): the runner
    still runs the workflow — ungoverned, logged, visible — rather than
    stranding a paid job."""
    try:
        return S3WorkStore(s3, bucket).load(mission, unit_id)
    except Exception:
        log.warning("work record %s/%s unreadable — running ungoverned",
                    mission, unit_id, exc_info=True)
        return None


def runner_injection(doc, record, spec: HarnessSpec) -> dict | None:
    """The context['work_unit'] dict for this run — None unless the spec
    opts in via mission_source: work_unit (the same conditional-injection
    contract as the local executor) or no record exists to render."""
    if spec.mission_source != "work_unit" or record is None:
        return None
    return {
        "mission": record.mission,
        "mission_objective": doc.objective if doc is not None else "",
        "unit_id": record.unit_id,
        "title": record.title,
        "objective": record.objective,
        "requires": list(record.requires),
        "posture": record.posture,
        "state": "in_progress",
        "attempts": record.attempts,
    }


def settle_work_unit(store: S3WorkStore, *, mission: str, unit_id: str,
                     job_id: str, status: str, cost_usd: float | None,
                     results: dict, spec: HarnessSpec) -> str:
    """Runner step 7.6 — move the work unit to its resting state.

    Mirrors the local executor (armature mission run): meter the cost on
    success AND failure; complete + closure → apply_closure; complete +
    no closure → done; failure → failed, then retry_pending when attempts
    remain. At-least-once: a job whose settle already landed is a no-op —
    never metered twice. A crash between the metering save and the apply
    re-drives to a second metering bounded by one job's cost (never-2PC,
    accepted). Returns the final state value.
    """
    from armature.state.work import WorkUnitState as S
    record = store.load(mission, unit_id)
    if record is None:
        raise KeyError(f"work unit '{mission}/{unit_id}' has no record")
    if record.last_job_id == job_id and record.state != S.IN_PROGRESS:
        return record.state.value          # this job's settle already landed

    if status != "complete":
        return fail_work_unit(store, mission=mission, unit_id=unit_id,
                              job_id=job_id, reason=f"run status {status}",
                              cost_usd=float(cost_usd or 0.0))

    record.spent_usd += float(cost_usd or 0.0)
    record.updated_at = _now_iso()
    store.save(record)
    from armature.state.closure import ClosureError, extract_closure
    try:
        closure = extract_closure(spec, results)
    except ClosureError as exc:
        return fail_work_unit(store, mission=mission, unit_id=unit_id,
                              job_id=job_id, reason=f"malformed closure: {exc}",
                              cost_usd=0.0)     # cost already metered above
    if closure is None:
        return store.apply(mission, unit_id, S.DONE,
                           reason="run complete, no closure declared",
                           job_id=job_id).state.value
    from armature.state.closure import apply_closure
    apply_closure(store, mission, unit_id, closure, job_id=job_id,
                  posture=record.posture)
    return store.load(mission, unit_id).state.value


def fail_work_unit(store: S3WorkStore, *, mission: str, unit_id: str,
                   job_id: str, reason: str, cost_usd: float = 0.0) -> str:
    """Meter the cost, move to failed, then retry_pending when attempts
    remain — the local executor's failure path, and the sweep's
    orphan-repair verb (design §6)."""
    from armature.state.work import WorkUnitState as S
    record = store.load(mission, unit_id)
    if record is None:
        raise KeyError(f"work unit '{mission}/{unit_id}' has no record")
    record.spent_usd += float(cost_usd or 0.0)
    record.updated_at = _now_iso()
    store.save(record)
    rec = store.apply(mission, unit_id, S.FAILED, reason=reason, job_id=job_id)
    if record.attempts < record.max_attempts:
        rec = store.apply(mission, unit_id, S.RETRY_PENDING,
                          reason=(f"attempt {record.attempts} of "
                                  f"{record.max_attempts} failed"),
                          job_id=job_id)
    return rec.state.value
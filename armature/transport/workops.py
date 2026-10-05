"""Work-unit submit gates + launch bookkeeping (design §6, spec 2026-10-05).

Split from armature-dispatch dispatch/workops.py @ c5fe34f: the gates are
engine policy and live here; the ECS launch (submit_job) is deployment
mechanism and stays there. The CLI composes gate → launch → start.

Gate order (the local executor's): validate the doc, validate names,
ensure the record, state gate, spec∪record requires, attempts, unit +
mission budgets — then the doc rides along. A refused submit must never
overwrite the live work/<mission>/mission.yml the sweep trusts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from armature.spec.mission import MissionSpec, load_mission, validate_mission
from armature.state.work import (BLOCKED_ON, IN_PROGRESS, PENDING,
                                 RETRY_PENDING, WorkUnitRecord)

from armature.transport.naming import validate_work_names
from armature.transport.s3work import S3WorkStore


class WorkSubmitError(ValueError):
    """A refusal before any spend: bad doc, bad names, a gate failure."""


def gate_work_unit(mission_path, unit_id: str, *, store: S3WorkStore,
                   inputs: dict | None = None) -> tuple[MissionSpec, WorkUnitRecord, dict]:
    """Run every submit gate. Returns (mission, record, run_inputs)."""
    mission_path = Path(mission_path)
    mission = load_mission(mission_path)
    errors = validate_mission(mission, strict=False)
    hard = [e for e in errors if e.severity != "warning"]
    if hard:
        raise WorkSubmitError("invalid mission document: " + "; ".join(
            f"{e.code}: {e.message}" for e in hard))
    try:
        validate_work_names(mission.name, unit_id)
    except ValueError as exc:
        raise WorkSubmitError(str(exc)) from exc

    unit = next((u for u in mission.work if u.id == unit_id), None)
    record = store.load(mission.name, unit_id)
    if unit is None and record is None:
        raise WorkSubmitError(
            f"unit {unit_id!r} is neither in the mission document nor in the store")
    if record is None:
        record = store.ensure_unit(mission, unit)

    # state gate — the same admission set as the local executor
    if record.state not in (PENDING, RETRY_PENDING, BLOCKED_ON):
        raise WorkSubmitError(
            f"unit {record.unit_id!r} is {record.state.value} — not launchable")
    # requires: spec ∪ record (a closure's blocked_on rewrite extends the record)
    by_id = {r.unit_id: r for r in store.list_units(mission.name)}
    unmet = [dep for dep in record.requires
             if by_id.get(dep) is None or by_id[dep].state.value != "done"]
    if unmet:
        raise WorkSubmitError("waiting on " + ", ".join(unmet))
    if record.attempts >= record.max_attempts:
        raise WorkSubmitError(
            f"no attempts left ({record.attempts}/{record.max_attempts})")
    mission_spent = sum(r.spent_usd for r in by_id.values())
    if record.max_budget_usd is not None and record.spent_usd >= record.max_budget_usd:
        raise WorkSubmitError(f"unit budget spent (${record.spent_usd:.2f})")
    if mission.budget_usd is not None and mission_spent >= mission.budget_usd:
        raise WorkSubmitError(f"mission budget spent (${mission_spent:.2f})")

    # gates passed — only now does the doc ride along
    store.put_mission_doc(mission.name, mission_path.read_text(encoding="utf-8"))

    run_inputs = dict(record.inputs)
    if inputs:
        run_inputs.update(inputs)     # explicit --input overrides record inputs
    return mission, record, run_inputs


def start_work_unit(store: S3WorkStore, *, mission: str, unit_id: str,
                    job_id: str) -> WorkUnitRecord:
    """Launch bookkeeping: attempts += 1 → save → in_progress with the job id.
    Idempotent for the same job (a re-drive of the same launch must not
    consume a second attempt — never-2PC, design §6)."""
    record = store.load(mission, unit_id)
    if record is None:
        raise KeyError(f"work unit '{mission}/{unit_id}' has no record")
    if record.last_job_id == job_id and record.state == IN_PROGRESS:
        return record
    record.attempts += 1
    record.updated_at = datetime.now(timezone.utc).isoformat()
    store.save(record)
    return store.apply(mission, unit_id, IN_PROGRESS,
                       reason="submitted for execution", job_id=job_id)
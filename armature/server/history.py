"""Deleting runs from the run history, with everything that belongs to them and nothing else.

What belongs to a run comes from the schema: every row whose foreign key, or whose id column named by the
convention below, points at the run, its jobs, or a row that belongs to them in turn. A required reference makes
the row the run's, so it goes. A nullable one is another record's link, so it's cleared, and that record stays.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Column, Connection, Table, delete, func, or_, select, update

from armature.server import audit, worker
from armature.server.db.schema import jobs, metadata, runs

OPEN_RUNS = ("queued", "running", worker.RUN_WAITING)
OPEN_JOBS = ("queued", *worker.HELD)
# Columns that point at another table's id without a foreign key, by name. The schema leaves these out on purpose,
# for tables that outlive or partition what they point at.
UNLINKED: dict[str, Callable[[str], bool]] = {
    "runs": lambda name: name in ("run_id", "rerun_of"),
    "jobs": lambda name: name.endswith("job_id"),
    "gates": lambda name: name == "resume_of_gate" or name.endswith("gate_id"),
}


class HistoryError(Exception):
    pass


@dataclass
class Plan:
    run_id: str
    workflow_name: str | None
    team_id: str | None
    job_ids: list[str]
    deleted: dict[str, int] = field(default_factory=dict)
    cleared: dict[str, int] = field(default_factory=dict)


def references(target: str) -> list[tuple[Table, Column]]:
    """Every column that points at ``target``'s id, by foreign key or by the unlinked naming convention."""
    found = []
    for table in metadata.sorted_tables:
        for column in table.columns:
            keys = list(column.foreign_keys)
            if keys:
                if any(k.column.table.name == target and k.column.name == "id" for k in keys):
                    found.append((table, column))
            elif target in UNLINKED and UNLINKED[target](column.name):
                found.append((table, column))
    return found


def _owns(table: Table, column: Column) -> bool:
    return not column.nullable or (table is jobs and column.name == "run_id")


def _owned(conn: Connection, run_id: str, job_ids: Iterable[str]) -> dict[str, set[str]]:
    """The ids of every row with an id that belongs to the run, by table."""
    owned: dict[str, set[str]] = {"runs": {run_id}, "jobs": set(job_ids)}
    changed = True
    while changed:
        changed = False
        for target, ids in list(owned.items()):
            for table, column in references(target):
                if not ids or not _owns(table, column) or "id" not in table.c:
                    continue
                found = {r.id for r in conn.execute(select(table.c.id).where(column.in_(ids)))}
                if not found <= owned.get(table.name, set()):
                    owned.setdefault(table.name, set()).update(found)
                    changed = True
    return owned


def _work(owned: dict[str, set[str]]) -> tuple[dict[str, list[Any]], list[tuple[Table, Column, set[str]]]]:
    gone: dict[str, list[Any]] = {}
    clears = []
    for target, ids in owned.items():
        if not ids:
            continue
        for table, column in references(target):
            if _owns(table, column):
                gone.setdefault(table.name, []).append(column.in_(ids))
            else:
                clears.append((table, column, ids))
    for name, ids in owned.items():
        if ids and "id" in metadata.tables[name].c:
            gone.setdefault(name, []).append(metadata.tables[name].c.id.in_(ids))
    return gone, clears


def _check(conn: Connection, run_ids: list[str]) -> list[Plan]:
    plans, open_ = [], []
    for run_id in run_ids:
        run = conn.execute(select(runs.c.status, runs.c.workflow_name, runs.c.team_id)
                           .where(runs.c.id == run_id)).first()
        if run is None:
            raise HistoryError(f"No run has the id {run_id}.")
        held = conn.execute(select(jobs.c.id, jobs.c.status).where(jobs.c.run_id == run_id)
                            .order_by(jobs.c.created_at, jobs.c.id)).all()
        if run.status in OPEN_RUNS or any(j.status in OPEN_JOBS for j in held):
            open_.append(run_id)
        plans.append(Plan(run_id, run.workflow_name, run.team_id, [j.id for j in held]))
    if open_:
        raise HistoryError(f"Run {', '.join(open_)} is still open. Cancel it or let it end first.")
    return plans


def delete_runs(conn: Connection, run_ids: list[str], *, actor_id: str, dry_run: bool = False) -> list[Plan]:
    """Delete each run and what belongs to it, in the caller's transaction, with one audit entry per run.

    Raises HistoryError, changing nothing, when a run doesn't exist or is still open. With ``dry_run``, only counts.
    """
    plans = _check(conn, list(dict.fromkeys(run_ids)))
    for plan in plans:
        gone, clears = _work(_owned(conn, plan.run_id, plan.job_ids))
        for table, column, ids in clears:
            where = [column.in_(ids)]
            if table.name in gone:
                where.append(~or_(*gone[table.name]))
            count = conn.execute(select(func.count()).select_from(table).where(*where)).scalar()
            if count:
                plan.cleared[f"{table.name}.{column.name}"] = count
                if not dry_run:
                    conn.execute(update(table).where(*where).values({column.name: None}))
        for table in reversed(metadata.sorted_tables):
            if table.name not in gone:
                continue
            where = or_(*gone[table.name])
            count = conn.execute(select(func.count()).select_from(table).where(where)).scalar()
            if count:
                plan.deleted[table.name] = count
                if not dry_run:
                    conn.execute(delete(table).where(where))
        if not dry_run:
            audit.record(conn, action="run.delete", actor_id=actor_id, team_id=plan.team_id, target_kind="run",
                         target_id=plan.run_id, detail={"workflow": plan.workflow_name, "jobs": plan.job_ids,
                                                        "deleted": plan.deleted, "cleared": plan.cleared})
    return plans

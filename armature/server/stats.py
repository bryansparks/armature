"""Aggregates for the Overview and Trends pages.

SQL stays portable across SQLite, MySQL, and Postgres, so the queries only
filter and fetch. Day buckets, durations, and percentiles are computed here
in the viewer's time zone.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, func, or_, select

from armature.server import display, rbac
from armature.server.db.schema import run_steps, runs, workflows
from armature.server.ingest import list_row, run_query, summary_of

RECENT = 8
SERIES_MAX = 7
SLOWEST = 10
OTHER = "other"
FAN_OUT_PARENT = "reviews"
REVIEW_WORKFLOWS = ("code-review", "pr-role-review", "pr-review")
DISCUSS = "discuss"
FAILED = ("failed", "lost")
FINISHED = ("succeeded", "failed", "canceled", "lost")
CREDENTIAL = re.compile(
    r"authentication|provider_auth|unauthori[sz]ed|permission ?denied|access ?denied|forbidden|\b403\b"
    r"|credential|api[ _-]?key|expired ?token|token[^\n]{0,40}expired|(?:session|sso|login)[^\n]{0,40}expired",
    re.IGNORECASE,
)


def local_day(at: datetime, zone: ZoneInfo) -> date:
    return at.astimezone(zone).date()


def day_start(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


def percentile(values: Iterable[float], q: float) -> float | None:
    """Linear interpolation between closest ranks, as Postgres percentile_cont does."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * q
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def man_hours(agent_seconds: float, multiplier: float) -> float:
    return round(agent_seconds / 3600 * multiplier, 2)


def _scoped(query, scope: rbac.Scope, team: str | None):
    query = query.where(rbac.scope_clause(scope, runs.c.team_id, runs.c.project_id))
    return query.where(runs.c.team_id == team) if team else query


def shown(name_column, hidden: list[str]):
    """Rows whose workflow is not hidden. A run with no workflow name always shows."""
    return or_(name_column.is_(None), name_column.not_in(hidden)) if hidden else None


def workflow_names(conn: Connection, scope: rbac.Scope) -> list[str]:
    """Every workflow name the viewer can see, from definitions and from runs."""
    name = func.coalesce(workflows.c.name, runs.c.workflow_name)
    from_runs = conn.execute(select(name).distinct()
                             .select_from(runs.outerjoin(workflows, workflows.c.id == runs.c.workflow_id))
                             .where(rbac.scope_clause(scope, runs.c.team_id, runs.c.project_id))).scalars()
    defined = select(workflows.c.name).distinct()
    if not scope.all:
        defined = defined.where(or_(workflows.c.team_id.is_(None), workflows.c.team_id.in_(scope.visible_team_ids)))
    return sorted({n for n in (*from_runs, *conn.execute(defined).scalars()) if n})


def _mode(trigger_ref: Any) -> str | None:
    return trigger_ref.get("mode") if isinstance(trigger_ref, dict) else None


def _row(summary: dict[str, Any], show_cost: bool) -> dict[str, Any]:
    row = list_row(summary)
    if not show_cost:
        row["cost_usd"] = None
    return row


def _cost(value: Any) -> float:
    return float(value) if value is not None else 0.0


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _alerts(window: list[dict[str, Any]], errors: list[Any], since: datetime) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    failed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lost: list[dict[str, Any]] = []
    for run in window:
        if run["started_at"] < since:
            continue
        if run["status"] == "failed":
            failed[run["workflow"] or "unnamed workflow"].append(run)
        elif run["status"] == "lost":
            lost.append(run)
    for workflow, group in sorted(failed.items(), key=lambda item: -len(item[1])):
        latest = max(group, key=lambda r: r["started_at"])
        alerts.append({"kind": "failed", "workflow": workflow, "count": len(group), "run_id": latest["id"],
                       "message": f"{_plural(len(group), 'run')} of {workflow} failed in the last 24 hours."})
    if lost:
        latest = max(lost, key=lambda r: r["started_at"])
        alerts.append({"kind": "lost", "count": len(lost), "run_id": latest["id"],
                       "message": f"{_plural(len(lost), 'run')} stopped reporting in the last 24 hours and "
                                  "were marked lost."})
    texts = [(r.run_id, r.started_at, r.error) for r in errors]
    texts += [(r["id"], r["started_at"], r["error"]) for r in window if r["started_at"] >= since and r["error"]]
    matched = [(run_id, at) for run_id, at, text in texts if CREDENTIAL.search(text)]
    if matched:
        run_ids = {run_id for run_id, _ in matched}
        latest = max(matched, key=lambda item: item[1])[0]
        alerts.append({"kind": "credential", "count": len(run_ids), "run_id": latest,
                       "message": f"{_plural(len(run_ids), 'run')} hit an authentication or credential error in "
                                  "the last 24 hours. Check the provider's credential."})
    return alerts


def overview(conn: Connection, scope: rbac.Scope, team: str | None, now: datetime,
             settings: dict[str, Any]) -> dict[str, Any]:
    zone = ZoneInfo(settings["timezone"])
    today = local_day(now, zone)
    today_start = day_start(today, zone)
    week_start = day_start(today - timedelta(days=7), zone)
    since_24h = now - timedelta(hours=24)
    columns = run_query().add_columns(runs.c.trigger_ref)
    window = [summary_of(r) for r in conn.execute(
        _scoped(columns, scope, team).where((runs.c.started_at >= week_start) | runs.c.status.in_(("running", "queued")))
    ).all()]
    hidden = display.hidden(settings)
    recent_query = _scoped(run_query(), scope, team)
    visible = shown(func.coalesce(workflows.c.name, runs.c.workflow_name), hidden)
    if visible is not None:
        recent_query = recent_query.where(visible)
    recent = [summary_of(r) for r in conn.execute(
        recent_query.order_by(runs.c.started_at.desc(), runs.c.id.desc()).limit(RECENT)).all()]
    steps = conn.execute(_scoped(
        select(run_steps.c.started_at, run_steps.c.finished_at)
        .select_from(run_steps.join(runs, runs.c.id == run_steps.c.run_id))
        .where(run_steps.c.parent_step.is_(None), run_steps.c.finished_at.is_not(None)), scope, team)).all()
    errors = conn.execute(_scoped(
        select(run_steps.c.run_id, runs.c.started_at, run_steps.c.error)
        .select_from(run_steps.join(runs, runs.c.id == run_steps.c.run_id))
        .where(runs.c.started_at >= since_24h, run_steps.c.error.is_not(None)), scope, team)).all()

    show_cost = settings["show_cost"]
    running = sorted((r for r in window if r["status"] == "running" and r["workflow"] not in hidden),
                     key=lambda r: r["started_at"], reverse=True)
    in_24h = [r for r in window if r["started_at"] >= since_24h]
    started_today = [r for r in window if r["started_at"] >= today_start]
    prior_week = [r for r in window if week_start <= r["started_at"] < today_start]
    agent = {"today": 0.0, "7d": 0.0, "all": 0.0}
    week_ago = now - timedelta(days=7)
    for step in steps:
        took = seconds(step.started_at, step.finished_at) or 0.0
        agent["all"] += took
        if step.started_at is not None and step.started_at >= week_ago:
            agent["7d"] += took
        if step.started_at is not None and step.started_at >= today_start:
            agent["today"] += took
    multiplier = settings["human_multiplier"]
    show_hours = settings["show_man_hours"]
    return {
        "running": [_row(r, show_cost) for r in running],
        "counts": {"running": sum(1 for r in window if r["status"] == "running"), "runs_24h": len(in_24h),
                   "failed_24h": sum(1 for r in in_24h if r["status"] in FAILED)},
        "spend": {"today": round(sum(_cost(r["cost_usd"]) for r in started_today), 6),
                  "avg_7d": round(sum(_cost(r["cost_usd"]) for r in prior_week) / 7, 6)} if show_cost else None,
        "reviews_today": sum(1 for r in started_today
                             if r["workflow"] in REVIEW_WORKFLOWS and _mode(r["trigger_ref"]) != DISCUSS),
        "reevaluations_today": sum(1 for r in started_today if _mode(r["trigger_ref"]) == DISCUSS),
        "time": {
            "agent_today": round(agent["today"], 1), "agent_7d": round(agent["7d"], 1),
            "agent_all": round(agent["all"], 1),
            **{f"man_hours_{span}": man_hours(agent[span], multiplier) if show_hours else None
               for span in ("today", "7d", "all")},
        },
        "recent": [_row(r, show_cost) for r in recent],
        "alerts": _alerts(window, errors, since_24h),
        "timezone": settings["timezone"],
    }


def _series(names: Iterable[str]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for name in names:
        counts[name] += 1
    ranked = sorted(counts, key=lambda name: (-counts[name], name))
    kept = ranked if len(ranked) <= SERIES_MAX + 1 else ranked[:SERIES_MAX]
    return kept + ([OTHER] if len(ranked) > len(kept) else [])


def rounded(values: list[float | None], digits: int = 1) -> list[float | None]:
    return [round(v, digits) if v is not None else None for v in values]


def _durations(per_day: list[list[float]]) -> dict[str, Any]:
    flat = [v for day in per_day for v in day]
    return {"n": len(flat), "p50": rounded([percentile(flat, 0.5)])[0], "p90": rounded([percentile(flat, 0.9)])[0],
            "daily_p50": rounded([percentile(day, 0.5) for day in per_day]),
            "daily_p90": rounded([percentile(day, 0.9) for day in per_day])}


def trends(conn: Connection, scope: rbac.Scope, team: str | None, now: datetime, days: int,
           settings: dict[str, Any]) -> dict[str, Any]:
    zone = ZoneInfo(settings["timezone"])
    today = local_day(now, zone)
    day_list = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    index = {day: i for i, day in enumerate(day_list)}
    since = day_start(day_list[0], zone)
    name = func.coalesce(workflows.c.name, runs.c.workflow_name).label("workflow")
    joined = runs.outerjoin(workflows, workflows.c.id == runs.c.workflow_id)
    run_rows = conn.execute(_scoped(
        select(runs.c.id, name, runs.c.status, runs.c.started_at, runs.c.finished_at, runs.c.cost_usd,
               runs.c.summary).select_from(joined).where(runs.c.started_at >= since), scope, team)).all()
    step_rows = conn.execute(_scoped(
        select(run_steps.c.run_id, run_steps.c.step_id, run_steps.c.parent_step, run_steps.c.started_at,
               run_steps.c.finished_at, run_steps.c.cost_usd, name)
        .select_from(run_steps.join(joined, runs.c.id == run_steps.c.run_id))
        .where(runs.c.started_at >= since), scope, team)).all()

    series = _series(r.workflow or OTHER for r in run_rows)
    kept = set(series) - {OTHER}

    def series_of(workflow: str | None) -> str:
        return workflow if workflow in kept else OTHER

    def blank() -> list[Any]:
        return [0] * days

    spend: dict[str, list[float]] = {s: [0.0] * days for s in series}
    counts: dict[str, list[int]] = {s: blank() for s in series}
    finished, failed = blank(), blank()
    verdicts: dict[str, list[int]] = {}
    durations: dict[str, list[list[float]]] = {s: [[] for _ in day_list] for s in series}
    for run in run_rows:
        i = index.get(local_day(run.started_at, zone))
        if i is None:
            continue
        s = series_of(run.workflow)
        spend[s][i] += _cost(run.cost_usd)
        counts[s][i] += 1
        if run.status in FINISHED:
            finished[i] += 1
            failed[i] += run.status in FAILED
            took = seconds(run.started_at, run.finished_at)
            if took is not None and run.status != "canceled":
                durations[s][i].append(took)
        verdict = run.summary.get("verdict") if isinstance(run.summary, dict) else None
        if isinstance(verdict, str) and verdict:
            verdicts.setdefault(verdict[:64], blank())[i] += 1

    slow: dict[tuple[str, str], list[float]] = defaultdict(list)
    roles: dict[str, dict[str, Any]] = {}
    agent = [0.0] * days
    for step in step_rows:
        took = seconds(step.started_at, step.finished_at)
        if step.parent_step is None:
            if took is None:
                continue
            slow[(step.workflow or OTHER, step.step_id)].append(took)
            i = index.get(local_day(step.started_at, zone))
            if i is not None:
                agent[i] += took
        elif step.parent_step == FAN_OUT_PARENT:
            role = roles.setdefault(step.step_id, {"role": step.step_id, "cost": 0.0, "n": 0, "took": []})
            role["cost"] += _cost(step.cost_usd)
            role["n"] += 1
            if took is not None:
                role["took"].append(took)

    show_cost = settings["show_cost"]
    multiplier = settings["human_multiplier"]

    slowest = sorted(({"workflow": wf, "step": step, "n": len(values), "p50": round(percentile(values, 0.5), 1),
                       "p90": round(percentile(values, 0.9), 1)} for (wf, step), values in slow.items()),
                     key=lambda row: -row["p90"])[:SLOWEST]
    return {
        "days": [day.isoformat() for day in day_list],
        "timezone": settings["timezone"],
        "workflows": series,
        "spend_by_workflow": {s: [round(v, 4) for v in values] for s, values in spend.items()} if show_cost else None,
        "runs_by_workflow": counts,
        "failure_rate": [{"finished": f, "failed": x, "rate": round(x / f, 4) if f else None}
                         for f, x in zip(finished, failed, strict=True)],
        "duration_p50_p90_by_workflow": {s: _durations(per_day) for s, per_day in durations.items()},
        "slowest_steps": slowest,
        "verdicts_per_day": verdicts,
        "cost_by_role": sorted(({"role": r["role"], "n": r["n"],
                                 "cost": round(r["cost"], 4) if show_cost else None,
                                 "p50": rounded([percentile(r["took"], 0.5)])[0],
                                 "p90": rounded([percentile(r["took"], 0.9)])[0]} for r in roles.values()),
                               key=lambda row: (-(row["cost"] or 0), -row["n"], row["role"])),
        "agent_hours_per_day": [round(v / 3600, 2) for v in agent],
        "man_hours_per_day": [man_hours(v, multiplier) for v in agent] if settings["show_man_hours"] else None,
    }

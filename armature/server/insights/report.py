"""The daily report: what Armature learned on one local day across every workflow, and what changed in Armature and in
each workflow's run configuration.

Every query is bounded by the day's start and the next day's start in the display time zone, so a DST day is 23 or 25
hours. Cost comes from runs.cost_usd, shown even where display.show_cost hides it, since only an admin reads the
report. llm_calls only breaks that cost down by model and is never added to it. Free text is redacted before it's
stored, and nothing here calls a model.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, func, select

from armature.server import display, rbac
from armature.server.config import ReportsConfig
from armature.server.db.schema import (
    audit,
    daily_reports,
    documents,
    findings,
    gh_pull_requests,
    gh_repos,
    llm_calls,
    run_artifacts,
    run_steps,
    runs,
    teams,
    users,
    versions,
    workflows,
)
from armature.server.history import OPEN_RUNS
from armature.server.ingest import redact
from armature.server.insights import arms
from armature.server.stats import (
    FAILED,
    FINISHED,
    day_start,
    percentile,
    seconds,
    trends,
)

VERSION = 1
# The day's last runs finish before the build: run p90 is about 16 minutes.
MARGIN = timedelta(hours=2)
DAYS = 8
TOP = 10
ERROR_MAX = 200
LIST_MAX = 100
DEPLOY_LINES = 50
DEPLOY_EVENTS = ("deploy ", "refused ", "live: ", "schema ")
CONFIG_ACTIONS = ("trigger.create", "trigger.update", "trigger.delete", "webhook.rule_create", "webhook.rule_update",
                  "webhook.rule_delete", "setting.set", "setting.delete")
SKIP_DIRS = frozenset({"secrets", ".git", "__pycache__"})
SKIP_FILES = frozenset({"ops.env"})
SUBJECT = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]{1,9})$")
SHA = re.compile(r"^[0-9a-f]{40}$")
VOLATILE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{7,40}\b|\d+",
                      re.IGNORECASE)
CATCH_UP = 7
ADMIN = rbac.Scope(all=True, team_ids=frozenset(), project_ids=frozenset(), visible_team_ids=frozenset())


def target_day(now: datetime, zone: ZoneInfo) -> date:
    """The previous local day."""
    return now.astimezone(zone).date() - timedelta(days=1)


def candidate_days(now: datetime, zone: ZoneInfo) -> list[date]:
    """The last CATCH_UP local days, oldest first, so a day missed while the server was down still gets built."""
    last = target_day(now, zone)
    return [last - timedelta(days=i) for i in range(CATCH_UP - 1, -1, -1)]


def due(now: datetime, day: date, zone: ZoneInfo) -> bool:
    return now >= day_start(day + timedelta(days=1), zone) + MARGIN


def _clean(value: Any) -> Any:
    return redact(value, ())


def _money(value: Any) -> float:
    return round(float(value), 6) if value is not None else 0.0


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() if at is not None else None


def _local(at: datetime | None, zone: ZoneInfo) -> str | None:
    if at is None:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(zone).isoformat()


def pr_link(subject: str | None) -> dict[str, Any] | None:
    """The repository and number a subject names, and its GitHub address built from only those two."""
    found = SUBJECT.match(subject or "")
    if not found:
        return None
    repo, number = found.group(1), int(found.group(2))
    return {"repo": repo, "number": number, "url": f"https://github.com/{repo}/pull/{number}"}


def _runs(conn: Connection, start: datetime, end: datetime) -> list[Any]:
    name = func.coalesce(workflows.c.name, runs.c.workflow_name).label("workflow")
    return conn.execute(
        select(runs.c.id, name, runs.c.status, runs.c.started_at, runs.c.finished_at, runs.c.cost_usd, runs.c.subject,
               runs.c.trigger_ref)
        .select_from(runs.outerjoin(workflows, workflows.c.id == runs.c.workflow_id))
        .where(runs.c.started_at >= start, runs.c.started_at < end, arms.not_arm(runs.c.id))).all()


def _compare(values: list[float]) -> dict[str, Any]:
    before = values[:-1]
    return {"value": values[-1], "previous": values[-2] if len(values) > 1 else None,
            "avg_7d": round(sum(before) / len(before), 2) if before else None}


def headline(conn: Connection, day: date, settings: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    zone = ZoneInfo(settings["timezone"])
    found = trends(conn, ADMIN, None, day_start(day, zone), DAYS, settings)
    order = found["workflows"]
    by_day = found["runs_by_workflow"]
    spend = found["spend_by_workflow"] or {}
    run_counts = [sum(by_day[s][i] for s in order) for i in range(DAYS)]
    costs = [round(sum(spend.get(s, [0.0] * DAYS)[i] for s in order), 4) for i in range(DAYS)]
    failed = [day_rate["failed"] for day_rate in found["failure_rate"]]
    finished = [day_rate["finished"] for day_rate in found["failure_rate"]]
    durations = found["duration_p50_p90_by_workflow"]
    by_workflow = sorted(({"workflow": s, "runs": by_day[s][-1], "cost_usd": spend.get(s, [0.0] * DAYS)[-1],
                           "p50": durations[s]["daily_p50"][-1], "p90": durations[s]["daily_p90"][-1]}
                          for s in order if by_day[s][-1]), key=lambda row: (-row["cost_usd"], -row["runs"]))
    return {"runs": _compare(run_counts), "cost_usd": _compare(costs), "failed": _compare(failed),
            "finished": finished[-1], "by_workflow": by_workflow, "models": _models(conn, start, end),
            "series": {"days": found["days"], "workflows": order, "runs": run_counts, "cost_usd": costs,
                       "runs_by_workflow": by_day, "spend_by_workflow": spend}}


def _models(conn: Connection, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """What each model's calls cost, for runs started in the window. A breakdown only, never added to run cost."""
    rows = conn.execute(
        select(llm_calls.c.model_id, llm_calls.c.cost_usd, llm_calls.c.input_tokens, llm_calls.c.output_tokens)
        .select_from(llm_calls.join(runs, runs.c.id == llm_calls.c.run_id))
        .where(runs.c.started_at >= start, runs.c.started_at < end, arms.not_arm(llm_calls.c.run_id))).all()
    models: dict[str, dict[str, Any]] = {}
    for r in rows:
        model = models.setdefault(r.model_id or "unknown", {"model": r.model_id or "unknown", "calls": 0,
                                                             "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0})
        model["calls"] += 1
        model["cost_usd"] += _money(r.cost_usd)
        model["input_tokens"] += r.input_tokens or 0
        model["output_tokens"] += r.output_tokens or 0
    for model in models.values():
        model["cost_usd"] = round(model["cost_usd"], 4)
    return sorted(models.values(), key=lambda m: (-m["cost_usd"], -m["calls"], m["model"]))


def error_pattern(text: str) -> str:
    """The error's first line, redacted, with ids and numbers folded so the same failure groups together."""
    first = next((line.strip() for line in _clean(text).splitlines() if line.strip()), "")
    return re.sub(r"\s+", " ", VOLATILE.sub("#", first))[:ERROR_MAX]


def _errors(conn: Connection, start: datetime, end: datetime) -> list[dict[str, Any]]:
    name = func.coalesce(workflows.c.name, runs.c.workflow_name).label("workflow")
    rows = conn.execute(
        select(run_steps.c.run_id, run_steps.c.step_id, run_steps.c.error, name)
        .select_from(run_steps.join(runs, runs.c.id == run_steps.c.run_id)
                     .outerjoin(workflows, workflows.c.id == runs.c.workflow_id))
        .where(runs.c.started_at >= start, runs.c.started_at < end, run_steps.c.error.is_not(None),
               arms.not_arm(runs.c.id))).all()
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        pattern = error_pattern(r.error)
        if not pattern:
            continue
        group = groups.setdefault(pattern, {"pattern": pattern, "count": 0, "runs": set(), "workflows": set(),
                                            "steps": set(), "run_id": r.run_id})
        group["count"] += 1
        group["runs"].add(r.run_id)
        group["workflows"].add(r.workflow or "unnamed workflow")
        group["steps"].add(r.step_id)
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["pattern"]))[:TOP]
    return [{**g, "runs": len(g["runs"]), "workflows": sorted(g["workflows"]), "steps": sorted(g["steps"])[:TOP]}
            for g in ranked]


def _run_row(r: Any) -> dict[str, Any]:
    return {"run_id": r.id, "workflow": r.workflow, "subject": _clean(r.subject), "status": r.status,
            "seconds": seconds(r.started_at, r.finished_at), "cost_usd": _money(r.cost_usd)}


def _dry(conn: Connection, day_runs: list[Any]) -> dict[str, Any]:
    """Dry runs that succeeded and left no finding and no artifact."""
    dry = [r for r in day_runs if isinstance(r.trigger_ref, dict) and r.trigger_ref.get("dry_run") is True]
    done = [r.id for r in dry if r.status == "succeeded"]
    produced: set[str] = set()
    if done:
        produced = set(conn.execute(select(findings.c.run_id).where(findings.c.run_id.in_(done))).scalars())
        produced |= set(conn.execute(select(run_artifacts.c.run_id).where(run_artifacts.c.run_id.in_(done)))
                        .scalars())
    empty: dict[str, int] = defaultdict(int)
    for r in dry:
        if r.status == "succeeded" and r.id not in produced:
            empty[r.workflow or "unnamed workflow"] += 1
    return {"runs": len(dry), "empty": sum(empty.values()),
            "by_workflow": [{"workflow": w, "runs": n} for w, n in sorted(empty.items(), key=lambda i: (-i[1], i[0]))]}


def findings_section(conn: Connection, start: datetime, end: datetime, day_runs: list[Any]) -> dict[str, Any]:
    per: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0, "failed": 0, "canceled": 0})
    for r in day_runs:
        counts = per[r.workflow or "unnamed workflow"]
        counts["runs"] += 1
        counts["failed"] += r.status in FAILED
        counts["canceled"] += r.status == "canceled"
    failing = sorted(({"workflow": w, **c} for w, c in per.items() if c["failed"]),
                     key=lambda row: (-row["failed"], row["workflow"]))[:TOP]
    timed = [r for r in day_runs if r.status in FINISHED and r.status != "canceled" and r.finished_at is not None]
    slowest = sorted(timed, key=lambda r: -(seconds(r.started_at, r.finished_at) or 0))[:5]
    costliest = sorted((r for r in day_runs if r.cost_usd), key=lambda r: -float(r.cost_usd))[:5]
    took = [seconds(r.started_at, r.finished_at) or 0.0 for r in timed]
    return {"errors": _errors(conn, start, end), "failing": failing, "p90_seconds": percentile(took, 0.9),
            "slowest": [_run_row(r) for r in slowest], "costliest": [_run_row(r) for r in costliest],
            "dry": _dry(conn, day_runs), "churn": churn(conn, start, end)}


def _titles(conn: Connection, links: Iterable[dict[str, Any]]) -> dict[tuple[str, int], str]:
    wanted = {(link["repo"].lower(), link["number"]) for link in links}
    if not wanted:
        return {}
    rows = conn.execute(
        select(func.lower(gh_repos.c.full_name).label("repo"), gh_pull_requests.c.number, gh_pull_requests.c.title)
        .select_from(gh_pull_requests.join(gh_repos, gh_repos.c.id == gh_pull_requests.c.repo_row_id))
        .where(func.lower(gh_repos.c.full_name).in_({repo for repo, _ in wanted}),
               gh_pull_requests.c.number.in_({number for _, number in wanted}))).all()
    return {(r.repo, r.number): r.title for r in rows if (r.repo, r.number) in wanted and r.title}


def churn(conn: Connection, start: datetime, end: datetime) -> dict[str, Any]:
    """Review rounds per pull request, and the day's findings by blocking, repeat, and late catch.

    A repeat restates an earlier finding on its pull request (lineage_key names another key). A late catch is new in
    round 2 or later (its lineage starts there). Dropped findings don't count.
    """
    rows = conn.execute(select(runs.c.id, runs.c.subject, runs.c.review_round).where(
        runs.c.started_at >= start, runs.c.started_at < end, runs.c.review_round.is_not(None),
        arms.not_arm(runs.c.id))).all()
    prs: dict[str, dict[str, Any]] = {}
    for r in rows:
        pr = prs.setdefault(r.subject or "", {"subject": r.subject, "rounds": 0, "runs": 0})
        pr["rounds"] = max(pr["rounds"], r.review_round)
        pr["runs"] += 1
    listed = sorted(prs.values(), key=lambda p: (-p["rounds"], p["subject"] or ""))[:TOP]
    for pr in listed:
        pr["link"] = pr_link(pr["subject"])
        pr["subject"] = _clean(pr["subject"])
    titles = _titles(conn, [pr["link"] for pr in listed if pr["link"]])
    for pr in listed:
        link = pr["link"]
        pr["title"] = _clean(titles.get((link["repo"].lower(), link["number"]))) if link else None
    found = conn.execute(
        select(findings.c.finding_key, findings.c.lineage_key, findings.c.blocking, runs.c.review_round)
        .select_from(findings.join(runs, runs.c.id == findings.c.run_id))
        .where(runs.c.started_at >= start, runs.c.started_at < end, arms.not_arm(runs.c.id),
               findings.c.decision.is_(None) | (findings.c.decision != "drop"))).all()
    rounds = [p["rounds"] for p in prs.values()]
    return {
        "prs": len(prs), "multi_round": sum(1 for n in rounds if n >= 2), "max_rounds": max(rounds, default=None),
        "avg_rounds": round(sum(rounds) / len(rounds), 2) if rounds else None, "top": listed,
        "findings": len(found), "blocking": sum(1 for f in found if f.blocking is True),
        "non_blocking": sum(1 for f in found if f.blocking is False),
        "repeats": sum(1 for f in found if f.lineage_key and f.lineage_key != f.finding_key),
        "late": sum(1 for f in found if (f.review_round or 0) >= 2 and f.lineage_key == f.finding_key),
    }


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _pct(part: float, whole: float) -> int:
    return round(part / whole * 100) if whole else 0


def _vs(now: float, before: float | None) -> str:
    if before is None or before == now:
        return "the same as" if before is not None else ""
    if not before:
        return "up from 0 on"
    return f"{'up' if now > before else 'down'} {_pct(abs(now - before), before)}% on"


def learned(head: dict[str, Any], found: dict[str, Any]) -> list[dict[str, Any]]:
    """Plain takeaways, each citing its numbers. Deterministic, so the same day always reads the same."""
    out: list[dict[str, Any]] = []
    runs_now, avg = head["runs"]["value"], head["runs"]["avg_7d"]
    if runs_now:
        out.append({"section": "headline", "text": f"{runs_now} runs, {_vs(runs_now, avg)} the 7-day average of "
                                                    f"{avg}." if avg is not None else f"{runs_now} runs."})
    cost = head["cost_usd"]["value"]
    if cost and head["by_workflow"]:
        top = head["by_workflow"][0]
        out.append({"section": "headline", "text": f"${cost:,.2f} spent, {_pct(top['cost_usd'], cost)}% of it on "
                                                    f"{top['workflow']} (${top['cost_usd']:,.2f} over {top['runs']} "
                                                    f"runs)."})
    failed, finished = head["failed"]["value"], head["finished"]
    if finished:
        out.append({"section": "findings", "text": f"{failed} of {finished} finished runs failed or were lost "
                                                    f"({_pct(failed, finished)}%)."})
    for error in found["errors"][:3]:
        out.append({"section": "findings", "text": f"\"{error['pattern']}\" came up {_n(error['count'], 'time')} in "
                                                    f"{_n(error['runs'], 'run')} of {', '.join(error['workflows'])}."})
    dry = found["dry"]
    if dry["empty"]:
        out.append({"section": "findings", "text": f"{dry['empty']} of {dry['runs']} dry runs produced no finding "
                                                    "and no artifact."})
    review = found["churn"]
    if review["prs"]:
        out.append({"section": "churn", "text": f"{review['multi_round']} of {review['prs']} reviewed pull requests "
                                                 f"went past one round, up to {review['max_rounds']}."})
    if review["findings"]:
        out.append({"section": "churn", "text": f"{review['late']} of {review['findings']} findings were late "
                                                 f"catches and {review['repeats']} were repeats. "
                                                 f"{review['blocking']} blocked."})
    return out


def _read(path: str | None, limit: int = 1 << 20) -> str | None:
    if not path:
        return None
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        size = os.fstat(fd).st_size
        handle.seek(max(0, size - limit))
        return handle.read(limit).decode("utf-8", "replace")


def deploys(cfg: ReportsConfig, start: datetime, end: datetime, zone: ZoneInfo) -> dict[str, Any]:
    """The deploy log's lines for the window, read in the display zone, and the live commit. Never commit dates."""
    lines: list[dict[str, Any]] = []
    for line in (_read(cfg.deploy_log) or "").splitlines():
        try:
            at = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone)
        except ValueError:
            continue
        text = line[19:].strip()
        if start <= at < end and text.startswith(DEPLOY_EVENTS):
            lines.append({"at": at.isoformat(), "line": _clean(text)[:ERROR_MAX]})
    live = (_read(cfg.live_sha, 128) or "").strip()
    return {"lines": lines[-DEPLOY_LINES:], "live_sha": live if SHA.match(live) else None}


def _people(conn: Connection, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = conn.execute(select(users.c.id, users.c.username, users.c.display_name).where(users.c.id.in_(ids))).all()
    return {r.id: r.display_name or r.username or "someone" for r in rows}


def _versions(conn: Connection, start: datetime, end: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    rows = conn.execute(select(versions.c.object_kind, versions.c.object_id, versions.c.version, versions.c.author_id,
                               versions.c.created_at, versions.c.note)
                        .where(versions.c.created_at >= start, versions.c.created_at < end)
                        .order_by(versions.c.created_at)).all()
    ids = {r.object_id for r in rows}
    names = {r.id: r.name for r in conn.execute(select(workflows.c.id, workflows.c.name)
                                                .where(workflows.c.id.in_(ids)))} if ids else {}
    names |= {r.id: f"{r.kind} {r.key}" for r in conn.execute(
        select(documents.c.id, documents.c.kind, documents.c.key).where(documents.c.id.in_(ids)))} if ids else {}
    people = _people(conn, {r.author_id for r in rows if r.author_id})
    return [{"kind": r.object_kind, "name": names.get(r.object_id, r.object_id), "version": r.version,
             "author": people.get(r.author_id) if r.author_id else None, "at": _local(r.created_at, zone),
             "note": _clean(r.note)} for r in rows][-LIST_MAX:]


def changed_keys(action: str, detail: Any) -> list[str]:
    """The names of what an audit row says changed, never the values."""
    if not isinstance(detail, dict):
        return []
    keys = [k for k in detail.get("changed") or [] if isinstance(k, str)]
    if action == "trigger.update" and isinstance(detail.get("config"), list):
        keys += [f"config.{k}" for k in detail["config"] if isinstance(k, str)]
    if not keys and isinstance(detail.get("key"), str):
        keys = [detail["key"]]
    return sorted(set(keys))


def _config(conn: Connection, start: datetime, end: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    rows = conn.execute(select(audit.c.at, audit.c.action, audit.c.actor_id, audit.c.team_id, audit.c.target_id,
                               audit.c.detail)
                        .where(audit.c.at >= start, audit.c.at < end, audit.c.action.in_(CONFIG_ACTIONS))
                        .order_by(audit.c.at)).all()
    team_ids = {r.team_id for r in rows if r.team_id}
    team_names = {r.id: r.name for r in conn.execute(select(teams.c.id, teams.c.name)
                                                     .where(teams.c.id.in_(team_ids)))} if team_ids else {}
    people = _people(conn, {r.actor_id for r in rows if r.actor_id})
    return [{"at": _local(r.at, zone), "action": r.action, "team": team_names.get(r.team_id) if r.team_id else None,
             "actor": people.get(r.actor_id) if r.actor_id else None, "target": _clean(r.target_id),
             "keys": _clean(changed_keys(r.action, r.detail))} for r in rows][-LIST_MAX:]


def _digest(path: Path) -> str | None:
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(roots: Iterable[str]) -> dict[str, str]:
    """Each file's sha256 under the configured roots, keyed by root name and relative path. Symlinks are skipped, and
    ops.env and secrets/ always are."""
    out: dict[str, str] = {}
    for root in roots:
        base = Path(root).expanduser()
        try:
            if not stat.S_ISDIR(os.lstat(base).st_mode):
                continue
        except OSError:
            continue
        for here, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                if name in SKIP_FILES:
                    continue
                path = Path(here) / name
                digest = _digest(path)
                if digest:
                    out[f"{base.name}/{path.relative_to(base).as_posix()}"] = digest
    return out


def previous_fingerprint(conn: Connection, day: date) -> dict[str, str] | None:
    """The newest earlier report's fingerprint, or None when this is the first."""
    data = conn.execute(select(daily_reports.c.data).where(daily_reports.c.day < day)
                        .order_by(daily_reports.c.day.desc()).limit(1)).scalar()
    found = data.get("fingerprint") if isinstance(data, dict) else None
    return found if isinstance(found, dict) else None


def fingerprint_diff(before: dict[str, str] | None, now: dict[str, str]) -> dict[str, Any]:
    if before is None:
        return {"baseline": True, "files": len(now), "added": [], "removed": [], "changed": []}
    return {"baseline": False, "files": len(now), "added": sorted(set(now) - set(before))[:LIST_MAX],
            "removed": sorted(set(before) - set(now))[:LIST_MAX],
            "changed": sorted(p for p in set(now) & set(before) if now[p] != before[p])[:LIST_MAX]}


def build(conn: Connection, day: date, cfg: ReportsConfig) -> dict[str, Any]:
    """The report for ``day``, every key present even when its section is empty."""
    settings = {**display.load(conn), "show_cost": True}
    zone = ZoneInfo(settings["timezone"])
    start, end = day_start(day, zone), day_start(day + timedelta(days=1), zone)
    day_runs = _runs(conn, start, end)
    head = headline(conn, day, settings, start, end)
    found = findings_section(conn, start, end, day_runs)
    prints = fingerprint(cfg.roots)
    return {
        "version": VERSION, "day": day.isoformat(), "zone": settings["timezone"],
        "open_runs": sum(1 for r in day_runs if r.status in OPEN_RUNS),
        "headline": head, "findings": found, "learned": learned(head, found),
        "changes": {"deploys": deploys(cfg, start, end, zone), "versions": _versions(conn, start, end, zone),
                    "config": _config(conn, start, end, zone),
                    "fingerprint": fingerprint_diff(previous_fingerprint(conn, day), prints)},
        "fingerprint": prints,
    }

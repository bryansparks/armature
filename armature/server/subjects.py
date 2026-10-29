"""Subjects: every run about one pull request or issue, grouped into a list row and a history page."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, String, and_, cast, func, or_, select

from armature.server import display, rbac
from armature.server.db.schema import findings, run_artifacts, runs, workflows
from armature.server.ingest import RUN_KIND
from armature.server.threads import PULL_SUBJECT, PULL_URL, links

RUNS_MAX = 500
FINDINGS_MAX = 2000
SEARCH_BATCHES = 10
HEAD_LENGTH = 12
POSTED_LABEL = "Posted result"

WORKFLOW = func.coalesce(workflows.c.name, runs.c.workflow_name)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _scalars(value: Any) -> dict[str, Any] | None:
    """Only the plain values of a reported object, so the page never renders nested data."""
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if isinstance(k, str) and (v is None or isinstance(v, str | int | float | bool))}


def _cost(value: Any) -> float | None:
    return float(value) if value is not None else None


def parse_cursor(before: str | None) -> tuple[datetime, str] | None:
    if not before:
        return None
    stamp, _, subject = before.partition("|")
    at = datetime.fromisoformat(stamp)
    return (at if at.tzinfo else at.replace(tzinfo=UTC)), subject


def make_cursor(at: datetime, subject: str) -> str:
    # Z rather than +00:00, because a bare + in a query string reads as a space.
    return f"{at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}|{subject}"


def _run_query(scope: rbac.Scope):
    return (select(runs.c.id, runs.c.subject, runs.c.team_id, WORKFLOW.label("workflow"), RUN_KIND.label("kind"),
                   runs.c.status, runs.c.started_at, runs.c.finished_at, runs.c.cost_usd, runs.c.summary,
                   runs.c.trigger_ref, runs.c.outcome, runs.c.risk, runs.c.risk_reasons)
            .select_from(runs.outerjoin(workflows, workflows.c.id == runs.c.workflow_id))
            .where(rbac.scope_clause(scope, runs.c.team_id, runs.c.project_id)))


def _repo_number(subject: str, ordered: list[Any]) -> tuple[str | None, int | None]:
    for run in reversed(ordered):
        ref = _dict(run.trigger_ref)
        if _text(ref.get("repo")):
            number = ref.get("number")
            return ref["repo"], number if isinstance(number, int) and not isinstance(number, bool) else None
    match = PULL_SUBJECT.fullmatch(subject)
    return (match["repo"], int(match["number"])) if match else (None, None)


def status_of(ordered: list[Any]) -> str:
    """Merged once a merge run exists, else the latest review verdict, else the latest run's status."""
    if any(run.kind == "merge" for run in ordered):
        return "merged"
    for run in reversed(ordered):
        verdict = _text(_dict(run.summary).get("verdict"))
        if run.kind == "review" and verdict:
            return verdict
    return ordered[-1].status if ordered else "unknown"


def _latest(ordered: list[Any], field: str) -> str | None:
    for run in reversed(ordered):
        value = _text(_dict(run.summary).get(field))
        if value:
            return value
    return None


def pull_states(conn: Connection, scope: rbac.Scope, names: set[str]) -> dict[str, dict[str, Any]]:
    """Each pull request subject's title, latest review verdict, and whether it merged, across its visible runs."""
    pulls = sorted(name for name in names if PULL_SUBJECT.fullmatch(name))
    if not pulls:
        return {}
    by_subject: dict[str, list[Any]] = {name: [] for name in pulls}
    query = _run_query(scope).where(runs.c.subject.in_(pulls)).order_by(runs.c.started_at, runs.c.id)
    for run in conn.execute(query).all():
        by_subject[run.subject].append(run)
    return {name: {"title": _latest(ordered, "title"),
                   "verdict": _latest([run for run in ordered if run.kind == "review"], "verdict"),
                   "merged": any(run.kind == "merge" for run in ordered)}
            for name, ordered in by_subject.items() if ordered}


def summarize(subject: str, ordered: list[Any], hidden: set[str], show_cost: bool) -> dict[str, Any]:
    """One list row. Hidden workflows stay out of the counts and cost, never out of the status."""
    counted = [run for run in ordered if run.workflow not in hidden]
    repo, _ = _repo_number(subject, ordered)
    cost = sum(_cost(run.cost_usd) or 0.0 for run in counted)
    return {
        "subject": subject,
        "title": _latest(ordered, "title"),
        "latest_verdict": _latest(ordered, "verdict"),
        "status": status_of(ordered),
        "runs": len(counted),
        "reviews": sum(1 for run in counted if run.kind == "review"),
        "fixes": sum(1 for run in counted if run.kind == "fix"),
        "cost_usd": round(cost, 6) if show_cost else None,
        "last_activity": ordered[-1].started_at if ordered else None,
        "repo": repo,
        "team_id": ordered[-1].team_id if ordered else None,
    }


def _grouped(conn: Connection, scope: rbac.Scope, team: str | None, q: str,
             cursor: tuple[datetime, str] | None, limit: int) -> list[Any]:
    last = func.max(runs.c.started_at)
    query = (select(runs.c.subject, last.label("last"))
             .where(runs.c.subject.is_not(None), rbac.scope_clause(scope, runs.c.team_id, runs.c.project_id))
             .group_by(runs.c.subject))
    if team:
        query = query.where(runs.c.team_id == team)
    if q:
        # A coarse match on the stored summary. The title check on the grouped row decides.
        query = query.where(or_(func.lower(runs.c.subject).contains(q, autoescape=True),
                                func.lower(cast(runs.c.summary, String)).contains(q, autoescape=True)))
    if cursor is not None:
        at, subject = cursor
        query = query.having(or_(last < at, and_(last == at, runs.c.subject < subject)))
    return conn.execute(query.order_by(last.desc(), runs.c.subject.desc()).limit(limit)).all()


def list_subjects(conn: Connection, scope: rbac.Scope, team: str | None, q: str, before: str | None,
                  limit: int) -> dict[str, Any]:
    settings = display.load(conn)
    hidden = set(display.hidden(settings))
    wanted = q.strip().lower()
    cursor = parse_cursor(before)
    rows: list[dict[str, Any]] = []
    more = False
    for _ in range(SEARCH_BATCHES):
        groups = _grouped(conn, scope, team, wanted, cursor, limit)
        more = len(groups) == limit
        if not groups:
            break
        names = [g.subject for g in groups]
        by_subject: dict[str, list[Any]] = {name: [] for name in names}
        query = _run_query(scope).where(runs.c.subject.in_(names))
        if team:
            query = query.where(runs.c.team_id == team)
        for run in conn.execute(query.order_by(runs.c.started_at, runs.c.id)).all():
            by_subject[run.subject].append(run)
        for index, group in enumerate(groups):
            cursor = (group.last, group.subject)
            row = summarize(group.subject, by_subject[group.subject], hidden, settings["show_cost"])
            if wanted and wanted not in group.subject.lower() and wanted not in (row["title"] or "").lower():
                continue
            rows.append(row)
            if len(rows) == limit:
                more = more or index < len(groups) - 1
                break
        if len(rows) == limit or not more:
            break
    return {"subjects": rows, "next_before": make_cursor(*cursor) if more and cursor is not None else None}


def merged_links(ordered: list[Any]) -> list[dict[str, str]]:
    """Every run's links, latest first. A url already taken is skipped, and so is a label, except on a GitHub pull
    request link, so an issue with an Android and an iOS pull request links to both."""
    shown: list[dict[str, str]] = []
    urls: set[str] = set()
    labels: set[str] = set()
    for run in reversed(ordered):
        summary = _dict(run.summary)
        candidates = links(run.subject, summary)
        if _text(summary.get("post_url")):
            candidates = [*candidates, {"label": POSTED_LABEL, "url": summary["post_url"]}]
        for link in candidates:
            label, url = _text(_dict(link).get("label")), _text(_dict(link).get("url"))
            if not label or not url or not url.startswith("https://") or url in urls:
                continue
            if label in labels and not PULL_URL.fullmatch(url):
                continue
            urls.add(url)
            labels.add(label)
            shown.append({"label": label, "url": url})
    return shown


def first_reason(reasons: Any) -> str | None:
    first = reasons[0] if isinstance(reasons, list) and reasons else None
    return _text(_dict(first).get("text"))


def finding_history(rows: list[Any]) -> list[dict[str, Any]]:
    """One entry per finding key, its dispositions in run order, newest finding first."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = grouped.setdefault(row.finding_key, {"finding_key": row.finding_key, "title": None, "path": None,
                                                     "line": None, "severity": None, "history": []})
        for field in ("title", "path", "line", "severity"):
            value = getattr(row, field)
            if value is not None and value != "":
                entry[field] = value
        entry["history"].append({"run_id": row.run_id, "started_at": row.started_at,
                                 "disposition": row.disposition})
    for entry in grouped.values():
        entry["current_disposition"] = entry["history"][-1]["disposition"]
    return sorted(grouped.values(), key=lambda e: (e["history"][-1]["started_at"], e["finding_key"]), reverse=True)


def subject_detail(conn: Connection, scope: rbac.Scope, subject: str) -> dict[str, Any] | None:
    settings = display.load(conn)
    labels = settings["workflow_labels"]
    # A thread's page lists every run in it, so an issue's page shows the runs on its pull requests too.
    # The newest RUNS_MAX, so a long thread never hides its latest result.
    ordered = conn.execute(_run_query(scope).where(or_(runs.c.subject == subject, runs.c.thread == subject))
                           .order_by(runs.c.started_at.desc(), runs.c.id.desc()).limit(RUNS_MAX)).all()[::-1]
    if not ordered:
        return None
    # The page's own state comes from its own subject's runs, so a merged pull request never marks its issue merged.
    own = ([run for run in ordered if run.subject == subject]
           or [run for run in ordered if run.kind != "merge"] or ordered)
    run_ids = [run.id for run in ordered]
    artifacts: dict[str, list[str]] = {}
    for row in conn.execute(select(run_artifacts.c.run_id, run_artifacts.c.name)
                            .where(run_artifacts.c.run_id.in_(run_ids)).order_by(run_artifacts.c.name)).all():
        artifacts.setdefault(row.run_id, []).append(row.name)
    finding_rows = conn.execute(
        select(findings.c.finding_key, findings.c.title, findings.c.path, findings.c.line, findings.c.severity,
               findings.c.disposition, findings.c.run_id, runs.c.started_at)
        .select_from(findings.join(runs, runs.c.id == findings.c.run_id))
        .where(findings.c.run_id.in_(run_ids))
        .order_by(runs.c.started_at, findings.c.run_id, findings.c.id).limit(FINDINGS_MAX)).all()
    repo, number = _repo_number(subject, own)
    show_cost = settings["show_cost"]

    def row(run: Any) -> dict[str, Any]:
        summary, ref = _dict(run.summary), _dict(run.trigger_ref)
        head = _text(ref.get("head"))
        return {
            "id": run.id, "subject": run.subject, "workflow": run.workflow,
            "workflow_label": _text(_dict(labels.get(run.workflow)).get("label")) or run.workflow,
            "kind": run.kind, "status": run.status, "started_at": run.started_at,
            "finished_at": run.finished_at, "cost_usd": _cost(run.cost_usd) if show_cost else None,
            "title": _text(summary.get("title")), "verdict": _text(summary.get("verdict")),
            "mode": _text(ref.get("mode")), "head": head[:HEAD_LENGTH] if head else None,
            "fields": _scalars(summary.get("fields")), "fix": _scalars(ref.get("fix")),
            "merge": _scalars(ref.get("merge")), "artifacts": artifacts.get(run.id, []),
            "outcome": run.outcome, "risk": run.risk, "risk_reason": first_reason(run.risk_reasons),
        }

    return {
        "subject": subject, "title": _latest(own, "title"), "status": status_of(own),
        "latest_verdict": _latest(own, "verdict"),
        "latest_outcome": next((run.outcome for run in reversed(own) if run.outcome), None),
        "risk": own[-1].risk, "repo": repo, "number": number,
        "links": merged_links(ordered), "runs": [row(run) for run in ordered],
        "findings": finding_history(finding_rows),
    }

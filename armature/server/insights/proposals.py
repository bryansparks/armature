"""The weekly fix proposals: each team's biggest recurring patterns in repositories Armature may push to become fix
proposals, each with a self-heal job, within the team's caps and budget.

A pattern gets one proposal at a time and none within 60 days of its last one. A repository gets at most one a week,
counted from Monday 00:00 UTC. The repository must be tracked, belong to a project whose providers define the
workflow's model aliases, and reach GitHub through a GitHub App whose installation writes contents and pull requests.
A gh CLI login never qualifies, since its token would sit with the user an unattended agent session runs beside.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, func, insert, or_, select, update

from armature.server import (
    audit,
    broker,
    connections,
    conversations,
    defs,
    webhooks,
    worker,
)
from armature.server.db import new_id
from armature.server.db.schema import (
    cluster_members,
    clusters,
    finding_candidates,
    fix_proposals,
    gh_pull_requests,
    gh_repos,
    github_installations,
    llm_calls,
    projects,
    runs,
)
from armature.server.db.schema import connections as connection_rows
from armature.server.insights import fixes, taxonomy
from armature.server.vendors import github as github_vendor

MIN_KEY = "insights.fix_min_flags"
WEEKLY_KEY = "insights.fix_weekly_per_team"
BUDGET_KEY = "insights.fix_weekly_budget_usd"
MIN_FLAGS = 5
WEEKLY = 3
BUDGET_USD = 40.0
REPEAT = timedelta(days=60)
# The model aliases self-heal's agent stages name.
ALIASES = ("develop", "review")

log = logging.getLogger(__name__)
DAYS = 28
PUSHES = ("contents", "pull_requests")


def _number(conn: Connection, key: str, team_id: str, default: float) -> float:
    value = broker.setting(conn, key, team_id, None)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else default


def spent(conn: Connection, team_id: str, workflow_id: str, since: datetime) -> float:
    """What the team's self-heal runs spent on models since ``since``."""
    return float(conn.execute(select(func.coalesce(func.sum(llm_calls.c.cost_usd), 0)).where(
        llm_calls.c.team_id == team_id, llm_calls.c.cached.is_(False), llm_calls.c.created_at >= since,
        llm_calls.c.run_id.in_(select(runs.c.id).where(runs.c.team_id == team_id,
                                                       runs.c.workflow_id == workflow_id)))).scalar() or 0)


def week_start(now: datetime) -> datetime:
    """This week's Monday at 00:00 UTC, which the weekly caps and budget count from."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())


def _can_push(conn: Connection, row: Any, owner: str) -> bool:
    if row.state != "ok" or github_vendor.uses_gh_cli(row.config):
        return False
    permissions = conn.execute(select(github_installations.c.permissions).where(
        github_installations.c.connection_id == row.id, github_installations.c.owner == owner.lower())).scalar()
    have = permissions if isinstance(permissions, dict) else {}
    return all(github_vendor.LEVEL.get(str(have.get(name)), 0) >= github_vendor.LEVEL["write"] for name in PUSHES)


def target(conn: Connection, team_id: str, full_name: str) -> tuple[Any, str, str] | None:
    """The tracked repository, its project, and its default branch, when Armature may push a fix to it."""
    repo = conn.execute(select(gh_repos).where(gh_repos.c.team_id == team_id, gh_repos.c.enabled.is_(True),
                                               func.lower(gh_repos.c.full_name) == full_name.lower())).first()
    if repo is None:
        return None
    project = conn.execute(select(projects.c.id, projects.c.default_branch).where(
        projects.c.id == repo.project_id, func.lower(projects.c.repo) == full_name.lower())).first() \
        if repo.project_id else None
    if project is None:
        found = webhooks.project_for_repo(conn, team_id, full_name)
        project = conn.execute(select(projects.c.id, projects.c.default_branch)
                               .where(projects.c.id == found)).first() if found else None
    if project is None:
        return None
    owner = full_name.split("/")[0]
    if repo.connection_id is not None:
        row = conn.execute(select(connection_rows).where(connection_rows.c.id == repo.connection_id,
                                                         connection_rows.c.team_id == team_id)).first()
    else:
        try:
            row = connections.resolve(conn, team_id=team_id, project_id=project.id, kind="github_app", owner=owner)
        except connections.ConnError:
            row = None
    if row is None or not _can_push(conn, row, owner):
        return None
    base = project.default_branch or conn.execute(select(gh_pull_requests.c.base_ref).where(
        gh_pull_requests.c.team_id == team_id, gh_pull_requests.c.repo_row_id == repo.id,
        gh_pull_requests.c.base_ref.is_not(None)).order_by(gh_pull_requests.c.created_at_gh.desc()).limit(1)).scalar()
    return (repo, project.id, base) if base else None


def _missing_aliases(conn: Connection, team_id: str, project_id: str) -> list[str]:
    return [a for a in ALIASES if defs.resolve_alias(conn, a, team_id, project_id) is None]


def _examples(conn: Connection, team_id: str, cluster_id: str) -> list[dict[str, Any]]:
    fc = finding_candidates.c
    rows = conn.execute(select(fc.path, fc.line, fc.claim).join(
        cluster_members, (cluster_members.c.subject_id == fc.id) & (cluster_members.c.subject_kind == "candidate"))
        .where(cluster_members.c.cluster_id == cluster_id, cluster_members.c.team_id == team_id, fc.team_id == team_id)
        .order_by(fc.created_at.desc(), fc.id).limit(fixes.EXAMPLES_MAX)).all()
    return [{"claim": fixes._line(r.claim, fixes.CLAIM_MAX), "path": fixes._line(r.path, fixes.PATH_MAX) or None,
             "line": r.line} for r in rows]


def propose(engine: Engine, team_id: str, now: datetime) -> dict[str, Any]:
    """Make this week's proposals for the team and queue a self-heal job for each. Returns what it did."""
    with engine.begin() as conn:
        fixes.reconcile(conn, team_id, now)
        workflow_id = conversations.builtin_workflow(conn, fixes.SELF_HEAL, now, kind="fix")
        budget = _number(conn, BUDGET_KEY, team_id, BUDGET_USD)
        week = week_start(now)
        if spent(conn, team_id, workflow_id, week) >= budget:
            return {"proposed": 0, "skipped": "budget"}
        fp = fix_proposals.c
        recent = conn.execute(select(fp.cluster_key, fp.repo_row_id, fp.created_at).where(
            fp.team_id == team_id, or_(fp.created_at >= now - REPEAT, fp.state.in_(fixes.ACTIVE)))).all()
        blocked = {r.cluster_key for r in recent}
        repos_this_week = {r.repo_row_id for r in recent if r.created_at >= week}
        left = int(_number(conn, WEEKLY_KEY, team_id, WEEKLY)) - sum(1 for r in recent if r.created_at >= week)
        floor = _number(conn, MIN_KEY, team_id, MIN_FLAGS)
        found = conn.execute(select(clusters).where(clusters.c.team_id == team_id,
                                                    clusters.c.taxonomy_version == taxonomy.VERSION,
                                                    clusters.c.label != taxonomy.OTHER, clusters.c.size >= floor)
                             .order_by(clusters.c.size.desc(), clusters.c.last_seen.desc(), clusters.c.id)).all()
        made, unmodeled = [], set()
        for cluster in found:
            if left <= 0:
                break
            scope = cluster.scope or {}
            repo_name, area = scope.get("repo"), scope.get("area")
            key = fixes.cluster_key(cluster.taxonomy_version, cluster.label, repo_name, area)
            picked = target(conn, team_id, repo_name) if repo_name and key not in blocked else None
            if picked is None or picked[0].id in repos_this_week:
                continue
            repo, project_id, base = picked
            missing = _missing_aliases(conn, team_id, project_id)
            if missing:
                unmodeled.add((repo.full_name, ", ".join(missing)))
                continue
            made.append(_create(conn, team_id, workflow_id, cluster, key, repo, project_id, base, now))
            blocked.add(key)
            repos_this_week.add(repo.id)
            left -= 1
    for name, missing in sorted(unmodeled):
        log.warning("no fix proposed for %s in team %s: its providers define no model alias %s", name, team_id,
                    missing)
    return {"proposed": len(made), "proposals": made}


def _create(conn: Connection, team_id: str, workflow_id: str, cluster: Any, key: str, repo: Any, project_id: str,
            base: str, now: datetime) -> str:
    scope = cluster.scope or {}
    proposal_id = new_id()
    branch = fixes.branch_of(proposal_id)
    evidence = {"taxonomy_version": cluster.taxonomy_version, "label": cluster.label, "repo": repo.full_name,
                "area": scope.get("area"), "flags": cluster.size, "days": DAYS,
                "examples": _examples(conn, team_id, cluster.id)}
    conn.execute(insert(fix_proposals).values(
        id=proposal_id, team_id=team_id, repo_row_id=repo.id, project_id=project_id, cluster_key=key,
        title=fixes.title_of(cluster.label, repo.full_name, scope.get("area")), rationale=cluster.summary,
        evidence=evidence, state="queued", branch=branch, base=base, created_at=now, updated_at=now))
    job_id = worker.queue_job(conn, team_id=team_id, project_id=project_id, workflow_id=workflow_id,
                              inputs={"proposal_id": proposal_id, "repo": f"https://github.com/{repo.full_name}.git",
                                      "slug": repo.full_name, "ref": base, "branch": branch},
                              now=now, key=f"{fixes.JOB_KEY}{proposal_id}")
    conn.execute(update(fix_proposals).where(fix_proposals.c.id == proposal_id).values(job_id=job_id))
    audit.record(conn, action="insights.fix.propose", actor_id=None, team_id=team_id, target_kind="fix_proposal",
                 target_id=proposal_id, detail={"cluster_key": key, "repo": repo.full_name, "job_id": job_id})
    return proposal_id

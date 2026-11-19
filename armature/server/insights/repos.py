"""Tracked repositories: which GitHub repositories a team collects pull request history for.

Adding one asks GitHub for its numeric ID through the team's GitHub connection, because deliveries name a
repository by ID. That call happens here, never in webhook ingest. A new repository gets one sync state per kind,
due now, so the sync service picks it up. Removing one deletes its whole gh_ history.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import audit, connections, webhooks
from armature.server.crypto import Keyring, SealError
from armature.server.db import new_id
from armature.server.db.schema import (
    GH_SYNC_KINDS,
    gh_check_runs,
    gh_conflict_samples,
    gh_pr_commits,
    gh_pr_files,
    gh_pr_links,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    gh_sync_state,
    github_installations,
    projects,
)
from armature.server.db.schema import (
    connections as connection_rows,
)
from armature.server.insights.people import gh_id
from armature.server.tools import Call, ToolError
from armature.server.tools import github as github_tools
from armature.server.vendors import VendorError
from armature.server.vendors import github as github_vendor

SYNC_KINDS = GH_SYNC_KINDS
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
KIND = "github_app"


class RepoError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class _Found:
    gh_repo_id: int
    full_name: str
    project_id: str | None
    connection_id: str | None


def get(conn: Connection, team_id: str, repo_id: str) -> Any:
    row = conn.execute(select(gh_repos).where(gh_repos.c.id == repo_id, gh_repos.c.team_id == team_id)).first()
    if row is None:
        raise RepoError(404, "No such tracked repository.")
    return row


def listed(conn: Connection, team_id: str) -> list[Any]:
    return conn.execute(select(gh_repos).where(gh_repos.c.team_id == team_id)
                        .order_by(gh_repos.c.full_name, gh_repos.c.id)).all()


def public(conn: Connection, row: Any) -> dict[str, Any]:
    """The tracked repository as the API shows it, with its project, connection, and each sync state."""
    project = conn.execute(select(projects.c.name).where(projects.c.id == row.project_id)).scalar() \
        if row.project_id else None
    connection = conn.execute(select(connection_rows.c.name).where(connection_rows.c.id == row.connection_id)) \
        .scalar() if row.connection_id else None
    states = {s.kind: s for s in conn.execute(select(gh_sync_state).where(gh_sync_state.c.repo_row_id == row.id))}
    return {"id": row.id, "team_id": row.team_id, "gh_repo_id": row.gh_repo_id, "full_name": row.full_name,
            "project_id": row.project_id, "project_name": project, "connection_id": row.connection_id,
            "connection_name": connection, "enabled": row.enabled, "backfill_since": row.backfill_since,
            "created_at": row.created_at, "updated_at": row.updated_at,
            "sync": [{"kind": kind, "next_run_at": states[kind].next_run_at, "watermark": states[kind].watermark,
                      "last_error": states[kind].last_error, "updated_at": states[kind].updated_at}
                     for kind in SYNC_KINDS if kind in states]}


def github_connections(conn: Connection, team_id: str) -> list[dict[str, Any]]:
    return [{"id": r.id, "name": r.name, "state": r.state, "scope_kind": "project" if r.project_id else "team"}
            for r in conn.execute(select(connection_rows).where(connection_rows.c.team_id == team_id,
                                                                connection_rows.c.kind == KIND)
                                  .order_by(connection_rows.c.name))]


def _name(value: Any) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value.strip()):
        raise RepoError(400, "full_name must be owner/name.")
    return value.strip()


def _enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise RepoError(400, "enabled must be true or false.")
    return value


def _since(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise RepoError(400, "backfill_since must be a date such as 2025-05-01.")


def _picked(conn: Connection, team_id: str, connection_id: Any) -> Any:
    row = conn.execute(select(connection_rows).where(connection_rows.c.id == connection_id)).first() \
        if isinstance(connection_id, str) else None
    if row is None or row.team_id != team_id or row.kind != KIND:
        raise RepoError(400, "No such GitHub connection in this team.")
    return row


def connection_for(conn: Connection, team_id: str, connection_id: Any, project_id: str | None, owner: str) -> Any:
    if connection_id is not None:
        row = _picked(conn, team_id, connection_id)
    else:
        try:
            row = connections.resolve(conn, team_id=team_id, project_id=project_id, kind=KIND, owner=owner)
        except connections.ConnError as exc:
            raise RepoError(409, exc.message) from exc
    if row.state != "ok":
        raise RepoError(409, f"Connection {row.name} is not verified. Verify it first.")
    return row


def _reached(conn: Connection, team_id: str, full_name: str) -> bool:
    return full_name.lower() in {r.lower() for r in connections.reaches(conn, team_id)}


def personal(conn: Connection, team_id: str, full_name: str, row: Any) -> bool:
    """Whether syncing ``full_name`` through ``row`` uses a person's gh token beyond the team's project repos."""
    return github_vendor.uses_gh_cli(row.config) and not _reached(conn, team_id, full_name)


def may_sync(conn: Connection, repo: Any, row: Any) -> bool:
    """A personal token syncs a repository outside the team's projects only where an admin pinned it."""
    return not personal(conn, repo.team_id, repo.full_name, row) or repo.connection_id == row.id


def _approved(conn: Connection, team_id: str, full_name: str, row: Any, admin: bool) -> str | None:
    """The connection to pin: a personal token beyond the team's projects needs an admin, and is pinned as approved."""
    if not personal(conn, team_id, full_name, row):
        return None
    if not admin:
        raise RepoError(403, f"Syncing {full_name} with the personal token of connection {row.name} needs an admin, "
                             f"because no project in this team uses the repository.")
    return row.id


def _resolve(engine: Engine, keyring: Keyring, team_id: str, name: str, connection_id: Any, now: datetime,
             transport: httpx.BaseTransport | None, admin: bool) -> _Found:
    """The repository's GitHub ID and canonical name, from one call through the team's GitHub connection."""
    owner, repo = name.split("/")
    with engine.connect() as conn:
        project_id = webhooks.project_for_repo(conn, team_id, name)
        row = connection_for(conn, team_id, connection_id, project_id, owner)
        pinned = _approved(conn, team_id, name, row, admin) or connection_id
        installation = conn.execute(select(github_installations.c.installation_id).where(
            github_installations.c.connection_id == row.id, github_installations.c.owner == owner.lower())).scalar()
    try:
        creds = connections.credentials(keyring, row)
    except (SealError, ValueError) as exc:
        raise RepoError(409, f"The credentials of connection {row.name} can't be opened.") from exc
    call = Call(run_id="", step_id="", connection=row, creds=creds, now=now, installation_id=installation,
                project_id=project_id, transport=transport, team_id=team_id)
    try:
        with github_tools.api(call, name, github_tools.METADATA) as gh:
            found = gh.json("GET", f"/repos/{owner}/{repo}")
    except (VendorError, ToolError) as exc:
        raise RepoError(409, f"Couldn't look up {name} through connection {row.name}: {exc}") from exc
    repo_id = gh_id(found.get("id")) if isinstance(found, dict) else None
    full_name = found.get("full_name") if isinstance(found, dict) else None
    if repo_id is None or not isinstance(full_name, str) or not NAME.fullmatch(full_name):
        raise RepoError(409, f"GitHub's answer for {name} had no repository ID.")
    return _Found(gh_repo_id=repo_id, full_name=full_name.lower(), project_id=project_id, connection_id=pinned)


def _taken(conn: Connection, team_id: str, full_name: str) -> None:
    if conn.execute(select(gh_repos.c.id).where(gh_repos.c.team_id == team_id,
                                                gh_repos.c.full_name == full_name)).first() is not None:
        raise RepoError(409, f"{full_name} is already tracked in this team.")


def track(engine: Engine, keyring: Keyring, *, team_id: str, body: dict[str, Any], actor_id: str, now: datetime,
          ip: str | None, transport: httpx.BaseTransport | None = None, admin: bool = False) -> str:
    """Start tracking a repository by owner/name. Returns the new row's id."""
    name = _name(body.get("full_name"))
    values = {"enabled": _enabled(body.get("enabled", True)), "backfill_since": _since(body.get("backfill_since"))}
    connection_id = body.get("connection_id")
    with engine.connect() as conn:
        _taken(conn, team_id, name.lower())
    found = _resolve(engine, keyring, team_id, name, connection_id, now, transport, admin)
    row_id = new_id()
    with engine.begin() as conn:
        _taken(conn, team_id, found.full_name)
        try:
            with conn.begin_nested():
                conn.execute(insert(gh_repos).values(id=row_id, team_id=team_id, gh_repo_id=found.gh_repo_id,
                                                     full_name=found.full_name, project_id=found.project_id,
                                                     connection_id=found.connection_id, created_at=now,
                                                     updated_at=now,
                                                     **values))
        except IntegrityError as exc:
            raise RepoError(409, f"{found.full_name} is already tracked in this team.") from exc
        conn.execute(insert(gh_sync_state), [{"id": new_id(), "team_id": team_id, "repo_row_id": row_id,
                                              "kind": kind, "next_run_at": now, "updated_at": now}
                                             for kind in SYNC_KINDS])
        audit.record(conn, action="gh_repo.create", actor_id=actor_id, team_id=team_id, target_kind="gh_repo",
                     target_id=row_id, detail={"full_name": found.full_name, "gh_repo_id": found.gh_repo_id,
                                               "connection_id": found.connection_id}, ip=ip)
    return row_id


def update_repo(conn: Connection, row: Any, *, body: dict[str, Any], actor_id: str, now: datetime,
                ip: str | None, admin: bool = False) -> Any:
    values: dict[str, Any] = {}
    if "enabled" in body:
        values["enabled"] = _enabled(body["enabled"])
    if "backfill_since" in body:
        values["backfill_since"] = _since(body["backfill_since"])
    if "connection_id" in body:
        given = body["connection_id"]
        picked = None if given is None else _picked(conn, row.team_id, given)
        if picked is not None:
            _approved(conn, row.team_id, row.full_name, picked, admin)
        values["connection_id"] = None if picked is None else picked.id
    changed = sorted(k for k, v in values.items() if v != getattr(row, k))
    if not changed:
        return row
    conn.execute(update(gh_repos).where(gh_repos.c.id == row.id).values(**values, updated_at=now))
    if "backfill_since" in changed:
        # A finished backfill resumes from the page it stopped at, so a new floor only costs the pages it adds.
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.repo_row_id == row.id,
                                                 gh_sync_state.c.kind == "backfill",
                                                 gh_sync_state.c.next_run_at.is_(None))
                     .values(next_run_at=now, updated_at=now))
    audit.record(conn, action="gh_repo.update", actor_id=actor_id, team_id=row.team_id, target_kind="gh_repo",
                 target_id=row.id, detail={"full_name": row.full_name, "changed": changed}, ip=ip)
    return get(conn, row.team_id, row.id)


def remove(conn: Connection, row: Any, *, actor_id: str, ip: str | None) -> None:
    """Stop tracking a repository and delete every gh_ row it has.

    The row lock first, so a delivery being collected for it finishes before, or finds it gone after.
    """
    conn.execute(select(gh_repos.c.id).where(gh_repos.c.id == row.id).with_for_update())
    prs = select(gh_pull_requests.c.id).where(gh_pull_requests.c.repo_row_id == row.id)
    count = len(conn.execute(prs).all())
    conn.execute(delete(gh_pr_links).where(or_(gh_pr_links.c.from_pr_row_id.in_(prs),
                                               gh_pr_links.c.to_pr_row_id.in_(prs))))
    for table in (gh_conflict_samples, gh_pushes, gh_reviews, gh_review_comments):
        conn.execute(delete(table).where(table.c.pr_row_id.in_(prs)))
    for table in (gh_check_runs, gh_pr_commits, gh_pr_files):
        conn.execute(delete(table).where(or_(table.c.repo_row_id == row.id, table.c.pr_row_id.in_(prs))))
    conn.execute(delete(gh_pull_requests).where(gh_pull_requests.c.repo_row_id == row.id))
    conn.execute(delete(gh_sync_state).where(gh_sync_state.c.repo_row_id == row.id))
    conn.execute(delete(gh_repos).where(gh_repos.c.id == row.id))
    audit.record(conn, action="gh_repo.delete", actor_id=actor_id, team_id=row.team_id, target_kind="gh_repo",
                 target_id=row.id, detail={"full_name": row.full_name, "pull_requests": count}, ip=ip)

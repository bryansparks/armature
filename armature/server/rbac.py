"""Roles, memberships, and what a user may see.

Roles rank admin > team_lead > project_lead > contributor > viewer. A team
membership covers every project in the team, and ``users.is_admin`` covers
everything. Scope always comes from database rows, never from a request body.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Connection, and_, false, or_, select, true
from sqlalchemy.sql.elements import ColumnElement

from armature.server.db.schema import memberships, projects, runs, teams

RANK = {"viewer": 1, "contributor": 2, "project_lead": 3, "team_lead": 4, "admin": 5}
GRANTABLE = ("team_lead", "project_lead", "contributor", "viewer")
# What a team lead may hand out. Team lead itself, like admin, comes only from an admin.
LEAD_GRANTABLE = ("project_lead", "contributor", "viewer")


class NotFound(LookupError):
    """The target object does not exist."""


@dataclass(frozen=True)
class Target:
    """The team and project a route acts on, read from the database. Both None means the instance."""

    team_id: str | None = None
    project_id: str | None = None


INSTANCE = Target()


@dataclass(frozen=True)
class Access:
    is_admin: bool
    teams: dict[str, str] = field(default_factory=dict)
    projects: dict[str, str] = field(default_factory=dict)
    project_teams: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Scope:
    """Rows a user may list: everything, or these teams' rows plus these projects' rows."""

    all: bool
    team_ids: frozenset[str]
    project_ids: frozenset[str]
    visible_team_ids: frozenset[str]


def at_least(role: str | None, min_role: str) -> bool:
    return role is not None and RANK[role] >= RANK[min_role]


def higher(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if RANK[a] >= RANK[b] else b


def load_access(conn: Connection, user_id: str, is_admin: bool) -> Access:
    rows = conn.execute(
        select(memberships.c.scope_kind, memberships.c.scope_id, memberships.c.role, projects.c.team_id)
        .select_from(memberships.outerjoin(projects, and_(memberships.c.scope_kind == "project",
                                                          projects.c.id == memberships.c.scope_id)))
        .where(memberships.c.user_id == user_id)
    ).all()
    team_roles: dict[str, str] = {}
    project_roles: dict[str, str] = {}
    project_teams: dict[str, str] = {}
    for r in rows:
        if r.scope_kind == "team":
            team_roles[r.scope_id] = higher(team_roles.get(r.scope_id), r.role)
        elif r.team_id is not None:
            project_roles[r.scope_id] = higher(project_roles.get(r.scope_id), r.role)
            project_teams[r.scope_id] = r.team_id
    return Access(is_admin=is_admin, teams=team_roles, projects=project_roles, project_teams=project_teams)


def role_for(access: Access, target: Target) -> str | None:
    if access.is_admin:
        return "admin"
    if target.team_id is None:
        return None
    role = access.teams.get(target.team_id)
    if target.project_id is not None:
        role = higher(role, access.projects.get(target.project_id))
    return role


def sees_team(access: Access, team_id: str) -> bool:
    return access.is_admin or team_id in access.teams or team_id in access.project_teams.values()


def visible_scope(access: Access) -> Scope:
    if access.is_admin:
        return Scope(all=True, team_ids=frozenset(), project_ids=frozenset(), visible_team_ids=frozenset())
    return Scope(all=False, team_ids=frozenset(access.teams), project_ids=frozenset(access.projects),
                 visible_team_ids=frozenset(access.teams) | frozenset(access.project_teams.values()))


def scope_clause(scope: Scope, team_col, project_col) -> ColumnElement[bool]:
    """A WHERE clause for rows that carry a team and an optional project."""
    if scope.all:
        return true()
    parts = []
    if scope.team_ids:
        parts.append(team_col.in_(scope.team_ids))
    if scope.project_ids:
        parts.append(project_col.in_(scope.project_ids))
    return or_(*parts) if parts else false()


def team_target(conn: Connection, team_id: str) -> Target:
    if conn.execute(select(teams.c.id).where(teams.c.id == team_id)).first() is None:
        raise NotFound("No such team.")
    return Target(team_id=team_id)


def project_target(conn: Connection, project_id: str) -> Target:
    row = conn.execute(select(projects.c.team_id).where(projects.c.id == project_id)).first()
    if row is None:
        raise NotFound("No such project.")
    return Target(team_id=row.team_id, project_id=project_id)


def scope_target(conn: Connection, scope_kind: str, scope_id: str) -> Target:
    if scope_kind == "team":
        return team_target(conn, scope_id)
    if scope_kind == "project":
        return project_target(conn, scope_id)
    raise NotFound("No such scope.")


def membership_target(conn: Connection, membership_id: str) -> Target:
    row = conn.execute(select(memberships.c.scope_kind, memberships.c.scope_id)
                       .where(memberships.c.id == membership_id)).first()
    if row is None:
        raise NotFound("No such membership.")
    return scope_target(conn, row.scope_kind, row.scope_id)


def run_target(conn: Connection, run_id: str) -> Target:
    row = conn.execute(select(runs.c.team_id, runs.c.project_id).where(runs.c.id == run_id)).first()
    if row is None:
        raise NotFound("No such run.")
    return Target(team_id=row.team_id, project_id=row.project_id)


def in_scope(scope: Scope, team_id: str, project_id: str | None) -> bool:
    """``scope_clause`` for one row already in memory."""
    return scope.all or team_id in scope.team_ids or (project_id is not None and project_id in scope.project_ids)

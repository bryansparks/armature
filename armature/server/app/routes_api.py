"""Teams, projects, users, memberships, secrets, runs, and the audit log."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, delete, exists, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import audit, display, invites, rbac, subjects, trigger
from armature.server import connections as vendor_connections
from armature.server import secrets as secret_store
from armature.server.app.deps import (
    Granted,
    by_membership,
    by_project,
    by_team,
    grant,
    require,
    require_step_up,
    server,
    signed_in,
    team_of,
)
from armature.server.app.http import ApiError, client_ip
from armature.server.auth import AuthError, Session, count_call
from armature.server.db import new_id
from armature.server.db.schema import audit as audit_table
from armature.server.db.schema import (
    connections,
    environment_types,
    memberships,
    projects,
    run_steps,
    runs,
    sessions,
    teams,
    users,
    workflows,
)
from armature.server.ingest import about
from armature.server.users import (
    LastAdmin,
    SlackTaken,
    create_user,
    public_user,
    reset_sign_in,
    set_slack,
    update_user,
)

router = APIRouter()

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")
PLATFORM = re.compile(r"^[a-z0-9-]{1,32}$")
# owner/name, or a clone or browser address on a host that ends in one.
REPO = re.compile(r"^(?:(?:https|ssh|git)://(?:git@)?(?P<url_host>[^/\s@:]+)(?::\d+)?/|git@(?P<ssh_host>[^:/\s]+):"
                  r"|(?P<bare_host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)/)?"
                  r"(?P<owner>[A-Za-z0-9-]{1,39})/(?P<name>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")
GITHUB_HOST = "github.com"
BRANCH = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,191}$")
SECRET_VALUE_MAX = 32 * 1024
LIMIT_DEFAULT = 50
LIMIT_MAX = 200
MAX_WORKFLOW_FILTER = 50
LOOKUP_LIMIT = 20
RUN_STATUSES = ("queued", "running", "waiting", "succeeded", "failed", "canceled", "lost")


class NameBody(BaseModel):
    name: str


class ProjectBody(BaseModel):
    name: str
    platform: str | None = None
    environment_type_id: str | None = None
    repo: str | None = None
    connection_id: str | None = None
    default_branch: str | None = None


class ProjectChangeBody(BaseModel):
    repo: str | None = None
    connection_id: str | None = None
    default_branch: str | None = None


class EnvironmentTypeBody(BaseModel):
    environment_type_id: str | None = None


class NewUserBody(BaseModel):
    username: str
    display_name: str | None = None
    email: str | None = None
    team_id: str | None = None
    role: str | None = None


class SlackBody(BaseModel):
    workspace_id: str | None = None
    user_id: str | None = None


class UserChangeBody(BaseModel):
    is_admin: bool | None = None
    disabled: bool | None = None
    display_name: str | None = None


class MembershipBody(BaseModel):
    user_id: str
    scope_kind: str
    scope_id: str
    role: str


class SecretBody(BaseModel):
    name: str
    value: str


def _check_name(name: str, what: str) -> str:
    name = name.strip()
    if not NAME.fullmatch(name):
        raise ApiError(400, f"A {what} name is 1 to 100 letters, digits, spaces, '.', '_', or '-'.")
    return name


def _team(row: Any, role: str | None = None) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "created_at": row.created_at, "role": role}


def _project(row: Any) -> dict[str, Any]:
    return {"id": row.id, "team_id": row.team_id, "name": row.name, "platform": row.platform,
            "enabled_workflows": row.enabled_workflows, "branch_filters": row.branch_filters,
            "environment_type_id": row.environment_type_id, "repo": row.repo, "connection_id": row.connection_id,
            "default_branch": row.default_branch, "created_at": row.created_at}


def repo_slug(value: str | None, host: str | None = None) -> str | None:
    """``owner/name`` as webhook routing matches it. Another host is allowed only as ``host`` and kept in front."""
    if value is None or not value.strip():
        return None
    match = REPO.fullmatch(value.strip())
    if not match or set(match.group("name")) == {"."}:
        raise ApiError(400, "A repository is owner/name, or its github.com address.")
    found = (match.group("url_host") or match.group("ssh_host") or match.group("bare_host") or GITHUB_HOST).lower()
    slug = f"{match.group('owner')}/{match.group('name')}"
    if found == GITHUB_HOST:
        return slug
    if host is not None and found == host.lower():
        return f"{found}/{slug}"
    raise ApiError(400, "Only github.com repositories work here. Paste a github.com address or owner/name.")


def _check_branch(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    if not BRANCH.fullmatch(value.strip()):
        raise ApiError(400, "A branch is letters, digits, '.', '_', '/', or '-'.")
    return value.strip()


def _check_connection(conn: Any, team_id: str, connection_id: str | None) -> None:
    if connection_id is not None and conn.execute(select(connections.c.id).where(
            connections.c.id == connection_id, connections.c.team_id == team_id,
            connections.c.project_id.is_(None), connections.c.kind == "github_app")).first() is None:
        raise ApiError(400, "Pick one of this team's GitHub connections.")


def _check_repo_reach(conn: Any, team_id: str, access: Any) -> None:
    """A gh CLI connection reaches any repository its login can, so only a server admin points a project at one."""
    if not access.is_admin and vendor_connections.gh_cli_in_team(conn, team_id):
        raise ApiError(403, "This team uses a gh CLI connection. Only a server admin can set a project's repository "
                            "or connection.")


def _check_environment_type(conn: Any, type_id: str | None) -> None:
    if type_id is not None and conn.execute(select(environment_types.c.id)
                                            .where(environment_types.c.id == type_id)).first() is None:
        raise ApiError(400, "No such environment type.")


def _cursor(before: str | None) -> tuple[datetime, str | None] | None:
    if not before:
        return None
    stamp, _, row_id = before.partition("|")
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise ApiError(400, "before is not a timestamp.") from exc
    return (at if at.tzinfo else at.replace(tzinfo=UTC)), (row_id or None)


def _before_clause(cursor: tuple[datetime, str | None] | None, at_col, id_col):
    if cursor is None:
        return None
    at, row_id = cursor
    if row_id is None:
        return at_col < at
    return or_(at_col < at, and_(at_col == at, id_col < row_id))


def _next(rows: list[Any], limit: int, at_name: str) -> str | None:
    if len(rows) < limit:
        return None
    last = rows[-1]
    # Z rather than +00:00, because a bare + in a query string reads as a space.
    stamp = getattr(last, at_name).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return f"{stamp}|{last.id}"


def _access(request: Request, session: Session) -> rbac.Access:
    with server(request).engine.connect() as conn:
        return rbac.load_access(conn, session.user_id, bool(session.user.is_admin))


@router.get("/api/teams")
def list_teams(request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    access = _access(request, session)
    scope = rbac.visible_scope(access)
    query = select(teams).order_by(teams.c.name)
    if not scope.all:
        query = query.where(teams.c.id.in_(scope.visible_team_ids))
    with server(request).engine.connect() as conn:
        rows = conn.execute(query).all()
    return {"teams": [_team(r, rbac.role_for(access, rbac.Target(team_id=r.id))) for r in rows]}


@router.post("/api/teams")
def create_team(body: NameBody, request: Request, granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    name = _check_name(body.name, "team")
    srv = server(request)
    team_id = new_id()
    try:
        with srv.engine.begin() as conn:
            conn.execute(insert(teams).values(id=team_id, name=name, created_at=srv.now()))
            audit.record(conn, action="team.create", actor_id=granted.user_id, team_id=team_id, target_kind="team",
                         target_id=team_id, detail={"name": name}, ip=client_ip(request))
            row = conn.execute(select(teams).where(teams.c.id == team_id)).one()
    except IntegrityError as exc:
        raise ApiError(409, "A team with that name exists.") from exc
    return {"team": _team(row, "admin")}


@router.get("/api/teams/{team_id}/projects")
def list_projects(team_id: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    access = _access(request, session)
    with server(request).engine.connect() as conn:
        try:
            rbac.team_target(conn, team_id)
        except rbac.NotFound as exc:
            raise ApiError(404, str(exc)) from exc
        if not rbac.sees_team(access, team_id):
            raise ApiError(404, "Not found.")
        query = select(projects).where(projects.c.team_id == team_id).order_by(projects.c.name)
        if rbac.role_for(access, rbac.Target(team_id=team_id)) is None:
            query = query.where(projects.c.id.in_(access.projects))
        rows = conn.execute(query).all()
    return {"projects": [_project(r) for r in rows]}


@router.post("/api/teams/{team_id}/projects")
def create_project(body: ProjectBody, request: Request,
                   granted: Granted = Depends(require("team_lead", by_team()))) -> dict[str, Any]:
    name = _check_name(body.name, "project")
    if body.platform is not None and not PLATFORM.fullmatch(body.platform):
        raise ApiError(400, "A platform is up to 32 lowercase letters, digits, or '-'.")
    repo, branch = repo_slug(body.repo), _check_branch(body.default_branch)
    srv = server(request)
    project_id = new_id()
    team_id = granted.target.team_id
    try:
        with srv.engine.begin() as conn:
            _check_environment_type(conn, body.environment_type_id)
            _check_connection(conn, team_id, body.connection_id or None)
            _check_repo_reach(conn, team_id, granted.access)
            conn.execute(insert(projects).values(id=project_id, team_id=team_id, name=name, platform=body.platform,
                                                 environment_type_id=body.environment_type_id, repo=repo,
                                                 connection_id=body.connection_id or None, default_branch=branch,
                                                 created_at=srv.now()))
            audit.record(conn, action="project.create", actor_id=granted.user_id, team_id=team_id,
                         target_kind="project", target_id=project_id,
                         detail={"name": name, "environment_type_id": body.environment_type_id, "repo": repo},
                         ip=client_ip(request))
            row = conn.execute(select(projects).where(projects.c.id == project_id)).one()
    except IntegrityError as exc:
        raise ApiError(409, "That team has a project with that name.") from exc
    return {"project": _project(row)}


@router.get("/api/projects")
def list_all_projects(request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    access = _access(request, session)
    scope = rbac.visible_scope(access)
    query = (select(projects, teams.c.name.label("team_name")).join(teams, teams.c.id == projects.c.team_id)
             .order_by(teams.c.name, projects.c.name))
    if not scope.all:
        query = query.where(or_(projects.c.team_id.in_(scope.team_ids), projects.c.id.in_(scope.project_ids)))
    with server(request).engine.connect() as conn:
        rows = conn.execute(query).all()
    return {"projects": [{**_project(r), "team_name": r.team_name} for r in rows]}


@router.post("/api/projects/{project_id}")
def update_project(body: ProjectChangeBody, request: Request,
                   granted: Granted = Depends(require("project_lead", by_project()))) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if "repo" in body.model_fields_set:
        values["repo"] = repo_slug(body.repo)
    if "default_branch" in body.model_fields_set:
        values["default_branch"] = _check_branch(body.default_branch)
    if "connection_id" in body.model_fields_set:
        values["connection_id"] = body.connection_id or None
    srv = server(request)
    project_id = granted.target.project_id
    with srv.engine.begin() as conn:
        if "connection_id" in values:
            _check_connection(conn, granted.target.team_id, values["connection_id"])
        if "repo" in values or "connection_id" in values:
            _check_repo_reach(conn, granted.target.team_id, granted.access)
        if values:
            conn.execute(update(projects).where(projects.c.id == project_id).values(**values))
            audit.record(conn, action="project.update", actor_id=granted.user_id, team_id=granted.target.team_id,
                         target_kind="project", target_id=project_id, detail=values, ip=client_ip(request))
        row = conn.execute(select(projects).where(projects.c.id == project_id)).one()
    return {"project": {**_project(row), "role": granted.role}}


@router.get("/api/projects/{project_id}")
def get_project(request: Request, granted: Granted = Depends(require("viewer", by_project()))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        row = conn.execute(select(projects).where(projects.c.id == granted.target.project_id)).one()
    return {"project": {**_project(row), "role": granted.role}}


@router.post("/api/projects/{project_id}/environment-type")
def set_project_environment_type(body: EnvironmentTypeBody, request: Request,
                                 granted: Granted = Depends(require("project_lead", by_project()))) -> dict[str, Any]:
    srv = server(request)
    project_id = granted.target.project_id
    with srv.engine.begin() as conn:
        _check_environment_type(conn, body.environment_type_id)
        before = conn.execute(select(projects.c.environment_type_id).where(projects.c.id == project_id)).scalar()
        conn.execute(update(projects).where(projects.c.id == project_id)
                     .values(environment_type_id=body.environment_type_id))
        audit.record(conn, action="project.environment_type", actor_id=granted.user_id,
                     team_id=granted.target.team_id, target_kind="project", target_id=project_id,
                     detail={"from": before, "to": body.environment_type_id}, ip=client_ip(request))
        row = conn.execute(select(projects).where(projects.c.id == project_id)).one()
    return {"project": {**_project(row), "role": granted.role}}


@router.get("/api/users")
def list_users(request: Request, team: str | None = Query(None),
               session: Session = Depends(signed_in)) -> dict[str, Any]:
    access = _access(request, session)
    if team is not None:
        if not access.is_admin and not rbac.at_least(access.teams.get(team), "team_lead"):
            raise ApiError(403, "Not allowed.")
        team_ids = {team}
    elif access.is_admin:
        team_ids = None
    else:
        team_ids = {t for t, role in access.teams.items() if rbac.at_least(role, "team_lead")}
        if not team_ids:
            raise ApiError(403, "Not allowed.")
    with server(request).engine.connect() as conn:
        listed = select(memberships, projects.c.team_id.label("project_team"),
                        projects.c.name.label("project_name"), teams.c.name.label("team_name")).select_from(
            memberships.outerjoin(projects, and_(memberships.c.scope_kind == "project",
                                                 projects.c.id == memberships.c.scope_id))
            .outerjoin(teams, and_(memberships.c.scope_kind == "team", teams.c.id == memberships.c.scope_id)))
        member_rows = conn.execute(listed).all()

        def in_scope(m: Any) -> bool:
            owner = m.scope_id if m.scope_kind == "team" else m.project_team
            return team_ids is None or owner in team_ids

        visible = [m for m in member_rows if in_scope(m)]
        query = select(users).order_by(users.c.username, users.c.id)
        if team_ids is not None:
            query = query.where(users.c.id.in_({m.user_id for m in visible}))
        rows = conn.execute(query).all()
    by_user: dict[str, list[dict[str, Any]]] = {}
    for m in visible:
        by_user.setdefault(m.user_id, []).append({
            "id": m.id, "scope_kind": m.scope_kind, "scope_id": m.scope_id,
            "scope_name": m.team_name if m.scope_kind == "team" else m.project_name, "role": m.role,
            "source": m.source})
    return {"users": [{**public_user(r), "memberships": by_user.get(r.id, [])} for r in rows]}


def _check_grantable(caller_role: str, role: str | None) -> None:
    """The one rule for every grant: a role must exist, and only an admin grants team lead."""
    if role not in rbac.GRANTABLE:
        raise ApiError(400, f"role must be one of {', '.join(rbac.GRANTABLE)}.")
    if caller_role != "admin" and role not in rbac.LEAD_GRANTABLE:
        raise ApiError(403, "Only an admin can grant team lead.")


def _invite_url(request: Request, token: str) -> str:
    cfg = server(request).cfg
    return f"{cfg.public_origin}{cfg.base_path}/invite#{token}"


@router.get("/api/users/lookup")
def lookup_users(request: Request, q: str = Query("", max_length=191),
                 session: Session = Depends(signed_in)) -> dict[str, Any]:
    """Exact username or email matches only, so a team lead can add someone without listing everyone."""
    access = _access(request, session)
    if not access.is_admin and not any(rbac.at_least(role, "team_lead") for role in access.teams.values()):
        raise ApiError(403, "Not allowed.")
    srv = server(request)
    try:
        count_call(srv.engine, key=f"lookup:{session.user_id}", limit=LOOKUP_LIMIT, now=srv.now())
    except AuthError as exc:
        raise ApiError(exc.status, exc.message, exc.headers) from exc
    wanted = q.strip().lower()
    if len(wanted) < 2:
        return {"users": []}
    with server(request).engine.connect() as conn:
        rows = conn.execute(select(users.c.id, users.c.username, users.c.display_name)
                            .where(or_(users.c.username == wanted, func.lower(users.c.email) == wanted),
                                   users.c.disabled.is_(False))
                            .order_by(users.c.username).limit(10)).all()
    return {"users": [{"id": r.id, "username": r.username, "display_name": r.display_name} for r in rows]}


@router.post("/api/users")
def create_user_route(body: NewUserBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    """Admins invite anyone. A team lead invites into a team they lead, below team lead."""
    srv = server(request)
    target = None
    with srv.engine.connect() as conn:
        if body.team_id is not None:
            if body.role not in rbac.GRANTABLE:
                raise ApiError(400, f"role must be one of {', '.join(rbac.GRANTABLE)}.")
            try:
                target = rbac.team_target(conn, body.team_id)
            except rbac.NotFound as exc:
                raise ApiError(404, str(exc)) from exc
            granted = grant(srv, session, target, "team_lead", conn)
            _check_grantable(granted.role, body.role)
        else:
            granted = grant(srv, session, rbac.INSTANCE, "admin", conn)
    require_step_up(request, session)
    now, ip = srv.now(), client_ip(request)
    try:
        with srv.engine.begin() as conn:
            user_id = create_user(conn, username=body.username, display_name=body.display_name, email=body.email,
                                  actor_id=granted.user_id, ip=ip)
            if target is not None:
                membership_id = new_id()
                conn.execute(insert(memberships).values(id=membership_id, user_id=user_id, scope_kind="team",
                                                        scope_id=target.team_id, role=body.role, source="manual",
                                                        created_at=now))
                audit.record(conn, action="membership.set", actor_id=granted.user_id, team_id=target.team_id,
                             target_kind="membership", target_id=membership_id,
                             detail={"user_id": user_id, "scope_kind": "team", "scope_id": target.team_id,
                                     "role": body.role, "old_role": None}, ip=ip)
            token = invites.issue(conn, srv.keyring, user_id, created_by=granted.user_id, now=now, ip=ip)
            row = conn.execute(select(users).where(users.c.id == user_id)).one()
    except ValueError as exc:
        raise ApiError(409 if "already exists" in str(exc) else 400, str(exc)) from exc
    except IntegrityError as exc:
        raise ApiError(409, "That username is taken.") from exc
    return {"user": public_user(row), "invite_url": _invite_url(request, token)}


@router.post("/api/users/{user_id}/invite")
def reissue_invite(user_id: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    """A new invite. For a user who already signed in, only an admin can do this, and it resets their sign-in."""
    srv = server(request)
    with srv.engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.id == user_id)).first()
        access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
        if row is None:
            raise ApiError(404, "No such user.")
        signed_up = row.totp_confirmed_at is not None or (not srv.cfg.require_totp and row.password_hash is not None)
        if not access.is_admin and (signed_up or row.is_admin
                                    or not _leads_every_membership(conn, access, user_id)):
            raise ApiError(403, "Not allowed.")
    if row.oidc_sub is not None and row.password_hash is None:
        raise ApiError(400, "This user signs in with single sign-on.")
    if row.id == session.user_id:
        raise ApiError(400, "Ask another admin to reset your own sign-in.")
    require_step_up(request, session)
    now, ip = srv.now(), client_ip(request)
    with srv.engine.begin() as conn:
        if row.totp_confirmed_at is not None or row.password_hash is not None:
            reset_sign_in(conn, user_id, actor_id=session.user_id, ip=ip)
        token = invites.issue(conn, srv.keyring, user_id, created_by=session.user_id, now=now, ip=ip)
    return {"invite_url": _invite_url(request, token)}


# A lead who added someone to their team must not gain their sign-in for every other team they are in.
def _leads_every_membership(conn, access: rbac.Access, user_id: str) -> bool:
    led = {team for team, role in access.teams.items() if rbac.at_least(role, "team_lead")}
    rows = conn.execute(
        select(memberships.c.scope_kind, memberships.c.scope_id, memberships.c.role, projects.c.team_id)
        .select_from(memberships.outerjoin(projects, and_(memberships.c.scope_kind == "project",
                                                          projects.c.id == memberships.c.scope_id)))
        .where(memberships.c.user_id == user_id)).all()
    return bool(rows) and all(
        r.role in rbac.LEAD_GRANTABLE and (r.scope_id if r.scope_kind == "team" else r.team_id) in led for r in rows)


@router.post("/api/users/{user_id}")
def update_user_route(user_id: str, body: UserChangeBody, request: Request,
                      granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    srv = server(request)
    try:
        with srv.engine.begin() as conn:
            changes = update_user(conn, user_id, is_admin=body.is_admin, disabled=body.disabled,
                                  display_name=body.display_name, actor_id=granted.user_id, ip=client_ip(request))
            if changes is None:
                raise ApiError(404, "No such user.")
            row = conn.execute(select(users).where(users.c.id == user_id)).one()
    except LastAdmin as exc:
        raise ApiError(409, "This is the last enabled admin. Make another admin first.") from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return {"user": public_user(row)}


@router.post("/api/users/{user_id}/slack")
def set_user_slack(user_id: str, body: SlackBody, request: Request,
                   granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    """Link a user to a Slack account directly. A user links their own with the code the bot sends them."""
    try:
        with server(request).engine.begin() as conn:
            if not set_slack(conn, user_id, workspace_id=body.workspace_id, slack_user_id=body.user_id,
                             actor_id=granted.user_id, ip=client_ip(request)):
                raise ApiError(404, "No such user.")
            row = conn.execute(select(users).where(users.c.id == user_id)).one()
    except SlackTaken as exc:
        raise ApiError(409, str(exc)) from exc
    except IntegrityError as exc:
        raise ApiError(409, "Another user already linked this Slack account.") from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return {"user": public_user(row)}


@router.post("/api/memberships")
def set_membership(body: MembershipBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    if body.role not in rbac.GRANTABLE:
        raise ApiError(400, f"role must be one of {', '.join(rbac.GRANTABLE)}.")
    srv = server(request)
    with srv.engine.connect() as conn:
        try:
            target = rbac.scope_target(conn, body.scope_kind, body.scope_id)
        except rbac.NotFound as exc:
            raise ApiError(404, str(exc)) from exc
        granted = grant(srv, session, rbac.Target(team_id=target.team_id), "team_lead", conn)
        _check_grantable(granted.role, body.role)
    require_step_up(request, session)
    with srv.engine.begin() as conn:
        if conn.execute(select(users.c.id).where(users.c.id == body.user_id)).first() is None:
            raise ApiError(404, "No such user.")
        existing = conn.execute(select(memberships).where(
            memberships.c.user_id == body.user_id, memberships.c.scope_kind == body.scope_kind,
            memberships.c.scope_id == body.scope_id).with_for_update()).first()
        if existing is not None:
            _check_grantable(granted.role, existing.role)
        if existing is None:
            membership_id = new_id()
            conn.execute(insert(memberships).values(id=membership_id, user_id=body.user_id,
                                                    scope_kind=body.scope_kind, scope_id=body.scope_id,
                                                    role=body.role, source="manual", created_at=srv.now()))
        else:
            membership_id = existing.id
            conn.execute(update(memberships).where(memberships.c.id == membership_id)
                         .values(role=body.role, source="manual"))
            if existing.role != body.role:
                conn.execute(delete(sessions).where(sessions.c.user_id == body.user_id))
        audit.record(conn, action="membership.set", actor_id=granted.user_id, team_id=target.team_id,
                     target_kind="membership", target_id=membership_id,
                     detail={"user_id": body.user_id, "scope_kind": body.scope_kind, "scope_id": body.scope_id,
                             "role": body.role, "old_role": existing.role if existing else None},
                     ip=client_ip(request))
    return {"membership": {"id": membership_id, "user_id": body.user_id, "scope_kind": body.scope_kind,
                           "scope_id": body.scope_id, "role": body.role, "source": "manual"}}


@router.post("/api/memberships/{membership_id}/delete")
def delete_membership(membership_id: str, request: Request,
                      granted: Granted = Depends(require("team_lead", team_of(by_membership()), step_up=True))
                      ) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.begin() as conn:
        row = conn.execute(select(memberships).where(memberships.c.id == membership_id)).first()
        if row is None:
            raise ApiError(404, "No such membership.")
        _check_grantable(granted.role, row.role)
        conn.execute(delete(memberships).where(memberships.c.id == membership_id))
        conn.execute(delete(sessions).where(sessions.c.user_id == row.user_id))
        audit.record(conn, action="membership.delete", actor_id=granted.user_id, team_id=granted.target.team_id,
                     target_kind="membership", target_id=membership_id,
                     detail={"user_id": row.user_id, "scope_kind": row.scope_kind, "scope_id": row.scope_id,
                             "role": row.role}, ip=client_ip(request))
    return {"ok": True}


def _list_secrets(request: Request, team_id: str | None) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        return {"secrets": secret_store.list_secrets(conn, team_id)}


def _set_secret(request: Request, granted: Granted, team_id: str | None, body: SecretBody) -> dict[str, Any]:
    if not body.value or len(body.value) > SECRET_VALUE_MAX:
        raise ApiError(400, f"A secret value is 1 to {SECRET_VALUE_MAX} characters.")
    srv = server(request)
    try:
        with srv.engine.begin() as conn:
            secret_store.set_secret(conn, srv.keyring, team_id=team_id, name=body.name, value=body.value,
                                    actor_id=granted.user_id)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return {"ok": True, "name": body.name}


def _delete_secret(request: Request, granted: Granted, team_id: str | None, name: str) -> dict[str, Any]:
    with server(request).engine.begin() as conn:
        if not secret_store.delete_secret(conn, team_id=team_id, name=name, actor_id=granted.user_id):
            raise ApiError(404, "No such secret.")
    return {"ok": True}


@router.get("/api/teams/{team_id}/secrets")
def list_team_secrets(request: Request,
                      granted: Granted = Depends(require("team_lead", by_team()))) -> dict[str, Any]:
    return _list_secrets(request, granted.target.team_id)


@router.post("/api/teams/{team_id}/secrets")
def set_team_secret(body: SecretBody, request: Request,
                    granted: Granted = Depends(require("team_lead", by_team(), step_up=True))) -> dict[str, Any]:
    return _set_secret(request, granted, granted.target.team_id, body)


@router.post("/api/teams/{team_id}/secrets/{name}/delete")
def delete_team_secret(name: str, request: Request,
                       granted: Granted = Depends(require("team_lead", by_team(), step_up=True))) -> dict[str, Any]:
    return _delete_secret(request, granted, granted.target.team_id, name)


@router.get("/api/secrets")
def list_instance_secrets(request: Request, granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    return _list_secrets(request, None)


@router.post("/api/secrets")
def set_instance_secret(body: SecretBody, request: Request,
                        granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    return _set_secret(request, granted, None, body)


@router.post("/api/secrets/{name}/delete")
def delete_instance_secret(name: str, request: Request,
                           granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    return _delete_secret(request, granted, None, name)


def _fix_outcome(ref: Any) -> str | None:
    fix = ref.get("fix") if isinstance(ref, dict) else None
    outcome = fix.get("outcome") if isinstance(fix, dict) else None
    return outcome if isinstance(outcome, str) and outcome else None


def _latest_per_subject(query):
    """Keep the newest run of each thread or subject the query finds, with how many runs it stands for."""
    bucket = func.coalesce(runs.c.thread, runs.c.subject, runs.c.id)
    newest = (runs.c.started_at.desc(), runs.c.id.desc())
    ranked = query.add_columns(func.row_number().over(partition_by=bucket, order_by=newest).label("subject_rank"),
                               func.count().over(partition_by=bucket).label("group_size")).subquery()
    latest = select(*[c for c in ranked.c if c.name != "subject_rank"]).where(ranked.c.subject_rank == 1)
    return latest, ranked.c.started_at, ranked.c.id


@router.get("/api/runs")
def list_runs(request: Request, team: str | None = None, project: str | None = None, status: str | None = None,
              workflow: str | None = Query(None, max_length=191), with_outputs: bool = False,
              subject: str | None = Query(None, max_length=191), thread: str | None = Query(None, max_length=191),
              before: str | None = None,
              workflow_list: str | None = Query(None, alias="workflows", max_length=4000),
              include_hidden: bool = False, group: bool = False,
              limit: int = Query(LIMIT_DEFAULT, ge=1, le=LIMIT_MAX),
              session: Session = Depends(signed_in)) -> dict[str, Any]:
    scope = rbac.visible_scope(_access(request, session))
    query = (select(runs.c.id, runs.c.team_id, runs.c.project_id,
                    func.coalesce(workflows.c.name, runs.c.workflow_name).label("workflow"), runs.c.status,
                    func.coalesce(runs.c.triggered_by, runs.c.actor).label("triggered_by"), runs.c.started_at,
                    runs.c.finished_at, runs.c.cost_usd, runs.c.subject, runs.c.thread, runs.c.summary,
                    runs.c.outcome, runs.c.risk, runs.c.location, *trigger.columns())
             .select_from(runs.outerjoin(workflows, workflows.c.id == runs.c.workflow_id))
             .where(rbac.scope_clause(scope, runs.c.team_id, runs.c.project_id)))
    if team:
        query = query.where(runs.c.team_id == team)
    if project:
        query = query.where(runs.c.project_id == project)
    name = func.coalesce(workflows.c.name, runs.c.workflow_name)
    if workflow:
        query = query.where(name == workflow)
    chosen = sorted({w.strip() for w in (workflow_list or "").split(",") if w.strip()})[:MAX_WORKFLOW_FILTER]
    if chosen:
        query = query.where(name.in_(chosen))
    if subject:
        query = query.where(runs.c.subject == subject)
    if thread:
        query = query.where(or_(runs.c.thread == thread, and_(runs.c.thread.is_(None), runs.c.subject == thread)))
    if with_outputs:
        query = query.where(exists().where(run_steps.c.run_id == runs.c.id, run_steps.c.parent_step.is_(None),
                                           run_steps.c.output.is_not(None)))
    if status:
        if status not in RUN_STATUSES:
            raise ApiError(400, f"status must be one of {', '.join(RUN_STATUSES)}.")
        query = query.where(runs.c.status == status)
    cursor = _cursor(before)
    with server(request).engine.connect() as conn:
        settings = display.load(conn)
        if not include_hidden:
            hidden = display.hidden(settings)
            if hidden and not chosen and not workflow:
                query = query.where(or_(name.is_(None), name.not_in(hidden)))
            actors = display.hidden_actors(settings)
            if actors:
                actor = func.coalesce(runs.c.triggered_by, runs.c.actor)
                query = query.where(or_(actor.is_(None), actor.not_in(actors)))
        at, row_id = runs.c.started_at, runs.c.id
        if group:
            query, at, row_id = _latest_per_subject(query)
        clause = _before_clause(cursor, at, row_id)
        if clause is not None:
            query = query.where(clause)
        rows = conn.execute(query.order_by(at.desc(), row_id.desc()).limit(limit)).all()
        pulls = subjects.pull_states(conn, scope, {r.subject for r in rows if r.subject})
        started = trigger.describe(conn, [r._mapping for r in rows])
    listed = [{"id": r.id, "team_id": r.team_id, "project_id": r.project_id, "workflow": r.workflow,
               "status": r.status, "triggered_by": r.triggered_by, "started_at": r.started_at,
               "finished_at": r.finished_at,
               "cost_usd": float(r.cost_usd) if r.cost_usd is not None and settings["show_cost"] else None,
               **about(r.subject, r.summary), "thread": r.thread, "pr": pulls.get(r.subject),
               "outcome": r.outcome, "fix_outcome": _fix_outcome(r.trigger_ref), "risk": r.risk,
               "trigger": started[r.id], "location": r.location, **({"group_size": r.group_size} if group else {})}
              for r in rows]
    return {"runs": listed, "next_before": _next(rows, limit, "started_at")}


@router.get("/api/audit")
def list_audit(request: Request, team: str | None = None, before: str | None = None,
               limit: int = Query(LIMIT_DEFAULT, ge=1, le=LIMIT_MAX),
               granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    query = select(audit_table)
    if team:
        query = query.where(audit_table.c.team_id == team)
    clause = _before_clause(_cursor(before), audit_table.c.at, audit_table.c.id)
    if clause is not None:
        query = query.where(clause)
    query = query.order_by(audit_table.c.at.desc(), audit_table.c.id.desc()).limit(limit)
    with server(request).engine.connect() as conn:
        rows = conn.execute(query).all()
    entries = [{"id": r.id, "at": r.at, "actor_id": r.actor_id, "team_id": r.team_id, "action": r.action,
                "target_kind": r.target_kind, "target_id": r.target_id, "detail": r.detail, "ip": r.ip}
               for r in rows]
    return {"entries": entries, "next_before": _next(rows, limit, "at")}

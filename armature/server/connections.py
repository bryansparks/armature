"""Vendor connections: a team's or project's GitHub, Slack, Jira, and Linear credentials.

Credentials are one sealed JSON object per row and write-only, as the AWS
connection's are: the API shows whether each is set and, for a token, its last
four characters. Any credential or config change makes the connection
unverified. Verify runs on the server and records who the credentials are. A
GitHub connection signs in as its App, or with ``auth: gh_cli`` as the gh CLI
login of the server's OS user.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import Connection, Engine, delete, insert, select, update

from armature.server import audit, vendors
from armature.server.crypto import Keyring, SealError
from armature.server.db import new_id
from armature.server.db.schema import (
    CONNECTION_KINDS,
    connections,
    github_installations,
    projects,
    webhooks,
)
from armature.server.vendors import VendorError, github, jira, linear, slack

log = logging.getLogger(__name__)

LABEL = "connections"
SECRETS = {"github_app": ("private_key_pem", "webhook_secret"), "slack": ("bot_token", "signing_secret"),
           "jira": ("api_token", "webhook_secret"), "linear": ("api_key", "webhook_secret")}
REQUIRED = {"github_app": "private_key_pem", "slack": "bot_token", "jira": "api_token", "linear": "api_key"}
HINTED = frozenset({"bot_token", "api_token", "api_key"})
WEBHOOK_SECRET = {"github_app": "webhook_secret", "slack": "signing_secret", "jira": "webhook_secret",
                  "linear": "webhook_secret"}
SOURCE_KIND = {"github": "github_app", "slack": "slack", "jira": "jira", "linear": "linear"}
VENDORS = {"github_app": github, "slack": slack, "jira": jira, "linear": linear}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9.]{8,64}$")
APP_ID = re.compile(r"^\d{1,12}$")
SLACK_TEAM = re.compile(r"^T[A-Z0-9]{2,20}$")
SLACK_APP = re.compile(r"^A[A-Z0-9]{2,20}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")
JIRA_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.atlassian\.net$")
HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
KEY_MAX = 16 * 1024
SECRET_MAX = 512
ERROR_MAX = 2000


class ConnError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def sealed_aad(connection_id: str, kind: str) -> dict[str, str]:
    return {"connection": connection_id, "kind": kind}


def credentials(keyring: Keyring, row: Any) -> dict[str, str]:
    if not row.sealed:
        return {}
    return json.loads(keyring.open_(row.sealed, sealed_aad(row.id, row.kind), label=LABEL))


def get(conn: Connection, connection_id: str) -> Any:
    row = conn.execute(select(connections).where(connections.c.id == connection_id)).first()
    if row is None:
        raise ConnError(404, "No such connection.")
    return row


def listed(conn: Connection, scope: str) -> list[Any]:
    return conn.execute(select(connections).where(connections.c.scope == scope)
                        .order_by(connections.c.kind, connections.c.name)).all()


def _hints(keyring: Keyring, row: Any) -> dict[str, Any]:
    try:
        found = credentials(keyring, row)
    except (SealError, ValueError):
        return {"unreadable": True}
    return {name: {"set": bool(found.get(name)), **({"last4": found[name][-4:]} if name in HINTED and found.get(name)
                                                      else {})}
            for name in SECRETS[row.kind]}


def public(conn: Connection, keyring: Keyring, row: Any) -> dict[str, Any]:
    """The connection as the API shows it: config, identity, and only hints of its credentials."""
    out = {"id": row.id, "team_id": row.team_id, "project_id": row.project_id,
           "scope_kind": "project" if row.project_id else "team", "kind": row.kind, "name": row.name,
           "config": row.config or {}, "credentials": _hints(keyring, row), "identity": row.identity or {},
           "state": row.state, "last_error": row.last_error, "verified_at": row.verified_at,
           "created_at": row.created_at, "updated_at": row.updated_at}
    if github.uses_gh_cli(row.config):
        out["reaches"] = reaches(conn, row.team_id)
    if row.kind == "github_app":
        out["installations"] = [{"owner": r.owner, "installation_id": r.installation_id,
                                 "missing": github.missing(r.permissions)}
                                for r in conn.execute(select(github_installations)
                                                      .where(github_installations.c.connection_id == row.id)
                                                      .order_by(github_installations.c.owner))]
    return out


def _text(value: Any, what: str, pattern: re.Pattern[str], *, required: bool = False) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ConnError(400, f"{what} is required.")
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
        raise ConnError(400, f"{what} is not valid.")
    return value.strip()


def _https(value: Any, what: str, host: re.Pattern[str], *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ConnError(400, f"{what} is required.")
        return None
    if isinstance(value, str) and "://" not in value:
        value = f"https://{value.strip()}"
    parts = urlsplit(value.strip()) if isinstance(value, str) else None
    if parts is None or parts.scheme != "https" or not parts.hostname or parts.username or parts.password \
            or parts.port or parts.query or parts.fragment or parts.path.strip("/") \
            or not host.fullmatch(parts.hostname):
        raise ConnError(400, f"{what} must be an https URL with no path, such as https://example.com.")
    url = f"https://{parts.hostname}"
    try:
        vendors.public_address(url, what)
    except VendorError as exc:
        raise ConnError(400, str(exc)) from exc
    return url


def _config(kind: str, values: dict[str, Any]) -> dict[str, Any]:
    if kind == "github_app" and values.get("auth") == github.GH_CLI:
        host = _text(values.get("host"), "host", HOST)
        found = {"auth": github.GH_CLI, "host": host, "api_base": github.gh_api(host)}
    elif kind == "github_app":
        if values.get("auth") not in (None, "", "app"):
            raise ConnError(400, f"auth must be app or {github.GH_CLI}.")
        found = {"client_id": _text(values.get("client_id"), "client_id", CLIENT_ID, required=True),
                 "app_id": _text(values.get("app_id"), "app_id", APP_ID),
                 "api_base": _https(values.get("api_base"), "api_base", HOST) or github.API}
    elif kind == "slack":
        found = {"team_id": _text(values.get("team_id"), "team_id", SLACK_TEAM),
                 "app_id": _text(values.get("app_id"), "app_id", SLACK_APP)}
    elif kind == "jira":
        found = {"site_url": _https(values.get("site_url"), "site_url", JIRA_HOST, required=True),
                 "email": _text(values.get("email"), "email", EMAIL, required=True)}
    else:
        found = {}
    return {k: v for k, v in found.items() if v is not None}


def _secret(kind: str, name: str, value: Any) -> str:
    limit = KEY_MAX if name == "private_key_pem" else SECRET_MAX
    if not isinstance(value, str) or len(value.strip()) > limit:
        raise ConnError(400, f"{name} is text of at most {limit} characters.")
    value = value.strip()
    if name == "private_key_pem":
        try:
            github.load_key(value)
        except VendorError as exc:
            raise ConnError(400, str(exc)) from exc
    elif not value.isprintable() or any(c.isspace() for c in value):
        raise ConnError(400, f"{name} has spaces or characters that can't be printed.")
    return value


def _stored(keyring: Keyring, row: Any, name: str) -> bool:
    try:
        return bool(credentials(keyring, row).get(name))
    except (SealError, ValueError):
        return False


def set_connection(conn: Connection, keyring: Keyring, *, team_id: str, project_id: str | None, row: Any | None,
                   body: dict[str, Any], actor_id: str, now: datetime, ip: str | None,
                   is_admin: bool = False) -> Any:
    """Create a connection, or change one. Credentials left blank keep their stored values.

    Only a server admin may add or change a gh CLI connection, since it acts as the server's own OS user.
    """
    kind = row.kind if row is not None else body.get("kind")
    if kind not in CONNECTION_KINDS:
        raise ConnError(400, f"kind must be one of {', '.join(CONNECTION_KINDS)}.")
    if row is not None and body.get("kind") not in (None, row.kind):
        raise ConnError(400, "A connection's kind can't change. Add another connection.")
    if row is None and kind == "github_app" and project_id is not None:
        raise ConnError(400, "A GitHub App connection belongs to a team. Add it on the team.")
    name = _text(body.get("name") if "name" in body or row is None else row.name, "name", NAME, required=True)
    current_config = (row.config or {}) if row is not None else {}
    config = _config(kind, {k: body[k] if k in body else current_config.get(k)
                            for k in ("auth", "host", "client_id", "app_id", "api_base", "team_id", "site_url",
                                      "email")})
    gh_cli = github.uses_gh_cli(config)
    if (gh_cli or github.uses_gh_cli(current_config)) and not is_admin:
        raise ConnError(403, "Only a server admin can add or change a gh CLI connection.")
    given = {field: _secret(kind, field, body[field]) for field in SECRETS[kind]
             if isinstance(body.get(field), str) and body[field].strip()}
    leaving_gh_cli = row is not None and github.uses_gh_cli(current_config) and not gh_cli
    if (row is None or leaving_gh_cli) and REQUIRED[kind] not in given and not gh_cli \
            and not (leaving_gh_cli and _stored(keyring, row, REQUIRED[kind])):
        raise ConnError(400, f"{REQUIRED[kind]} is required.")
    connection_id = row.id if row is not None else new_id()
    values: dict[str, Any] = {"name": name, "config": config, "updated_at": now, "updated_by": actor_id}
    if given:
        try:
            stored = credentials(keyring, row) if row is not None else {}
        except (SealError, ValueError) as exc:
            if REQUIRED[kind] not in given:
                raise ConnError(409, "The stored credentials can't be opened. Enter them all again.") from exc
            stored = {}
        values.update(sealed=keyring.seal(json.dumps({**stored, **given}, sort_keys=True),
                                          sealed_aad(connection_id, kind), label=LABEL), key_id=keyring.key_id)
    changed = sorted(k for k in set(config) | set(current_config) if config.get(k) != current_config.get(k))
    if row is None or given or changed:
        values.update(state="unverified", last_error=None, verified_at=None)
    if row is None:
        conn.execute(insert(connections).values(id=connection_id, team_id=team_id, project_id=project_id, kind=kind,
                                                created_at=now, **values))
    else:
        conn.execute(update(connections).where(connections.c.id == connection_id).values(**values))
    if given or changed:
        github.forget(connection_id)
    detail = {"name": name, "kind": kind, "created": row is None, "config": changed, "credentials": sorted(given)}
    if gh_cli and not github.uses_gh_cli(current_config):
        detail["reaches"] = reaches(conn, team_id)
    audit.record(conn, action="connection.set", actor_id=actor_id, team_id=team_id, target_kind="connection",
                 target_id=connection_id, detail=detail, ip=ip)
    return get(conn, connection_id)


def remove(conn: Connection, row: Any, *, actor_id: str, ip: str | None) -> None:
    used = conn.execute(select(webhooks.c.name).where(webhooks.c.connection_id == row.id).limit(1)).scalar()
    if used is not None:
        raise ConnError(409, f"Webhook {used} uses this connection. Change or delete the webhook first.")
    project = conn.execute(select(projects.c.name).where(projects.c.connection_id == row.id).limit(1)).scalar()
    if project is not None:
        raise ConnError(409, f"Project {project} uses this connection. Pick another first.")
    conn.execute(delete(github_installations).where(github_installations.c.connection_id == row.id))
    conn.execute(delete(connections).where(connections.c.id == row.id))
    github.forget(row.id)
    audit.record(conn, action="connection.delete", actor_id=actor_id, team_id=row.team_id, target_kind="connection",
                 target_id=row.id, detail={"name": row.name, "kind": row.kind}, ip=ip)


def verify(engine: Engine, keyring: Keyring, connection_id: str, *, actor_id: str, ip: str | None, now: datetime,
           transport: httpx.BaseTransport | None = None) -> Any:
    """Ask the vendor who the credentials are, and record the answer.

    Outbound calls need an ok connection. Inbound signature checks use its secrets whatever its state.
    """
    with engine.connect() as conn:
        row = get(conn, connection_id)
    result = None
    try:
        result = VENDORS[row.kind].verify(credentials(keyring, row), row.config or {}, now=now, transport=transport)
        problems = result.problems
    except VendorError as exc:
        problems = [str(exc)]
    except (SealError, ValueError, KeyError):
        problems = ["The stored credentials can't be opened. Enter them again."]
    except Exception:
        log.exception("verifying connection %s failed", row.id)
        problems = ["The check failed unexpectedly. See the server log."]
    state = "error" if problems else "ok"
    values: dict[str, Any] = {"state": state, "last_error": "\n".join(problems)[:ERROR_MAX] or None,
                              "verified_at": None if problems else now}
    if result is not None:
        values["identity"] = result.identity
    with engine.begin() as conn:
        done = conn.execute(update(connections).where(connections.c.id == row.id,
                                                      connections.c.updated_at == row.updated_at).values(**values))
        if done.rowcount != 1:
            raise ConnError(409, "The connection changed while it was being checked. Verify it again.")
        if result is not None and row.kind == "github_app":
            conn.execute(delete(github_installations).where(github_installations.c.connection_id == row.id))
            if result.installations:
                conn.execute(insert(github_installations), [{"id": new_id(), "connection_id": row.id, **item}
                                                            for item in result.installations])
        audit.record(conn, action="connection.verify", actor_id=actor_id, team_id=row.team_id,
                     target_kind="connection", target_id=row.id,
                     detail={"name": row.name, "kind": row.kind, "state": state, "problems": len(problems)}, ip=ip)
        return get(conn, row.id)


def resolve(conn: Connection, *, team_id: str, project_id: str | None, kind: str, name: str | None = None,
            owner: str | None = None) -> Any:
    """The connection of ``kind`` a project's run uses: the project's own, else its team's.

    ``name`` picks one when a scope has several. A GitHub App connection must have an installation on ``owner``.
    A gh CLI connection reaches every owner its login can.
    """
    for scope in [s for s in (project_id, team_id) if s]:
        query = select(connections).where(connections.c.scope == scope, connections.c.kind == kind)
        if name is not None:
            query = query.where(connections.c.name == name)
        rows = conn.execute(query).all()
        if owner is not None and kind == "github_app":
            installed = set(conn.execute(select(github_installations.c.connection_id).where(
                github_installations.c.owner == owner.lower())).scalars())
            rows = [r for r in rows if r.id in installed or github.uses_gh_cli(r.config)]
        if len(rows) > 1:
            raise ConnError(409, f"More than one {kind} connection fits. Name the one to use.")
        if rows:
            return rows[0]
    raise ConnError(404, f"No {kind} connection fits this project or its team.")


def reaches(conn: Connection, team_id: str) -> list[str]:
    """The repositories of a team's projects, which a gh CLI connection on the team can act on."""
    return sorted({r for r in conn.execute(select(projects.c.repo).where(projects.c.team_id == team_id)).scalars()
                   if r})


def gh_cli_in_team(conn: Connection, team_id: str) -> bool:
    """Whether the team has a gh CLI connection, which reaches every repository the server's login can."""
    return any(github.uses_gh_cli(config) for config in conn.execute(select(connections.c.config).where(
        connections.c.team_id == team_id, connections.c.kind == "github_app")).scalars())


def for_webhook(conn: Connection, team_id: str, connection_id: Any, source: str) -> Any:
    """The connection a team's webhook may verify with: a team connection, never a project's, of the source's kind."""
    row = conn.execute(select(connections).where(connections.c.id == connection_id)).first() \
        if isinstance(connection_id, str) else None
    if row is None or row.team_id != team_id:
        raise ConnError(400, "No such connection in this team.")
    if row.project_id is not None:
        raise ConnError(400, "A team's webhook can't use a project's connection. Use a team connection.")
    if SOURCE_KIND.get(source) != row.kind:
        raise ConnError(400, f"A {source} webhook can't use a {row.kind} connection.")
    return row


def webhook_secret(keyring: Keyring, row: Any) -> str | None:
    try:
        return credentials(keyring, row).get(WEBHOOK_SECRET[row.kind]) or None
    except (SealError, ValueError):
        return None


def slack_pins(row: Any) -> dict[str, str | None]:
    """The workspace Verify found and the configured app a Slack connection's deliveries must come from."""
    pins = {"team_id": (row.identity or {}).get("team_id")}
    if (row.config or {}).get("app_id"):
        pins["api_app_id"] = row.config["app_id"]
    return pins

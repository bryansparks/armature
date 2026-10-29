"""The authoring assistant: a change described in words comes back as a validated body and a diff, never saved.

A request starts or continues a ``workflow-assistant`` conversation, whose turn
runs on a runner. The server never calls a model. The turn sees the current
body and the messages as fenced data, the workflow schema or the role rules,
the installed packs, and, for a non-admin, the fields they may change. Its
draft is validated here with the user's own permissions.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, and_, select

from armature.server import audit, conversations, defs, llm, rbac
from armature.server.auth import count_call
from armature.server.db.schema import memberships, projects
from armature.spec.models import HarnessSpec

KINDS = ("workflow", "document")
RATE_LIMIT = 30
RATE_WINDOW = timedelta(hours=1)
TEAM_DAILY_LIMIT = 300
TEAM_WINDOW = timedelta(days=1)
MAX_IN_FLIGHT = 2
INSTRUCTION_MAX = 4000
NAME_MAX = 100
CATALOG_MAX = 20_000
DEFAULT_ALIAS = "lead"
WORKFLOW_RULES = """Workflow rules:
- The body is YAML that matches the JSON Schema below. No anchors or aliases.
- A step runs an installed pack adapter, named pack/adapter, with parameters under `with:` that match its schema.
- Every {{ }} holds only a dotted or indexed reference to an input or a step output, such as {{ resolve.number }},
  and only inside a string value, never in a key or a comment.
- Never add adapters with a raw cmd, and never set pack_dir or params on an adapter."""

DOCUMENT_RULES = """Document rules:
- A {kind} is Markdown. It may start with YAML front matter between two lines of ---.
- A role's front matter needs a name. tools, mcp_servers, and mcp_inherit are lists.
- Keep the body as plain Markdown text."""

NON_ADMIN_WORKFLOW = """This user is not an admin, so a save is refused unless every other field matches the
saved version exactly. They may change only:
- top level: {top}
- each step: {stage}, and a step's adapter only to a pack adapter (pack/adapter) or one the saved version defines
- a step's role: {role}
- timeout_s up to {timeout}
They may add or remove steps that hold only those fields."""

NON_ADMIN_DOCUMENT = """This user is not an admin, so they may add to tools only from {tools}, and to mcp_servers
and mcp_inherit only from {mcp}. Removing entries is fine."""



class AssistError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Target:
    kind: defs.Kind
    row: Any | None
    team_id: str
    doc_kind: str | None
    previous_body: str | None
    project_id: str | None = None


def _member(access: rbac.Access, team_id: str) -> bool:
    return access.is_admin or team_id in access.teams or team_id in access.project_teams.values()


def _creates_in_team(access: rbac.Access, team_id: str) -> bool:
    if defs.can_write(access, defs.DefScope("team", team_id)):
        return True
    return any(access.project_teams.get(project) == team_id and rbac.at_least(role, "project_lead")
               for project, role in access.projects.items())


def _picked_team(conn, access: rbac.Access, team_id: Any) -> str:
    if not isinstance(team_id, str) or not team_id:
        raise AssistError(400, "Pick a team whose provider the assistant uses.")
    try:
        rbac.team_target(conn, team_id)
    except rbac.NotFound as exc:
        raise AssistError(404, "Not found.") from exc
    if not _member(access, team_id):
        raise AssistError(404, "Not found.")
    return team_id


def default_team(conn, user_id: str | None, alias: str) -> tuple[str, str | None]:
    """The team, and project, of the user's oldest membership whose providers define ``alias``."""
    rows = conn.execute(
        select(memberships.c.scope_kind, memberships.c.scope_id, projects.c.team_id)
        .select_from(memberships.outerjoin(projects, and_(memberships.c.scope_kind == "project",
                                                          projects.c.id == memberships.c.scope_id)))
        .where(memberships.c.user_id == user_id)
        .order_by(memberships.c.created_at, memberships.c.id)).all()
    for r in rows:
        team = r.scope_id if r.scope_kind == "team" else r.team_id
        project = r.scope_id if r.scope_kind == "project" else None
        if team and alias in llm.team_aliases(conn, team, project):
            return team, project
    raise AssistError(400, f"Pick a team whose provider the assistant uses. None of yours defines the alias {alias}.")


def _instance_team(conn, access: rbac.Access, team_id: Any, user_id: str | None,
                   alias: str) -> tuple[str, str | None]:
    if team_id:
        return _picked_team(conn, access, team_id), None
    return default_team(conn, user_id, alias)


def resolve_target(conn, access: rbac.Access, *, kind: Any, object_id: Any, team_id: Any,
                   doc_kind: Any, user_id: str | None = None, alias: str = DEFAULT_ALIAS,
                   scope_kind: Any = None, scope_id: Any = None) -> Target:
    """The object's team, or for an instance object or a new one the picked team, after the edit check.

    With no team picked, an instance object, or a new one for the instance, uses the user's default team.
    """
    if kind not in KINDS:
        raise AssistError(400, f"kind must be one of {', '.join(KINDS)}.")
    def_kind = defs.WORKFLOW if kind == "workflow" else defs.DOCUMENT
    if object_id:
        try:
            row = defs.readable_row(conn, def_kind, str(object_id), access)
            defs.authorize_write(access, defs.row_scope(row))
        except defs.DefError as exc:
            raise AssistError(exc.status, exc.message) from exc
        team, project = ((row.team_id, row.project_id) if row.team_id is not None
                         else _instance_team(conn, access, team_id, user_id, alias))
        return Target(def_kind, row, team, getattr(row, "kind", None),
                      defs.body_of(conn, def_kind, row.id, row.current_version), project)
    if scope_kind == "instance":
        if not access.is_admin:
            raise AssistError(403, "Not allowed.")
        team, project = _instance_team(conn, access, team_id, user_id, alias)
        return Target(def_kind, None, team, doc_kind if kind == "document" else None, None, project)
    if not team_id and scope_kind == "team":
        team_id = scope_id
    elif not team_id and scope_kind == "project" and isinstance(scope_id, str) and scope_id:
        try:
            team_id = rbac.project_target(conn, scope_id).team_id
        except rbac.NotFound as exc:
            raise AssistError(404, "Not found.") from exc
    team = _picked_team(conn, access, team_id)
    if not _creates_in_team(access, team):
        raise AssistError(403, "Not allowed.")
    project = None
    if scope_kind == "project" and isinstance(scope_id, str) and scope_id:
        try:
            # The project's aliases apply only when it belongs to the team the target resolved to.
            project = scope_id if rbac.project_target(conn, scope_id).team_id == team else None
        except rbac.NotFound as exc:
            raise AssistError(404, "Not found.") from exc
    return Target(def_kind, None, team, doc_kind if kind == "document" else None, None, project)


def fence(label: str, text: str, tag: str) -> str:
    return f"<<<{label} {tag}\n{text}\n{label} {tag}>>>"


def catalog_text(catalog: Any) -> str:
    packs = getattr(catalog, "packs", None)
    lines = []
    for pack in (packs() if callable(packs) else []):
        for name, adapter in sorted(pack.adapters.items()):
            lines.append(json.dumps({"ref": pack.ref(name), "description": adapter.description,
                                     "params": adapter.params, "writes": adapter.writes}, separators=(",", ":")))
    text = "\n".join(lines)
    return text[:CATALOG_MAX] if text else "(no packs are installed)"


def _allowlist(catalog: Any, name: str) -> str:
    method = getattr(catalog, name, None)
    items = list(method()) if callable(method) else []
    return ", ".join(items) if items else "nothing"


def rules(target: Target, access: rbac.Access, catalog: Any) -> str:
    if target.kind is defs.WORKFLOW:
        parts = [WORKFLOW_RULES, "JSON Schema of a workflow:\n"
                 + json.dumps(HarnessSpec.model_json_schema(), separators=(",", ":")),
                 "Installed pack adapters, one JSON object per line:\n" + catalog_text(catalog)]
        if not access.is_admin:
            parts.append(NON_ADMIN_WORKFLOW.format(top=", ".join(defs.TOP_MUTABLE),
                                                   stage=", ".join(defs.STAGE_MUTABLE),
                                                   role=", ".join(defs.ROLE_MUTABLE), timeout=defs.TIMEOUT_MAX))
        return "\n\n".join(parts)
    parts = [DOCUMENT_RULES.format(kind=target.doc_kind or "document")]
    if not access.is_admin:
        parts.append(NON_ADMIN_DOCUMENT.format(tools=_allowlist(catalog, "tools_allowlist"),
                                               mcp=_allowlist(catalog, "mcp_allowlist")))
    return "\n\n".join(parts)


def validate(target: Target, access: rbac.Access, body: str, catalog: Any) -> defs.Validation:
    if target.kind is defs.WORKFLOW:
        return defs.validate_workflow(body, actor=access, previous_body=target.previous_body, catalog=catalog)
    return defs.validate_document(target.doc_kind or "", body, actor=access, previous_body=target.previous_body,
                                  catalog=catalog)


def unified(before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile="current", tofile="proposed"))


def _check_text(value: Any, what: str) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > defs.BODY_MAX):
        raise AssistError(400, f"{what} is text of at most {defs.BODY_MAX // 1024} KB.")
    return value


def check_name(name: Any) -> str | None:
    if name in (None, ""):
        return None
    if not isinstance(name, str) or len(name) > NAME_MAX:
        raise AssistError(400, f"name is at most {NAME_MAX} characters.")
    return name.strip() or None


def check_request(body: Any, instruction: Any, alias: Any) -> tuple[str | None, str, str]:
    body = _check_text(body, "body")
    if not isinstance(instruction, str) or not instruction.strip():
        raise AssistError(400, "Say what to change.")
    if len(instruction) > INSTRUCTION_MAX:
        raise AssistError(400, f"An instruction is at most {INSTRUCTION_MAX} characters.")
    alias = DEFAULT_ALIAS if alias in (None, "") else alias
    if not isinstance(alias, str) or not defs.ALIAS.fullmatch(alias):
        raise AssistError(400, "alias is not valid.")
    return body, instruction.strip(), alias


def _continued(conn, user_id: str, conversation_id: Any) -> Any:
    try:
        conv = conversations.get(conn, str(conversation_id))
    except conversations.ConversationError as exc:
        raise AssistError(404, "Not found.") from exc
    if conv.owner_id != user_id or "target" not in (conv.data or {}):
        raise AssistError(404, "Not found.")
    if conv.state != "open":
        raise AssistError(409, "This conversation is closed. Ask again to start a new one.")
    return conv


def ask(engine: Engine, *, access: rbac.Access, user_id: str, payload: dict[str, Any], now: datetime,
        ip: str | None, team_daily_limit: int = TEAM_DAILY_LIMIT) -> dict[str, Any]:
    """Start a workflow-assistant conversation about one definition, or add to the user's own one.

    Its turn runs on a runner and drafts there. The user's edit rights are checked now and again on the draft.
    """
    body, instruction, alias = check_request(payload.get("body"), payload.get("instruction"), payload.get("alias"))
    name = check_name(payload.get("name"))
    with engine.connect() as conn:
        target = resolve_target(conn, access, kind=payload.get("kind"), object_id=payload.get("id"),
                                team_id=payload.get("team_id"), doc_kind=payload.get("doc_kind"), user_id=user_id,
                                alias=alias, scope_kind=payload.get("scope_kind"), scope_id=payload.get("scope_id"))
        continued = _continued(conn, user_id, payload["conversation_id"]) if payload.get("conversation_id") else None
    base_version = payload.get("base_version")
    if base_version is not None and target.row is not None and base_version != target.row.current_version:
        raise AssistError(409, "The workflow changed since you opened it. Reload it and ask again.")
    count_call(engine, key=f"assist:{user_id}", limit=RATE_LIMIT, now=now, window=RATE_WINDOW)
    count_call(engine, key=f"assist-team:{target.team_id}", limit=team_daily_limit, now=now, window=TEAM_WINDOW)
    kind = "workflow" if target.kind is defs.WORKFLOW else "document"
    with engine.begin() as conn:
        workflow_id = conversations.assistant_workflow(conn, now)
        if conversations.busy(conn, user_id, workflow_id) >= MAX_IN_FLIGHT and continued is None:
            raise AssistError(429, f"You already have {MAX_IN_FLIGHT} assistant requests going. Wait for one.")
        if continued is None:
            data = {"target": {"kind": kind, "id": target.row.id if target.row is not None else None,
                               "team_id": target.team_id, "doc_kind": target.doc_kind, "alias": alias,
                               "scope_kind": payload.get("scope_kind"), "scope_id": payload.get("scope_id"),
                               "name": name}}
            if body is not None and body != (target.previous_body or ""):
                data["start"] = body
            conversation_id = conversations.open_conversation(
                conn, team_id=target.team_id, project_id=target.project_id, workflow_id=workflow_id, kind="web",
                thread=None, now=now, owner_id=user_id, data=data)
        else:
            conversation_id = continued.id
        accepted = conversations.accept(conn, conversation_id, author=user_id, text=instruction, now=now)
        audit.record(conn, action="assist.request", actor_id=user_id, team_id=target.team_id, target_kind=kind,
                     target_id=target.row.id if target.row is not None else None,
                     detail={"alias": alias, "conversation_id": conversation_id, "job_id": accepted["job_id"]},
                     ip=ip)
        return {"conversation": conversations.public(conn, conversations.get(conn, conversation_id)),
                "seq": accepted["seq"]}

"""Turn an armature-ops workflow into database rows for one project.

Each ops workflow has a module whose ``rows`` returns the workflow body, the
documents it needs, and its webhook rules, with every prompt and template
copied in, since the platform has no workflow files. ``import_ops`` writes
them through the defs and webhook layers, so validation and audit apply. It
upserts by name and never deletes. A new rule starts in shadow and dry, and a
later import keeps the rule's shadow, dry, and enabled flags, so it never undoes
a cutover. A new rule's ``order`` is past every rule already on its webhook, so
rules never tie, and a later import keeps it. Each import records a hash of what it wrote, in the version note and
the rule's ``import_hash``. A row edited since then is overwritten only with
``force``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, or_, select

from armature.server import broker, defs, rbac, webhooks
from armature.server.db.schema import (
    documents,
    projects,
    teams,
    triggers,
    versions,
    workflows,
)
from armature.server.db.schema import webhooks as webhooks_table

ADMIN = rbac.Access(is_admin=True)
NOTE = "Imported from armature-ops"
NOTE_HASH = re.compile(r"^Imported from armature-ops \(sha256 ([0-9a-f]{64})\)", re.MULTILINE)
KEPT_RULE_FLAGS = ("shadow", "dry")
EDITED = "{what} {name} was edited since import, pass --force to overwrite."


class MigrateError(Exception):
    pass


@dataclass(frozen=True)
class Document:
    kind: str
    key: str
    body: str
    # A document every project of the team shares, such as a role, lives on the team.
    team: bool = False


@dataclass(frozen=True)
class Webhook:
    name: str
    source: str
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    webhook: Webhook
    config: dict[str, Any]
    # Tells two rules of one workflow on one webhook apart in the change list.
    name: str = ""


def _identity(config: dict[str, Any]) -> str:
    """A rule's events and first condition, which tell one imported rule from another of the same workflow."""
    return json.dumps([config.get("events") or [], (config.get("conditions") or [None])[0]], sort_keys=True)


@dataclass(frozen=True)
class Poll:
    """A poll or schedule trigger (S23). It starts disabled, and an import never changes whether it is on."""
    kind: str
    config: dict[str, Any]


@dataclass(frozen=True)
class Rows:
    name: str
    kind: str
    body: str
    documents: tuple[Document, ...] = ()
    rules: tuple[Rule, ...] = ()
    notes: tuple[str, ...] = ()
    # Workflows the body calls, as (name, kind, body), upserted into the same project first.
    callees: tuple[tuple[str, str, str], ...] = ()
    # Whether the body runs S14b's review roles, which the import seeds on the instance when missing.
    review_roles: bool = False
    polls: tuple[Poll, ...] = ()
    # Project settings the workflow's tools read, such as the push gate's author allowlist.
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Change:
    what: str
    name: str
    action: str
    detail: str = ""

    def line(self) -> str:
        return f"{self.what} {self.name}: {self.action}" + (f" ({self.detail})" if self.detail else "")


def resolve_project(conn: Connection, text: str) -> tuple[str, str]:
    """The team and project ``team/project`` names, by team slug or name, else the one project whose repo it is."""
    team, _, name = text.partition("/")
    found = conn.execute(select(projects.c.team_id, projects.c.id)
                         .select_from(projects.join(teams, teams.c.id == projects.c.team_id))
                         .where(or_(teams.c.slug == team, teams.c.name == team), projects.c.name == name)).all()
    if not found:
        found = conn.execute(select(projects.c.team_id, projects.c.id)
                             .where(func.lower(projects.c.repo) == text.lower())).all()
    if len(found) != 1:
        raise MigrateError(f"{text} matches {len(found) or 'no'} projects. Name one as team/project or by its "
                           "repository.")
    return found[0].team_id, found[0].id


def registry() -> dict[str, Callable[[Callable[[str], Any], Any], Rows]]:
    from armature.server.migrate_ops import (
        build_announce,
        code_review,
        development,
        ios_port,
        localization,
        pr_fix,
        pr_merged,
        repo_review,
        slack_intake,
    )
    return {"build-announce": build_announce.rows, "pr-merged": pr_merged.rows, "code-review": code_review.rows,
            "slack-intake": slack_intake.rows, "repo-review": repo_review.rows,
            "pr-fix": pr_fix.rows, "development": development.rows,
            "ios-port": ios_port.rows, "localization": localization.rows}


def digest(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _imported(conn: Connection, kind: defs.Kind, row_id: str) -> str | None:
    """The hash the last import recorded in a version note, or None when nothing was imported."""
    notes = conn.execute(select(versions.c.note).where(versions.c.object_kind == kind.object_kind,
                                                       versions.c.object_id == row_id)
                         .order_by(versions.c.version.desc())).scalars()
    return next((found[1] for found in map(NOTE_HASH.search, (n or "" for n in notes)) if found), None)


def _upsert(conn: Connection, kind: defs.Kind, row: Any, *, scope: defs.DefScope, name: str, body: str,
            row_kind: str, actor_id: str | None, now: datetime, catalog: Any, force: bool) -> str:
    note = f"{NOTE} (sha256 {digest(body)})."
    if row is None:
        defs.create(conn, kind, access=ADMIN, scope=scope, name=name, body=body, note=note, actor_id=actor_id,
                    now=now, ip=None, doc_kind=row_kind, catalog=catalog)
        return "created"
    current = defs.body_of(conn, kind, row.id, row.current_version)
    if current == body:
        return "unchanged"
    if not force and digest(current or "") != _imported(conn, kind, row.id):
        raise MigrateError(EDITED.format(what=row_kind if kind is defs.DOCUMENT else "Workflow", name=name))
    saved, _ = defs.save(conn, kind, row, access=ADMIN, body=body, note=note, base_version=row.current_version,
                         actor_id=actor_id, now=now, ip=None, catalog=catalog)
    return f"updated to version {saved.current_version}"


def _document(conn: Connection, doc: Document, team_id: str, project_id: str, actor_id: str | None,
              now: datetime, catalog: Any, force: bool) -> Change:
    scope = defs.DefScope("team", team_id) if doc.team else defs.DefScope("project", team_id, project_id)
    row = conn.execute(select(documents).where(documents.c.scope == scope.scope, documents.c.kind == doc.kind,
                                               documents.c.key == doc.key)).first()
    action = _upsert(conn, defs.DOCUMENT, row, scope=scope, name=doc.key, body=doc.body, row_kind=doc.kind,
                     actor_id=actor_id, now=now, catalog=catalog, force=force)
    return Change(doc.kind, doc.key, action, scope.kind)


def upsert_documents(conn: Connection, docs: tuple[Document, ...], *, team_id: str, project_id: str,
                     now: datetime, actor_id: str | None = None, catalog: Any = None,
                     force: bool = False) -> list[Change]:
    """Upsert ``docs`` as import_ops does, on the team or the project each one names."""
    return [_document(conn, doc, team_id, project_id, actor_id, now, catalog, force) for doc in docs]


def _webhook(conn: Connection, hook: Webhook, team_id: str, actor_id: str | None, now: datetime) -> tuple[Any, Change]:
    row = conn.execute(select(webhooks_table).where(webhooks_table.c.team_id == team_id,
                                                    webhooks_table.c.name == hook.name)).first()
    if row is not None:
        if row.source != hook.source:
            raise MigrateError(f"The team's webhook {hook.name} is a {row.source} webhook, not {hook.source}. "
                               "Rename it first.")
        return row, Change("webhook", hook.name, "unchanged")
    row = webhooks.create(conn, team_id=team_id, name=hook.name, body={"source": hook.source, **hook.settings},
                          actor_id=actor_id, now=now, ip=None)
    return row, Change("webhook", hook.name, "created",
                       f"pending approval; set secret {row.secret_name}" if row.secret_name else "pending approval")


def _content(config: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in webhooks.check_rule(config).items()
            if k not in (*KEPT_RULE_FLAGS, "order", "import_hash")}


def _rule(conn: Connection, rule: Rule, hook: Any, workflow: Any, actor_id: str | None, now: datetime,
          force: bool) -> Change:
    found = [r for r in conn.execute(select(triggers).where(triggers.c.webhook_id == hook.id,
                                                            triggers.c.workflow_id == workflow.id,
                                                            triggers.c.kind == webhooks.KIND))
             if _identity(r.config or {}) == _identity(rule.config)]
    name = f"{hook.name} -> {workflow.name}" + (f" ({rule.name})" if rule.name else "")
    if len(found) > 1:
        raise MigrateError(f"Webhook {hook.name} has {len(found)} rules for {workflow.name}. Keep one and import "
                           "again.")
    wanted = _content(rule.config)
    stamped = {**rule.config, "import_hash": digest(wanted)}
    if not found:
        taken = [(r.config or {}).get("order", 0) for r in webhooks.rules_of(conn, hook.id)]
        order = max(taken) + 1 if taken else 0
        webhooks.create_rule(conn, hook, body={"workflow_id": workflow.id,
                                               "config": {**stamped, "order": order, "shadow": True, "dry": True}},
                             actor_id=actor_id, now=now, ip=None)
        return Change("rule", name, "created", "shadow, dry")
    current = found[0].config or {}
    kept = {k: current.get(k, False) for k in KEPT_RULE_FLAGS}
    state = ", ".join(k for k, on in kept.items() if on) or "live"
    config = webhooks.check_rule({**stamped, **kept, "order": current.get("order", 0)})
    if config == webhooks.check_rule(current):
        return Change("rule", name, "unchanged", state)
    if not force and _content(current) != wanted and digest(_content(current)) != current.get("import_hash"):
        raise MigrateError(EDITED.format(what="Rule", name=name))
    webhooks.update_rule(conn, found[0], body={"config": config}, actor_id=actor_id, ip=None)
    return Change("rule", name, "updated", state)


def _poll(conn: Connection, poll: Poll, workflow: Any, team_id: str, project_id: str, actor_id: str | None,
          now: datetime, force: bool) -> Change:
    from armature.server import connections, pollers
    config = dict(poll.config)
    if config.get("connection_id") == "@jira":
        try:
            config["connection_id"] = connections.resolve(conn, team_id=team_id, project_id=project_id,
                                                          kind="jira").id
        except connections.ConnError as exc:
            raise MigrateError(f"Add the team's Jira connection first: {exc.message}") from exc
    found = conn.execute(select(triggers).where(triggers.c.workflow_id == workflow.id,
                                                triggers.c.kind == poll.kind)).all()
    name = f"{poll.kind} -> {workflow.name}"
    if len(found) > 1:
        raise MigrateError(f"{workflow.name} has {len(found)} {poll.kind} triggers. Keep one and import again.")
    if not found:
        pollers.create(conn, team_id=team_id, body={"workflow_id": workflow.id, "kind": poll.kind,
                                                    "config": config, "enabled": False},
                       actor_id=actor_id, now=now, ip=None)
        return Change("trigger", name, "created", "disabled")
    state = "enabled" if found[0].enabled else "disabled"
    if "shadow" in (found[0].config or {}):
        # Like a rule's, the shadow flag is the user's cutover and survives an import.
        config["shadow"] = found[0].config["shadow"]
    if pollers.check_config(poll.kind, config, now) == found[0].config:
        return Change("trigger", name, "unchanged", state)
    if not force:
        raise MigrateError(EDITED.format(what="Trigger", name=name))
    pollers.update_trigger(conn, found[0], body={"config": config}, actor_id=actor_id, now=now, ip=None)
    return Change("trigger", name, "updated", state)


def _setting(conn: Connection, key: str, value: Any, team_id: str, project_id: str, actor_id: str | None,
             now: datetime, force: bool) -> Change:
    from armature.server.db.schema import settings
    current = conn.execute(select(settings.c.value).where(settings.c.scope == project_id,
                                                          settings.c.key == key)).first()
    if current is not None and current.value == value:
        return Change("setting", key, "unchanged", "project")
    if current is not None and not force:
        raise MigrateError(EDITED.format(what="Setting", name=key))
    defs.put_setting(conn, team_id=team_id, project_id=project_id, key=key, value=value, actor_id=actor_id, now=now,
                     ip=None)
    return Change("setting", key, "updated" if current is not None else "created", "project")


def import_ops(conn: Connection, name: str, *, team_id: str, project_id: str, now: datetime,
               actor_id: str | None = None, catalog: Any = None,
               force: bool = False) -> tuple[list[Change], tuple[str, ...]]:
    """Upsert one ops workflow's rows into the project. Returns what changed and the module's notes for the user."""
    build = registry().get(name)
    if build is None:
        raise MigrateError(f"No ops workflow {name}. Known: {', '.join(sorted(registry()))}.")
    project = conn.execute(select(projects).where(projects.c.id == project_id)).one()
    rows = build(lambda key: broker.setting(conn, key, team_id, project_id), project)
    changes = []
    if rows.review_roles:
        from armature.server import starters
        changes += [Change("role", key, "seeded", "instance") for key in starters.seed_roles(conn, now)]
    changes += [_setting(conn, key, value, team_id, project_id, actor_id, now, force)
                for key, value in rows.settings.items()]
    changes += upsert_documents(conn, rows.documents, team_id=team_id, project_id=project_id, now=now,
                                actor_id=actor_id, catalog=catalog, force=force)
    for wanted, kind, body in (*rows.callees, (rows.name, rows.kind, rows.body)):
        row = conn.execute(select(workflows).where(workflows.c.scope == project_id,
                                                   workflows.c.name == wanted)).first()
        action = _upsert(conn, defs.WORKFLOW, row, scope=defs.DefScope("project", team_id, project_id), name=wanted,
                         body=body, row_kind=kind, actor_id=actor_id, now=now, catalog=catalog, force=force)
        changes.append(Change("workflow", wanted, action))
    workflow = conn.execute(select(workflows).where(workflows.c.scope == project_id,
                                                    workflows.c.name == rows.name)).one()
    for rule in rows.rules:
        hook, change = _webhook(conn, rule.webhook, team_id, actor_id, now)
        changes += [change, _rule(conn, rule, hook, workflow, actor_id, now, force)]
    changes += [_poll(conn, poll, workflow, team_id, project_id, actor_id, now, force) for poll in rows.polls]
    return changes, rows.notes

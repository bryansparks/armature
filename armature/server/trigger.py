"""What started a run, as a short label and an optional detail for the run list."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from armature.server.db.schema import runner_keys, runs, users, webhook_deliveries, webhooks

KIND_LABELS = {"pr": "Pull request", "jira": "Jira", "linear": "Linear", "schedule": "Schedule", "cron": "Schedule",
               "slack": "Slack", "webhook": "Webhook", "cli": "Command line", "dry_run": "Test run", "test": "Test run"}


def columns() -> list:
    """The run columns ``describe`` reads, to add to a run query."""
    return [runs.c.trigger_kind.label("trigger_kind"), runs.c.trigger_ref.label("trigger_ref"),
            runs.c.triggered_by.label("trigger_user_id"), runs.c.actor.label("trigger_actor"),
            runs.c.runner_key_id.label("trigger_key_id")]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _names(conn: Connection, id_column: Any, name_column: Any, ids: set[str]) -> dict[str, Any]:
    if not ids:
        return {}
    return {row[0]: row[1] for row in conn.execute(select(id_column, name_column).where(id_column.in_(sorted(ids))))}


def _generic(kind: str | None, ref: dict[str, Any], actor: str | None, key_name: str | None) -> dict[str, Any]:
    label = KIND_LABELS.get(kind or "") or (kind.replace("_", " ").replace("-", " ").title() if kind else "Unknown")
    detail = None
    if kind == "pr" and _text(ref.get("repo")):
        number = ref.get("number")
        detail = f"{ref['repo']}#{number}" if isinstance(number, int) and not isinstance(number, bool) else ref["repo"]
    elif kind in ("jira", "linear"):
        detail = _text(ref.get("issue"))
    elif kind == "schedule":
        detail = _text(ref.get("cron"))
    if detail is None and actor and actor != key_name:
        detail = actor
    return {"label": label, "detail": detail}


def describe(conn: Connection, rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each run's trigger by run id, with one query per lookup table for the whole batch.

    Rows carry ``id`` and the fields ``columns`` names.
    """
    rows = list(rows)
    refs = {row["id"]: _dict(row["trigger_ref"]) for row in rows}
    hooked = [refs[row["id"]] for row in rows if row["trigger_kind"] == "webhook"]
    hook_ids = {_text(ref.get("webhook_id")) for ref in hooked} - {None}
    delivery_ids = {_text(ref.get("delivery_id")) for ref in hooked} - {None}
    hooks = _names(conn, webhooks.c.id, webhooks.c.name, hook_ids)
    events = _names(conn, webhook_deliveries.c.id, webhook_deliveries.c.event, delivery_ids)
    names = _names(conn, users.c.id, users.c.username, {row["trigger_user_id"] for row in rows} - {None})
    keys = _names(conn, runner_keys.c.id, runner_keys.c.name, {row["trigger_key_id"] for row in rows} - {None})
    described = {}
    for row in rows:
        ref = refs[row["id"]]
        hook = hooks.get(_text(ref.get("webhook_id"))) if row["trigger_kind"] == "webhook" else None
        username = names.get(row["trigger_user_id"])
        if hook is not None:
            described[row["id"]] = {"label": hook, "detail": events.get(_text(ref.get("delivery_id")))}
        elif username is not None:
            described[row["id"]] = {"label": username, "detail": "manual"}
        else:
            key_name = keys.get(row["trigger_key_id"])
            described[row["id"]] = _generic(row["trigger_kind"], ref, row["trigger_actor"], key_name)
    return described


def describe_one(conn: Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(select(runs.c.id, *columns()).where(runs.c.id == run_id)).mappings().first()
    return describe(conn, [row])[run_id] if row else None

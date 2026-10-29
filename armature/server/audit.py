"""Append-only audit log."""
from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, insert

from armature.server.db import new_id, utcnow
from armature.server.db.schema import audit

# A commit after a write to one of these re-exports definitions when export_on_change is set.
DEFINITION_KINDS = frozenset({"workflow", "document", "provider", "setting", "team", "project", "connection"})
DEFINITIONS_CHANGED = "armature.definitions_changed"


def record(conn: Connection, *, action: str, actor_id: str | None, team_id: str | None = None,
           target_kind: str | None = None, target_id: str | None = None,
           detail: dict[str, Any] | None = None, ip: str | None = None) -> str:
    row_id = new_id()
    conn.execute(insert(audit).values(
        id=row_id, at=utcnow(), actor_id=actor_id, team_id=team_id, action=action,
        target_kind=target_kind, target_id=target_id, detail=detail, ip=ip,
    ))
    if action.partition(".")[0] in DEFINITION_KINDS:
        conn.info[DEFINITIONS_CHANGED] = True
    return row_id

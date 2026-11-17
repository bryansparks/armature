"""The categories a finding can carry. A new set is a new version, stored per team the first time the team uses it."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert, select
from sqlalchemy.exc import IntegrityError

from armature.server.db import new_id
from armature.server.db.schema import taxonomies

VERSION = "v1"
OTHER = "other"
CATEGORIES = {
    "correctness": "wrong result, wrong condition, off-by-one, broken logic",
    "crash-safety": "force unwraps, null dereference, unchecked casts, index out of range",
    "concurrency": "races, wrong thread or actor, unawaited work, ordering, cancellation",
    "lifecycle-memory": "retain cycles, leaks, work outliving its owner, observer cleanup",
    "error-handling": "swallowed errors, missing failure paths, bad fallbacks, silent failure",
    "security-privacy": "secrets, personal data, auth, injection, logging sensitive data",
    "data-contract": "API request or response shape, persistence, migrations, schema, serialization",
    "performance": "repeated work, heavy main-thread work, query cost, render cost, startup",
    "ui-layout": "layout, states (loading, empty, error), design system misuse",
    "accessibility": "labels, traits, dynamic type, contrast, focus order",
    "localization": "hard-coded strings, plurals, formatting, right-to-left",
    "testing": "missing or weak tests, tests that can't fail, flaky tests",
    "api-misuse": "wrong use of a platform or library API, deprecated APIs",
    "compatibility": "OS version availability, device differences, backward compatibility",
    "simplicity": "unneeded complexity, duplicated or reinvented code, dead code",
    "conventions": "repo rules, naming, structure, style the repo documents",
    "architecture": "wrong layer, coupling, module boundaries, ownership",
    "build-dependencies": "build config, dependency versions, project files, CI",
    "observability": "logging, analytics, metrics, crash reporting gaps",
    OTHER: "none of the above",
}


def definition() -> dict[str, Any]:
    return {"version": VERSION, "categories": [{"id": k, "description": v} for k, v in CATEGORIES.items()]}


def _stored(conn: Connection, team_id: str) -> Any:
    return conn.execute(select(taxonomies.c.definition).where(taxonomies.c.team_id == team_id,
                                                              taxonomies.c.version == VERSION)).scalar()


def ensure(conn: Connection, team_id: str, now: datetime) -> dict[str, Any]:
    """The team's stored copy of this version, stored now if it has none. The loser of a race reads the winner's."""
    found = _stored(conn, team_id)
    if found is not None:
        return found
    try:
        with conn.begin_nested():
            conn.execute(insert(taxonomies).values(id=new_id(), team_id=team_id, version=VERSION,
                                                   definition=definition(), created_at=now))
    except IntegrityError:
        pass
    return _stored(conn, team_id)

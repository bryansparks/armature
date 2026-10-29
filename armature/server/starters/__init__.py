"""Built-in workflow bodies and review roles a project starts from. A data step copies them into the database.

``code-review`` reviews a pull request through ``review-pass`` and, with auto_fix, pushes one fix and reviews
again. ``development-review`` is what a development job runs once its branch is pushed. The roles are the review
specialists' and the team lead's instructions, ported from armature-ops prompts/.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import Connection

from armature.server import defs

DIR = Path(__file__).resolve().parent
ROLES_DIR = DIR / "roles"
# Each starter's workflow kind. A webhook rule skips queuing a review of a head that already carries a mark.
KINDS = {"review-pass": "review", "code-review": "review", "development-review": "work"}
ROLES = ("simplicity", "correctness", "security", "performance", "beauty", "test_adequacy", "concurrency",
         "conventions", "compatibility", "team_lead")


def body(name: str) -> str:
    if name not in KINDS:
        raise KeyError(name)
    return (DIR / f"{name}.yaml").read_text(encoding="utf-8")


def role(key: str) -> str:
    if key not in ROLES:
        raise KeyError(key)
    return (ROLES_DIR / f"{key}.md").read_text(encoding="utf-8")


def seed_roles(conn: Connection, now: datetime) -> list[str]:
    """Add each review role the instance doesn't have yet as an instance role document. Returns the keys added.

    An existing role, edited or not, is left as it is.
    """
    added = []
    for key in ROLES:
        if defs._name_taken(conn, defs.DOCUMENT, defs.INSTANCE, key, "role"):
            continue
        defs._insert(conn, defs.DOCUMENT, defs.INSTANCE, key, "role", role(key), "Seeded from armature-ops prompts.",
                     None, now)
        added.append(key)
    return added

"""Encrypted secrets, per team or for the whole instance (team_id None).

Nothing here returns plaintext except ``resolve_for_run``, which a run calls
for the names its pack declares.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Connection, delete, insert, select, update

from armature.server import audit
from armature.server.crypto import Keyring
from armature.server.db import new_id, utcnow
from armature.server.db.schema import INSTANCE_SCOPE, scope_of, secrets

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class MissingSecrets(KeyError):
    """A run asked for secrets that are not set."""


class InstanceOnlySecrets(KeyError):
    """A run that may not receive instance secrets asked for ones set only for the instance."""


def aad(team_id: str | None, secret_id: str, name: str) -> dict[str, Any]:
    return {"team_id": team_id, "secret_id": secret_id, "name": name}


def _check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        raise ValueError("secret name must be an environment variable name of at most 128 characters")


def set_secret(conn: Connection, keyring: Keyring, *, team_id: str | None, name: str, value: str,
               actor_id: str | None) -> str:
    _check_name(name)
    scope = scope_of(team_id)
    existing = conn.execute(select(secrets.c.id).where(secrets.c.scope == scope, secrets.c.name == name)).first()
    secret_id = existing.id if existing else new_id()
    row = {
        "ciphertext": keyring.seal(value, aad(team_id, secret_id, name)),
        "key_id": keyring.key_id,
        "updated_at": utcnow(),
        "updated_by": actor_id,
    }
    if existing:
        conn.execute(update(secrets).where(secrets.c.id == secret_id).values(**row))
    else:
        conn.execute(insert(secrets).values(id=secret_id, team_id=team_id, scope=scope, name=name, **row))
    audit.record(conn, action="secret.set", actor_id=actor_id, team_id=team_id,
                 target_kind="secret", target_id=secret_id, detail={"name": name})
    return secret_id


def list_secrets(conn: Connection, team_id: str | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(secrets.c.name, secrets.c.updated_at, secrets.c.updated_by)
        .where(secrets.c.scope == scope_of(team_id)).order_by(secrets.c.name)
    )
    return [{"name": r.name, "set": True, "updated_at": r.updated_at, "updated_by": r.updated_by} for r in rows]


def delete_secret(conn: Connection, *, team_id: str | None, name: str, actor_id: str | None) -> bool:
    row = conn.execute(
        select(secrets.c.id).where(secrets.c.scope == scope_of(team_id), secrets.c.name == name)).first()
    if not row:
        return False
    conn.execute(delete(secrets).where(secrets.c.id == row.id))
    audit.record(conn, action="secret.delete", actor_id=actor_id, team_id=team_id,
                 target_kind="secret", target_id=row.id, detail={"name": name})
    return True


def resolve_for_run(conn: Connection, keyring: Keyring, team_id: str, names: list[str], *,
                    instance: bool = True) -> dict[str, str]:
    """A team's secret wins over an instance secret of the same name. Any missing name fails the call.

    Without ``instance``, a name set only for the instance fails it too.
    """
    wanted = set(names)
    if not wanted:
        return {}
    rows = conn.execute(
        select(secrets.c.id, secrets.c.scope, secrets.c.name, secrets.c.ciphertext)
        .where(secrets.c.name.in_(wanted), secrets.c.scope.in_([team_id, INSTANCE_SCOPE]))
    ).all()
    chosen = {}
    for r in sorted(rows, key=lambda r: r.scope != INSTANCE_SCOPE):
        chosen[r.name] = r
    missing = wanted - chosen.keys()
    if missing:
        raise MissingSecrets(sorted(missing))
    instance_only = sorted(name for name, r in chosen.items() if r.scope == INSTANCE_SCOPE)
    if instance_only and not instance:
        raise InstanceOnlySecrets(instance_only)
    # AAD comes from the scope the caller asked for, never from row columns a writer could change.
    return {name: keyring.open_(r.ciphertext, aad(None if r.scope == INSTANCE_SCOPE else team_id, r.id, name))
            for name, r in chosen.items()}


def resolve_instance(conn: Connection, keyring: Keyring, name: str) -> str | None:
    row = conn.execute(select(secrets.c.id, secrets.c.ciphertext)
                       .where(secrets.c.scope == INSTANCE_SCOPE, secrets.c.name == name)).first()
    return keyring.open_(row.ciphertext, aad(None, row.id, name)) if row else None

"""Runner keys, run registration, and the per-run event tokens runs stream with.

A runner key is a machine credential, ``ark_<random>``, shown once and stored
only as an HMAC. A team key registers runs in its team. An instance key
registers each run in the team that owns its project, else in the admin-only
``operations`` team. Registering a run with it mints a per-run event token,
also stored only as an HMAC, which expires after 48 hours or when the run ends.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets as _random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, delete, func, insert, or_, select, update

from armature.events import location
from armature.events.sign import sign_engine
from armature.events.summary import subject_problem, trigger_ref_problem
from armature.server import audit, threads
from armature.server.auth import valid_token
from armature.server.crypto import Keyring
from armature.server.db import new_id
from armature.server.db.schema import (
    environments,
    projects,
    run_tokens,
    runner_keys,
    runs,
    scope_of,
    teams,
)
from armature.server.defs import resolve_workflow

KEY_PREFIX = "ark_"
TOKEN_TTL = timedelta(hours=48)
TOUCH = timedelta(seconds=60)
WORKFLOW = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,190}$")
TRIGGER_KIND = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
SERVER_REF_KEYS = frozenset({"dry_run", "source", "import_hash"})
ACTOR_MAX = 191
THREAD_MAX = 191
OPERATIONS_SLUG = "operations"
OPERATIONS_NAME = "Operations (unassigned runs)"


class RegistrationError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class RunToken:
    run_id: str
    team_id: str
    expires_at: datetime


def digest(keyring: Keyring, secret: str) -> str:
    return hmac.new(keyring.subkey("runners"), secret.encode("ascii", "replace"), hashlib.sha256).hexdigest()


def engine_key(keyring: Keyring, run_id: str) -> str:
    """The key a run's engine signs with. Only the server can derive it again, and no step of the run holds it."""
    return hmac.new(keyring.subkey("runners"), b"engine:" + run_id.encode(), hashlib.sha256).hexdigest()


def engine_env(keyring: Keyring, run_id: str) -> str:
    """The run's ARMATURE_ENGINE_KEY: ``<run id>:<engine key>``."""
    return f"{run_id}:{engine_key(keyring, run_id)}"


def signed_suspend(keyring: Keyring, run_id: str, payload: Any) -> bool:
    """Whether a run_suspend event was signed by the run's engine. Anything else that holds its token can't."""
    names = ("step_id", "key", "digest", "ts", "sig")
    fields = [payload.get(name) for name in names] if isinstance(payload, dict) else []
    if len(fields) != len(names) or not all(isinstance(f, str) for f in fields):
        return False
    step_id, key, digest_, ts, sig = fields
    signed = {"kind": "run_suspend", "run_id": run_id, "step_id": step_id, "key": key, "digest": digest_, "ts": ts}
    return hmac.compare_digest(sign_engine(engine_key(keyring, run_id), signed), sig)


def public_key(row: Any) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "created_at": row.created_at, "created_by": row.created_by,
            "last_used_at": row.last_used_at}


def list_keys(conn: Connection, team_id: str | None) -> list[dict[str, Any]]:
    rows = conn.execute(select(runner_keys).where(runner_keys.c.scope == scope_of(team_id),
                                                  runner_keys.c.revoked_at.is_(None))
                        .order_by(runner_keys.c.name, runner_keys.c.id)).all()
    return [public_key(r) for r in rows]


def create_key(conn: Connection, keyring: Keyring, *, team_id: str | None, name: str, actor_id: str, now: datetime,
               ip: str | None) -> tuple[dict[str, Any], str]:
    key = KEY_PREFIX + _random.token_urlsafe(32)
    key_id = new_id()
    conn.execute(insert(runner_keys).values(id=key_id, team_id=team_id, name=name, key_hmac=digest(keyring, key),
                                            created_by=actor_id, created_at=now))
    audit.record(conn, action="runner_key.create", actor_id=actor_id, team_id=team_id, target_kind="runner_key",
                 target_id=key_id, detail={"name": name}, ip=ip)
    row = conn.execute(select(runner_keys).where(runner_keys.c.id == key_id)).one()
    return public_key(row), key


def revoke_key(conn: Connection, *, team_id: str | None, key_id: str, actor_id: str, now: datetime,
               ip: str | None) -> bool:
    result = conn.execute(update(runner_keys).where(runner_keys.c.id == key_id,
                                                    runner_keys.c.scope == scope_of(team_id),
                                                    runner_keys.c.revoked_at.is_(None)).values(revoked_at=now))
    if result.rowcount != 1:
        return False
    conn.execute(delete(run_tokens).where(run_tokens.c.run_id.in_(
        select(runs.c.id).where(runs.c.runner_key_id == key_id).scalar_subquery())))
    audit.record(conn, action="runner_key.delete", actor_id=actor_id, team_id=team_id, target_kind="runner_key",
                 target_id=key_id, ip=ip)
    return True


def authenticate(conn: Connection, keyring: Keyring, key: str | None, now: datetime) -> Any | None:
    if not key or not key.startswith(KEY_PREFIX) or not valid_token(key[len(KEY_PREFIX):]):
        return None
    row = conn.execute(select(runner_keys).where(runner_keys.c.key_hmac == digest(keyring, key))).first()
    if row is None or row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
        return None
    if row.last_used_at is None or now - row.last_used_at > TOUCH:
        conn.execute(update(runner_keys).where(runner_keys.c.id == row.id).values(last_used_at=now))
    return row


def _text(value: Any, what: str, limit: int, pattern: re.Pattern[str] | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistrationError(400, f"{what} must be a string.")
    value = "".join(ch for ch in value.strip() if ch.isprintable())[:limit]
    if pattern is not None and not pattern.fullmatch(value):
        raise RegistrationError(400, f"{what} is not valid.")
    return value or None


def register_run(conn: Connection, keyring: Keyring, *, key: Any, body: dict[str, Any], now: datetime,
                 ip: str | None) -> tuple[dict[str, Any], str]:
    """Create a running run and mint its event token. Returns the run row and the token."""
    if key.kind == "ephemeral" or conn.execute(select(environments.c.id)
                                               .where(environments.c.runner_key_id == key.id)).first():
        raise RegistrationError(403, "An environment's runner key runs only jobs the server hands it.")
    workflow = _text(body.get("workflow"), "workflow", 191, WORKFLOW)
    if workflow is None:
        raise RegistrationError(400, "workflow is required.")
    trigger_kind = _text(body.get("trigger_kind"), "trigger_kind", 32, TRIGGER_KIND)
    actor = _text(body.get("actor"), "actor", ACTOR_MAX)
    trigger_ref = body.get("trigger_ref")
    problem = trigger_ref_problem(trigger_ref) if trigger_ref is not None else None
    if problem:
        raise RegistrationError(400, f"{problem[0].upper()}{problem[1:]}.")
    subject = body.get("subject")
    if subject is not None and subject_problem(subject):
        raise RegistrationError(400, "subject is not valid.")
    thread = _text(body.get("thread"), "thread", THREAD_MAX, threads.THREAD)
    project = _text(body.get("project"), "project", 191)
    if key.team_id is None:
        team_id, project_id = _instance_target(conn, project, now, ip)
    else:
        team_id, project_id = key.team_id, None
        if project is not None:
            project_id = conn.execute(select(projects.c.id).where(
                projects.c.team_id == team_id, or_(projects.c.name == project, projects.c.repo == project))
                .order_by(projects.c.name != project).limit(1)).scalar()
            if project_id is None:
                raise RegistrationError(404, "No such project in this runner key's team.")
    rerun_of = body.get("rerun_of")
    if rerun_of is not None and (not isinstance(rerun_of, str) or conn.execute(
            select(runs.c.id).where(runs.c.id == rerun_of, runs.c.team_id == team_id)).first() is None):
        raise RegistrationError(404, "No such run to re-run in this runner key's team.")
    resolved = resolve_workflow(conn, workflow, team_id, project_id)
    workflow_id = resolved.id if resolved is not None else None
    row, token = open_run(conn, keyring, team_id=team_id, project_id=project_id, workflow_id=workflow_id,
                          workflow_name=workflow, now=now, actor=actor, trigger_kind=trigger_kind,
                          trigger_ref=_server_ref(trigger_ref), subject=subject, rerun_of=rerun_of,
                          runner_key_id=key.id, thread=thread,
                          location=location.parse(body.get("location")))
    audit.record(conn, action="run.register", actor_id=None, team_id=team_id, target_kind="run",
                 target_id=row["id"], detail={"runner_key_id": key.id, "workflow": workflow, "actor": actor}, ip=ip)
    return row, token


def _server_ref(trigger_ref: dict[str, Any] | None) -> dict[str, Any]:
    """The sender's reference with the server's own keys over it, so a runner cannot claim a dry run."""
    ref = {k: v for k, v in (trigger_ref or {}).items() if k not in SERVER_REF_KEYS}
    return {**ref, "source": "runner"}


def _instance_target(conn: Connection, project: str | None, now: datetime,
                     ip: str | None) -> tuple[str, str | None]:
    """The team and project an instance key's run lands in: by repository, then ``team-slug/name``."""
    if project is not None:
        order = (projects.c.created_at, projects.c.id)
        row = conn.execute(select(projects.c.team_id, projects.c.id)
                           .where(func.lower(projects.c.repo) == project.lower()).order_by(*order).limit(1)).first()
        slug, _, name = project.partition("/")
        if row is None and name:
            row = conn.execute(select(projects.c.team_id, projects.c.id)
                               .select_from(projects.join(teams, teams.c.id == projects.c.team_id))
                               .where(teams.c.slug == slug, projects.c.name == name).limit(1)).first()
        if row is not None:
            return row.team_id, row.id
    return _operations_team(conn, now, ip), None


def _operations_team(conn: Connection, now: datetime, ip: str | None) -> str:
    team_id = conn.execute(select(teams.c.id).where(teams.c.slug == OPERATIONS_SLUG)).scalar()
    if team_id is None:
        team_id = new_id()
        conn.execute(insert(teams).values(id=team_id, name=OPERATIONS_NAME, slug=OPERATIONS_SLUG, created_at=now))
        audit.record(conn, action="team.create", actor_id=None, team_id=team_id, target_kind="team",
                     target_id=team_id, detail={"source": "runner", "slug": OPERATIONS_SLUG}, ip=ip)
    return team_id


def open_run(conn: Connection, keyring: Keyring, *, team_id: str, project_id: str | None, workflow_id: str | None,
             workflow_name: str, now: datetime, **columns: Any) -> tuple[dict[str, Any], str]:
    """Insert a running run and mint its event token. Returns the run row and the token.

    An explicit ``thread`` column wins over the one the subject would open.
    """
    run_id = new_id()
    row = {"id": run_id, "team_id": team_id, "project_id": project_id, "workflow_id": workflow_id,
           "workflow_name": workflow_name, "status": "running", "started_at": now, "cost_usd": 0, "input_tokens": 0,
           "output_tokens": 0, **columns}
    row["location"] = row.get("location") or location.make(location.UNKNOWN)
    row["thread"] = row.get("thread") or threads.opening_thread(conn, team_id, row.get("subject"), now)
    conn.execute(insert(runs).values(**row))
    return row, _mint(conn, keyring, run_id, team_id, now)


def reopen_run(conn: Connection, keyring: Keyring, *, run_id: str, now: datetime,
               runner_key_id: str | None,
               location: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
    """Mark a run waiting at a gate running again under a new event token. (None, None) when it is not waiting."""
    row = conn.execute(select(runs.c.team_id).where(runs.c.id == run_id, runs.c.status == "waiting")).first()
    if row is None or conn.execute(update(runs).where(runs.c.id == run_id, runs.c.status == "waiting").values(
            status="running", finished_at=None, error=None, runner_key_id=runner_key_id,
            **({"location": location} if location else {}))).rowcount != 1:
        return None, None
    return run_id, _mint(conn, keyring, run_id, row.team_id, now)


def _mint(conn: Connection, keyring: Keyring, run_id: str, team_id: str, now: datetime) -> str:
    token = _random.token_urlsafe(32)
    conn.execute(insert(run_tokens).values(token_hmac=digest(keyring, token), run_id=run_id, team_id=team_id,
                                           expires_at=now + TOKEN_TTL))
    return token


def lookup_token(conn: Connection, token_digest: str) -> RunToken | None:
    row = conn.execute(select(run_tokens).where(run_tokens.c.token_hmac == token_digest)).first()
    return RunToken(run_id=row.run_id, team_id=row.team_id, expires_at=row.expires_at) if row else None


def revoke_tokens(conn: Connection, run_ids: list[str]) -> None:
    if run_ids:
        conn.execute(delete(run_tokens).where(run_tokens.c.run_id.in_(run_ids)))

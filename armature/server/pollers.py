"""Poll and schedule triggers: a Jira or Linear search, or a cron schedule, that queues a workflow.

A poll is one HTTPS call from the server through a team's connection, never a
workflow run. Each key a poll finds is marked seen before its job is queued,
so it is queued once ever. A trigger's first poll only marks what it finds, so
turning one on doesn't flood the queue. A failed poll marks nothing.

Servers share the work through ``trigger_state``: a claim moves next_run_at
with a compare-and-set, and the seen table's primary key stops a second poller
that got past it.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from croniter import CroniterError, croniter
from sqlalchemy import Connection, Engine, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import audit, connections, webhooks, worker
from armature.server import secrets as secret_store
from armature.server.crypto import Keyring, SealError
from armature.server.db import new_id, utcnow
from armature.server.db.schema import (
    jobs,
    projects,
    trigger_polls,
    trigger_seen,
    trigger_state,
    triggers,
    workflows,
)
from armature.server.defs import detach_triggers
from armature.server.vendors import Item, VendorError, jira, linear

log = logging.getLogger(__name__)

KINDS = ("jira_poll", "linear_poll", "schedule")
CONNECTION_KIND = {"jira_poll": "jira", "linear_poll": "linear"}
KEY = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
DEFAULT_JQL = "assignee = currentUser() AND sprint in openSprints() AND statusCategory != Done ORDER BY created ASC"
ORDER_BY = re.compile(r"\s+ORDER\s+BY\s.*$", re.IGNORECASE | re.DOTALL)
JQL_MAX = 2000
DEFAULT_INTERVAL = 5
INTERVAL_MAX = 24 * 60
MAX_RESULTS = 100
DEFAULT_INPUTS = {"issue": "$.key"}
LINEAR_TEAM = re.compile(r"^[A-Z][A-Z0-9]{0,9}$")
LINEAR_STATES = ("triage", "backlog", "unstarted", "started", "completed", "canceled")
LABEL_MAX = 80
NAME_MAX = 100
CRON_SCAN = 1000
CRON_MIN_GAP = timedelta(minutes=5)
CRON_GAPS = 48
CONFIG_KEYS = {
    "jira_poll": ("name", "connection_id", "jql", "interval_minutes", "max_results", "inputs", "project_id",
                  "seed_first", "shadow", "secrets"),
    "linear_poll": ("name", "connection_id", "team", "states", "assignee", "label", "interval_minutes",
                    "max_results", "inputs", "project_id", "seed_first", "shadow", "secrets"),
    "schedule": ("name", "cron", "timezone", "inputs", "project_id", "secrets"),
}
BACKOFF_MAX = timedelta(minutes=30)
TICK_S = 15.0
POOL = 4
SCAN = 100
STOP_TIMEOUT_S = 30.0
KEEP_POLLS = timedelta(days=14)
POLLS_PER_TRIGGER = 200
PRUNE_EVERY = timedelta(hours=1)
# A user's preview, check, or poll of one trigger, at most once in this window.
CHECK_EVERY = timedelta(seconds=10)
DAY = timedelta(days=1)
ERROR_MAX = 2000
MISSED = "Missed runs while no server was running. Ran once now."
NOT_RETURNED = {
    "jira_poll": "The JQL doesn't return it. With sprint in openSprints(), an issue outside an open sprint never "
                 "matches, and one pulled into a sprint later matches on the next poll.",
    "linear_poll": "The filter doesn't return it.",
}

_checked: dict[str, datetime] = {}
_checked_lock = threading.Lock()


class TriggerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def next_cron(expr: str, timezone: str, after: datetime) -> datetime:
    """The first time after ``after`` the expression names on the zone's wall clock, in UTC.

    A wall time the clocks skip runs an hour later. One they repeat runs once, the first time.
    """
    zone = ZoneInfo(timezone)
    times = croniter(expr, after.astimezone(zone).replace(tzinfo=None))
    for _ in range(CRON_SCAN):
        at = times.get_next(datetime).replace(tzinfo=zone).astimezone(UTC)
        if at > after:
            return at
    raise TriggerError(400, "The schedule names no time soon enough.")


def _flag(config: dict[str, Any], name: str, default: bool) -> bool:
    value = config.get(name, default)
    if not isinstance(value, bool):
        raise TriggerError(400, f"{name} must be true or false.")
    return value


def _whole(config: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise TriggerError(400, f"{name} must be a whole number from {low} to {high}.")
    return value


def _cron(config: dict[str, Any], now: datetime) -> dict[str, Any]:
    expr, timezone = config.get("cron"), config.get("timezone") or "UTC"
    if not isinstance(expr, str) or len(expr.split()) != 5 or not croniter.is_valid(expr):
        raise TriggerError(400, "cron must have five fields, such as 30 6 * * 1-5.")
    try:
        next_cron(expr, timezone, now)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise TriggerError(400, "timezone must be a zone name such as America/Denver.") from exc
    except CroniterError as exc:
        raise TriggerError(400, "The schedule names no time that comes.") from exc
    at = next_cron(expr, timezone, now)
    for _ in range(CRON_GAPS):
        later = next_cron(expr, timezone, at)
        if later - at < CRON_MIN_GAP:
            raise TriggerError(400, "A schedule runs at most once every 5 minutes, such as */5 * * * *.")
        at = later
    return {"cron": " ".join(expr.split()), "timezone": timezone}


def _query(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    if kind == "jira_poll":
        jql = config.get("jql") or DEFAULT_JQL
        if not isinstance(jql, str) or len(jql) > JQL_MAX or not jql.strip():
            raise TriggerError(400, f"jql must be text of at most {JQL_MAX} characters.")
        return {"jql": jql.strip()}
    team, states, label = config.get("team"), config.get("states") or [], config.get("label")
    if not isinstance(team, str) or not LINEAR_TEAM.fullmatch(team):
        raise TriggerError(400, "team must be a Linear team key such as ENG.")
    if not isinstance(states, list) or not set(states) <= set(LINEAR_STATES):
        raise TriggerError(400, f"states must be a list of {', '.join(LINEAR_STATES)}.")
    if config.get("assignee") not in (None, "me"):
        raise TriggerError(400, "assignee must be me or left out.")
    if label is not None and (not isinstance(label, str) or not 0 < len(label) <= LABEL_MAX):
        raise TriggerError(400, f"label must be text of at most {LABEL_MAX} characters.")
    found = {"team": team, "states": sorted(set(states), key=LINEAR_STATES.index)}
    if config.get("assignee"):
        found["assignee"] = "me"
    if label:
        found["label"] = label
    return found


def check_config(kind: str, config: Any, now: datetime) -> dict[str, Any]:
    """A trigger's config with defaults filled in. The connection and project are checked against the team apart."""
    if kind not in KINDS:
        raise TriggerError(400, f"kind must be one of {', '.join(KINDS)}.")
    if not isinstance(config, dict):
        raise TriggerError(400, "config must be an object.")
    unknown = set(config) - set(CONFIG_KEYS[kind])
    if unknown:
        raise TriggerError(400, f"Unknown keys: {', '.join(sorted(map(str, unknown)))}.")
    name = config.get("name") or ""
    if not isinstance(name, str) or len(name) > NAME_MAX or not name.isprintable():
        raise TriggerError(400, f"name must be text of at most {NAME_MAX} characters.")
    out: dict[str, Any] = {"name": name.strip()}
    inputs = config.get("inputs", {} if kind == "schedule" else DEFAULT_INPUTS)
    if not isinstance(inputs, dict) or len(inputs) > webhooks.INPUTS_MAX:
        raise TriggerError(400, f"inputs must be an object of at most {webhooks.INPUTS_MAX} entries.")
    try:
        out["inputs"] = {name: webhooks._check_input(name, value) for name, value in inputs.items()}
    except webhooks.WebhookError as exc:
        raise TriggerError(exc.status, exc.message) from exc
    names = config.get("secrets", [])
    if not isinstance(names, list) or len(names) > webhooks.SECRETS_MAX or not all(
            isinstance(n, str) and secret_store.NAME.fullmatch(n) for n in names):
        raise TriggerError(400, "secrets must be a list of secret names.")
    out["secrets"] = names
    if config.get("project_id") is not None:
        if not isinstance(config["project_id"], str):
            raise TriggerError(400, "project_id must be a project id.")
        out["project_id"] = config["project_id"]
    if kind == "schedule":
        return {**out, **_cron(config, now)}
    if not isinstance(config.get("connection_id"), str):
        raise TriggerError(400, "connection_id names the connection to poll with.")
    return {**out, "connection_id": config["connection_id"], **_query(kind, config),
            "interval_minutes": _whole(config, "interval_minutes", DEFAULT_INTERVAL, 1, INTERVAL_MAX),
            "max_results": _whole(config, "max_results", MAX_RESULTS, 1, MAX_RESULTS),
            "seed_first": _flag(config, "seed_first", True), "shadow": _flag(config, "shadow", False)}


def _connection(conn: Connection, trigger: Any) -> Any:
    """The trigger's connection: one of its team's, of its kind, or of a project in the team."""
    row = conn.execute(select(connections.connections).where(
        connections.connections.c.id == (trigger.config or {}).get("connection_id"))).first()
    if row is None or row.team_id != trigger.team_id:
        raise TriggerError(409, "The trigger's connection is gone or belongs to another team.")
    if row.kind != CONNECTION_KIND[trigger.kind]:
        raise TriggerError(409, f"A {trigger.kind} trigger needs a {CONNECTION_KIND[trigger.kind]} connection.")
    return row


def project_of(conn: Connection, trigger: Any, connection: Any | None) -> str | None:
    """The project a trigger's jobs run in: its own, else its project connection's, else its workflow's."""
    config = trigger.config or {}
    workflow = conn.execute(select(workflows.c.team_id, workflows.c.project_id)
                            .where(workflows.c.id == trigger.workflow_id)).first()
    if workflow is None or (workflow.team_id is not None and workflow.team_id != trigger.team_id):
        raise TriggerError(409, "The workflow no longer exists or is not visible to this team.")
    project_id = config.get("project_id")
    if project_id is not None and conn.execute(select(projects.c.id).where(
            projects.c.id == project_id, projects.c.team_id == trigger.team_id)).first() is None:
        raise TriggerError(409, "No such project in this team.")
    owner = connection.project_id if connection is not None else None
    for other in (owner, workflow.project_id):
        if other is not None and project_id is not None and other != project_id:
            raise TriggerError(409, "The project, its connection, and its workflow must agree.")
        project_id = project_id or other
    return project_id


def throttle(trigger_id: str, now: datetime) -> None:
    """At most one user-made vendor call per trigger in ``CHECK_EVERY``, in this process."""
    with _checked_lock:
        last = _checked.get(trigger_id)
        if last is not None and now - last < CHECK_EVERY:
            raise TriggerError(429, f"Wait {int(CHECK_EVERY.total_seconds())} seconds between checks of a trigger.")
        _checked[trigger_id] = now


def fetch(keyring: Keyring, connection: Any, trigger: Any, *, only: str | None = None,
          transport: httpx.BaseTransport | None = None) -> list[Item]:
    """The distinct issues the trigger's query returns now, or only ``only`` if it does. One vendor call."""
    config = trigger.config or {}
    try:
        creds = connections.credentials(keyring, connection)
    except (SealError, ValueError) as exc:
        raise TriggerError(409, "The connection's credentials can't be opened. Enter them again.") from exc
    if trigger.kind == "jira_poll":
        jql = f"({ORDER_BY.sub('', config['jql'])}) AND key = {only}" if only else config["jql"]
        items = jira.search(creds, connection.config or {}, jql, limit=config["max_results"], transport=transport)
    else:
        number = int(only.rsplit("-", 1)[1]) if only else None
        items = linear.search(creds, config, limit=config["max_results"], number=number, transport=transport)
    found: dict[str, Item] = {}
    for item in items:
        if KEY.fullmatch(item.key) and (only is None or item.key == only):
            found.setdefault(item.key, item)
    return list(found.values())


def _queue(conn: Connection, trigger: Any, project_id: str | None, payload: Any, now: datetime,
           **concurrency: Any) -> str:
    return worker.queue_job(conn, team_id=trigger.team_id, project_id=project_id, workflow_id=trigger.workflow_id,
                            inputs=webhooks.map_inputs((trigger.config or {}).get("inputs", {}), payload), now=now,
                            trigger_id=trigger.id, **concurrency)


def _mark(conn: Connection, trigger_id: str, key: str, outcome: str, now: datetime) -> bool:
    """Insert a seen row. False when one is there already, as when another server marked the key first."""
    try:
        with conn.begin_nested():
            conn.execute(insert(trigger_seen).values(trigger_id=trigger_id, item_key=key, outcome=outcome,
                                                     first_seen_at=now, last_seen_at=now))
    except IntegrityError:
        return False
    return True


def _take(engine: Engine, trigger: Any, project_id: str | None, item: Item, now: datetime) -> tuple[str, str] | None:
    """Mark one new key and queue its job in one transaction. Its outcome and any problem, or None if seen."""
    shadow = bool((trigger.config or {}).get("shadow"))
    with engine.begin() as conn:
        if not _mark(conn, trigger.id, item.key, "shadow" if shadow else "queued", now):
            return None
        if shadow:
            return "shadow", ""
        try:
            job_id = _queue(conn, trigger, project_id, item.fields, now)
        except ValueError as exc:
            conn.execute(update(trigger_seen).where(trigger_seen.c.trigger_id == trigger.id,
                                                    trigger_seen.c.item_key == item.key).values(outcome="skipped"))
            return "skipped", f"{item.key}: a workflow it calls doesn't resolve: {exc}"
        conn.execute(update(trigger_seen).where(trigger_seen.c.trigger_id == trigger.id,
                                                trigger_seen.c.item_key == item.key).values(job_id=job_id))
    return "queued", ""


def _record(conn: Connection, trigger_id: str, *, started: datetime, now: datetime, status: str,
            error: str | None = None, matched: int | None = None, new_keys: list[str] | None = None,
            duration_ms: int | None = None) -> None:
    conn.execute(insert(trigger_polls).values(id=new_id(), trigger_id=trigger_id, started_at=started,
                                              finished_at=now, status=status,
                                              error=error[:ERROR_MAX] if error else None, matched=matched,
                                              new_keys=new_keys or [], duration_ms=duration_ms))
    cutoff = conn.execute(select(trigger_polls.c.started_at).where(trigger_polls.c.trigger_id == trigger_id)
                          .order_by(trigger_polls.c.started_at.desc()).offset(POLLS_PER_TRIGGER - 1)
                          .limit(1)).scalar()
    if cutoff is not None:
        conn.execute(delete(trigger_polls).where(trigger_polls.c.trigger_id == trigger_id,
                                                 trigger_polls.c.started_at < cutoff))


def _interval(trigger: Any) -> timedelta:
    return timedelta(minutes=(trigger.config or {}).get("interval_minutes") or DEFAULT_INTERVAL)


def next_run(trigger: Any, now: datetime) -> datetime:
    config = trigger.config or {}
    if trigger.kind == "schedule":
        return next_cron(config["cron"], config.get("timezone") or "UTC", now)
    return now + _interval(trigger)


def _failed(engine: Engine, trigger: Any, state: Any, *, started: datetime, now: datetime, error: str,
            status_code: int | None, connection_id: str | None, ms: int) -> dict[str, Any]:
    failures = (state.failures or 0) + 1
    interval = _interval(trigger)
    wait = max(interval, min(interval * 2 ** min(failures, 10), BACKOFF_MAX)) if trigger.kind != "schedule" \
        else next_run(trigger, now) - now
    with engine.begin() as conn:
        _record(conn, trigger.id, started=started, now=now, status="error", error=error, duration_ms=ms)
        conn.execute(update(trigger_state).where(trigger_state.c.trigger_id == trigger.id).values(
            last_run_at=now, last_status="error", last_error=error[:ERROR_MAX], failures=failures,
            next_run_at=now + wait))
        if status_code in (401, 403) and connection_id is not None:
            conn.execute(update(connections.connections).where(connections.connections.c.id == connection_id)
                         .values(state="error", last_error=error[:ERROR_MAX]))
    return {"status": "error", "error": error, "matched": None, "new_keys": []}


def run(engine: Engine, keyring: Keyring, trigger_id: str, *, now: datetime, due: datetime | None = None,
        transport: httpx.BaseTransport | None = None) -> dict[str, Any] | None:
    """Poll a claimed trigger once, or fire a schedule, and record how it went. None when the trigger is gone.

    ``due`` is the time the claim was for. A schedule that missed a later time too records that, and runs once.
    """
    started, clock = now, time.monotonic()
    with engine.connect() as conn:
        trigger = conn.execute(select(triggers).where(triggers.c.id == trigger_id, triggers.c.kind.in_(KINDS))).first()
        state = conn.execute(select(trigger_state).where(trigger_state.c.trigger_id == trigger_id)).first()
    if trigger is None or state is None or not trigger.enabled:
        return None
    config = trigger.config or {}
    connection_id = config.get("connection_id")

    def ms() -> int:
        return int((time.monotonic() - clock) * 1000)

    try:
        with engine.connect() as conn:
            connection = _connection(conn, trigger) if trigger.kind != "schedule" else None
            project_id = project_of(conn, trigger, connection)
        items = fetch(keyring, connection, trigger, transport=transport) if connection is not None else []
    except (TriggerError, VendorError) as exc:
        return _failed(engine, trigger, state, started=started, now=now, error=str(exc),
                       status_code=getattr(exc, "status", None) if isinstance(exc, VendorError) else None,
                       connection_id=connection_id, ms=ms())
    if trigger.kind == "schedule":
        return _fire(engine, trigger, project_id, started=started, now=now, due=due, ms=ms)
    with engine.connect() as conn:
        seen = set(conn.execute(select(trigger_seen.c.item_key).where(
            trigger_seen.c.trigger_id == trigger.id,
            trigger_seen.c.item_key.in_([i.key for i in items]))).scalars()) if items else set()
    fresh = [item for item in items if item.key not in seen]
    seeding = state.seeded_at is None and config.get("seed_first", True)
    new_keys, problems, seeded = [], [], 0
    if seeding:
        with engine.begin() as conn:
            seeded = sum(_mark(conn, trigger.id, item.key, "seeded", now) for item in fresh)
            conn.execute(update(trigger_state).where(trigger_state.c.trigger_id == trigger.id)
                         .values(seeded_at=now, seeded_count=seeded))
    else:
        for item in fresh:
            taken = _take(engine, trigger, project_id, item, now)
            if taken is not None:
                new_keys.append(item.key)
                if taken[1]:
                    problems.append(taken[1])
    error = "\n".join(problems) or None
    with engine.begin() as conn:
        if seen:
            conn.execute(update(trigger_seen).where(trigger_seen.c.trigger_id == trigger.id,
                                                    trigger_seen.c.item_key.in_(sorted(seen)))
                         .values(last_seen_at=now))
        _record(conn, trigger.id, started=started, now=now, status="ok", error=error, matched=len(items),
                new_keys=new_keys, duration_ms=ms())
        values: dict[str, Any] = {"last_run_at": now, "last_status": "ok", "last_error": error,
                                  "last_matched": len(items), "last_new": len(new_keys), "failures": 0}
        conn.execute(update(trigger_state).where(trigger_state.c.trigger_id == trigger.id).values(**values))
    return {"status": "ok", "error": error, "matched": len(items), "new_keys": new_keys, "seeded": seeded}


def _fire(engine: Engine, trigger: Any, project_id: str | None, *, started: datetime, now: datetime,
          due: datetime | None, ms: Callable[[], int]) -> dict[str, Any]:
    config = trigger.config or {}
    late = due is not None and next_cron(config["cron"], config.get("timezone") or "UTC", due) <= now
    with engine.begin() as conn:
        if late:
            _record(conn, trigger.id, started=due, now=now, status="skipped", error=MISSED)
        try:
            # A schedule's newer run replaces one still queued, so a stuck queue never piles up runs.
            job_id = _queue(conn, trigger, project_id, {"scheduled_at": (due or now).isoformat()}, now,
                            key=f"schedule:{trigger.id}", supersede=True)
        except ValueError as exc:
            job_id, error = None, f"a workflow it calls doesn't resolve: {exc}"
        else:
            error = None
        status = "error" if job_id is None else "ok"
        _record(conn, trigger.id, started=started, now=now, status=status, error=error, matched=1,
                duration_ms=ms())
        conn.execute(update(trigger_state).where(trigger_state.c.trigger_id == trigger.id).values(
            last_run_at=now, last_status=status, last_error=error, last_matched=1, last_new=1 if job_id else 0,
            failures=0))
    return {"status": status, "error": error, "matched": 1, "new_keys": [], "job_id": job_id}


def due(conn: Connection, now: datetime) -> list[Any]:
    return conn.execute(select(triggers.c.id, triggers.c.kind, triggers.c.config, trigger_state.c.next_run_at)
                        .select_from(triggers.join(trigger_state, trigger_state.c.trigger_id == triggers.c.id))
                        .where(triggers.c.kind.in_(KINDS), triggers.c.enabled.is_(True),
                               trigger_state.c.next_run_at <= now)
                        .order_by(trigger_state.c.next_run_at, triggers.c.id).limit(SCAN)).all()


def claim(conn: Connection, trigger_id: str, was: datetime, until: datetime) -> bool:
    """Move a due trigger's next run on, if no other server moved it first."""
    return conn.execute(update(trigger_state).where(trigger_state.c.trigger_id == trigger_id,
                                                    trigger_state.c.next_run_at == was)
                        .values(next_run_at=until)).rowcount == 1


def prune(conn: Connection, now: datetime) -> int:
    return conn.execute(delete(trigger_polls).where(trigger_polls.c.started_at < now - KEEP_POLLS)).rowcount


class Scheduler:
    """Claims due triggers every ``interval`` seconds and runs them on a small pool, so a slow vendor call never
    holds up the job worker. Tests leave it stopped and call ``claim_due`` and ``run`` themselves.
    """

    def __init__(self, engine: Engine, keyring: Keyring, *, clock: Callable[[], datetime] = utcnow,
                 interval: float = TICK_S, transport: Callable[[], httpx.BaseTransport | None] = lambda: None):
        self.engine = engine
        self.keyring = keyring
        self.interval = interval
        self._clock = clock
        self._transport = transport
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._pruned_at: datetime | None = None

    def start(self) -> None:
        if self._thread is None:
            self._pool = ThreadPoolExecutor(max_workers=POOL, thread_name_prefix="armature-poll")
            self._thread = threading.Thread(target=self._loop, name="armature-scheduler", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("trigger scheduler tick failed")
            self._stop.wait(self.interval)

    def tick(self) -> None:
        now = self._clock()
        for trigger_id, was in self.claim_due(now):
            if self._pool is not None:
                self._pool.submit(self._run, trigger_id, was)
        if self._pruned_at is None or now - self._pruned_at >= PRUNE_EVERY:
            with self.engine.begin() as conn:
                prune(conn, now)
            self._pruned_at = now

    def claim_due(self, now: datetime) -> list[tuple[str, datetime]]:
        """The due triggers this server claimed, each with the time it was due."""
        with self.engine.connect() as conn:
            rows = due(conn, now)
        claimed = []
        for row in rows:
            try:
                until = next_run(row, now)
            except (TriggerError, ValueError, KeyError, CroniterError):
                log.warning("trigger %s has a schedule that no longer evaluates", row.id)
                continue
            with self.engine.begin() as conn:
                if claim(conn, row.id, row.next_run_at, until):
                    claimed.append((row.id, row.next_run_at))
        return claimed

    def _run(self, trigger_id: str, was: datetime) -> None:
        try:
            run(self.engine, self.keyring, trigger_id, now=self._clock(), due=was, transport=self._transport())
        except Exception:
            log.exception("trigger %s failed", trigger_id)


# The team lead's API


def get(conn: Connection, trigger_id: str) -> Any:
    row = conn.execute(select(triggers).where(triggers.c.id == trigger_id, triggers.c.kind.in_(KINDS))).first()
    if row is None:
        raise TriggerError(404, "No such trigger.")
    return row


def listed(conn: Connection, team_id: str) -> list[Any]:
    return conn.execute(select(triggers).where(triggers.c.team_id == team_id, triggers.c.kind.in_(KINDS))
                        .order_by(triggers.c.created_at, triggers.c.id)).all()


def public(conn: Connection, row: Any, now: datetime) -> dict[str, Any]:
    """The trigger as the API shows it, with its state and the keys it picked up in the last day."""
    config = row.config or {}
    state = conn.execute(select(trigger_state).where(trigger_state.c.trigger_id == row.id)).first()
    workflow = conn.execute(select(workflows.c.name).where(workflows.c.id == row.workflow_id)).scalar()
    connection = conn.execute(select(connections.connections.c.name, connections.connections.c.state).where(
        connections.connections.c.id == config.get("connection_id"))).first() if config.get("connection_id") else None
    picked = conn.execute(select(func.count()).select_from(trigger_seen).where(
        trigger_seen.c.trigger_id == row.id, trigger_seen.c.outcome == "queued",
        trigger_seen.c.first_seen_at > now - DAY)).scalar_one()
    return {"id": row.id, "team_id": row.team_id, "kind": row.kind, "workflow_id": row.workflow_id,
            "workflow_name": workflow, "enabled": row.enabled, "config": config, "created_at": row.created_at,
            "connection_name": connection.name if connection else None,
            "connection_state": connection.state if connection else None, "picked_up_24h": picked,
            "state": None if state is None else {
                "next_run_at": state.next_run_at, "last_run_at": state.last_run_at, "last_status": state.last_status,
                "last_error": state.last_error, "last_matched": state.last_matched, "last_new": state.last_new,
                "failures": state.failures, "seeded_at": state.seeded_at, "seeded_count": state.seeded_count}}


def _check_workflow(conn: Connection, team_id: str, workflow_id: Any) -> Any:
    try:
        return webhooks._check_workflow(conn, team_id, workflow_id)
    except webhooks.WebhookError as exc:
        raise TriggerError(exc.status, exc.message) from exc


def _check_scope(conn: Connection, row: Any) -> None:
    project_of(conn, row, _connection(conn, row) if row.kind != "schedule" else None)


def _enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TriggerError(400, "enabled must be true or false.")
    return value


def secrets_change(row: Any | None, body: dict[str, Any]) -> bool:
    """Whether a create or update sets or changes the secrets its runs get, which needs a step-up."""
    config = body.get("config")
    if not isinstance(config, dict) or "secrets" not in config:
        return False
    return config["secrets"] != ((row.config or {}).get("secrets", []) if row is not None else [])


def create(conn: Connection, *, team_id: str, body: dict[str, Any], actor_id: str, now: datetime,
           ip: str | None) -> Any:
    workflow = _check_workflow(conn, team_id, body.get("workflow_id"))
    kind = body.get("kind")
    config = check_config(kind, body.get("config", {}), now)
    trigger_id = new_id()
    conn.execute(insert(triggers).values(id=trigger_id, team_id=team_id, workflow_id=workflow.id, kind=kind,
                                         config=config, enabled=_enabled(body.get("enabled", True)), created_at=now))
    row = get(conn, trigger_id)
    _check_scope(conn, row)
    conn.execute(insert(trigger_state).values(trigger_id=trigger_id, failures=0,
                                              next_run_at=now if kind != "schedule" else next_run(row, now)))
    audit.record(conn, action="trigger.create", actor_id=actor_id, team_id=team_id, target_kind="trigger",
                 target_id=trigger_id, detail={"kind": kind, "workflow": workflow.name, "name": config["name"]},
                 ip=ip)
    return row


def update_trigger(conn: Connection, row: Any, *, body: dict[str, Any], actor_id: str, now: datetime,
                   ip: str | None) -> Any:
    """Change a trigger. Its seen keys stay, so an edited query never queues an old key again."""
    values: dict[str, Any] = {}
    if "workflow_id" in body:
        values["workflow_id"] = _check_workflow(conn, row.team_id, body["workflow_id"]).id
    if "config" in body:
        given = body["config"] if isinstance(body["config"], dict) else None
        values["config"] = check_config(row.kind, {**(row.config or {}), **given} if given is not None else None, now)
    if "enabled" in body:
        values["enabled"] = _enabled(body["enabled"])
    changed = sorted(k for k, v in values.items() if v != getattr(row, k))
    if not changed:
        return row
    conn.execute(update(triggers).where(triggers.c.id == row.id).values(**values))
    fresh = get(conn, row.id)
    _check_scope(conn, fresh)
    if "config" in changed or (values.get("enabled") and not row.enabled):
        conn.execute(update(trigger_state).where(trigger_state.c.trigger_id == row.id).values(
            next_run_at=now if row.kind != "schedule" else next_run(fresh, now), failures=0))
    detail: dict[str, Any] = {"changed": changed}
    if "config" in changed:
        detail["config"] = sorted(k for k in set(values["config"]) | set(row.config or {})
                                  if values["config"].get(k) != (row.config or {}).get(k))
    audit.record(conn, action="trigger.update", actor_id=actor_id, team_id=row.team_id, target_kind="trigger",
                 target_id=row.id, detail=detail, ip=ip)
    return fresh


def remove(conn: Connection, row: Any, *, actor_id: str, ip: str | None) -> None:
    detach_triggers(conn, select(triggers.c.id).where(triggers.c.id == row.id))
    conn.execute(delete(triggers).where(triggers.c.id == row.id))
    audit.record(conn, action="trigger.delete", actor_id=actor_id, team_id=row.team_id, target_kind="trigger",
                 target_id=row.id, detail={"kind": row.kind, "name": (row.config or {}).get("name")}, ip=ip)


def _poll_trigger(row: Any) -> None:
    if row.kind == "schedule":
        raise TriggerError(409, "A schedule has no query to run.")


def _connected(engine: Engine, row: Any) -> Any:
    with engine.connect() as conn:
        connection = _connection(conn, row)
        project_of(conn, row, connection)
    return connection


def _vendor(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except VendorError as exc:
        raise TriggerError(502, str(exc)) from exc


def preview(engine: Engine, keyring: Keyring, row: Any, *, now: datetime,
            transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Run the query once with no marks and no jobs: the keys it returns, and which are already seen."""
    _poll_trigger(row)
    throttle(row.id, now)
    connection = _connected(engine, row)
    items = _vendor(lambda: fetch(keyring, connection, row, transport=transport))
    with engine.connect() as conn:
        seen = {r.item_key: r for r in conn.execute(select(trigger_seen).where(
            trigger_seen.c.trigger_id == row.id, trigger_seen.c.item_key.in_([i.key for i in items])))} \
            if items else {}
    return {"items": [{"key": i.key, "title": i.title, "url": i.url,
                       "seen": _seen(seen[i.key]) if i.key in seen else None} for i in items]}


def _seen(row: Any) -> dict[str, Any]:
    return {"outcome": row.outcome, "first_seen_at": row.first_seen_at, "last_seen_at": row.last_seen_at,
            "job_id": row.job_id}


def _check_key(key: Any) -> str:
    key = key.strip().upper() if isinstance(key, str) else None
    if not key or not KEY.fullmatch(key):
        raise TriggerError(400, "Give an issue key such as ENG-123.")
    return key


def why(engine: Engine, keyring: Keyring, row: Any, *, key: Any, now: datetime,
        transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Why a key was or wasn't picked up: already seen, returned by the query and waiting, or not returned."""
    _poll_trigger(row)
    key = _check_key(key)
    throttle(row.id, now)
    with engine.connect() as conn:
        seen = conn.execute(select(trigger_seen, jobs.c.status.label("job_status"), jobs.c.run_id)
                            .select_from(trigger_seen.outerjoin(jobs, jobs.c.id == trigger_seen.c.job_id))
                            .where(trigger_seen.c.trigger_id == row.id, trigger_seen.c.item_key == key)).first()
        state = conn.execute(select(trigger_state).where(trigger_state.c.trigger_id == row.id)).first()
    connection = _connected(engine, row)
    returned = bool(_vendor(lambda: fetch(keyring, connection, row, only=key, transport=transport)))
    out: dict[str, Any] = {"key": key, "returned": returned, "seen": None}
    if seen is not None:
        out["seen"] = {**_seen(seen), "job_status": seen.job_status, "run_id": seen.run_id}
        out["verdict"] = "seen"
    elif not returned:
        out.update(verdict="not_returned", message=NOT_RETURNED[row.kind])
    elif not row.enabled:
        out.update(verdict="disabled", message="The query returns it, and the trigger is off.")
    elif state is not None and state.seeded_at is None and (row.config or {}).get("seed_first", True):
        out.update(verdict="seeding", message="The query returns it. The first poll marks it seen and queues nothing.")
    else:
        out.update(verdict="next_poll", message="The query returns it. The next poll picks it up.",
                   next_run_at=state.next_run_at if state is not None else None)
    return out


def seed(engine: Engine, keyring: Keyring, row: Any, *, actor_id: str, now: datetime, ip: str | None,
         transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Mark everything the query returns now as seen, queuing nothing."""
    _poll_trigger(row)
    throttle(row.id, now)
    connection = _connected(engine, row)
    items = _vendor(lambda: fetch(keyring, connection, row, transport=transport))
    with engine.begin() as conn:
        marked = sum(_mark(conn, row.id, item.key, "seeded", now) for item in items)
        state = conn.execute(select(trigger_state).where(trigger_state.c.trigger_id == row.id)).one()
        conn.execute(update(trigger_state).where(trigger_state.c.trigger_id == row.id).values(
            seeded_at=now, seeded_count=(state.seeded_count or 0) + marked))
        audit.record(conn, action="trigger.seed", actor_id=actor_id, team_id=row.team_id, target_kind="trigger",
                     target_id=row.id, detail={"matched": len(items), "seeded": marked}, ip=ip)
    return {"matched": len(items), "seeded": marked}


def reset(conn: Connection, row: Any, *, actor_id: str, ip: str | None) -> int:
    """Forget every seen key. The next poll seeds again when the trigger seeds first, else queues them all."""
    _poll_trigger(row)
    count = conn.execute(delete(trigger_seen).where(trigger_seen.c.trigger_id == row.id)).rowcount
    conn.execute(update(trigger_state).where(trigger_state.c.trigger_id == row.id)
                 .values(seeded_at=None, seeded_count=None))
    audit.record(conn, action="trigger.reset", actor_id=actor_id, team_id=row.team_id, target_kind="trigger",
                 target_id=row.id, detail={"forgot": count}, ip=ip)
    return count


def requeue(engine: Engine, keyring: Keyring, row: Any, *, key: Any, actor_id: str, now: datetime, ip: str | None,
            transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Queue a seen key again, as ops re-queues parked work past its mark."""
    _poll_trigger(row)
    key = _check_key(key)
    if (row.config or {}).get("shadow"):
        raise TriggerError(409, "A shadow trigger queues nothing. Turn shadow off first.")
    throttle(row.id, now)
    connection = _connected(engine, row)
    found = _vendor(lambda: fetch(keyring, connection, row, only=key, transport=transport))
    payload = found[0].fields if found else {"key": key}
    with engine.begin() as conn:
        seen = conn.execute(select(trigger_seen).where(trigger_seen.c.trigger_id == row.id,
                                                       trigger_seen.c.item_key == key).with_for_update()).first()
        if seen is None:
            raise TriggerError(409, f"{key} hasn't been seen. The next poll picks it up if the query returns it.")
        try:
            job_id = _queue(conn, row, project_of(conn, row, connection), payload, now)
        except ValueError as exc:
            raise TriggerError(409, f"A workflow it calls doesn't resolve: {exc}") from exc
        conn.execute(update(trigger_seen).where(trigger_seen.c.trigger_id == row.id, trigger_seen.c.item_key == key)
                     .values(outcome="queued", job_id=job_id, last_seen_at=now))
        audit.record(conn, action="trigger.requeue", actor_id=actor_id, team_id=row.team_id, target_kind="trigger",
                     target_id=row.id, detail={"key": key, "job_id": job_id, "previous": seen.outcome}, ip=ip)
    return {"key": key, "job_id": job_id}


def run_now(engine: Engine, keyring: Keyring, row: Any, *, actor_id: str, now: datetime, ip: str | None,
            transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Claim the trigger as the scheduler does and run it now, so no server runs it twice."""
    if not row.enabled:
        raise TriggerError(409, "Turn the trigger on first.")
    throttle(row.id, now)
    with engine.begin() as conn:
        state = conn.execute(select(trigger_state).where(trigger_state.c.trigger_id == row.id)).one()
        if not claim(conn, row.id, state.next_run_at, next_run(row, now)):
            raise TriggerError(409, "Another server is running this trigger now.")
        audit.record(conn, action="trigger.run", actor_id=actor_id, team_id=row.team_id, target_kind="trigger",
                     target_id=row.id, ip=ip)
    return run(engine, keyring, row.id, now=now, transport=transport) or {}


def polls(conn: Connection, trigger_id: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(select(trigger_polls).where(trigger_polls.c.trigger_id == trigger_id)
                        .order_by(trigger_polls.c.started_at.desc(), trigger_polls.c.id.desc()).limit(limit)).all()
    return [{"id": r.id, "started_at": r.started_at, "finished_at": r.finished_at, "status": r.status,
             "error": r.error, "matched": r.matched, "new_keys": r.new_keys or [], "duration_ms": r.duration_ms}
            for r in rows]


def seen_keys(conn: Connection, trigger_id: str, *, outcome: str | None, q: str | None,
              limit: int) -> list[dict[str, Any]]:
    query = (select(trigger_seen, jobs.c.status.label("job_status"), jobs.c.run_id)
             .select_from(trigger_seen.outerjoin(jobs, jobs.c.id == trigger_seen.c.job_id))
             .where(trigger_seen.c.trigger_id == trigger_id))
    if outcome:
        query = query.where(trigger_seen.c.outcome == outcome)
    if q:
        query = query.where(trigger_seen.c.item_key.contains(q.strip().upper(), autoescape=True))
    rows = conn.execute(query.order_by(trigger_seen.c.first_seen_at.desc(), trigger_seen.c.item_key)
                        .limit(limit)).all()
    return [{"key": r.item_key, **_seen(r), "job_status": r.job_status, "run_id": r.run_id} for r in rows]


def visible_connections(conn: Connection, team_id: str) -> list[dict[str, Any]]:
    """The team's Jira and Linear connections, its projects' included, that a trigger may poll with."""
    rows = conn.execute(select(connections.connections).where(
        connections.connections.c.team_id == team_id,
        connections.connections.c.kind.in_(tuple(CONNECTION_KIND.values())))
        .order_by(connections.connections.c.kind, connections.connections.c.name)).all()
    return [{"id": r.id, "kind": r.kind, "name": r.name, "state": r.state,
             "scope_kind": "project" if r.project_id else "team", "project_id": r.project_id} for r in rows]

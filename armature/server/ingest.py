"""Run event ingest: verify, cap, redact, number, broadcast, then hand to the writer.

Events arrive on a mode 0600 Unix datagram socket or as NDJSON over HTTP. The
run and team always come from the event token, never from the event. Every
event is broadcast before it is stored (DESIGN.md, "Run history and live
activity"). A bad event is refused on its own and never stops the others.

Duplicates are dropped by ``eid`` (``<emitter nonce>:<counter>``), remembered
in memory per run. After a server restart that memory is empty, so a sender
that retries an event the old process had already stored can store it twice.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import socket
import stat
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, func, select

from armature.events.emitter import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_PARTS,
    artifact_problem,
)
from armature.events.redact import REDACTED, redact_text
from armature.events.summary import merge_summary, summary_patch
from armature.server import runners
from armature.server import secrets as secret_store
from armature.server.auth import valid_token
from armature.server.crypto import Keyring
from armature.server.db.schema import run_events, runs, secrets, workflows
from armature.server.live import LocalBroker, event_message, summary_message
from armature.server.writer import EventWriter, StepChange, step_key, sweep_lost

log = logging.getLogger(__name__)

WIRE_VERSION = 1
MAX_EVENT_BYTES = 64 * 1024
MAX_DEPTH = 32
MAX_EVENTS_PER_RUN = 50_000
RATE_PER_S = 500.0
RATE_BURST = 2000.0
TOKEN_CACHE_S = 5.0
TOKEN_CACHE_MAX = 10_000
MISS_CACHE_MAX = 10_000
MAX_NONCES = 256
IP_POSTS_PER_S = 200.0
IP_POSTS_BURST = 400.0
SOCKET_RCVBUF = 4 * 1024 * 1024
SOCKET_QUEUE = 20_000
SUMMARY_INTERVAL_S = 1.0
SWEEP_INTERVAL_S = 60.0
IDLE_STATE_S = 600.0
TS_SKEW = timedelta(days=1)
MIN_SECRET = 4
KIND = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
EID = re.compile(r"^([A-Za-z0-9_-]{1,64}):(\d{1,18})$")
RUNNER_KEY = re.compile(r"\bark_[\w-]{40,}")
SURROGATE = re.compile("[\ud800-\udfff]")
STRIPPED = ("run_id", "team_id", "token", "eid")
STEP_STATUS = {"step_start": "running", "step_end": "succeeded", "step_failed": "failed",
               "step_skipped": "skipped"}
SUCCEEDED = frozenset({"succeeded", "success", "ok", "completed", "complete", "done", "passed"})
CANCELED = frozenset({"canceled", "cancelled"})

TRUNCATED = "_truncated"
UPLOAD = re.compile(r"^[0-9a-f]{16}$")
MAX_UPLOADS = 4
# Per run, so a token holder cannot fill the database with distinctly named artifacts.
MAX_ARTIFACTS_PER_RUN = 50
MAX_ARTIFACT_BYTES_PER_RUN = 16 * 1024 * 1024

ACCEPTED, REJECTED, DUPLICATE = "accepted", "rejected", "duplicate"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_ts(value: Any, now: datetime) -> datetime:
    """The sender's time when it is within a day of ``now``, else ``now``."""
    try:
        parsed = datetime.fromisoformat(value)
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if now - TS_SKEW <= parsed <= now + TS_SKEW:
            return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        pass
    return now


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) and value > 0 else 0.0


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def run_status(value: Any) -> str:
    text = str(value or "").lower()
    if text in SUCCEEDED:
        return "succeeded"
    if text in CANCELED:
        return "canceled"
    return "failed"


def shallow(value: Any, limit: int = MAX_DEPTH) -> bool:
    """True when ``value`` nests no deeper than ``limit``, checked without recursion."""
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth >= limit:
                return False
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in children)
    return True


def _scrub(text: str, secret_values: tuple[str, ...]) -> str:
    return RUNNER_KEY.sub(REDACTED, redact_text(text, secret_values))


def redact(value: Any, secret_values: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _scrub(value, secret_values)
    if isinstance(value, dict):
        return {k: redact(v, secret_values) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret_values) for v in value]
    return value


def _fix_surrogates(value: Any) -> Any:
    if isinstance(value, str):
        return SURROGATE.sub("\ufffd", value)
    if isinstance(value, dict):
        return {_fix_surrogates(k): _fix_surrogates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fix_surrogates(v) for v in value]
    return value


def utf8(value: Any) -> Any:
    """``value`` with any lone surrogate replaced by U+FFFD, which no database or UTF-8 encoder takes."""
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
        return value
    except UnicodeEncodeError:
        return _fix_surrogates(value)


@dataclass
class Branches:
    expected: int = 0
    ended: set[str] = field(default_factory=set)
    failed: bool = False
    outputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Upload:
    """An artifact sent in parts, held until the last part arrives."""

    name: str
    media_type: str
    parts: int
    chunks: dict[int, str] = field(default_factory=dict)
    size: int = 0


def _index(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class RunState:
    run_id: str
    team_id: str
    project_id: str | None
    seq: int
    secrets: tuple[str, ...]
    summary: dict[str, Any]
    allowance: float = RATE_BURST
    refilled: float = field(default_factory=time.monotonic)
    rejected: int = 0
    seen: float = field(default_factory=time.monotonic)
    last_event_at: datetime | None = None
    published: float = 0.0
    summary_timer: asyncio.TimerHandle | None = None
    branches: dict[str, Branches] = field(default_factory=dict)
    eids: OrderedDict[str, int] = field(default_factory=OrderedDict)
    uploads: OrderedDict[str, Upload] = field(default_factory=OrderedDict)
    artifact_sizes: dict[str, int] = field(default_factory=dict)

    @property
    def ended(self) -> bool:
        return self.summary["status"] not in ("running", "queued", "lost")

    def allow(self) -> bool:
        now = time.monotonic()
        self.allowance = min(RATE_BURST, self.allowance + (now - self.refilled) * RATE_PER_S)
        self.refilled = now
        if self.allowance < 1:
            return False
        self.allowance -= 1
        return True

    def duplicate(self, eid: tuple[str, int] | None) -> bool:
        return eid is not None and eid[1] <= self.eids.get(eid[0], -1)

    def remember(self, eid: tuple[str, int] | None) -> None:
        if eid is None:
            return
        self.eids[eid[0]] = eid[1]
        self.eids.move_to_end(eid[0])
        while len(self.eids) > MAX_NONCES:
            self.eids.popitem(last=False)


def step_output(payload: dict[str, Any]) -> Any:
    """A step_end's output for run_steps, marked when the emitter cut it to fit an event."""
    output = payload.get("output")
    if not payload.get("truncated"):
        return output
    return {**output, TRUNCATED: True} if isinstance(output, dict) else {TRUNCATED: True, "value": output}


def parse_eid(value: Any) -> tuple[str, int] | None:
    match = EID.fullmatch(value) if isinstance(value, str) else None
    return (match.group(1), int(match.group(2))) if match else None


def about(subject: str | None, reported: Any) -> dict[str, Any]:
    """A run list row's subject, title, and verdict, the last two from the run's reported summary."""
    reported = reported if isinstance(reported, dict) else {}
    return {"subject": subject, "title": reported.get("title"), "verdict": reported.get("verdict")}


def list_row(summary: dict[str, Any]) -> dict[str, Any]:
    """The fields a row of ``GET /api/runs`` has."""
    return {**{k: summary[k] for k in ("id", "team_id", "project_id", "workflow", "status", "triggered_by",
                                       "started_at", "finished_at", "cost_usd")},
            **about(summary.get("subject"), summary.get("summary"))}


def run_columns() -> list:
    return [runs.c.id, runs.c.team_id, runs.c.project_id,
            func.coalesce(workflows.c.name, runs.c.workflow_name).label("workflow"), runs.c.status,
            func.coalesce(runs.c.triggered_by, runs.c.actor).label("triggered_by"), runs.c.trigger_kind,
            runs.c.started_at, runs.c.finished_at, runs.c.cost_usd, runs.c.input_tokens, runs.c.output_tokens,
            runs.c.rerun_of, runs.c.cancelled_by, runs.c.error, runs.c.subject, runs.c.summary]


def run_query():
    return select(*run_columns()).select_from(runs.outerjoin(workflows, workflows.c.id == runs.c.workflow_id))


def summary_of(row: Any) -> dict[str, Any]:
    summary = dict(row._mapping)
    summary["cost_usd"] = float(summary["cost_usd"]) if summary["cost_usd"] is not None else None
    return summary


def load_summary(conn: Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(run_query().where(runs.c.id == run_id)).first()
    return summary_of(row) if row else None


def team_secret_values(conn: Connection, keyring: Keyring, team_id: str) -> tuple[str, ...]:
    """Every secret value a run of this team could hold, for exact-value redaction. Raises if any won't open."""
    names = set(conn.execute(select(secrets.c.name).where(secrets.c.scope.in_([team_id, "*"]))).scalars())
    values = secret_store.resolve_for_run(conn, keyring, team_id, sorted(names)).values()
    return tuple(v for v in values if len(v) >= MIN_SECRET)


def _longest_first(values) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=len, reverse=True))


class RateLimit:
    """A token bucket per key, the least recently seen keys forgotten first."""

    def __init__(self, per_s: float, burst: float, size: int = 10_000):
        self.per_s, self.burst, self.size = per_s, burst, size
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        allowance, last = self._buckets.get(key, (self.burst, now))
        allowance = min(self.burst, allowance + (now - last) * self.per_s)
        ok = allowance >= 1
        _remember(self._buckets, key, (allowance - 1 if ok else allowance, now), self.size)
        return ok


class LiveHub:
    """Owns the broker, the writer, the socket, and the in-memory state of runs in progress."""

    def __init__(self, engine: Engine, keyring: Keyring, clock: Callable[[], datetime], *,
                 socket_path: str = "", lost_after: timedelta = timedelta(minutes=30),
                 sse_per_user: int = 8, sse_total: int = 500):
        self.engine = engine
        self.keyring = keyring
        self.clock = clock
        self.socket_path = str(Path(socket_path).expanduser()) if socket_path else ""
        self.lost_after = lost_after
        self.sse_per_user = sse_per_user
        self.sse_total = sse_total
        self.broker = LocalBroker()
        self.writer = EventWriter(engine, clock)
        self.ip_limit = RateLimit(IP_POSTS_PER_S, IP_POSTS_BURST)
        self.running = False
        self.closing = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runs: dict[str, RunState] = {}
        self._loading: dict[str, asyncio.Future] = {}
        self._tokens: OrderedDict[str, tuple[runners.RunToken, float]] = OrderedDict()
        self._misses: OrderedDict[str, float] = OrderedDict()
        self._sock: socket.socket | None = None
        self._datagrams: asyncio.Queue[bytes] | None = None
        self._tasks: list[asyncio.Task] = []
        self._streams: dict[str, int] = {}
        self.accepted = 0
        self.rejected = 0
        self.duplicates = 0
        self.errors = 0
        self.socket_dropped = 0
        self.lag_ms: list[float] = []

    @property
    def unix_endpoint(self) -> str | None:
        return self.socket_path if self._sock is not None else None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.writer.start()
        if self.socket_path:
            self._open_socket()
        self._tasks.append(asyncio.create_task(self._sweeper(), name="armature-lost-sweep"))
        self._tasks.append(asyncio.create_task(self._lag_monitor(), name="armature-lag"))
        self.running = True

    async def stop(self) -> None:
        self.running = False
        self.closing = True
        try:
            if self._sock is not None:
                self._loop.remove_reader(self._sock.fileno())
                while self._datagrams is not None and not self._datagrams.empty():
                    await self.ingest(self._datagrams.get_nowait())
            for task in self._tasks:
                task.cancel()
            for task in self._tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._tasks.clear()
            for state in self._runs.values():
                if state.summary_timer is not None:
                    state.summary_timer.cancel()
        finally:
            await self.writer.stop()
            if self._sock is not None:
                self._sock.close()
                self._sock = None
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.socket_path)

    # The socket.

    def _open_socket(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise RuntimeError(f"{path} exists and is not a socket")
        temp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RCVBUF)
        except OSError:
            log.warning("could not raise SO_RCVBUF on %s to %d", path, SOCKET_RCVBUF)
        # Created 0600 under the umask and renamed into place, so no other user can ever send to it.
        old_umask = os.umask(0o177)
        try:
            sock.bind(str(temp))
        finally:
            os.umask(old_umask)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        sock.setblocking(False)
        self._sock = sock
        self._datagrams = asyncio.Queue(SOCKET_QUEUE)
        self._loop.add_reader(sock.fileno(), self._on_readable)
        self._tasks.append(asyncio.create_task(self._drain_socket(), name="armature-socket"))

    def _on_readable(self) -> None:
        for _ in range(512):
            try:
                data = self._sock.recv(MAX_EVENT_BYTES + 1)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                log.exception("event socket read failed")
                return
            try:
                self._datagrams.put_nowait(data)
            except asyncio.QueueFull:
                self.socket_dropped += 1

    async def _drain_socket(self) -> None:
        handled = 0
        while True:
            data = await self._datagrams.get()
            try:
                await self.ingest(data)
            except Exception:
                log.exception("event from the socket failed")
            handled += 1
            if handled % 200 == 0:
                await asyncio.sleep(0)

    # Ingest.

    async def ingest(self, raw: bytes, header_token: str | None = None) -> str:
        """One event. Returns ACCEPTED, REJECTED, or DUPLICATE, and never raises."""
        try:
            outcome = await self._ingest(raw, header_token)
        except Exception:
            self.errors += 1
            log.warning("refused an event that failed to process", exc_info=True)
            outcome = REJECTED
        if outcome == ACCEPTED:
            self.accepted += 1
        elif outcome == DUPLICATE:
            self.duplicates += 1
        else:
            self.rejected += 1
        return outcome

    async def _ingest(self, raw: bytes, header_token: str | None) -> str:
        if len(raw) > MAX_EVENT_BYTES:
            return REJECTED
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return REJECTED
        if not isinstance(event, dict) or event.get("v") != WIRE_VERSION:
            return REJECTED
        token = event.get("token") or header_token
        if not isinstance(token, str) or not valid_token(token):
            return REJECTED
        run = await self._token(runners.digest(self.keyring, token))
        if run is None or run.expires_at <= self.clock():
            return REJECTED
        state = self._runs.get(run.run_id) or await self._state(run, token)
        if state is None or state.ended:
            return REJECTED
        kind, step_id, payload = event.get("kind"), event.get("step_id"), event.get("payload")
        if not isinstance(kind, str) or not KIND.fullmatch(kind):
            return REJECTED
        if step_id is not None and (not isinstance(step_id, str) or not step_id or len(step_id) > 191):
            return REJECTED
        if payload is None:
            payload = {}
        if not isinstance(payload, dict) or not shallow(payload):
            return REJECTED
        eid = parse_eid(event.get("eid"))
        if state.duplicate(eid):
            return DUPLICATE
        if kind != "run_end" and (state.seq >= MAX_EVENTS_PER_RUN or not state.allow()):
            state.rejected += 1
            return REJECTED
        now = self.clock()
        ts = parse_ts(event.get("ts"), now)
        if kind == "artifact":
            return self._artifact(state, eid, step_id, ts, now, payload)
        payload = utf8(redact({k: v for k, v in payload.items() if k not in STRIPPED}, state.secrets))
        if step_id is not None:
            step_id = utf8(_scrub(step_id, state.secrets))[:191]
        state.remember(eid)
        if state.rejected:
            self._accept(state, "dropped", None, now, now, {"count": state.rejected, "reason": "server_limit"})
            state.rejected = 0
        self._accept(state, kind, step_id, ts, now, payload)
        return ACCEPTED

    def _artifact(self, state: RunState, eid: tuple[str, int] | None, step_id: str | None, ts: datetime,
                  now: datetime, payload: dict[str, Any]) -> str:
        """One part of an artifact. The whole artifact is redacted and stored when its last part arrives."""
        name, media_type, content = payload.get("name"), payload.get("media_type"), payload.get("content")
        part, parts = _index(payload.get("part", 0)), _index(payload.get("parts", 1))
        if (artifact_problem(name, media_type, content) or parts is None or part is None
                or not 1 <= parts <= MAX_ARTIFACT_PARTS or not 0 <= part < parts):
            return REJECTED
        state.remember(eid)
        if parts > 1:
            upload_id = payload.get("upload")
            if not isinstance(upload_id, str) or not UPLOAD.fullmatch(upload_id):
                return REJECTED
            upload = state.uploads.get(upload_id)
            if upload is None:
                upload = state.uploads[upload_id] = Upload(name=name, media_type=media_type, parts=parts)
                while len(state.uploads) > MAX_UPLOADS:
                    state.uploads.popitem(last=False)
            elif (upload.name, upload.media_type, upload.parts) != (name, media_type, parts):
                return REJECTED
            previous = upload.chunks.get(part)
            upload.size += len(content.encode("utf-8", "surrogatepass")) - (
                len(previous.encode("utf-8", "surrogatepass")) if previous is not None else 0)
            upload.chunks[part] = content
            if upload.size > MAX_ARTIFACT_BYTES:
                del state.uploads[upload_id]
                return REJECTED
            if len(upload.chunks) < parts:
                return ACCEPTED
            del state.uploads[upload_id]
            content = "".join(upload.chunks[index] for index in range(parts))
        content = utf8(_scrub(content, state.secrets))
        if step_id is not None:
            step_id = utf8(_scrub(step_id, state.secrets))[:191]
        size = len(content.encode("utf-8"))
        sizes = state.artifact_sizes
        if name not in sizes and len(sizes) >= MAX_ARTIFACTS_PER_RUN:
            return REJECTED
        if sum(sizes.values()) - sizes.get(name, 0) + size > MAX_ARTIFACT_BYTES_PER_RUN:
            return REJECTED
        sizes[name] = size
        self._accept(state, "artifact", step_id, ts, now, {"name": name, "media_type": media_type, "size": size})
        self.writer.add_artifact(state.run_id, {"name": name, "step_id": step_id, "media_type": media_type,
                                                "content": content, "size": size, "created_at": now})
        return ACCEPTED

    async def _token(self, token_digest: str) -> runners.RunToken | None:
        now = time.monotonic()
        hit = self._tokens.get(token_digest)
        if hit is not None and now - hit[1] < TOKEN_CACHE_S:
            self._tokens.move_to_end(token_digest)
            return hit[0]
        missed = self._misses.get(token_digest)
        if missed is not None and now - missed < TOKEN_CACHE_S:
            return None
        try:
            found = await asyncio.to_thread(self._read, lambda conn: runners.lookup_token(conn, token_digest))
        except Exception:
            log.exception("event token lookup failed")
            return None
        if found is None:
            _remember(self._misses, token_digest, now, MISS_CACHE_MAX)
            self._tokens.pop(token_digest, None)
        else:
            _remember(self._tokens, token_digest, (found, now), TOKEN_CACHE_MAX)
            self._misses.pop(token_digest, None)
        return found

    def forget_tokens(self) -> None:
        """Drop cached token lookups, so a revocation takes effect now rather than in TOKEN_CACHE_S."""
        self._tokens.clear()
        self._misses.clear()

    async def _state(self, run: runners.RunToken, token: str) -> RunState | None:
        pending = self._loading.get(run.run_id)
        if pending is None:
            pending = self._loading[run.run_id] = asyncio.ensure_future(self._load_state(run, token))
            pending.add_done_callback(lambda _: self._loading.pop(run.run_id, None))
        return await asyncio.shield(pending)

    async def _load_state(self, run: runners.RunToken, token: str) -> RunState | None:
        def load(conn: Connection):
            summary = load_summary(conn, run.run_id)
            if summary is None or summary["team_id"] != run.team_id:
                return None
            seq = conn.execute(select(func.max(run_events.c.seq)).where(run_events.c.run_id == run.run_id)).scalar()
            return summary, seq or 0, team_secret_values(conn, self.keyring, run.team_id)

        try:
            loaded = await asyncio.to_thread(self._read, load)
        except Exception:
            # Storing events we could not redact is worse than refusing them.
            log.exception("could not load run %s for ingest, refusing its events", run.run_id)
            return None
        if loaded is None:
            return None
        if run.run_id in self._runs:
            return self._runs[run.run_id]
        summary, seq, secret_values = loaded
        state = RunState(run_id=run.run_id, team_id=run.team_id, project_id=summary["project_id"],
                         seq=max(seq, self.writer.max_seq(run.run_id)),
                         secrets=_longest_first((*secret_values, token)), summary=summary)
        self._runs[run.run_id] = state
        return state

    def _read(self, fn: Callable[[Connection], Any]) -> Any:
        with self.engine.begin() as conn:
            return fn(conn)

    def _accept(self, state: RunState, kind: str, step_id: str | None, ts: datetime, now: datetime,
                payload: dict[str, Any]) -> None:
        event = {"seq": state.seq + 1, "ts": _iso(ts), "kind": kind, "step_id": step_id, "payload": payload}
        message = event_message(state.run_id, state.team_id, state.project_id, event)
        state.seq += 1
        state.seen = time.monotonic()
        state.last_event_at = now
        self.broker.publish(message)
        change = self.writer.add_event(state.run_id, {**event, "ts": ts})
        change.last_event_at = now
        summary = state.summary
        changed = immediate = False
        if summary["status"] == "lost":
            summary.update(status="running", finished_at=None)
            changed = immediate = True
        parent = _text(payload.get("parent_step"), 191)
        if kind in STEP_STATUS and step_id is not None:
            key = step_key(step_id, parent)
            step = change.steps.setdefault(key, StepChange(step_id=step_id))
            self._step(state, step, key, kind, ts, parent, payload)
        elif kind == "cost":
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            cost = _number(payload.get("cost_usd", payload.get("total_cost_usd")))
            tokens_in = int(_number(payload.get("input_tokens", usage.get("input_tokens"))))
            tokens_out = int(_number(payload.get("output_tokens", usage.get("output_tokens"))))
            change.cost_usd += cost
            change.input_tokens += tokens_in
            change.output_tokens += tokens_out
            if step_id is not None:
                key = step_key(step_id, parent)
                step = change.steps.setdefault(key, StepChange(step_id=step_id))
                step.cost_usd += cost
                step.input_tokens += tokens_in
                step.output_tokens += tokens_out
                step.parent_step = parent or step.parent_step
            summary["cost_usd"] = (summary["cost_usd"] or 0.0) + cost
            summary["input_tokens"] = (summary["input_tokens"] or 0) + tokens_in
            summary["output_tokens"] = (summary["output_tokens"] or 0) + tokens_out
            changed = changed or bool(cost or tokens_in or tokens_out)
        elif kind == "finding":
            change.findings.append(self._finding(state, step_id, now, payload))
        elif kind == "summary":
            patch = summary_patch(payload)
            if patch:
                change.summary = merge_summary(change.summary, patch)
                summary["summary"] = merge_summary(summary.get("summary"), patch)
                changed = immediate = True
        elif kind == "run_end":
            status = run_status(payload.get("status"))
            error = None if status == "succeeded" else _text(payload.get("error") or payload.get("reason"), 10_000)
            change.end = (status, now, error)
            summary.update(status=status, finished_at=now, error=error)
            changed = immediate = True
        if changed:
            self._publish_summary(state, immediate)

    def _step(self, state: RunState, step: StepChange, key: str, kind: str, ts: datetime, parent: str | None,
              payload: dict[str, Any]) -> None:
        step.parent_step = parent or step.parent_step
        if kind == "step_start":
            step.status = "running"
            step.started_at = step.started_at or ts
            count = payload.get("branches")
            count = len(count) if isinstance(count, list) else count
            if isinstance(count, int) and not isinstance(count, bool) and count > 1:
                # A fan-out narrower than its items starts the step more than once. Keep the first tracker.
                state.branches.setdefault(key, Branches(expected=count))
            return
        failed = kind == "step_failed" and not payload.get("fail_as_value")
        if kind == "step_failed":
            step.error = _text(payload.get("reason") or payload.get("error"), 10_000) or step.error
        tracked = state.branches.get(key)
        branch = payload.get("branch")
        if tracked is not None and branch is not None:
            tracked.ended.add(str(branch))
            tracked.failed = tracked.failed or failed
            if "output" in payload:
                tracked.outputs[str(branch)] = step_output(payload)
            if len(tracked.ended) < tracked.expected:
                return
            del state.branches[key]
            step.status, step.finished_at = ("failed" if tracked.failed else "succeeded"), ts
            step.output = [tracked.outputs.get(str(index)) for index in range(tracked.expected)]
            step.has_output = True
            return
        step.status = STEP_STATUS[kind]
        step.finished_at = ts
        if "output" in payload:
            step.output, step.has_output = step_output(payload), True

    def _finding(self, state: RunState, step_id: str | None, now: datetime, payload: dict[str, Any]) -> dict[str, Any]:
        path = _text(payload.get("path"), 4096)
        body = _text(payload.get("body") or payload.get("message") or payload.get("claim"), 60_000)
        line = payload.get("line")
        line = line if isinstance(line, int) and not isinstance(line, bool) and 0 <= line < 2 ** 31 else None
        key = _text(payload.get("finding_key") or payload.get("id"), 64)
        if key is None:
            key = hashlib.sha256(f"{path}|{line}|{body}".encode()).hexdigest()[:64]
        return {"project_id": state.project_id, "finding_key": key, "path": path, "line": line,
                "severity": _text(payload.get("severity"), 16), "disposition": _text(payload.get("disposition"), 32),
                "title": _text(payload.get("title"), 2000), "body": body, "step_id": step_id, "created_at": now}

    # Run summaries.

    def run_started(self, summary: dict[str, Any]) -> None:
        if self.running:
            self.broker.publish(summary_message(summary, list_row(summary)))

    def _publish_summary(self, state: RunState, immediate: bool) -> None:
        wait = state.published + SUMMARY_INTERVAL_S - time.monotonic()
        if not immediate and wait > 0:
            if state.summary_timer is None:
                state.summary_timer = self._loop.call_later(wait, self._publish_now, state)
            return
        self._publish_now(state)

    def _publish_now(self, state: RunState) -> None:
        if state.summary_timer is not None:
            state.summary_timer.cancel()
            state.summary_timer = None
        state.published = time.monotonic()
        self.broker.publish(summary_message(dict(state.summary), list_row(state.summary)))

    def summary(self, run_id: str) -> dict[str, Any] | None:
        state = self._runs.get(run_id)
        return dict(state.summary) if state is not None else None

    # Reads that see events the writer has not stored yet.

    async def events_after(self, run_id: str, after: int, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` events with seq above ``after``, oldest first."""
        return await self._events(run_id, after, None, limit)

    async def events_before(self, run_id: str, before: int, limit: int) -> list[dict[str, Any]]:
        """The ``limit`` events just below ``before``, oldest first."""
        return await self._events(run_id, 0, before, limit)

    async def _events(self, run_id: str, after: int, before: int | None, limit: int) -> list[dict[str, Any]]:
        def read(conn: Connection) -> list[dict[str, Any]]:
            query = (select(run_events.c.seq, run_events.c.ts, run_events.c.kind, run_events.c.step_id,
                            run_events.c.payload).where(run_events.c.run_id == run_id, run_events.c.seq > after))
            if before is None:
                query = query.order_by(run_events.c.seq)
            else:
                query = query.where(run_events.c.seq < before).order_by(run_events.c.seq.desc())
            return [{"seq": r.seq, "ts": _iso(r.ts), "kind": r.kind, "step_id": r.step_id, "payload": r.payload}
                    for r in conn.execute(query.limit(limit)).all()]

        pending = [{"seq": e["seq"], "ts": _iso(e["ts"]), "kind": e["kind"], "step_id": e["step_id"],
                    "payload": e["payload"]} for e in self.writer.pending(run_id, after)
                   if before is None or e["seq"] < before]
        stored = await asyncio.to_thread(self._read, read)
        merged = {e["seq"]: e for e in (*stored, *pending)}
        seqs = sorted(merged)
        return [merged[seq] for seq in (seqs[:limit] if before is None else seqs[-limit:])]

    async def last_seq(self, run_id: str) -> int:
        state = self._runs.get(run_id)
        if state is not None:
            return state.seq

        def read(conn: Connection) -> int:
            return conn.execute(select(func.max(run_events.c.seq)).where(run_events.c.run_id == run_id)).scalar() or 0

        return max(await asyncio.to_thread(self._read, read), self.writer.max_seq(run_id))

    # Stream slots.

    def open_stream(self, user_id: str) -> bool:
        if sum(self._streams.values()) >= self.sse_total or self._streams.get(user_id, 0) >= self.sse_per_user:
            return False
        self._streams[user_id] = self._streams.get(user_id, 0) + 1
        return True

    def close_stream(self, user_id: str) -> None:
        left = self._streams.get(user_id, 0) - 1
        if left > 0:
            self._streams[user_id] = left
        else:
            self._streams.pop(user_id, None)

    # Housekeeping.

    async def sweep(self) -> list[str]:
        """Mark runs silent for ``lost_after`` as lost, and forget idle run state."""
        await self.writer.flush()
        now = self.clock()
        cutoff = now - self.lost_after
        lost = await self.writer.run_in_writer(lambda conn: sweep_lost(conn, now, self.lost_after))
        for run_id in lost:
            state = self._runs.get(run_id)
            if state is not None and state.last_event_at is not None and state.last_event_at >= cutoff:
                # It spoke after the flush above. The next flush marks it running again.
                continue
            summary = await asyncio.to_thread(self._read, lambda conn, r=run_id: load_summary(conn, r))
            if summary is None:
                continue
            if state is None:
                self.broker.publish(summary_message(summary, list_row(summary)))
            elif state.summary["status"] == "running":
                state.summary.update(status=summary["status"], finished_at=summary["finished_at"])
                self._publish_now(state)
        idle = time.monotonic() - IDLE_STATE_S
        for run_id, state in list(self._runs.items()):
            if (state.seen < idle and state.summary_timer is None and not state.branches
                    and not self.writer.max_seq(run_id)):
                del self._runs[run_id]
        return lost

    async def _sweeper(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            try:
                await self.sweep()
            except Exception:
                log.exception("lost-run sweep failed")

    async def _lag_monitor(self) -> None:
        interval = 0.1
        while True:
            started = time.perf_counter()
            await asyncio.sleep(interval)
            self.lag_ms.append(max(0.0, (time.perf_counter() - started - interval) * 1000))
            del self.lag_ms[:-6000]

    def stats(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "rejected": self.rejected, "duplicates": self.duplicates,
                "errors": self.errors, "socket_dropped": self.socket_dropped, "overflows": self.broker.overflows,
                "subscribers": self.broker.subscribers, "flushes": self.writer.flushes, "rows": self.writer.rows,
                "flush_failures": self.writer.failures, "writer_dropped": self.writer.dropped,
                "writer_dropped_changes": self.writer.dropped_changes, "backlog": self.writer.backlog}


def _remember(cache: OrderedDict, key: str, value: Any, limit: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)

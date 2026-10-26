"""Non-blocking event emitter for runs, scripts, and session wrappers.

`ARMATURE_EVENTS` names the endpoint: `unix:/path/to.sock` for a datagram
socket, or an `http://` or `https://` URL that takes NDJSON batches.
`ARMATURE_EVENTS_TOKEN` is the per-run token. With no endpoint every call is
a no-op. No call ever raises to the caller or waits on the server.
"""
from __future__ import annotations

import atexit
import errno
import ipaddress
import itertools
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from armature.events.redact import default_redactor, redact_exact

MUST_KEEP = frozenset({
    "run_start", "run_end", "step_start", "step_end", "step_failed",
    "step_skipped", "retry", "cost", "finding", "tool_call", "tool_result",
    "session_start", "session_end", "error", "dropped", "artifact", "summary",
})
DROPPABLE = frozenset({"text", "log", "progress"})

WIRE_VERSION = 1
MAX_EVENT_BYTES = 60 * 1024
QUEUE_SIZE = 2000
BATCH_SIZE = 200
MAX_BATCH_BYTES = 900 * 1024
BATCH_INTERVAL_S = 0.25
SEND_TIMEOUT_S = 0.05
HTTP_TIMEOUT_S = 2.0
EXIT_FLUSH_S = 1.0
TEXT_INTERVAL_S = 0.25
_SOCKET_BUFFER = 256 * 1024
_RETRY_SLEEP_S = 0.005
_RESERVED = ("run_id", "team_id")
_PREFIX = b'{"v":%d,' % WIRE_VERSION
_EID_ROOM = 64
_MIN_SECRET = 4

ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ARTIFACT_TYPES = ("text/markdown", "text/plain", "application/json")
MAX_ARTIFACT_BYTES = 1024 * 1024
# Each part's content, JSON-escaped, so one part plus its envelope stays under MAX_EVENT_BYTES.
ARTIFACT_PART_BYTES = 40 * 1024
MAX_ARTIFACT_PARTS = 512

log = logging.getLogger(__name__)
_warned: set[str] = set()


def warn_once(key: str, message: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def plain_http_to_remote(url: str) -> bool:
    """True for an http:// URL whose host is not loopback, which would send the token in clear."""
    parts = urlsplit(url)
    return parts.scheme == "http" and not is_loopback_host(parts.hostname)


def is_must_keep(kind: str) -> bool:
    return kind in MUST_KEEP


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _dumps(event: dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _cap(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        return value[:cap]
    if isinstance(value, dict):
        return {k: _cap(v, cap) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cap(v, cap) for v in value[: max(cap // 64, 1)]]
    return value


def artifact_problem(name: Any, media_type: Any, content: Any) -> str | None:
    """Why an artifact would be refused, or None."""
    if not isinstance(name, str) or not ARTIFACT_NAME.fullmatch(name):
        return "an artifact name is 1 to 100 letters, digits, '.', '_', or '-', starting with a letter or digit"
    if media_type not in ARTIFACT_TYPES:
        return f"an artifact type is one of {', '.join(ARTIFACT_TYPES)}"
    if not isinstance(content, str):
        return "artifact content must be text"
    if len(content.encode("utf-8", "surrogatepass")) > MAX_ARTIFACT_BYTES:
        return f"an artifact is at most {MAX_ARTIFACT_BYTES} bytes"
    return None


def _escaped_size(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def artifact_parts(content: str) -> list[str]:
    """``content`` cut into pieces that each fit one event once JSON-escaped."""
    parts, start = [], 0
    while start < len(content):
        piece = content[start:start + ARTIFACT_PART_BYTES]
        while _escaped_size(piece) > ARTIFACT_PART_BYTES:
            piece = piece[:len(piece) // 2]
        parts.append(piece)
        start += len(piece)
    return parts or [""]


def encode_event(event: dict[str, Any], limit: int = MAX_EVENT_BYTES) -> bytes:
    data = _dumps(event)
    if len(data) <= limit:
        return data
    payload = event.get("payload") or {}
    cap = 8192
    while cap >= 64:
        data = _dumps({**event, "payload": {**_cap(payload, cap), "truncated": True}})
        if len(data) <= limit:
            return data
        cap //= 4
    return _dumps({**event, "payload": {"truncated": True}})


class Emitter:
    """Sends events to `endpoint` with `token`. Unset endpoint means no-op."""

    def __init__(
        self,
        endpoint: str | None = None,
        token: str = "",
        *,
        redact: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        queue_size: int = QUEUE_SIZE,
        batch_size: int = BATCH_SIZE,
        batch_interval_s: float = BATCH_INTERVAL_S,
        http_timeout_s: float = HTTP_TIMEOUT_S,
        text_interval_s: float = TEXT_INTERVAL_S,
    ):
        self.endpoint = endpoint or ""
        self._token = token
        self._redact = redact if redact is not None else default_redactor()
        self._own_secrets = tuple(
            v for v in (token, os.environ.get("ARMATURE_RUNNER_KEY", "")) if len(v) >= _MIN_SECRET
        )
        self._nonce = os.urandom(8).hex()
        self._counter = itertools.count(1)
        self._marker_nonce = os.urandom(8).hex()
        self._marker_counter = itertools.count(1)
        self._batch_cap = batch_size
        self._queue_size = queue_size
        self._batch_size = batch_size
        self._batch_interval_s = batch_interval_s
        self._http_timeout_s = http_timeout_s
        self._text_interval_s = text_interval_s
        self._pid = os.getpid()

        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._keep: deque[tuple[int, bytes]] = deque()
        self._drop: deque[tuple[int, bytes]] = deque()
        self._seq = 0
        self._inflight = 0
        self._flushing = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._text_last: dict[str | None, float] = {}
        self._text_pending: dict[str | None, bytes] = {}
        self._unreported_drops = 0

        self.sent = 0
        self.dropped = 0
        self.dropped_must_keep = 0
        self.coalesced = 0
        self.errors = 0

        self._sock: socket.socket | None = None
        self._sock_path = ""
        self._url = ""
        if self.endpoint.startswith("unix:"):
            self._sock_path = self.endpoint[len("unix:"):]
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SOCKET_BUFFER)
            except OSError:
                pass
        elif self.endpoint.startswith(("http://", "https://")):
            if plain_http_to_remote(self.endpoint):
                raise ValueError(f"refusing plain http to a non-loopback host: {self.endpoint!r}")
            self._url = self.endpoint
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            raise ValueError(f"unsupported ARMATURE_EVENTS endpoint: {self.endpoint!r}")
        atexit.register(self._at_exit)

    enabled = True

    @classmethod
    def from_env(
        cls,
        *,
        redact: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> Emitter:
        endpoint = os.environ.get("ARMATURE_EVENTS", "").strip()
        if not endpoint:
            return NullEmitter()
        try:
            return cls(endpoint, os.environ.get("ARMATURE_EVENTS_TOKEN", ""), redact=redact)
        except (ValueError, OSError) as exc:
            warn_once(f"from_env:{endpoint}", "Not sending run events: %s", exc)
            return NullEmitter()

    @property
    def queued(self) -> int:
        with self._lock:
            return len(self._keep) + len(self._drop) + len(self._text_pending)

    def stats(self) -> dict[str, int]:
        return {
            "sent": self.sent,
            "dropped": self.dropped,
            "dropped_must_keep": self.dropped_must_keep,
            "coalesced": self.coalesced,
            "errors": self.errors,
            "queued": self.queued,
        }

    def emit(self, kind: str, step_id: str | None = None, /, **payload: Any) -> None:
        if self._closed:
            return
        try:
            self._emit(kind, step_id, payload)
        except Exception:
            self.errors += 1

    def artifact(self, name: str, content: str, media_type: str = "text/markdown",
                 step_id: str | None = None) -> bool:
        """Send ``content`` as the run artifact ``name``, in as many events as it takes. False when refused."""
        if self._closed:
            return False
        try:
            problem = artifact_problem(name, media_type, content)
            if problem is None:
                content = self.redact({"content": content}).get("content")
                problem = artifact_problem(name, media_type, content)
            if problem is not None:
                warn_once(f"artifact:{problem}", "Not sending artifact %r: %s", name, problem)
                return False
            parts = artifact_parts(content)
            upload = os.urandom(8).hex()
            with self._lock:
                for index, part in enumerate(parts):
                    payload = {"name": name, "media_type": media_type, "upload": upload, "part": index,
                               "parts": len(parts), "content": part}
                    data = encode_event(self._envelope("artifact", step_id, payload), MAX_EVENT_BYTES - _EID_ROOM)
                    self._submit_locked(True, data)
            return True
        except Exception:
            self.errors += 1
            return False

    @property
    def token(self) -> str:
        return self._token

    def redact(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = self._redact(payload)
        except Exception:
            return {"redaction_failed": True}
        return redact_exact(payload, self._own_secrets) if self._own_secrets else payload

    def _envelope(self, kind: str, step_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        event: dict[str, Any] = {"v": WIRE_VERSION}
        # HTTP carries the token in the Authorization header only.
        if self._sock is not None:
            event["token"] = self._token
        event.update(kind=kind, ts=_now_iso(), step_id=step_id, pid=self._pid, payload=payload)
        return event

    def _build(self, kind: str, step_id: str | None, payload: dict[str, Any]) -> bytes:
        for key in _RESERVED:
            payload.pop(key, None)
        return encode_event(self._envelope(kind, step_id, self.redact(payload)), MAX_EVENT_BYTES - _EID_ROOM)

    def _stamp(self, data: bytes, nonce: str | None = None, counter: Any = None) -> bytes:
        # The server keeps the highest counter per nonce and drops anything at or below it, so
        # the counter is taken when an event enters the send order, and a retry keeps it.
        eid = f"{nonce or self._nonce}:{next(counter or self._counter)}"
        return _PREFIX + b'"eid":"' + eid.encode() + b'",' + data[len(_PREFIX):]

    def _emit(self, kind: str, step_id: str | None, payload: dict[str, Any]) -> None:
        data = self._build(kind, step_id, payload)
        must_keep = is_must_keep(kind)
        with self._lock:
            if kind == "text":
                now = time.monotonic()
                if now - self._text_last.get(step_id, 0.0) < self._text_interval_s:
                    if step_id in self._text_pending:
                        self.coalesced += 1
                    self._text_pending[step_id] = data
                    self._ensure_thread_locked()
                    return
                if self._text_pending.pop(step_id, None) is not None:
                    self.coalesced += 1
                self._text_last[step_id] = now
            else:
                pending = self._text_pending.pop(step_id, None)
                if pending is not None:
                    self._text_last[step_id] = time.monotonic()
                    self._submit_locked(False, pending)
            self._submit_locked(must_keep, data)

    def _submit_locked(self, must_keep: bool, data: bytes) -> None:
        data = self._stamp(data)
        if self._sock is not None and not self._keep and not self._drop and self._inflight == 0:
            outcome = self._report_drops_locked() if self._unreported_drops else "sent"
            if outcome == "sent":
                outcome = self._send_datagram(data)
            if outcome == "sent":
                self.sent += 1
                return
            if outcome == "gone":
                self._count_drop(must_keep)
                return
        self._enqueue_locked(must_keep, data)

    def _enqueue_locked(self, must_keep: bool, data: bytes) -> None:
        if len(self._keep) + len(self._drop) + self._inflight >= self._queue_size:
            if self._drop:
                self._drop.popleft()
                self._count_drop(False)
            elif must_keep and self._keep:
                self._keep.popleft()
                self._count_drop(True)
            else:
                self._count_drop(must_keep)
                return
        self._seq += 1
        (self._keep if must_keep else self._drop).append((self._seq, data))
        self._ensure_thread_locked()
        self._wake.notify()

    def _count_drop(self, must_keep: bool) -> None:
        self.dropped += 1
        self._unreported_drops += 1
        if must_keep:
            self.dropped_must_keep += 1

    def _drop_marker(self) -> bytes:
        # Markers count on their own nonce, so one built late never hides a queued event.
        marker = encode_event(self._envelope("dropped", None, {"count": self._unreported_drops}))
        return self._stamp(marker, self._marker_nonce, self._marker_counter)

    def _report_drops_locked(self) -> str:
        outcome = self._send_datagram(self._drop_marker())
        if outcome == "sent":
            self.sent += 1
            self._unreported_drops = 0
        return outcome

    def _send_datagram(self, data: bytes) -> str:
        try:
            self._sock.sendto(data, self._sock_path)
            return "sent"
        except (BlockingIOError, InterruptedError):
            return "busy"
        except OSError as exc:
            return "busy" if exc.errno in (errno.ENOBUFS, errno.EAGAIN) else "gone"

    def _ensure_thread_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            target = self._run_unix if self._sock is not None else self._run_http
            self._thread = threading.Thread(target=target, name="armature-events", daemon=True)
            self._thread.start()

    def _next_queue_locked(self) -> deque[tuple[int, bytes]] | None:
        if self._keep and (not self._drop or self._keep[0][0] < self._drop[0][0]):
            return self._keep
        return self._drop or None

    def _pop_next_locked(self) -> tuple[bool, int, bytes] | None:
        queue = self._next_queue_locked()
        if queue is None:
            return None
        seq, data = queue.popleft()
        return queue is self._keep, seq, data

    def _release_due_text_locked(self, force: bool = False) -> None:
        now = time.monotonic()
        for step_id in list(self._text_pending):
            if force or now - self._text_last.get(step_id, 0.0) >= self._text_interval_s:
                self._text_last[step_id] = now
                self._seq += 1
                self._drop.append((self._seq, self._stamp(self._text_pending.pop(step_id))))

    def _has_work_locked(self) -> bool:
        return bool(self._keep or self._drop or self._text_pending)

    def _run_unix(self) -> None:
        while not self._closed:
            with self._lock:
                while not self._has_work_locked():
                    if self._closed:
                        return
                    self._wake.wait(0.5)
                self._release_due_text_locked(force=self._flushing or self._closed)
                if self._unreported_drops and self._report_drops_locked() == "busy":
                    item = None
                else:
                    item = self._pop_next_locked()
                if item is not None:
                    self._inflight += 1
            if item is None:
                time.sleep(_RETRY_SLEEP_S)
                continue
            must_keep, seq, data = item
            deadline = time.monotonic() + SEND_TIMEOUT_S
            while True:
                outcome = self._send_datagram(data)
                if outcome != "busy" or time.monotonic() >= deadline:
                    break
                time.sleep(_RETRY_SLEEP_S)
            with self._lock:
                self._inflight -= 1
                if outcome == "sent":
                    self.sent += 1
                elif outcome == "gone":
                    self._count_drop(must_keep)
                else:
                    self._requeue_locked([(must_keep, seq, data)])
                    self._wake.wait(_RETRY_SLEEP_S)
                self._wake.notify_all()

    def _requeue_locked(self, items: list[tuple[bool, int, bytes]], keep_droppable: bool = False) -> None:
        for must_keep, seq, data in reversed(items):
            if not (must_keep or keep_droppable):
                self._count_drop(False)
                continue
            if len(self._keep) + len(self._drop) + self._inflight >= self._queue_size:
                self._count_drop(must_keep)
                continue
            (self._keep if must_keep else self._drop).appendleft((seq, data))

    def _run_http(self) -> None:
        backoff = 0.0
        while not self._closed:
            with self._lock:
                while not self._has_work_locked():
                    if self._closed:
                        return
                    self._wake.wait(0.5)
                first_seen = time.monotonic()
                while (
                    len(self._keep) + len(self._drop) < self._batch_size
                    and not (self._flushing or self._closed)
                    and time.monotonic() - first_seen < self._batch_interval_s
                ):
                    self._wake.wait(self._batch_interval_s - (time.monotonic() - first_seen))
                self._release_due_text_locked(force=self._flushing or self._closed)
                marker = self._drop_marker() if self._unreported_drops else None
                drops_in_batch = self._unreported_drops
                self._unreported_drops = 0
                size = len(marker) + 1 if marker else 0
                batch: list[tuple[bool, int, bytes]] = []
                while len(batch) < self._batch_cap:
                    queue = self._next_queue_locked()
                    if queue is None or (batch and size + len(queue[0][1]) + 1 > MAX_BATCH_BYTES):
                        break
                    size += len(queue[0][1]) + 1
                    item = self._pop_next_locked()
                    batch.append(item)
                self._inflight += len(batch)
            lines = ([marker] if marker else []) + [d for _, _, d in batch]
            status = self._post(b"\n".join(lines) + b"\n") if lines else "ok"
            with self._lock:
                self._inflight -= len(batch)
                ok = status == "ok"
                if ok:
                    self.sent += len(lines)
                    backoff = 0.0
                    self._batch_cap = self._batch_size
                elif status == "too_large" and len(lines) > 1:
                    self._unreported_drops += drops_in_batch
                    self._requeue_locked(batch, keep_droppable=True)
                    self._batch_cap = max(1, len(batch) // 2)
                    ok = True
                else:
                    self.errors += 1
                    self._unreported_drops += drops_in_batch
                    if status == "too_large":
                        for must_keep, _, _ in batch:
                            self._count_drop(must_keep)
                    else:
                        self._requeue_locked(batch)
                        backoff = min(max(backoff * 2, 0.5), 5.0)
                self._wake.notify_all()
                retry_at = time.monotonic() + backoff
                while not (ok or self._flushing or self._closed) and time.monotonic() < retry_at:
                    self._wake.wait(retry_at - time.monotonic())

    def _post(self, body: bytes) -> str:
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-ndjson",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with self._opener.open(request, timeout=self._http_timeout_s) as response:
                return "ok" if 200 <= response.status < 300 else "failed"
        except urllib.error.HTTPError as exc:
            return "too_large" if exc.code == 413 else "failed"
        except Exception:
            return "failed"

    def flush(self, timeout: float = EXIT_FLUSH_S) -> bool:
        """Wait up to `timeout` for queued events to go out. True when drained."""
        deadline = time.monotonic() + timeout
        with self._lock:
            if self._has_work_locked():
                self._ensure_thread_locked()
            self._flushing = True
            self._wake.notify_all()
            try:
                while self._has_work_locked() or self._inflight:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._wake.wait(min(remaining, 0.05))
                return True
            finally:
                self._flushing = False

    def close(self, timeout: float = EXIT_FLUSH_S) -> None:
        if self._closed:
            return
        self.flush(timeout)
        with self._lock:
            self._closed = True
            self._wake.notify_all()
        try:
            atexit.unregister(self._at_exit)
        except Exception:
            pass
        if self._sock is not None:
            self._sock.close()

    def _at_exit(self) -> None:
        try:
            self.close(EXIT_FLUSH_S)
        except Exception:
            pass


class NullEmitter(Emitter):
    """What `from_env` returns when `ARMATURE_EVENTS` is unset."""

    enabled = False

    def __init__(self) -> None:
        self.endpoint = ""
        self._token = ""
        self._own_secrets = ()
        self.sent = self.dropped = self.dropped_must_keep = self.coalesced = self.errors = 0

    @property
    def queued(self) -> int:
        return 0

    def emit(self, kind: str, step_id: str | None = None, /, **payload: Any) -> None:
        return None

    def artifact(self, name: str, content: str, media_type: str = "text/markdown",
                 step_id: str | None = None) -> bool:
        return False

    def redact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def flush(self, timeout: float = EXIT_FLUSH_S) -> bool:
        return True

    def close(self, timeout: float = EXIT_FLUSH_S) -> None:
        return None


def get_emitter() -> Emitter:
    """The emitter `armature emit` uses: whatever the environment names."""
    return Emitter.from_env()

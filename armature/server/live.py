"""In-process pub/sub for live run events (DESIGN.md, "Run history and live activity").

``Broker`` is the seam for running several server processes. A Postgres
``LISTEN``/``NOTIFY`` or Redis broker would implement the same ``publish`` and
``subscribe``: ``publish`` sends (run_id, seq) or the run summary to the other
processes, and each process hands what it hears to its own ``LocalBroker``,
which fans out to that process's subscribers. Only ``LocalBroker`` exists.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi.encoders import jsonable_encoder

QUEUE_SIZE = 500
RUN_EVENT = "run-event"
RUN = "run"


def _json(data: Any) -> str:
    return json.dumps(jsonable_encoder(data), separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Message:
    """One SSE frame, serialized once and shared by every subscriber."""

    run_id: str
    team_id: str
    project_id: str | None
    kind: str
    frame: str
    list_frame: str | None = None
    seq: int | None = None


def event_message(run_id: str, team_id: str, project_id: str | None, event: dict[str, Any]) -> Message:
    frame = f"id: {event['seq']}\nevent: {RUN_EVENT}\ndata: {_json(event)}\n\n"
    return Message(run_id, team_id, project_id, RUN_EVENT, frame, seq=event["seq"])


def summary_message(summary: dict[str, Any], row: dict[str, Any]) -> Message:
    return Message(summary["id"], summary["team_id"], summary["project_id"], RUN,
                   f"event: {RUN}\ndata: {_json(summary)}\n\n", f"event: {RUN}\ndata: {_json(row)}\n\n")


class Subscription:
    """A bounded queue. When it fills, the broker drops it and ``overflowed`` tells the reader to reconnect."""

    def __init__(self, keys: tuple[str, ...], accept: Callable[[Message], bool] | None, size: int):
        self.keys = keys
        self.accept = accept
        self.queue: asyncio.Queue[Message] = asyncio.Queue(size)
        self.overflowed = False


class Broker(Protocol):
    def subscribe(self, keys: Iterable[str], accept: Callable[[Message], bool] | None = None) -> Subscription: ...

    def unsubscribe(self, sub: Subscription) -> None: ...

    def publish(self, message: Message) -> None: ...


def run_key(run_id: str) -> str:
    return f"run:{run_id}"


def runs_key(team_id: str | None) -> str:
    return f"runs:{team_id or '*'}"


class LocalBroker:
    """Fans out on the event loop. ``publish`` must be called from the loop's thread."""

    def __init__(self, queue_size: int = QUEUE_SIZE):
        self.queue_size = queue_size
        self._subs: dict[str, set[Subscription]] = {}
        self.overflows = 0

    def subscribe(self, keys: Iterable[str], accept: Callable[[Message], bool] | None = None) -> Subscription:
        sub = Subscription(tuple(keys), accept, self.queue_size)
        for key in sub.keys:
            self._subs.setdefault(key, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        for key in sub.keys:
            subs = self._subs.get(key)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    del self._subs[key]

    @property
    def subscribers(self) -> int:
        return len({sub for subs in self._subs.values() for sub in subs})

    def publish(self, message: Message) -> None:
        keys = [run_key(message.run_id)]
        if message.kind == RUN:
            keys += [runs_key(message.team_id), runs_key(None)]
        for key in keys:
            for sub in list(self._subs.get(key, ())):
                if sub.accept is not None and not sub.accept(message):
                    continue
                try:
                    sub.queue.put_nowait(message)
                except asyncio.QueueFull:
                    sub.overflowed = True
                    self.overflows += 1
                    self.unsubscribe(sub)

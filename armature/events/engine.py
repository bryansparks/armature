"""Translates Harness `on_event` callbacks into wire events."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

_STEP_EVENTS = {
    "stage_start": "step_start",
    "stage_complete": "step_end",
    "stage_failed": "step_failed",
    "stage_skipped": "step_skipped",
    "stage_resumed": "step_skipped",
    "retry_attempt": "retry",
    "loop_iteration": "progress",
    "tier_escalation_warning": "log",
}


def forward_engine_event(emit: Callable[..., None], name: str, data: dict[str, Any]) -> None:
    kind = _STEP_EVENTS.get(name)
    if kind is None:
        return
    payload = {k: v for k, v in data.items() if k != "stage"}
    if name == "stage_resumed":
        payload["reason"] = "resumed"
    elif kind in ("progress", "log"):
        payload["event"] = name
    emit(kind, data.get("stage"), **payload)

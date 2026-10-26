"""Instance display settings: cost and man-hour visibility, the viewer's time zone, and how workflows show."""
from __future__ import annotations

import copy
import math
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Connection, select

from armature.server.db.schema import INSTANCE_SCOPE, settings

PREFIX = "display."
DEFAULTS: dict[str, Any] = {
    "show_cost": True,
    "show_man_hours": True,
    "human_multiplier": 1,
    "timezone": "America/Denver",
    # Bookkeeping workflows stay out of run lists until an admin saves labels of their own.
    "workflow_labels": {name: {"label": "", "color": "", "hidden": True} for name in ("pr-merged", "build-announce")},
    "hidden_actors": [],
}
HUMAN_MULTIPLIER_MAX = 100
LABEL_MAX = 32
COLORS = tuple(f"s{n}" for n in range(1, 9))
WORKFLOW_NAME_MAX = 191
ACTOR_MAX = 191
HIDDEN_ACTORS_MAX = 50
CONTROL = re.compile(r"[\u0000-\u001f\u007f-\u009f]")


class Invalid(ValueError):
    """A display setting value that fails its check. The message is for the person."""


def _bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise Invalid(f"{name} must be true or false.")
    return value


def _multiplier(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) \
            or value <= 0 or value > HUMAN_MULTIPLIER_MAX:
        raise Invalid(f"human_multiplier must be a number above 0 and at most {HUMAN_MULTIPLIER_MAX}.")
    return value


def _timezone(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise Invalid("timezone must be an IANA name such as America/Denver.")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise Invalid(f"{value} is not a known time zone.") from exc
    return value


def _labels(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise Invalid("workflow_labels must be an object keyed by workflow name.")
    cleaned: dict[str, dict[str, str]] = {}
    seen: dict[str, str] = {}
    for workflow, entry in value.items():
        if not workflow or len(workflow) > WORKFLOW_NAME_MAX:
            raise Invalid("A workflow name is 1 to 191 characters.")
        if not isinstance(entry, dict) or set(entry) - {"label", "color", "hidden"}:
            raise Invalid(f"The entry for {workflow} must be an object with label, color, and hidden.")
        label, color, hidden = entry.get("label", ""), entry.get("color", ""), entry.get("hidden", False)
        if not isinstance(hidden, bool):
            raise Invalid(f"hidden for {workflow} must be true or false.")
        if not isinstance(label, str) or not isinstance(color, str):
            raise Invalid(f"The label and color for {workflow} must be text.")
        label = label.strip()
        if len(label) > LABEL_MAX or CONTROL.search(label):
            raise Invalid(f"The label for {workflow} is up to {LABEL_MAX} printable characters.")
        if color and color not in COLORS:
            raise Invalid(f"The color for {workflow} must be one of {', '.join(COLORS)}.")
        if label:
            if label.lower() in seen:
                raise Invalid(f"{seen[label.lower()]} and {workflow} would both read as {label}.")
            seen[label.lower()] = workflow
        if label or color or hidden:
            cleaned[workflow] = {"label": label, "color": color, "hidden": hidden}
    return cleaned


def _actors(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise Invalid("hidden_actors must be a list of account names.")
    cleaned: list[str] = []
    for actor in value:
        if not isinstance(actor, str) or not actor or len(actor) > ACTOR_MAX or CONTROL.search(actor):
            raise Invalid(f"An account name is 1 to {ACTOR_MAX} printable characters.")
        if actor not in cleaned:
            cleaned.append(actor)
    if len(cleaned) > HIDDEN_ACTORS_MAX:
        raise Invalid(f"hidden_actors holds at most {HIDDEN_ACTORS_MAX} account names.")
    return cleaned


CHECKS = {
    "show_cost": lambda v: _bool("show_cost", v),
    "show_man_hours": lambda v: _bool("show_man_hours", v),
    "human_multiplier": _multiplier,
    "timezone": _timezone,
    "workflow_labels": _labels,
    "hidden_actors": _actors,
}


def check(key: str, value: Any, team_id: str | None) -> Any:
    """The value to store for a ``display.`` key. Raises Invalid with a message for the person."""
    name = key.removeprefix(PREFIX)
    if name not in CHECKS:
        raise Invalid(f"Unknown display setting. The display settings are {', '.join(PREFIX + k for k in CHECKS)}.")
    if team_id is not None:
        raise Invalid("Display settings apply to the whole instance, so set them on the instance scope.")
    return CHECKS[name](value)


def hidden(current: dict[str, Any]) -> list[str]:
    """Workflows whose runs stay out of run lists unless asked for by name."""
    return sorted(name for name, entry in current["workflow_labels"].items() if entry.get("hidden"))


def hidden_actors(current: dict[str, Any]) -> list[str]:
    """Accounts whose runs stay out of the run list unless hidden runs are shown."""
    return list(current["hidden_actors"])


def load(conn: Connection) -> dict[str, Any]:
    """Stored display settings over the defaults. A stored value that no longer passes its check is ignored."""
    rows = conn.execute(select(settings.c.key, settings.c.value)
                        .where(settings.c.scope == INSTANCE_SCOPE,
                               settings.c.key.in_([PREFIX + k for k in DEFAULTS]))).all()
    current = copy.deepcopy(DEFAULTS)
    for row in rows:
        name = row.key.removeprefix(PREFIX)
        try:
            current[name] = CHECKS[name](row.value)
        except Invalid:
            continue
    return current

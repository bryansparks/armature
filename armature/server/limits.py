"""Instance limits that runner hello, heartbeat, and claim answers carry for runners that follow them."""
from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, select

from armature.server.db.schema import INSTANCE_SCOPE, settings

MAX_SLOTS_KEY = "runner.max_slots"
MAX_SLOTS_LIMIT = 200


class Invalid(ValueError):
    """A limit setting that fails its check. The message is for the person."""


def check_setting(key: str, value: Any) -> Any:
    """The value to store for ``runner.max_slots``, the jobs a runner that follows it keeps going at once."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SLOTS_LIMIT:
        raise Invalid(f"{key} is a whole number from 1 to {MAX_SLOTS_LIMIT}.")
    return value


def max_slots(conn: Connection) -> int | None:
    """The stored ``runner.max_slots``, or None when it is unset or no longer passes its check."""
    value = conn.execute(select(settings.c.value).where(settings.c.scope == INSTANCE_SCOPE,
                                                       settings.c.key == MAX_SLOTS_KEY)).scalar()
    try:
        return check_setting(MAX_SLOTS_KEY, value)
    except Invalid:
        return None

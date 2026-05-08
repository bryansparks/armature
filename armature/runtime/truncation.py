"""Output truncation utilities for context window management."""
from __future__ import annotations
import json
from typing import Any

_TRUNCATION_MARKER = "...[truncated]"


def truncate_result(result: Any, limit: int) -> Any:
    """Truncate a stage result to at most `limit` characters.

    - dict: each leaf string value is truncated; a "_truncated" sentinel key is added
      when any value was shortened.
    - str: truncated with marker appended.
    - list/other: serialised to JSON string, then truncated if needed.

    Non-string scalars (int, float, bool, None) are never truncated.
    """
    if isinstance(result, dict):
        return _truncate_dict(result, limit)
    if isinstance(result, str):
        return _truncate_str(result, limit)
    if isinstance(result, (list, tuple)):
        serialised = json.dumps(result)
        if len(serialised) <= limit:
            return result
        return _truncate_str(serialised, limit)
    return result


def _truncate_str(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + _TRUNCATION_MARKER


def _truncate_dict(d: dict[str, Any], limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    was_truncated = False
    for k, v in d.items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = _truncate_str(v, limit)
            was_truncated = True
        elif isinstance(v, dict):
            out[k] = _truncate_dict(v, limit)
            if out[k].get("_truncated"):
                was_truncated = True
        else:
            out[k] = v
    if was_truncated:
        out["_truncated"] = True
    return out

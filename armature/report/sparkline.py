"""Unicode sparkline helper — maps a sequence of floats to block characters."""
from __future__ import annotations

_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Return a string of Unicode block characters representing *values* (0–1 range)."""
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    span = hi - lo if hi != lo else 1.0
    result = []
    for v in values:
        v = max(0.0, min(1.0, v))
        norm = (v - lo) / span if hi != lo else 0.5
        idx = min(int(norm * len(_BLOCKS)), len(_BLOCKS) - 1)
        result.append(_BLOCKS[idx])
    return "".join(result)

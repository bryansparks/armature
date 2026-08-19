"""Stage credit attribution — per-stage leverage analysis.

Pure functions over ``list[TraceRecord]``. No async, no store dependency:
callers (the improve runner and the dashboard loader) already hold the
traces list in memory.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

from armature.state.traces import TraceRecord, compute_hqs_from_traces


def _pearson_r(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation of two equal-length series.

    Returns ``None`` when length < 2 or either series has zero variance
    (the correlation is undefined in those cases).
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0.0 or syy == 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / ((sxx ** 0.5) * (syy ** 0.5))
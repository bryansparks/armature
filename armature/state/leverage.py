"""Stage credit attribution — per-stage leverage analysis.

Pure functions over ``list[TraceRecord]``. No async, no store dependency:
callers (the improve runner and the dashboard loader) already hold the
traces list in memory.
"""
from __future__ import annotations
from dataclasses import dataclass
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


@dataclass
class StageLeverage:
    stage_id: str
    signal_name: Literal["quorum", "success_valid"]
    r: float | None
    n_runs: int
    sufficient: bool


@dataclass
class LeverageReport:
    stages: dict[str, StageLeverage]
    n_runs: int
    min_runs_required: int
    min_abs_r: float
    sufficient: bool
    reason: str


def _stage_signal(run_traces: list[TraceRecord]) -> tuple[float, Literal["quorum", "success_valid"]]:
    """Per-run signal for one stage: mean quorum if any quorum present, else mean(success & valid)."""
    quorum_vals = [t.quorum_score for t in run_traces if t.quorum_score is not None]
    if quorum_vals:
        return sum(quorum_vals) / len(quorum_vals), "quorum"
    sv = [1.0 if (t.success and t.output_valid) else 0.0 for t in run_traces]
    return sum(sv) / len(sv), "success_valid"


def compute_leverage(
    traces: list[TraceRecord],
    *,
    min_runs: int = 8,
    min_abs_r: float = 0.4,
) -> LeverageReport:
    """Compute per-stage leverage = Pearson r of (per-run stage signal, run HQS).

    Pure over an in-memory trace list. Every guard returns a valid report;
    never raises.
    """
    # Group by run_id, preserving first-seen order.
    runs: dict[str, list[TraceRecord]] = {}
    for t in traces:
        runs.setdefault(t.run_id, []).append(t)
    n_runs = len(runs)

    if n_runs < 2:
        return LeverageReport(stages={}, n_runs=n_runs, min_runs_required=min_runs,
                              min_abs_r=min_abs_r, sufficient=False, reason="need >=2 runs")

    # Per-run HQS and per-stage per-run signals.
    run_hqs: dict[str, float] = {rid: compute_hqs_from_traces(rt).hqs for rid, rt in runs.items()}

    # stage -> list of (signal, hqs) across runs where the stage appears.
    stage_series: dict[str, list[tuple[float, float]]] = {}
    stage_signal_name: dict[str, Literal["quorum", "success_valid"]] = {}
    for rid, rt in runs.items():
        by_stage: dict[str, list[TraceRecord]] = {}
        for t in rt:
            by_stage.setdefault(t.stage_id, []).append(t)
        for sid, stage_traces in by_stage.items():
            sig, name = _stage_signal(stage_traces)
            stage_signal_name[sid] = name
            stage_series.setdefault(sid, []).append((sig, run_hqs[rid]))

    stages: dict[str, StageLeverage] = {}
    any_sufficient = False
    for sid, series in stage_series.items():
        xs = [p[0] for p in series]
        ys = [p[1] for p in series]
        r = _pearson_r(xs, ys)
        stage_n = len(series)
        sufficient = (
            n_runs >= min_runs
            and stage_n >= 2
            and r is not None
            and abs(r) >= min_abs_r
        )
        if sufficient:
            any_sufficient = True
        stages[sid] = StageLeverage(
            stage_id=sid,
            signal_name=stage_signal_name[sid],
            r=r,
            n_runs=stage_n,
            sufficient=sufficient,
        )

    if n_runs < min_runs:
        reason = f"need >= {min_runs} runs (have {n_runs})"
        sufficient = False
    elif not any_sufficient:
        reason = f"no stage reached |r|>= {min_abs_r}"
        sufficient = False
    else:
        reason = "ok"
        sufficient = True

    return LeverageReport(
        stages=stages,
        n_runs=n_runs,
        min_runs_required=min_runs,
        min_abs_r=min_abs_r,
        sufficient=sufficient,
        reason=reason,
    )

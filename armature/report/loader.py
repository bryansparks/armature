"""Load DashboardData from disk — queries TraceStore and improvement log."""
from __future__ import annotations
from pathlib import Path

from armature.report.aggregator import (
    DashboardData,
    build_stage_stats,
    load_improvement_cycles,
    load_safety_stats,
)


async def load_dashboard_data(
    workflow_name: str,
    traces_db: Path | None = None,
    improve_log: Path | None = None,
    last_n: int = 200,
) -> DashboardData:
    """Query the last *last_n* traces for *workflow_name* and aggregate into DashboardData."""
    from armature.state.traces import TraceStore, compute_hqs_from_traces
    from armature.state.leverage import compute_leverage

    db = traces_db or Path("~/.armature/traces.db").expanduser()
    store = TraceStore(db)

    if db.exists():
        await store.init()
        traces = await store.query(workflow_name, limit=last_n)
    else:
        traces = []

    # HQS trend: compute per-run HQS, ordered oldest to newest
    hqs_trend: list[float] = []
    run_ids_seen: list[str] = []
    for t in traces:
        if t.run_id not in run_ids_seen:
            run_ids_seen.append(t.run_id)
    # store.query returns traces newest-first (timestamp DESC); reverse so the
    # trend and last_run_id follow the documented oldest → newest contract.
    run_ids_seen.reverse()

    for run_id in run_ids_seen:
        run_traces = [t for t in traces if t.run_id == run_id]
        if run_traces:
            hqs = compute_hqs_from_traces(run_traces).hqs
            hqs_trend.append(round(hqs, 4))

    last_run_id = run_ids_seen[-1] if run_ids_seen else None

    # Most recent trace timestamp for display
    last_run_at: str | None = None
    if last_run_id:
        last_run_traces = [t for t in traces if t.run_id == last_run_id]
        if last_run_traces:
            last_run_at = max(t.timestamp for t in last_run_traces)

    # Load improvement log
    log_path = improve_log
    if log_path is None:
        log_path = Path(f"{workflow_name}.improve_log.jsonl")

    cycles = load_improvement_cycles(log_path)
    stage_stats = build_stage_stats(traces)
    safety_stats = load_safety_stats(traces)
    leverage = compute_leverage(traces)

    return DashboardData(
        workflow_name=workflow_name,
        total_runs=len(run_ids_seen),
        traces=traces,
        stage_stats=stage_stats,
        improvement_cycles=cycles,
        safety_stats=safety_stats,
        hqs_trend=hqs_trend,
        last_run_id=last_run_id,
        last_run_at=last_run_at,
        leverage=leverage,
    )

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
    from armature.state.traces import TraceStore

    db = traces_db or Path("~/.armature/traces.db").expanduser()
    store = TraceStore(db)

    if db.exists():
        await store.init()
        traces = await store.query(workflow_name, limit=last_n)
    else:
        traces = []

    # IHR trend: compute per-run IHR, ordered oldest to newest
    ihr_trend: list[float] = []
    run_ids_seen: list[str] = []
    for t in traces:
        if t.run_id not in run_ids_seen:
            run_ids_seen.append(t.run_id)

    for run_id in run_ids_seen:
        run_traces = [t for t in traces if t.run_id == run_id]
        if run_traces:
            success_rate = sum(1 for t in run_traces if t.success) / len(run_traces)
            valid_rate = sum(1 for t in run_traces if t.output_valid) / len(run_traces)
            quorum_vals = [t.quorum_score for t in run_traces if t.quorum_score is not None]
            avg_quorum = sum(quorum_vals) / len(quorum_vals) if quorum_vals else 0.5
            latencies = [t.latency_ms for t in run_traces]
            max_lat = max(latencies) if latencies else 1.0
            latency_score = max(0.0, 1.0 - max_lat / 60_000)
            ihr = (
                0.40 * valid_rate
                + 0.30 * success_rate
                + 0.20 * avg_quorum
                + 0.10 * latency_score
            )
            ihr_trend.append(round(ihr, 4))

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

    return DashboardData(
        workflow_name=workflow_name,
        total_runs=len(run_ids_seen),
        traces=traces,
        stage_stats=stage_stats,
        improvement_cycles=cycles,
        safety_stats=safety_stats,
        ihr_trend=ihr_trend,
        last_run_id=last_run_id,
        last_run_at=last_run_at,
    )

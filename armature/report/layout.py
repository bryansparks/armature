"""Full dashboard layout — assembles all panels into a Rich Layout."""
from __future__ import annotations
from pathlib import Path
from typing import Any

from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.table import Table
import time

from armature.report.aggregator import DashboardData
from armature.report.panels import (
    health_strip,
    stage_breakdown,
    improvement_timeline,
    safety_governance,
)


def _build_layout(data: DashboardData) -> Layout:
    root = Layout()
    root.split_column(
        Layout(health_strip(data), name="health", size=5),
        Layout(name="middle", ratio=1),
    )
    root["middle"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=1),
    )
    root["middle"]["left"].update(stage_breakdown(data))
    root["middle"]["right"].split_column(
        Layout(improvement_timeline(data), name="timeline", ratio=1),
        Layout(safety_governance(data), name="safety", size=8),
    )
    return root


def render_terminal(data: DashboardData, console: Console | None = None) -> None:
    """Print the full dashboard to the terminal once (content-height only)."""
    c = console or Console()
    c.print(health_strip(data))
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    right = Group(improvement_timeline(data), safety_governance(data))
    grid.add_row(stage_breakdown(data), right)
    c.print(grid)


def render_terminal_watch(
    loader: "callable[[], DashboardData]",
    interval: float = 5.0,
    console: Console | None = None,
) -> None:
    """Continuously refresh the dashboard; Ctrl-C to quit."""
    c = console or Console()
    try:
        with Live(console=c, refresh_per_second=0.2, screen=True) as live:
            while True:
                data = loader()
                live.update(_build_layout(data))
                time.sleep(interval)
    except KeyboardInterrupt:
        pass


def render_json(data: DashboardData) -> dict[str, Any]:
    """Return a machine-readable dict of dashboard data."""
    return {
        "workflow_name": data.workflow_name,
        "total_runs": data.total_runs,
        "current_hqs": data.current_hqs,
        "health_color": data.health_color,
        "hqs_delta": data.hqs_delta,
        "hqs_trend": data.hqs_trend,
        "stage_stats": {
            sid: {
                "run_count": s.run_count,
                "failure_rate": s.failure_rate,
                "avg_latency_ms": s.avg_latency_ms,
                "avg_quorum": s.avg_quorum,
                "escalation_rate": s.escalation_rate,
                "is_post_run": s.is_post_run,
            }
            for sid, s in data.stage_stats.items()
        },
        "improvement_cycles": [
            {
                "cycle_number": c.cycle_number,
                "timestamp": c.timestamp,
                "hqs_before": c.hqs_before,
                "drift_score": c.drift_score,
                "applied": c.applied,
                "requires_review": c.requires_review,
                "verified_fixes": c.verified_fixes,
                "missed_predictions": c.missed_predictions,
                "unexpected_regressions": c.unexpected_regressions,
                "escalated_oscillation": c.escalated_oscillation,
            }
            for c in data.improvement_cycles
        ],
        "safety": {
            "warn_hits": data.safety_stats.warn_hits,
            "block_hits": data.safety_stats.block_hits,
            "approval_hits": data.safety_stats.approval_hits,
            "postcondition_failures": data.safety_stats.postcondition_failures,
            "stale_memory_count": data.safety_stats.stale_memory_count,
            "current_policy_version": data.safety_stats.current_policy_version,
        },
    }

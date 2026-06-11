"""Rich panel renderables — one function per dashboard panel."""
from __future__ import annotations
from datetime import datetime, timezone

from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from armature.report.aggregator import DashboardData, ImprovementCycle, StageStats
from armature.report.sparkline import sparkline


# ── colours ───────────────────────────────────────────────────────────────────

_HEALTH_COLORS = {"green": "bright_green", "yellow": "yellow", "red": "red1", "dim": "dim"}


def _ihr_bar(ihr: float, width: int = 20) -> Text:
    filled = max(0, min(width, round(ihr * width)))
    color = _HEALTH_COLORS.get("green" if ihr >= 0.85 else ("yellow" if ihr >= 0.70 else "red"), "red1")
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim")
    return bar


def _delta_text(delta: float | None) -> str:
    if delta is None:
        return ""
    arrow = "▲" if delta >= 0 else "▼"
    return f"{arrow}{abs(delta):.2f}"


# ── health_strip ──────────────────────────────────────────────────────────────

def health_strip(data: DashboardData) -> Panel:
    """Full-width workflow health summary strip."""
    ihr = data.current_ihr or 0.0
    delta = data.ihr_delta
    color = data.health_color
    rich_color = _HEALTH_COLORS.get(color, "white")

    line1 = Text()
    line1.append("IHR", style="bold")
    line1.append("  Instruction-Harness Rate — composite quality score (0–1)  ", style="dim")
    line1.append(_ihr_bar(ihr))
    line1.append(f"  {ihr:.2f}", style=f"bold {rich_color}")
    if delta is not None:
        line1.append(f"  {_delta_text(delta)}", style="bold green" if delta >= 0 else "bold red")

    spark = sparkline(data.ihr_trend) if data.ihr_trend else "—"
    line2 = Text(spark + "  trend across runs", style="dim")

    content = Text()
    content.append_text(line1)
    content.append("\n")
    content.append_text(line2)

    run_info = f"{data.total_runs} runs"
    if data.last_run_at:
        try:
            dt = datetime.fromisoformat(data.last_run_at).astimezone()
            ts = dt.strftime("%Y-%m-%d %H:%M")
            tz = dt.strftime("%Z")
        except Exception:
            ts = data.last_run_at[:16].replace("T", " ")
            tz = "UTC"
        run_info += f"  ·  last run {ts} {tz}"
    title = f"[bold]{data.workflow_name}[/bold]  [dim]{run_info}[/dim]"
    return Panel(content, title=title, border_style=rich_color, padding=(0, 1))


# ── stage_breakdown ───────────────────────────────────────────────────────────

def _stage_row_style(s: StageStats) -> str:
    if s.is_post_run:
        return "dim"
    if s.failure_rate >= 0.20 or (s.avg_quorum is not None and s.avg_quorum < 0.50):
        return "red1"
    if s.failure_rate >= 0.10 or (s.avg_quorum is not None and s.avg_quorum < 0.70) or s.escalation_rate >= 0.20:
        return "yellow"
    return ""


_ROLE_STYLE = {
    "orchestrator": "cyan",
    "judge": "magenta",
    "researcher": "blue",
    "worker": "",
    "post_run": "dim",
    "tool": "dim",
}

_MAX_STAGE_ROWS = 12


def stage_breakdown(data: DashboardData) -> Panel:
    """Per-stage health table."""
    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold dim")
    t.add_column("Stage", style="bold", no_wrap=True, max_width=14)
    t.add_column("Role", no_wrap=True, max_width=12)
    t.add_column("×", justify="right", width=4, no_wrap=True)
    t.add_column("St", justify="center", width=2, no_wrap=True)
    t.add_column("Fail%", justify="right")
    t.add_column("Latency", justify="right")
    t.add_column("Esc%", justify="right")

    if not data.stage_stats:
        t.add_row("—", "—", "—", "no data", "—", "—", "—")
    else:
        for s in list(data.stage_stats.values())[:_MAX_STAGE_ROWS]:
            row_style = _stage_row_style(s)
            if s.is_post_run:
                status = Text("○", style="dim")
            elif s.failure_rate >= 0.20:
                status = Text("✗", style="red1 bold")
            elif s.failure_rate >= 0.10:
                status = Text("⚠", style="yellow")
            else:
                status = Text("✓", style="bright_green")

            role_color = _ROLE_STYLE.get(s.role_type, "")
            role_text = Text(s.role_type, style=role_color) if role_color else Text(s.role_type)
            fan_out = "—" if s.fan_out_per_run <= 1 else str(s.fan_out_per_run)

            fail_pct = f"{s.failure_rate * 100:.0f}%"
            latency = f"{s.avg_latency_ms / 1000:.1f}s" if s.avg_latency_ms < 60_000 else f"{s.avg_latency_ms / 60_000:.1f}m"
            esc = f"{s.escalation_rate * 100:.0f}%"

            t.add_row(s.stage_id, role_text, fan_out, status, fail_pct, latency, esc, style=row_style)

    return Panel(t, title="[bold]Stage Breakdown[/bold]", border_style="dim", padding=(0, 0))


# ── improvement_timeline ──────────────────────────────────────────────────────

def improvement_timeline(data: DashboardData) -> Panel:
    """Improvement cycle history table, newest first."""
    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold dim")
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Date", no_wrap=True, width=10)
    t.add_column("IHR", justify="right", width=5)
    t.add_column("Drift", justify="right", width=5)
    t.add_column("Applied", justify="center", width=10)
    t.add_column("✓Fix", justify="right", width=4)
    t.add_column("✗Miss", justify="right", width=5)
    t.add_column("↯Reg", justify="right", width=4)

    run_subtitle = f"{data.total_runs} run{'s' if data.total_runs != 1 else ''}"
    if data.last_run_at:
        try:
            dt = datetime.fromisoformat(data.last_run_at).astimezone()
            run_subtitle += f"  ·  {dt.strftime('%Y-%m-%d %H:%M')}"
        except Exception:
            run_subtitle += f"  ·  {data.last_run_at[:10]}"

    if not data.improvement_cycles:
        t.add_row("—", "—", "—", "—", "no cycles", "—", "—", "—")
    else:
        for c in data.improvement_cycles:
            date = c.timestamp[:10] if c.timestamp else "—"

            if c.requires_review:
                applied_text = Text("⏳ review", style="yellow bold")
                row_style = "yellow"
            elif not c.applied and not c.requires_review:
                applied_text = Text("✗ skip", style="dim")
                row_style = "dim"
            else:
                applied_text = Text("✓ auto", style="bright_green")
                row_style = ""

            drift_text = Text(f"{c.drift_score:.2f}", style="red1 bold" if c.drift_score > 0.5 else "")
            reg_text = Text(str(c.unexpected_regressions), style="red1 bold" if c.unexpected_regressions > 0 else "")

            t.add_row(
                str(c.cycle_number),
                date,
                f"{c.ihr_before:.2f}",
                drift_text,
                applied_text,
                str(c.verified_fixes),
                str(c.missed_predictions),
                reg_text,
                style=row_style,
            )

    title = f"[bold]Improvement Cycles[/bold]  [dim]{run_subtitle}[/dim]"
    return Panel(t, title=title, border_style="dim", padding=(0, 0))


# ── safety_governance ─────────────────────────────────────────────────────────

def safety_governance(data: DashboardData) -> Panel:
    """Safety rule hits, governance state."""
    s = data.safety_stats
    lines = Text()

    pv = s.current_policy_version or "—"
    lines.append("Policy version  ", style="bold dim")
    lines.append(f"{pv}\n")

    lines.append("Approvals       ", style="bold dim")
    ap_style = "bright_green" if s.approval_hits > 0 else "dim"
    lines.append(f"{s.approval_hits} gate{'s' if s.approval_hits != 1 else ''} completed\n", style=ap_style)

    lines.append("Rule hits       ", style="bold dim")
    lines.append("— warn  │  — block", style="dim")
    lines.append("  (not yet tracked)\n", style="dim")

    lines.append("Postcond. fails ", style="bold dim")
    pc_style = "red1 bold" if s.postcondition_failures > 0 else "bright_green"
    lines.append(f"{s.postcondition_failures}\n", style=pc_style)

    return Panel(lines, title="[bold]Safety & Governance[/bold]", border_style="dim", padding=(0, 1))

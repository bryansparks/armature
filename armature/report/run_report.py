"""Rich-rendered single-run report for `armature report`."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from armature.reporting import ReportData
from armature.state.traces import TraceRecord

_SLOW_MS = 30_000


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_latency(ms: float) -> str:
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60_000:.1f}m"


def _fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M ") + dt.strftime("%Z")
    except Exception:
        return iso[:16].replace("T", " ") + " UTC"


def _truncate(text: str, limit: int = 120) -> str:
    text = str(text).strip().replace("\n", " ")
    return text[:limit] + "…" if len(text) > limit else text


def _summarize_val(val: Any, limit: int = 100) -> str:
    if val is None:
        return "—"
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return _truncate(val, limit)
    if isinstance(val, list):
        if not val:
            return "(empty)"
        if all(isinstance(i, str) for i in val):
            return _truncate(", ".join(val), limit)
        return f"({len(val)} items)"
    if isinstance(val, dict):
        parts = [f"{k}: {_truncate(str(v), 40)}" for k, v in list(val.items())[:3]]
        suffix = "…" if len(val) > 3 else ""
        return "{" + ", ".join(parts) + suffix + "}"
    return _truncate(str(val), limit)


# ── panels ────────────────────────────────────────────────────────────────────

def _header_panel(data: ReportData) -> Panel:
    traces = data.traces
    n = len(traces)
    total_tokens = sum(t.input_tokens + t.output_tokens for t in traces)
    total_ms = sum(t.latency_ms for t in traces)
    ts = _fmt_ts(traces[0].timestamp) if traces else "—"
    run_short = data.run_id[:8]

    ihr = data.ihr
    if ihr:
        ihr_val = ihr.ihr
        health_color = "bright_green" if ihr_val >= 0.85 else ("yellow" if ihr_val >= 0.70 else "red1")
    else:
        ihr_val = None
        health_color = "dim"

    failures = [t for t in traces if not t.success or not t.output_valid]
    status_text = Text()
    if failures:
        status_text.append(f"⚠  {len(failures)} failure{'s' if len(failures) != 1 else ''}", style="bold red1")
    else:
        status_text.append("✓  All stages passed", style="bold bright_green")
    if ihr_val is not None:
        status_text.append(f"   IHR {ihr_val:.2f}", style=f"bold {health_color}")

    meta = Text()
    meta.append(f"{n} stage{'s' if n != 1 else ''}", style="dim")
    meta.append("  ·  ", style="dim")
    meta.append(_fmt_latency(total_ms), style="dim")
    meta.append("  ·  ", style="dim")
    meta.append(f"{total_tokens:,} tokens", style="dim")
    meta.append("  ·  ", style="dim")
    meta.append(ts, style="dim")

    content = Text()
    content.append_text(status_text)
    content.append("\n")
    content.append_text(meta)

    title = f"[bold]{data.workflow_name}[/bold]  [dim]run {run_short}[/dim]"
    return Panel(content, title=title, border_style=health_color, padding=(0, 1))


def _timeline_panel(traces: list[TraceRecord]) -> Panel:
    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold dim", expand=True)
    t.add_column("Stage", style="bold", no_wrap=True)
    t.add_column("Role", style="dim")
    t.add_column("Latency", justify="right")
    t.add_column("Tokens", justify="right")
    t.add_column("", justify="center", width=2)

    for tr in traces:
        ok = Text("✓", style="bright_green") if (tr.success and tr.output_valid) else Text("✗", style="red1")
        lat_text = Text(_fmt_latency(tr.latency_ms))
        if tr.latency_ms > _SLOW_MS:
            lat_text.stylize("yellow")
        tokens = tr.input_tokens + tr.output_tokens
        t.add_row(tr.stage_id, tr.role_type, lat_text, f"{tokens:,}", ok)

    return Panel(t, title="[bold]Stage Timeline[/bold]", border_style="dim", padding=(0, 0))


def _quality_panel(decision_traces: list[TraceRecord]) -> Panel | None:
    if not decision_traces:
        return None

    content = Text()
    for i, tr in enumerate(decision_traces):
        if i > 0:
            content.append("\n\n")
        out = tr.outputs

        # Stage header
        content.append(f"[{tr.stage_id}]  ", style="bold")
        content.append(tr.role_type, style="dim")

        # Classic accept/reject decision
        if "accept" in out:
            verdict = "✓ ACCEPTED" if out["accept"] else "✗ REJECTED"
            style = "bold bright_green" if out["accept"] else "bold red1"
            content.append(f"  {verdict}", style=style)
        elif "overall_ready_to_launch" in out:
            ready = out["overall_ready_to_launch"]
            verdict = "✓ Ready to launch" if ready else "✗ Not ready to launch"
            style = "bold bright_green" if ready else "bold red1"
            content.append(f"  {verdict}", style=style)

        # IHR / confidence / score
        for key in ("confidence", "score"):
            if key in out and out[key] is not None:
                content.append(f"  {key}={float(out[key]):.2f}", style="dim")

        # Human-readable text fields
        for key in ("notes", "feedback", "reasoning", "rationale", "summary"):
            if key in out and out[key]:
                content.append(f"\n  {key}: ", style="bold dim")
                content.append(_truncate(str(out[key]), 200))

        # Critical blocking issues as a bullet list
        if "critical_blocking_issues" in out:
            issues = out["critical_blocking_issues"]
            if isinstance(issues, list) and issues:
                content.append("\n  Critical issues:", style="bold red1")
                for item in issues[:5]:
                    content.append(f"\n    • {_truncate(str(item), 120)}", style="red1")
                if len(issues) > 5:
                    content.append(f"\n    … +{len(issues) - 5} more", style="dim")

        # Per-platform scores as a compact table if present
        if "scores" in out and isinstance(out["scores"], dict):
            scores = out["scores"]
            content.append("\n  Scores:", style="bold dim")
            for platform, score_data in list(scores.items())[:6]:
                if isinstance(score_data, dict):
                    nums = {k: v for k, v in score_data.items() if isinstance(v, (int, float))}
                    avg = sum(nums.values()) / len(nums) if nums else None
                    avg_str = f"  avg={avg:.1f}" if avg is not None else ""
                    content.append(f"\n    {platform}:{avg_str}", style="dim")
                else:
                    content.append(f"\n    {platform}: {_summarize_val(score_data)}", style="dim")

        # Improvement suggestions — one per platform, truncated
        if "improvement_suggestions" in out:
            sugg = out["improvement_suggestions"]
            if isinstance(sugg, dict) and sugg:
                content.append("\n  Suggestions:", style="bold dim")
                for platform, items in list(sugg.items())[:4]:
                    first = items[0] if isinstance(items, list) and items else str(items)
                    content.append(f"\n    {platform}: ", style="dim")
                    content.append(_truncate(str(first), 100))
            elif isinstance(sugg, list) and sugg:
                content.append("\n  Suggestions:", style="bold dim")
                for item in sugg[:3]:
                    content.append(f"\n    • {_truncate(str(item), 120)}")

    return Panel(content, title="[bold]Quality Gate[/bold]", border_style="dim", padding=(0, 1))


def _issues_panel(failures: list[TraceRecord]) -> Panel | None:
    if not failures:
        return None
    content = Text()
    for i, t in enumerate(failures):
        if i > 0:
            content.append("\n")
        label = "output invalid" if t.success else "failed"
        content.append(f"[{t.stage_id}]  ", style="bold")
        content.append(label, style="red1")
        err = t.outputs.get("stderr") or t.outputs.get("error") or t.error_type or ""
        if err:
            content.append(f"  {_truncate(str(err), 120)}", style="dim")
    return Panel(content, title="[bold red1]Issues[/bold red1]", border_style="red1", padding=(0, 1))


# ── render helpers ────────────────────────────────────────────────────────────

def _render_to_console(data: ReportData, c: Console) -> None:
    c.print()
    c.print(_header_panel(data))

    failures = [t for t in data.traces if not t.success or not t.output_valid]
    if failures:
        c.print(_issues_panel(failures))

    c.print(_timeline_panel(data.traces))

    decision_traces = [t for t in data.traces if t.role_type in ("judge", "orchestrator")]
    quality = _quality_panel(decision_traces)
    if quality:
        c.print(quality)

    c.print()


# ── terminal renderer ─────────────────────────────────────────────────────────

def render_run_report(data: ReportData, console: Console | None = None) -> None:
    _render_to_console(data, console or Console())


# ── html renderer ─────────────────────────────────────────────────────────────

def render_run_report_html(data: ReportData) -> str:
    import io
    from rich.terminal_theme import MONOKAI
    c = Console(record=True, width=100, force_terminal=True, file=io.StringIO())
    _render_to_console(data, c)
    return c.export_html(theme=MONOKAI)


# ── markdown renderer ─────────────────────────────────────────────────────────

def render_run_report_markdown(data: ReportData) -> str:
    traces = data.traces
    n = len(traces)
    total_tokens = sum(t.input_tokens + t.output_tokens for t in traces)
    total_ms = sum(t.latency_ms for t in traces)
    ts = _fmt_ts(traces[0].timestamp) if traces else "—"
    run_short = data.run_id[:8]

    ihr = data.ihr
    ihr_str = f"IHR {ihr.ihr:.2f}" if ihr else ""

    failures = [t for t in traces if not t.success or not t.output_valid]
    status = "✓ All stages passed" if not failures else f"⚠ {len(failures)} failure(s)"

    lines: list[str] = [
        f"# {data.workflow_name}",
        "",
        f"**Run:** `{run_short}`  **Date:** {ts}  ",
        f"**Stages:** {n}  **Duration:** {_fmt_latency(total_ms)}  "
        f"**Tokens:** {total_tokens:,}  ",
        f"**Status:** {status}  {ihr_str}",
        "",
        "---",
        "",
        "## Stage Timeline",
        "",
        "| Stage | Role | Latency | Tokens | OK |",
        "|---|---|---:|---:|:---:|",
    ]

    for tr in traces:
        ok = "✓" if (tr.success and tr.output_valid) else "✗"
        slow = " ⚠" if tr.latency_ms > _SLOW_MS else ""
        tokens = tr.input_tokens + tr.output_tokens
        lines.append(
            f"| {tr.stage_id} | {tr.role_type} | {_fmt_latency(tr.latency_ms)}{slow} | {tokens:,} | {ok} |"
        )

    if failures:
        lines += ["", "---", "", "## Issues", ""]
        for t in failures:
            label = "output invalid" if t.success else "failed"
            err = t.outputs.get("stderr") or t.outputs.get("error") or t.error_type or ""
            err_str = f" — {_truncate(str(err), 120)}" if err else ""
            lines.append(f"- **{t.stage_id}** {label}{err_str}")

    decision_traces = [t for t in traces if t.role_type in ("judge", "orchestrator")]
    if decision_traces:
        lines += ["", "---", "", "## Quality Gate", ""]
        for tr in decision_traces:
            out = tr.outputs
            lines.append(f"### {tr.stage_id} ({tr.role_type})")

            if "accept" in out:
                verdict = "✓ Accepted" if out["accept"] else "✗ Rejected"
                lines.append(f"\n**Decision:** {verdict}")
            elif "overall_ready_to_launch" in out:
                ready = out["overall_ready_to_launch"]
                verdict = "✓ Ready to launch" if ready else "✗ Not ready to launch"
                lines.append(f"\n**Decision:** {verdict}")

            for key in ("confidence", "score"):
                if key in out and out[key] is not None:
                    lines.append(f"**{key.capitalize()}:** {float(out[key]):.2f}")

            for key in ("notes", "feedback", "reasoning", "rationale", "summary"):
                if key in out and out[key]:
                    lines.append(f"\n**{key.capitalize()}:** {_truncate(str(out[key]), 400)}")

            if "critical_blocking_issues" in out:
                issues = out["critical_blocking_issues"]
                if isinstance(issues, list) and issues:
                    lines.append("\n**Critical issues:**")
                    for item in issues[:10]:
                        lines.append(f"- {_truncate(str(item), 200)}")

            if "scores" in out and isinstance(out["scores"], dict):
                lines.append("\n**Scores:**")
                lines.append("")
                lines.append("| Platform | Avg score |")
                lines.append("|---|---:|")
                for platform, score_data in out["scores"].items():
                    if isinstance(score_data, dict):
                        nums = {k: v for k, v in score_data.items() if isinstance(v, (int, float))}
                        avg = f"{sum(nums.values()) / len(nums):.1f}" if nums else "—"
                    else:
                        avg = str(score_data)
                    lines.append(f"| {platform} | {avg} |")

            if "improvement_suggestions" in out:
                sugg = out["improvement_suggestions"]
                if isinstance(sugg, dict) and sugg:
                    lines.append("\n**Improvement suggestions:**")
                    for platform, items in sugg.items():
                        first = items[0] if isinstance(items, list) and items else str(items)
                        lines.append(f"- **{platform}:** {_truncate(str(first), 200)}")
                elif isinstance(sugg, list) and sugg:
                    lines.append("\n**Improvement suggestions:**")
                    for item in sugg[:5]:
                        lines.append(f"- {_truncate(str(item), 200)}")

            lines.append("")

    lines += ["---", "", f"*Generated by armature · run `{run_short}`*", ""]
    return "\n".join(lines)

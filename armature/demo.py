"""armature demo — live quality witness for new developers."""
from __future__ import annotations

import subprocess
import sys
import time
import re
from dataclasses import dataclass, field
from pathlib import Path

from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

console = Console()

# ── subsystem metadata ─────────────────────────────────────────────────────────

@dataclass
class Subsystem:
    name: str
    label: str
    what_it_does: str
    color: str
    test_dirs: list[str]
    passed: int = 0
    failed: int = 0
    task_id: TaskID | None = None


SUBSYSTEMS: list[Subsystem] = [
    Subsystem("spec",        "Spec & Validation",    "YAML loading, schema validation, DAG integrity",          "cyan",    ["tests/spec"]),
    Subsystem("runtime",     "Runtime Engine",       "DAG execution, context, retries, fan-in, loops",          "green",   ["tests/runtime"]),
    Subsystem("nodes",       "Node Types",           "LLM, script, tool-call, gate, subagent nodes",            "blue",    ["tests/nodes"]),
    Subsystem("state",       "State & Memory",       "Traces, memory, diagnostics, artifacts, evaluation",      "magenta", ["tests/state"]),
    Subsystem("synthesis",   "Self-Improvement",     "IHR-driven SpecRefiner, optimizer, AutoHarness",          "yellow",  ["tests/synthesis", "tests/optimizer"]),
    Subsystem("registry",    "Tool Registry",        "Built-in tools, permissions, reversibility",              "cyan",    ["tests/registry", "tests/permissions"]),
    Subsystem("hooks",       "Lifecycle Hooks",      "Safety rules, pre/post-stage and pre/post-tool hooks",    "red",     ["tests/hooks"]),
    Subsystem("report",      "Reporting Dashboard",  "Sparklines, panels, aggregator, Rich dashboard",          "blue",    ["tests/report"]),
    Subsystem("integration", "Integration & CLI",    "End-to-end harness runs, CLI commands, wizard",           "green",   ["tests/integration", "tests/test_cli.py", "tests/test_wizard.py"]),
    Subsystem("service",     "Async Service",        "FastAPI endpoints, channels, MCP, telemetry",             "magenta", ["tests/service", "tests/channels", "tests/mcp", "tests/telemetry"]),
]

SAMPLE_SPEC = """\
# A minimal Armature workflow spec
harness:
  name: content-pipeline
  description: Research, draft, and review an article

stages:
  - id: researcher
    role: researcher
    model_tier: frontier
    prompt: "Research the topic: {topic}. Return key findings."
    output_schema:
      findings: {type: string}

  - id: writer
    role: worker
    model_tier: standard
    depends_on: [researcher]
    prompt: "Write a 500-word article using: {findings}"
    output_schema:
      draft: {type: string}

  - id: editor
    role: judge
    model_tier: frontier
    depends_on: [writer]
    prompt: "Score this draft 1-10 and suggest improvements: {draft}"
    output_schema:
      score:    {type: number}
      feedback: {type: string}

  - id: refiner
    role: worker
    model_tier: standard
    depends_on: [editor]
    skip_if: "score >= 8"
    prompt: "Revise the draft based on: {feedback}"

self_improve:
  enabled: true
  target_ihr: 0.85
"""

# ── helpers ────────────────────────────────────────────────────────────────────

def _banner() -> Panel:
    title = Text()
    title.append("ARMATURE", style="bold green")
    title.append("  —  ", style="dim")
    title.append("a maturity of AI agents", style="italic green")
    title.append("\n")
    title.append("YAML-first  ·  self-improving  ·  open source  ·  MIT licensed", style="dim")
    return Panel(Align.center(title), border_style="green", padding=(1, 4))


def _intro() -> Panel:
    body = Text(justify="left")
    body.append("What this does\n", style="bold")
    body.append(
        "Runs Armature's full test suite — live, subsystem by subsystem — so you can "
        "see exactly what is tested, how many tests guard each component, and that "
        "every one of them passes before you write a single line of code against it.\n\n",
        style="dim",
    )
    body.append("Why subsystems\n", style="bold")
    body.append(
        "Armature is not a single module — it is a layered harness with ten distinct "
        "components: spec loading, DAG execution, node types, state/memory, "
        "self-improvement, tool registry, safety hooks, reporting, CLI integration, and "
        "async services. Each subsystem runs its own isolated suite so you can pinpoint "
        "exactly where coverage lives.\n\n",
        style="dim",
    )
    body.append("What you will see\n", style="bold")
    body.append(
        "Live progress bars as each suite runs  →  a results table with per-subsystem "
        "pass counts  →  a final verdict panel  →  a sample YAML spec  →  "
        "the four commands that cover 90 % of day-to-day use.",
        style="dim",
    )
    return Panel(body, border_style="dim", padding=(0, 2))


def _capability_table() -> Table:
    t = Table(show_header=True, header_style="bold", border_style="dim", expand=True, show_lines=False)
    t.add_column("Subsystem", style="bold", min_width=22)
    t.add_column("What it does", style="dim")
    t.add_column("Tests", justify="right", min_width=6)
    t.add_column("Status", justify="center", min_width=8)
    for s in SUBSYSTEMS:
        total = s.passed + s.failed
        status = "[green]PASS[/green]" if s.failed == 0 and total > 0 else ("[red]FAIL[/red]" if s.failed > 0 else "[dim]—[/dim]")
        t.add_row(
            f"[{s.color}]{s.label}[/{s.color}]",
            s.what_it_does,
            str(total) if total else "—",
            status,
        )
    return t


def _run_subsystem_tests(s: Subsystem, root: Path) -> tuple[int, int]:
    """Run pytest for one subsystem, return (passed, failed)."""
    paths = []
    for td in s.test_dirs:
        p = root / td
        if p.exists():
            paths.append(str(p))
    if not paths:
        return 0, 0

    result = subprocess.run(
        [sys.executable, "-m", "pytest", *paths, "-q", "--tb=no", "--no-header", "-p", "no:warnings"],
        capture_output=True, text=True, cwd=str(root),
    )
    passed = failed = 0
    for line in result.stdout.splitlines():
        m = re.search(r"(\d+) passed", line)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", line)
        if m:
            failed = int(m.group(1))
    return passed, failed


# ── main flow ──────────────────────────────────────────────────────────────────

def run_demo() -> None:
    root = Path(__file__).parent.parent

    console.print()
    console.print(_banner())
    console.print()
    console.print(_intro())
    console.print()
    console.print(Rule("[bold green]Quality Witness[/bold green]  ·  running the live test suite", style="green"))
    console.print()

    progress = Progress(
        SpinnerColumn(style="green"),
        TextColumn("[bold]{task.description:<28}"),
        BarColumn(bar_width=30, style="green", complete_style="bright_green"),
        TextColumn("[cyan]{task.completed}/{task.total}[/cyan]"),
        TextColumn("[dim]{task.fields[status]}[/dim]"),
        TimeElapsedColumn(),
        console=console,
        expand=False,
    )

    overall = progress.add_task("[bold]Overall", total=len(SUBSYSTEMS), status="starting…")

    for s in SUBSYSTEMS:
        s.task_id = progress.add_task(s.label, total=1, status="queued")

    with Live(progress, console=console, refresh_per_second=12):
        completed = 0
        total_pass = total_fail = 0

        for s in SUBSYSTEMS:
            progress.update(s.task_id, status="[yellow]running…[/yellow]")
            t0 = time.monotonic()
            passed, failed = _run_subsystem_tests(s, root)
            elapsed = time.monotonic() - t0
            s.passed, s.failed = passed, failed
            total_pass += passed
            total_fail += failed
            completed += 1

            if failed == 0 and passed > 0:
                status = f"[green]✓ {passed} passed[/green]  [dim]{elapsed:.1f}s[/dim]"
            elif failed > 0:
                status = f"[red]✗ {failed} failed[/red]  [dim]{elapsed:.1f}s[/dim]"
            else:
                status = "[dim]no tests found[/dim]"

            progress.update(s.task_id, completed=1, status=status)
            progress.update(overall, completed=completed,
                            status=f"[green]{total_pass} passed[/green]" +
                                   (f"  [red]{total_fail} failed[/red]" if total_fail else ""))

    console.print()
    console.print(Rule("[bold]Results by Subsystem[/bold]", style="dim"))
    console.print()
    console.print(_capability_table())
    console.print()

    # ── final verdict ──────────────────────────────────────────────────────────
    if total_fail == 0:
        verdict = Text()
        verdict.append(f"  {total_pass} tests  ·  ", style="bold")
        verdict.append("0 failures", style="bold green")
        verdict.append("  ·  production-grade  ✓", style="bold")
        console.print(Panel(Align.center(verdict), border_style="green", padding=(0, 4)))
    else:
        verdict = Text()
        verdict.append(f"  {total_pass} passed  ·  ", style="bold")
        verdict.append(f"{total_fail} failures", style="bold red")
        console.print(Panel(Align.center(verdict), border_style="red", padding=(0, 4)))

    console.print()
    console.print(Rule("[bold]What a workflow spec looks like[/bold]", style="dim"))
    console.print()
    console.print(
        Panel(
            Syntax(SAMPLE_SPEC, "yaml", theme="monokai", line_numbers=False),
            title="[dim]examples/content-pipeline.yaml[/dim]",
            border_style="dim",
            padding=(0, 1),
        )
    )

    console.print()
    console.print(Rule(style="dim"))
    console.print()
    cols = Columns([
        Panel("[bold cyan]armature new[/bold cyan]\n[dim]Guided spec wizard[/dim]",       border_style="dim", padding=(0, 3)),
        Panel("[bold cyan]armature run spec.yaml[/bold cyan]\n[dim]Execute a workflow[/dim]", border_style="dim", padding=(0, 3)),
        Panel("[bold cyan]armature improve spec.yaml[/bold cyan]\n[dim]Self-improve loop[/dim]", border_style="dim", padding=(0, 3)),
        Panel("[bold cyan]armature dashboard[/bold cyan]\n[dim]Live run dashboard[/dim]",  border_style="dim", padding=(0, 3)),
    ], equal=True, expand=True)
    console.print(cols)
    console.print()

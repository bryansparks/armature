import asyncio
import json
from pathlib import Path
import typer
from armature.runtime.engine import Harness

app = typer.Typer(name="armature", help="ELF ecosystem agent harness runner", no_args_is_help=True)

channels_app = typer.Typer(name="channels", help="Manage messaging channel connectors")
app.add_typer(channels_app, name="channels")


@app.command()
def new(
    output: Path = typer.Argument(None, help="Output YAML file path (prompted if omitted)"),
):
    """Interactively create a new workflow spec (YAML)."""
    from armature.cli_wizard import run_wizard
    run_wizard(output_path=output)


def _print_run_header(spec, quiet: bool) -> None:
    if quiet:
        return

    normal = [s for s in spec.stages if not s.post_run]
    post_run = [s for s in spec.stages if s.post_run]
    fan_outs = [s for s in normal if s.fan_out]

    stage_label = f"{len(normal)} stage{'s' if len(normal) != 1 else ''}"
    if fan_outs:
        stage_label += f", {len(fan_outs)} fan-out"
    if post_run:
        stage_label += f", {len(post_run)} post-run"

    safety = spec.safety_mode if spec.safety_mode != "permissive" else ""
    safety_str = f"  ·  safety: {safety}" if safety else ""
    typer.echo(f"\n{spec.name}  v{spec.version}  ·  {stage_label}{safety_str}")

    if spec.description:
        desc = spec.description.strip().replace("\n", " ")
        if len(desc) > 100:
            desc = desc[:97] + "..."
        typer.echo(f"  {desc}")

    # Model tiers
    tier_names = ["tiny", "small", "medium", "large", "frontier"]
    tiers = [(n, getattr(spec.model_tiers, n)) for n in tier_names if getattr(spec.model_tiers, n)]
    if spec.model_tiers.__pydantic_extra__:
        tiers += [(k, v) for k, v in spec.model_tiers.__pydantic_extra__.items() if v]
    if tiers:
        parts = []
        provider = None
        for name, cfg in tiers:
            short = cfg.model.split("/")[-1] if "/" in cfg.model else cfg.model
            parts.append(f"{name}={short}")
            if provider is None:
                provider = cfg.provider
        provider_str = f"  ({provider})" if provider else ""
        typer.echo(f"  Tiers: {'  ·  '.join(parts)}{provider_str}")

    # Extras line
    extras = []
    if spec.tools:
        extras.append("tools: " + ", ".join(t.module for t in spec.tools))
    if spec.mcp_servers:
        extras.append(f"mcp: {len(spec.mcp_servers)} server{'s' if len(spec.mcp_servers) != 1 else ''}")
    if spec.continuation:
        n = len(spec.continuation.carry_forward)
        extras.append(f"continuation: {n} key{'s' if n != 1 else ''}")
    if spec.triggers:
        types = ", ".join(t.type for t in spec.triggers)
        extras.append(f"triggers: {types}")
    if spec.checkpoint:
        extras.append("checkpoint: on")
    if extras:
        typer.echo("  " + "  ·  ".join(extras))

    # Agent roster
    all_stages = normal + post_run
    if all_stages:
        col = max(len(s.id) for s in all_stages) + 2
        typer.echo("")
        typer.echo("  Agents:")
        for stage in all_stages:
            if stage.role:
                kind = f"{stage.role.name} ({stage.role.type.value})"
            elif stage.tool_call:
                kind = "tool_call"
            elif stage.gate:
                kind = "human gate"
            elif stage.subagent_spec:
                kind = "subagent"
            elif stage.adapter:
                kind = "adapter"
            else:
                kind = "?"

            badges = []
            if stage.fan_out:
                badges.append(f"[fan-out ×{stage.fan_out}]")
            if stage.skip_if:
                badges.append("[conditional]")
            if stage.post_run:
                badges.append("[post-run]")
            badge_str = "  " + "  ".join(badges) if badges else ""

            typer.echo(f"    {stage.id:<{col}}{kind}{badge_str}")

    typer.echo("  " + "─" * 72)


def _version_callback(value: bool) -> None:
    if value:
        from armature import __version__
        typer.echo(f"armature {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Armature — multi-agent workflow engine."""


def parse_inputs(raw: list[str]) -> dict:
    result = {}
    for item in raw:
        if "=" not in item:
            typer.echo(f"Invalid input format '{item}' — use key=value", err=True)
            raise typer.Exit(1)
        k, _, v = item.partition("=")
        result[k.strip()] = v.strip()
    return result


def _make_on_event(quiet: bool):
    """Return an on_event callback that prints live progress."""
    if quiet:
        return None

    def on_event(event_type: str, data: dict) -> None:
        if event_type == "stage_start":
            kind = data.get("kind", "?")
            role = f" [{data['role']}]" if data.get("role") else ""
            typer.echo(f"  → {data['stage']} ({kind}){role}")
        elif event_type == "stage_complete":
            typer.echo(f"  ✓ {data['stage']} ({data['elapsed_s']}s)")
        elif event_type == "stage_skipped":
            reason = data.get("reason", "")
            typer.echo(f"  - {data['stage']} [skipped: {reason}]")
        elif event_type == "stage_resumed":
            typer.echo(f"  ↩ {data['stage']} [resumed from checkpoint]")
        elif event_type == "stage_failed":
            typer.echo(f"  ✗ {data['stage']} [{data['type']}]: {data['reason'][:80]}", err=True)
        elif event_type == "retry_attempt":
            typer.echo(f"  ⟳ {data['stage']} retry {data['attempt']}/{data['max']}: {data['reason'][:60]}")
        elif event_type == "run_summary":
            rogue = data.get("rogue_signals", 0)
            rogue_str = f", {rogue} blocked" if rogue else ""
            typer.echo(
                f"\nDone in {data['elapsed_s']}s — "
                f"{data['stages_ran']} ran, "
                f"{data['stages_skipped']} skipped, "
                f"{data['stages_resumed']} resumed, "
                f"{data['stages_failed']} failed"
                f"{rogue_str}"
            )

    return on_event


@app.command()
def validate(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
):
    """Validate a workflow spec file and report all errors."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.spec.loader import load_spec
    from armature.spec.validator import validate_spec, SpecValidationError

    try:
        loaded = load_spec(spec)
    except Exception as exc:
        typer.echo(f"Failed to parse spec: {exc}", err=True)
        raise typer.Exit(1)

    all_issues = validate_spec(loaded, strict=False)
    hard_errors = [e for e in all_issues if e.severity == "error"]
    warnings = [e for e in all_issues if e.severity == "warning"]

    if warnings:
        typer.echo(f"⚠ {len(warnings)} warning(s):")
        for w in warnings:
            stage_label = f"  stage='{w.stage_id}'" if w.stage_id else ""
            typer.echo(f"  [{w.code}]{stage_label}: {w.message}")

    if not hard_errors:
        from armature.spec.risk import compute_spec_risk
        risk = compute_spec_risk(loaded)
        typer.echo(f"✓ '{loaded.name}' is valid ({len(loaded.stages)} stages)")
        typer.echo(f"  Risk: {risk.tier.upper()} [score={risk.score}]")
        for factor in risk.factors:
            sign = "+" if factor.delta >= 0 else ""
            typer.echo(f"    {sign}{factor.delta}  {factor.label}")
        return

    typer.echo(f"✗ '{loaded.name}' has {len(hard_errors)} validation error(s):\n", err=True)
    for e in hard_errors:
        stage_label = f"  stage='{e.stage_id}'" if e.stage_id else ""
        typer.echo(f"  [{e.code}]{stage_label}: {e.message}", err=True)
    raise typer.Exit(1)


@app.command()
def run(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    inputs: list[str] = typer.Option([], "--input", "-i", help="Input values as key=value"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate spec without executing"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress output"),
    output_file: Path = typer.Option(None, "--output", "-o", help="Write result JSON to file"),
    force: bool = typer.Option(False, "--force", help="Ignore checkpoint and rerun all stages"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable LLM response cache"),
    auto_improve: bool = typer.Option(False, "--auto-improve", help="Analyze traces and auto-apply spec improvements when IHR < 0.75"),
):
    """Run a workflow from a YAML spec file."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    parsed_inputs = parse_inputs(inputs)

    from armature.spec.validator import SpecValidationError
    try:
        harness = Harness.from_spec(spec, vars=parsed_inputs, use_cache=not no_cache)
    except SpecValidationError as exc:
        typer.echo(f"Spec validation failed:\n{exc}", err=True)
        raise typer.Exit(1)

    if dry_run:
        typer.echo(f"✓ Spec '{harness.name}' is valid ({len(harness._spec.stages)} stages)")
        typer.echo("Dry run — no execution.")
        return

    _print_run_header(harness._spec, quiet)
    harness._on_event = _make_on_event(quiet)

    async def _run():
        return await harness.run(parsed_inputs, force=force)

    result = asyncio.run(_run())

    result_json = json.dumps(result, indent=2, default=str)
    if output_file:
        output_file.write_text(result_json)
        if not quiet:
            typer.echo(f"Result written to {output_file}")
    else:
        typer.echo(result_json)

    if auto_improve:
        from armature.synthesis.improve import SelfImproveRunner

        if not quiet:
            typer.echo("\nAuto-improve: analyzing traces...")

        async def _improve():
            improve_runner = SelfImproveRunner(spec, target_ihr=0.75)
            return await improve_runner.analyze()

        report = asyncio.run(_improve())

        if not report.needs_improvement:
            typer.echo("Auto-improve: workflow is healthy — no improvement needed.")
        elif report.applied:
            typer.echo(f"Auto-improve: spec updated → {spec}")
        elif report.requires_review:
            typer.echo(f"Auto-improve: structural changes require review → {report.pending_path}")
        else:
            typer.echo("Auto-improve: refiner could not produce a valid revision.")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host"),
    port: int = typer.Option(8080, "--port", "-p", help="Bind port"),
    specs_dir: Path = typer.Option(None, "--specs-dir", help="Directory of workflow specs to register"),
):
    """Start the Armature HTTP service."""
    try:
        import uvicorn
        from armature.service.app import build_app
        from armature.service.registry import WorkflowRegistry
    except ImportError:
        typer.echo("FastAPI/uvicorn not installed. Run: pip install 'armature[service]'", err=True)
        raise typer.Exit(1)

    registry = WorkflowRegistry()
    if specs_dir:
        if not specs_dir.is_dir():
            typer.echo(f"specs-dir not found: {specs_dir}", err=True)
            raise typer.Exit(1)
        registry.load_dir(specs_dir)
        typer.echo(f"Registered {len(registry.list_all())} workflow(s) from {specs_dir}")

    fastapi_app = build_app(registry=registry)
    typer.echo(f"Starting Armature service on {host}:{port}")
    uvicorn.run(fastapi_app, host=host, port=port)


@app.command()
def optimize(
    spec: Path = typer.Argument(..., help="Path to the workflow spec to optimize"),
    trace_db: Path = typer.Option(
        Path("~/.armature/traces.db").expanduser(),
        "--traces",
        help="Path to trace database",
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply the proposed diff if accepted"),
):
    """Run the Meta-Harness optimizer on a workflow spec."""
    if not spec.exists():
        typer.echo(f"Spec not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.optimizer.runner import OptimizerRunner

    async def _run():
        runner = OptimizerRunner(target_spec_path=spec, trace_db_path=trace_db)
        return await runner.optimize()

    typer.echo(f"Analyzing traces for: {spec.name}")
    result = asyncio.run(_run())

    if result is None:
        typer.echo("Not enough trace data to optimize. Run more workflows first.")
        return

    typer.echo(f"\nOptimizer result (accepted={result.accepted}, score={result.score:.2f}):")
    typer.echo(f"Rationale: {result.rationale}")
    typer.echo(f"\nProposed diff:\n{result.proposed_diff}")

    if result.accepted and apply:
        from armature.optimizer.runner import OptimizerRunner
        ok, msg = OptimizerRunner.apply_diff(spec, result.proposed_diff)
        typer.echo(f"\n{'Applied' if ok else 'Apply failed'}: {msg}")
    elif result.accepted and not apply:
        typer.echo("\nProposal accepted — re-run with --apply to patch the spec file.")


@app.command()
def report(
    run_id: str = typer.Option(None, "--run-id", help="Run ID to report on"),
    workflow: str = typer.Option(None, "--workflow", help="Workflow name — resolves to the most recent run for that workflow"),
    output_file: Path = typer.Option(None, "--output-file", help="Write report to file (.md or .html); suppresses terminal output"),
    traces: Path = typer.Option(None, "--traces", help="Path to traces.db (default: ~/.armature/traces.db)"),
    evals: Path = typer.Option(None, "--evals", help="Path to evaluations database"),
    knowledge: Path = typer.Option(None, "--knowledge", help="Path to knowledge database"),
    session_log: Path = typer.Option(None, "--session-log", help="Path to session.jsonl"),
):
    """Print a human-readable report for a completed workflow run.

    Identify the run by workflow name or exact run ID:

      armature report --workflow launchpad
      armature report --run-id abc123

    Save to a file (terminal output suppressed):

      armature report --workflow launchpad --output-file launchpad.md
      armature report --workflow launchpad --output-file launchpad.html
    """
    from armature.reporting import load_report_data
    from armature.report.run_report import render_run_report, render_run_report_markdown, render_run_report_html
    from armature.state.traces import TraceStore

    if not run_id and not workflow:
        typer.echo("Provide --workflow <name> or --run-id <id>.", err=True)
        raise typer.Exit(1)

    if output_file:
        ext = output_file.suffix.lower()
        if ext not in (".md", ".html", ".htm"):
            typer.echo(f"Unsupported extension '{ext}'. Use .md or .html", err=True)
            raise typer.Exit(1)

    resolved_traces = traces or Path("~/.armature/traces.db").expanduser()

    async def _resolve_run_id() -> str | None:
        if run_id:
            return run_id
        store = TraceStore(resolved_traces)
        await store.init()
        resolved = await store.latest_run_id(workflow)
        if resolved is None:
            typer.echo(
                f"No runs found for workflow '{workflow}' in {resolved_traces}.",
                err=True,
            )
        return resolved

    resolved_id = asyncio.run(_resolve_run_id())
    if resolved_id is None:
        raise typer.Exit(1)

    resolved_session = session_log or Path(f"~/.armature/runs/{resolved_id}/session.jsonl").expanduser()

    async def _load():
        return await load_report_data(
            run_id=resolved_id,
            traces_db=resolved_traces,
            evals_db=evals,
            knowledge_db=knowledge,
            session_log=resolved_session,
        )

    data = asyncio.run(_load())
    if data is None:
        typer.echo(
            f"No traces found for run_id='{resolved_id}'.\n"
            f"  Looked in: {resolved_traces}",
            err=True,
        )
        raise typer.Exit(1)

    if output_file:
        ext = output_file.suffix.lower()
        if ext == ".md":
            content = render_run_report_markdown(data)
        else:
            content = render_run_report_html(data)
        output_file.write_text(content, encoding="utf-8")
        typer.echo(f"Report written to {output_file}")
    else:
        render_run_report(data)


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="Run ID to display"),
    traces: Path = typer.Option(None, "--traces", help="Path to traces.db (default: ~/.armature/traces.db)"),
):
    """Display a recorded run stage-by-stage from the TraceStore."""
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console()
    resolved_traces = traces or Path("~/.armature/traces.db").expanduser()

    from armature.state.traces import TraceStore

    async def _load():
        store = TraceStore(resolved_traces)
        await store.init()
        records = await store.query_by_run(run_id)
        ihr_result = await store.compute_ihr(run_id) if records else None
        return records, ihr_result

    records, ihr_result = asyncio.run(_load())

    if not records:
        typer.echo(f"No traces found for run_id='{run_id}' in {resolved_traces}", err=True)
        raise typer.Exit(1)

    t = Table(show_header=True, header_style="bold", border_style="dim", expand=True)
    t.add_column("Stage", style="bold")
    t.add_column("Role")
    t.add_column("Model", style="dim")
    t.add_column("Latency", justify="right")
    t.add_column("Status", justify="center")
    t.add_column("Quorum", justify="right")
    t.add_column("Outputs (truncated)", style="dim")

    for r in records:
        status = "[green]✓[/green]" if r.success else "[red]✗[/red]"
        quorum = f"{r.quorum_score:.2f}" if r.quorum_score is not None else "—"
        out_str = json.dumps(r.outputs, default=str)
        if len(out_str) > 80:
            out_str = out_str[:77] + "..."
        t.add_row(r.stage_id, r.role_type, r.model, f"{r.latency_ms:.0f}ms", status, quorum, out_str)

    console.print(f"\n[bold]Replay[/bold]: run [cyan]{run_id}[/cyan]  ({len(records)} stages)\n")
    console.print(t)

    if ihr_result:
        console.print(
            f"\n[bold]IHR[/bold]: [cyan]{ihr_result.ihr:.3f}[/cyan]  "
            f"(valid={ihr_result.output_valid_rate:.0%}  "
            f"success={ihr_result.success_rate:.0%}  "
            f"n={ihr_result.n_traces})\n"
        )


@app.command(name="export-traces")
def export_traces(
    workflow: str = typer.Option(..., "--workflow", "-w", help="Workflow name to export traces for"),
    output: Path = typer.Option(..., "--output", "-o", help="Output JSONL file path"),
    traces_db: Path = typer.Option(
        None, "--traces", help="Path to traces.db (default: ~/.armature/traces.db)"
    ),
    format: str = typer.Option("chat", "--format", "-f", help="Format: chat | alpaca | sharegpt | dpo"),
    min_score: float = typer.Option(0.85, "--min-score", help="Minimum quorum score for chosen traces"),
    rejected_max_score: float = typer.Option(0.30, "--rejected-max-score", help="Max quorum score for DPO rejected traces"),
    role_types: str = typer.Option(None, "--role-types", help="Comma-separated role types to include (e.g. judge,researcher)"),
    system_prompt: str = typer.Option(None, "--system-prompt", help="Override the system/instruction field in all records"),
    limit: int = typer.Option(1000, "--limit", help="Maximum traces to fetch"),
):
    """Export high-quality traces as SFT or DPO training data (JSONL).

    Formats:
      chat      OpenAI ChatML — system/user/assistant messages (default; Qwen compatible)
      alpaca    Stanford Alpaca — instruction/input/output
      sharegpt  ShareGPT — human/gpt conversation pairs
      dpo       DPO/GRPO — chosen/rejected pairs for preference training
    """
    from armature.state.traces import TraceStore
    from armature.state.export import TraceExporter

    db_path = traces_db or Path("~/.armature/traces.db").expanduser()
    if not db_path.exists():
        typer.echo(f"Traces database not found: {db_path}", err=True)
        raise typer.Exit(1)

    role_list = [r.strip() for r in role_types.split(",")] if role_types else None
    store = TraceStore(db_path)
    exporter = TraceExporter(store)

    async def _run():
        if format == "dpo":
            return await exporter.export_dpo(
                workflow,
                output,
                chosen_min_score=min_score,
                rejected_max_score=rejected_max_score,
                system_prompt=system_prompt,
                limit=limit,
            )
        return await exporter.export(
            workflow,
            output,
            format=format,  # type: ignore[arg-type]
            min_quorum_score=min_score,
            role_types=role_list,
            system_prompt=system_prompt,
            limit=limit,
        )

    summary = asyncio.run(_run())
    typer.echo(
        f"Exported {summary.total_exported} record{'s' if summary.total_exported != 1 else ''} "
        f"→ {summary.output_path}  [{summary.format} format, min_score={summary.min_quorum_score}]"
    )


@app.command()
def improve(
    spec: Path = typer.Argument(..., help="Path to the workflow spec to improve"),
    trace_db: Path = typer.Option(
        None, "--traces", help="Path to traces database (default: ~/.armature/traces.db)"
    ),
    model: str = typer.Option("claude-sonnet-4-6", "--model", help="LLM used by SpecRefiner"),
    target_ihr: float = typer.Option(0.90, "--target-ihr", help="IHR threshold below which improvement is triggered"),
    min_traces: int = typer.Option(3, "--min-traces", help="Minimum traces required before analysis"),
    apply: bool = typer.Option(True, "--apply/--no-apply", help="Auto-apply proposed spec (default: apply)"),
    log: Path = typer.Option(None, "--log", help="Path to improvement log JSONL (default: <spec>.improve_log.jsonl)"),
):
    """Analyze traces and propose/apply a targeted spec improvement."""
    if not spec.exists():
        typer.echo(f"Spec not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.synthesis.improve import SelfImproveRunner

    db_path = trace_db or Path("~/.armature/traces.db").expanduser()

    async def _run():
        runner = SelfImproveRunner(
            spec,
            db_path,
            model=model,
            target_ihr=target_ihr,
            min_traces=min_traces,
            auto_apply=apply,
            log_path=log,
        )
        return await runner.analyze()

    typer.echo(f"Analyzing: {spec.name}")
    report = asyncio.run(_run())

    typer.echo(f"  traces: {report.n_traces}  IHR: {f'{report.ihr_before:.3f}' if report.ihr_before is not None else 'n/a'}  needs_improvement: {report.needs_improvement}")

    if report.n_traces == 0:
        typer.echo("No traces found — run the workflow first.")
        return

    if not report.needs_improvement:
        typer.echo("Workflow is healthy — no improvement needed.")
        return

    if report.proposed_spec is None:
        typer.echo("Refiner could not produce a valid revised spec.", err=True)
        return

    if report.applied:
        typer.echo(f"Applied revised spec → {spec}")
    else:
        typer.echo("Proposed revision available (--no-apply was set — spec not written).")

    if report.diagnostics:
        typer.echo("Failure signatures:")
        for d in report.diagnostics:
            detail = f" — {d.details}" if d.details else ""
            typer.echo(f"  [{d.stage_id}] {d.code.value}{detail}")

    if report.log_path:
        typer.echo(f"Log: {report.log_path}")


@app.command()
def export(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    target: str = typer.Option("hermes", "--target", "-t", help="Export target platform (hermes)"),
    output: Path = typer.Option(Path("."), "--output", "-o", help="Output directory for the bundle"),
):
    """Export a HarnessSpec to an external agent platform format."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    if target != "hermes":
        typer.echo(f"Unknown target '{target}'. Supported: hermes", err=True)
        raise typer.Exit(1)

    from armature.spec.loader import load_spec
    from armature.emitters.hermes import HermesEmitter

    try:
        loaded = load_spec(spec)
    except Exception as exc:
        typer.echo(f"Failed to parse spec: {exc}", err=True)
        raise typer.Exit(1)

    bundle_dir = HermesEmitter().emit(loaded, output)
    typer.echo(f"Hermes-agent bundle written to: {bundle_dir}")


@app.command()
def watch(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    host: str = typer.Option("0.0.0.0", "--host", help="Host for webhook listener"),
    port: int = typer.Option(8081, "--port", "-p", help="Port for webhook triggers"),
    traces: Path = typer.Option(None, "--traces", help="Path to traces SQLite database"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress run output"),
):
    """Run trigger listeners for a spec. Blocks until Ctrl-C."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.spec.loader import load_spec
    from armature.service.triggers import TriggerDispatcher

    try:
        loaded = load_spec(spec)
    except Exception as exc:
        typer.echo(f"Failed to parse spec: {exc}", err=True)
        raise typer.Exit(1)

    if not loaded.triggers:
        typer.echo("No triggers defined in spec — nothing to watch.", err=True)
        raise typer.Exit(1)

    async def _run_fn(payload: dict) -> None:
        harness = Harness(spec=loaded, traces_db=traces)
        result = await harness.run({"trigger_payload": payload})
        if not quiet:
            typer.echo(f"Run complete: {list(result.keys())}")

    dispatcher = TriggerDispatcher()
    typer.echo(f"Watching {len(loaded.triggers)} trigger(s). Press Ctrl-C to stop.")
    try:
        asyncio.run(dispatcher.run_forever(loaded, _run_fn, host=host, port=port))
    except KeyboardInterrupt:
        typer.echo("Watch stopped.")


@app.command()
def doctor(
    spec: Path = typer.Option(None, "--spec", help="Optional spec file to validate"),
):
    """Check environment health: packages, env vars, and optional spec validity."""
    import importlib
    import os

    all_ok = True

    # ── Package checks ───────────────────────────────────────────────────────
    required_packages = ["litellm", "pydantic", "jinja2", "ruamel.yaml", "aiosqlite", "typer"]
    optional_packages = {
        "sentence_transformers": "embeddings",
        "fastapi": "service",
        "questionary": "wizard",
        "opentelemetry": "telemetry",
        "langfuse": "langfuse",
        "langsmith": "langsmith",
        "mcp": "mcp",
    }

    typer.echo("Packages:")
    for pkg in required_packages:
        mod = pkg.replace("-", "_")
        try:
            importlib.import_module(mod)
            typer.echo(f"  ✓ {pkg}")
        except ImportError:
            typer.echo(f"  ✗ {pkg} (MISSING — required)", err=True)
            all_ok = False

    for mod, group in optional_packages.items():
        try:
            importlib.import_module(mod)
            typer.echo(f"  ✓ {mod} (optional: {group})")
        except ImportError:
            typer.echo(f"  - {mod} (optional: {group} — not installed)")

    # ── Env var checks ───────────────────────────────────────────────────────
    typer.echo("\nEnv vars:")
    api_keys = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"]
    any_key = any(os.environ.get(k) for k in api_keys)
    for key in api_keys:
        val = os.environ.get(key)
        status = "✓" if val else "-"
        typer.echo(f"  {status} {key}")
    if not any_key:
        typer.echo("  ! No LLM API key set — LLM stages will fail", err=True)

    # ── DB directory ─────────────────────────────────────────────────────────
    typer.echo("\nData paths:")
    db_dir = Path("~/.armature").expanduser()
    if db_dir.exists():
        typer.echo(f"  ✓ {db_dir}")
    else:
        typer.echo(f"  - {db_dir} (will be created on first run)")

    # ── Spec validation (optional) ───────────────────────────────────────────
    if spec is not None:
        typer.echo(f"\nSpec: {spec}")
        if not spec.exists():
            typer.echo(f"  ✗ File not found: {spec}", err=True)
            all_ok = False
        else:
            from armature.spec.loader import load_spec
            from armature.spec.validator import validate_spec
            try:
                loaded = load_spec(spec)
            except Exception as exc:
                typer.echo(f"  ✗ Parse error: {exc}", err=True)
                all_ok = False
            else:
                errors = validate_spec(loaded, strict=False)
                if errors:
                    for e in errors:
                        typer.echo(f"  ✗ [{e.code}]: {e.message}", err=True)
                    all_ok = False
                else:
                    typer.echo(f"  ✓ '{loaded.name}' is valid ({len(loaded.stages)} stages)")

    if not all_ok:
        raise typer.Exit(1)


@channels_app.command("start")
def channels_start(
    spec_file: Path = typer.Argument(..., help="Path to channel spec YAML"),
):
    """Start channel connectors from a spec file."""
    if not spec_file.exists():
        typer.echo(f"Channel spec not found: {spec_file}", err=True)
        raise typer.Exit(1)

    from ruamel.yaml import YAML
    from armature.channels.models import ChannelSpec
    import pydantic

    yaml_parser = YAML()
    try:
        with open(spec_file) as fh:
            data = dict(yaml_parser.load(fh))
        spec = ChannelSpec.model_validate(data)
    except pydantic.ValidationError as exc:
        typer.echo(f"Invalid channel spec: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Failed to load spec: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Channel spec '{spec.name}' loaded — {len(spec.channels)} channel(s).")
    typer.echo("(Live channel server not yet implemented — spec validated successfully.)")


@app.command()
def demo() -> None:
    """Run the live quality witness — test suite + capability overview for new developers."""
    from armature.demo import run_demo
    run_demo()


@app.command()
def dashboard(
    spec: Path = typer.Argument(None, help="Workflow spec YAML (extracts workflow name)"),
    workflow: str = typer.Option(None, "--workflow", "-w", help="Workflow name (alternative to spec path)"),
    traces_db: Path = typer.Option(None, "--traces", help="Path to traces.db (default: ~/.armature/traces.db)"),
    improve_log: Path = typer.Option(None, "--log", help="Path to improvement log JSONL"),
    last: int = typer.Option(200, "--last", help="Number of most recent traces to aggregate"),
    watch: bool = typer.Option(False, "--watch", is_flag=True, help="Auto-refresh every --interval seconds"),
    interval: float = typer.Option(5.0, "--interval", help="Refresh interval in seconds for --watch mode"),
    format: str = typer.Option("terminal", "--format", "-f", help="Output format: terminal | json"),
):
    """Show a Rich multi-panel workflow health dashboard aggregated across runs."""
    try:
        from rich.console import Console
    except ImportError:
        typer.echo("Rich is required for the dashboard. Install it: pip install rich", err=True)
        raise typer.Exit(1)

    from armature.report.loader import load_dashboard_data
    from armature.report.layout import render_terminal, render_terminal_watch, render_json

    # Resolve workflow name
    wf_name = workflow
    if wf_name is None and spec is not None:
        if not spec.exists():
            typer.echo(f"Spec not found: {spec}", err=True)
            raise typer.Exit(1)
        from armature.spec.loader import load_spec
        try:
            loaded = load_spec(spec)
            wf_name = loaded.name
        except Exception as exc:
            typer.echo(f"Could not parse spec: {exc}", err=True)
            raise typer.Exit(1)

    if wf_name is None:
        typer.echo("Provide a spec path or --workflow <name>.", err=True)
        raise typer.Exit(1)

    db = traces_db or Path("~/.armature/traces.db").expanduser()

    async def _load():
        return await load_dashboard_data(
            wf_name,
            traces_db=db,
            improve_log=improve_log,
            last_n=last,
        )

    if format == "json":
        data = asyncio.run(_load())
        import json as _json
        typer.echo(_json.dumps(render_json(data), indent=2))
        return

    console = Console()

    if watch:
        def _loader():
            return asyncio.run(_load())
        render_terminal_watch(_loader, interval=interval, console=console)
    else:
        data = asyncio.run(_load())
        render_terminal(data, console=console)


if __name__ == "__main__":
    app()

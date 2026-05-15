import asyncio
import json
from pathlib import Path
import typer
from armature.runtime.engine import Harness

app = typer.Typer(name="armature", help="ELF ecosystem agent harness runner", no_args_is_help=True)


@app.command()
def new(
    output: Path = typer.Argument(None, help="Output YAML file path (prompted if omitted)"),
):
    """Interactively create a new workflow spec (YAML)."""
    from armature.cli_wizard import run_wizard
    run_wizard(output_path=output)


@app.callback()
def main():
    """Armature — ELF ecosystem agent harness runner."""


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
            typer.echo(
                f"\nDone in {data['elapsed_s']}s — "
                f"{data['stages_ran']} ran, "
                f"{data['stages_skipped']} skipped, "
                f"{data['stages_resumed']} resumed, "
                f"{data['stages_failed']} failed"
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

    errors = validate_spec(loaded, strict=False)

    if not errors:
        typer.echo(f"✓ '{loaded.name}' is valid ({len(loaded.stages)} stages)")
        return

    typer.echo(f"✗ '{loaded.name}' has {len(errors)} validation error(s):\n", err=True)
    for e in errors:
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
):
    """Run a workflow from a YAML spec file."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    parsed_inputs = parse_inputs(inputs)

    from armature.spec.validator import SpecValidationError
    try:
        harness = Harness.from_spec(spec, vars=parsed_inputs)
    except SpecValidationError as exc:
        typer.echo(f"Spec validation failed:\n{exc}", err=True)
        raise typer.Exit(1)

    if dry_run:
        typer.echo(f"✓ Spec '{harness.name}' is valid ({len(harness._spec.stages)} stages)")
        typer.echo("Dry run — no execution.")
        return

    if not quiet:
        typer.echo(f"Running: {harness.name}")
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


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host"),
    port: int = typer.Option(8080, "--port", "-p", help="Bind port"),
):
    """Start the Armature HTTP service."""
    try:
        import uvicorn
        from armature.service.app import app as fastapi_app
    except ImportError:
        typer.echo("FastAPI/uvicorn not installed. Run: pip install 'armature[service]'", err=True)
        raise typer.Exit(1)
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
    run_id: str = typer.Option(..., "--run-id", help="Run ID to report on"),
    traces: Path = typer.Option(None, "--traces", help="Path to traces.db (default: ~/.armature/runs/{run_id}/traces.db)"),
    evals: Path = typer.Option(None, "--evals", help="Path to evaluations database"),
    knowledge: Path = typer.Option(None, "--knowledge", help="Path to knowledge database"),
    session_log: Path = typer.Option(None, "--session-log", help="Path to session.jsonl"),
):
    """Print a human-readable report for a completed workflow run."""
    from armature.reporting import load_report_data, ReportBuilder

    # Resolve per-run traces.db if not explicitly provided
    resolved_traces = traces or Path(f"~/.armature/runs/{run_id}/traces.db").expanduser()
    resolved_session = session_log or Path(f"~/.armature/runs/{run_id}/session.jsonl").expanduser()

    async def _load():
        return await load_report_data(
            run_id=run_id,
            traces_db=resolved_traces,
            evals_db=evals,
            knowledge_db=knowledge,
            session_log=resolved_session,
        )

    data = asyncio.run(_load())
    if data is None:
        typer.echo(
            f"No traces found for run_id='{run_id}'.\n"
            f"  Looked in: {resolved_traces}\n"
            f"  Run 'armature report --list' or check ~/.armature/runs/ for valid run IDs.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(ReportBuilder(data).build())


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


if __name__ == "__main__":
    app()

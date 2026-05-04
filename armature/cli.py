import asyncio
import json
from pathlib import Path
import typer
from armature.runtime.engine import Harness

app = typer.Typer(name="armature", help="ELF ecosystem agent harness runner", no_args_is_help=True)


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


@app.command()
def run(
    spec: Path = typer.Argument(..., help="Path to workflow spec YAML"),
    inputs: list[str] = typer.Option([], "--input", "-i", help="Input values as key=value"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate spec without executing"),
):
    """Run a workflow from a YAML spec file."""
    if not spec.exists():
        typer.echo(f"Spec file not found: {spec}", err=True)
        raise typer.Exit(1)

    parsed_inputs = parse_inputs(inputs)
    harness = Harness.from_spec(spec, vars=parsed_inputs)

    if dry_run:
        typer.echo(f"Spec '{harness.name}' loaded successfully ({len(harness._spec.stages)} stages)")
        typer.echo("Dry run — no execution.")
        return

    typer.echo(f"Running workflow: {harness.name}")

    async def _run():
        return await harness.run(parsed_inputs)

    result = asyncio.run(_run())
    typer.echo(json.dumps(result, indent=2, default=str))


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
        typer.echo("\nApplying diff... (manual review recommended)")
        typer.echo("Auto-apply not yet implemented. Review the diff and edit manually.")


if __name__ == "__main__":
    app()

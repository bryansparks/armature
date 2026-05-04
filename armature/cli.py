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


if __name__ == "__main__":
    app()

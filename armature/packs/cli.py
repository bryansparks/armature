"""`armature pack` commands: validate and list."""
from __future__ import annotations

from pathlib import Path

import typer

pack_app = typer.Typer(name="pack", help="Inspect workflow packs", no_args_is_help=True)


@pack_app.command("validate")
def validate(directory: Path = typer.Argument(..., help="A pack directory holding pack.yaml")):
    """Check a pack's pack.yaml and that every adapter's script command is static."""
    from armature.packs import PackError, load_pack
    try:
        pack = load_pack(directory)
    except PackError as exc:
        typer.echo(f"✗ {directory}: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"✓ {pack.name} {pack.version} is valid ({len(pack.adapters)} adapters)")
    for name in sorted(pack.adapters):
        typer.echo(f"  {pack.ref(name)}")


@pack_app.command("list")
def list_packs():
    """List every pack on ARMATURE_PACKS_PATH and in ~/.armature/packs, with load errors."""
    from armature.packs import catalog
    cat = catalog()
    packs = cat.packs()
    if not packs:
        typer.echo("No packs installed.")
    for pack in packs:
        typer.echo(f"{pack.name} {pack.version}  {pack.dir}")
        for name in sorted(pack.adapters):
            adapter = pack.adapters[name]
            typer.echo(f"  {pack.ref(name)}{'  (writes)' if adapter.writes else ''}  {adapter.description}".rstrip())
    for error in cat.errors:
        typer.echo(f"✗ {error}", err=True)
    if cat.errors:
        raise typer.Exit(1)

# armature/packaging/cli.py
from __future__ import annotations
from pathlib import Path
import typer

package_app = typer.Typer(name="package", help="Build and run self-contained workflow packages",
                          no_args_is_help=True)


def register(app) -> None:
    app.add_typer(package_app, name="package")


def _parse_inputs(items: list[str]) -> dict:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            typer.echo(f"Invalid input '{item}' — use key=value", err=True)
            raise typer.Exit(1)
        k, _, v = item.partition("=")
        out[k.strip()] = v.strip()
    return out


@package_app.command("build")
def build(
    spec: Path = typer.Option(..., "--spec", help="Path to workflow spec YAML"),
    out: Path = typer.Option(..., "--out", help="Output package directory (or archive with --archive)"),
    tools: Path | None = typer.Option(None, "--tools", help="Directory of custom tool source to vendor"),
    requirements: Path | None = typer.Option(None, "--requirements", help="requirements.txt to bundle"),
    destinations: Path | None = typer.Option(None, "--destinations", help="destinations.yaml to bundle"),
    runtime_inputs: str | None = typer.Option(None, "--runtime-inputs",
                                              help="Comma-separated input names supplied at run"),
    profile: Path | None = typer.Option(None, "--profile", help=".env profile to verify secrets against"),
    archive: str | None = typer.Option(None, "--archive", help="tar | zip — also archive the package"),
    input_kv: list[str] = typer.Option([], "--input", help="Bundled default inputs as key=value"),
):
    """Build a self-contained, verified workflow package."""
    from armature.packaging.builder import PackageBuilder, PackageBuildError
    try:
        pkg = PackageBuilder().build(
            spec=spec, out=out, inputs=_parse_inputs(input_kv), tools=tools,
            requirements=requirements, destinations=destinations,
            runtime_inputs=[s.strip() for s in runtime_inputs.split(",")] if runtime_inputs else [],
            profile_env=_load_profile_env(profile) if profile else None,
            archive=archive,
        )
    except PackageBuildError as exc:
        typer.echo(f"Build failed: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Built package: {pkg}")


@package_app.command("run")
def run(
    pkg: Path = typer.Argument(..., help="Package directory or archive"),
    results: Path = typer.Option(Path("./results"), "--results", help="Results directory"),
    profile: Path | None = typer.Option(None, "--profile", help=".env file with secret values"),
    input_kv: list[str] = typer.Option([], "--input", help="Input overrides as key=value"),
    include_trace: bool = typer.Option(False, "--include-trace", help="Write trace.jsonl alongside results"),
    direct: bool = typer.Option(False, "--direct", help="Run in-process (no Docker). Internal/test path."),
    secrets: Path | None = typer.Option(None, "--secrets",
                                        help="Container-internal secrets .env path (used by --direct under Docker)"),
    inputs_override_file: Path | None = typer.Option(None, "--inputs-override",
                                                     help="Container-internal inputs-override YAML path (used by --direct under Docker)"),
):
    """Run a workflow package to completion (default: in a container)."""
    pkg_dir = _resolve_pkg(pkg)
    if direct:
        from armature.packaging.runner import PackageRunner, PackageError, SecretMissingError
        runner = PackageRunner(skip_deps_install=False)
        try:
            overrides = _parse_inputs(input_kv) or None
            if inputs_override_file is not None:
                from ruamel.yaml import YAML
                overrides = dict(YAML().load(inputs_override_file) or {})
            receipt = runner.run_sync(pkg_dir, results, profile_path=profile, secrets_path=secrets,
                                      inputs_override=overrides, include_trace=include_trace)
        except SecretMissingError as exc:
            typer.echo(f"Missing secrets: {exc}", err=True)
            raise typer.Exit(2)
        except PackageError as exc:
            typer.echo(f"Run failed: {exc}", err=True)
            raise typer.Exit(1)
        typer.echo(f"Run {receipt.run_id}: {receipt.status} → {results / receipt.run_id}")
        return
    # container mode
    from armature.packaging.docker_runner import DockerRunnerLauncher
    overrides = _write_overrides(pkg_dir.parent, _parse_inputs(input_kv)) if input_kv else None
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile.runner"
    launcher = DockerRunnerLauncher()
    try:
        launcher.ensure_image(dockerfile)
    except Exception as exc:
        typer.echo(f"Could not prepare runner image: {exc}", err=True)
        raise typer.Exit(1)
    rc = launcher.run(pkg=pkg_dir, results=results, profile=profile,
                      inputs_override=overrides, include_trace=include_trace)
    raise typer.Exit(rc)


@package_app.command("verify")
def verify(
    pkg: Path = typer.Argument(..., help="Package directory"),
):
    """Re-run the completeness verifier without executing."""
    from ruamel.yaml import YAML
    from armature.packaging.manifest import PackageManifest
    from armature.packaging.verifier import CompletenessVerifier
    pkg_dir = _resolve_pkg(pkg)
    manifest = PackageManifest.model_validate(YAML().load(pkg_dir / "package.yaml"))
    report = CompletenessVerifier().verify(pkg_dir, manifest)
    for c in report.checks:
        sym = {"pass": "✓", "fail": "✗", "warn": "⚠"}[c.status]
        typer.echo(f"  {sym} {c.check}: {c.detail}")
    if not report.ok:
        raise typer.Exit(1)


@package_app.command("inspect")
def inspect(
    pkg: Path = typer.Argument(..., help="Package directory"),
):
    """Print the package manifest (read-only)."""
    from ruamel.yaml import YAML
    from armature.packaging.manifest import PackageManifest
    pkg_dir = _resolve_pkg(pkg)
    manifest = PackageManifest.model_validate(YAML().load(pkg_dir / "package.yaml"))
    typer.echo(f"{manifest.name} v{manifest.version}  (api {manifest.api_version})")
    typer.echo(f"  armature: {manifest.armature_version}")
    typer.echo(f"  spec: {manifest.spec}  inputs: {manifest.inputs}")
    typer.echo(f"  runtime_inputs: {manifest.runtime_inputs}")
    typer.echo(f"  tools_dir: {manifest.tools_dir}  requirements: {manifest.requirements}")


# -- helpers ---------------------------------------------------------------
def _resolve_pkg(pkg: Path) -> Path:
    if pkg.is_dir():
        return pkg
    if pkg.exists() and pkg.suffix in (".tar", ".zip"):
        import tempfile, tarfile, zipfile
        target = Path(tempfile.mkdtemp(prefix="armature-pkg-")) / pkg.stem
        target.mkdir()
        if pkg.suffix == ".tar":
            with tarfile.open(pkg) as tf:
                tf.extractall(target)
        else:
            with zipfile.ZipFile(pkg) as zf:
                zf.extractall(target)
        # archive may wrap a top dir
        entries = list(target.iterdir())
        if len(entries) == 1 and entries[0].is_dir() and (entries[0] / "package.yaml").exists():
            return entries[0]
        return target
    typer.echo(f"Package not found: {pkg}", err=True)
    raise typer.Exit(1)


def _load_profile_env(profile: Path) -> dict[str, str]:
    from armature.packaging.runner import PackageRunner
    return PackageRunner._parse_env_file(profile)


def _write_overrides(parent: Path, overrides: dict) -> Path:
    from ruamel.yaml import YAML
    p = parent / "_inputs-override.yaml"
    YAML().dump(overrides, p)
    return p
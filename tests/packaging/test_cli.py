# tests/packaging/test_cli.py
from pathlib import Path
from typer.testing import CliRunner
from armature.cli import app

runner = CliRunner()

def test_package_help_lists_subcommands():
    res = runner.invoke(app, ["package", "--help"])
    assert res.exit_code == 0
    assert "build" in res.stdout
    assert "run" in res.stdout
    assert "verify" in res.stdout
    assert "inspect" in res.stdout

def test_package_build_via_cli(tmp_path: Path, tiny_spec, monkeypatch):
    out = tmp_path / "echo.pkg"
    res = runner.invoke(app, ["package", "build", "--spec", str(tiny_spec),
                              "--out", str(out), "--input", "topic=hello"])
    assert res.exit_code == 0, res.stdout
    assert (out / "package.yaml").exists()

def test_package_inspect_via_cli(tmp_path: Path, tiny_spec):
    out = tmp_path / "echo.pkg"
    runner.invoke(app, ["package", "build", "--spec", str(tiny_spec), "--out", str(out),
                        "--input", "topic=hello"])
    res = runner.invoke(app, ["package", "inspect", str(out)])
    assert res.exit_code == 0
    assert "echo-demo" in res.stdout

def test_package_verify_via_cli(tmp_path: Path, tiny_spec):
    out = tmp_path / "echo.pkg"
    runner.invoke(app, ["package", "build", "--spec", str(tiny_spec), "--out", str(out),
                        "--input", "topic=hello"])
    res = runner.invoke(app, ["package", "verify", str(out)])
    assert res.exit_code == 0


def test_package_run_direct_exits_nonzero_on_failure(tmp_path: Path, tiny_spec):
    """A failed --direct run must exit non-zero (R8 contract). Tamper the package
    so R1 integrity fails → status='failed' receipt → CLI exits 1, not 0."""
    out = tmp_path / "echo.pkg"
    build_res = runner.invoke(app, ["package", "build", "--spec", str(tiny_spec),
                                    "--out", str(out), "--input", "topic=hello"])
    assert build_res.exit_code == 0, build_res.stdout

    # Tamper: append a byte to workflow.yaml so manifest.sha256 no longer matches → R1 fails.
    wf = out / "workflow.yaml"
    wf.write_text(wf.read_text() + "\n# tampered\n")

    res = runner.invoke(app, ["package", "run", "--direct", str(out),
                              "--results", str(tmp_path / "results")])
    assert res.exit_code != 0, f"expected non-zero exit on failed run, got {res.exit_code}"


def test_package_run_container_mode_absolutizes_paths(tmp_path: Path, no_llm_pkg, monkeypatch):
    """Regression: container-mode ``package run`` must pass ABSOLUTE host paths
    to the Docker launcher. Docker silently treats a relative path matching its
    volume-name charset (``[a-zA-Z0-9][a-zA-Z0-9_.-]*``) as an empty *named
    volume* rather than a bind-mount, and rejects names beginning with ``_``
    (the original failure: ``_inputs-override.yaml``). The integration tests use
    pytest's absolute ``tmp_path`` so they miss this; here we chdir into the
    temp dir and pass relative paths, then assert the launcher saw absolutes.
    """
    spec_path, tools_dir = no_llm_pkg
    pkg = tmp_path / "echo.pkg"
    build_res = runner.invoke(app, ["package", "build", "--spec", str(spec_path),
                                    "--tools", str(tools_dir), "--out", str(pkg)])
    assert build_res.exit_code == 0, build_res.stdout

    import armature.packaging.docker_runner as dr_mod
    captured: dict = {}

    class _StubLauncher:
        def ensure_image(self, dockerfile):
            return

        def run(self, *, pkg, results, profile, inputs_override, include_trace):
            captured.update(pkg=pkg, results=results, profile=profile,
                            inputs_override=inputs_override, include_trace=include_trace)
            return 0

    monkeypatch.setattr(dr_mod, "DockerRunnerLauncher", _StubLauncher)

    # Relative paths from tmp_path as cwd — the bug condition.
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["package", "run", "echo.pkg",
                              "--results", "results",
                              "--input", "msg=hi"])
    assert res.exit_code == 0, res.stdout
    assert captured["pkg"].is_absolute(), f"pkg path not absolute: {captured['pkg']}"
    assert captured["results"].is_absolute(), f"results path not absolute: {captured['results']}"
    # The --input override is written to a unique temp file, which must be absolute
    # (a relative path is what originally triggered the Docker volume-name error).
    assert captured["inputs_override"] is not None
    assert captured["inputs_override"].is_absolute(), (
        f"inputs-override path not absolute: {captured['inputs_override']}")
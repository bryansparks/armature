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
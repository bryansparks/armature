# tests/packaging/test_docker_integration.py
"""Real-Docker integration tests for workflow packages.

These build the ``armature-runner`` image and run the example packages in real
containers — the path ``armature package run <pkg>`` takes by default (not the
``--direct`` in-process path). They close the biggest testing gap from
docs/WORKFLOW-PACKAGES.md:

* the full container round-trip (build image -> mount package -> run -> host
  results dir + receipt + artifact),
* the container-mode CLI (the host-side path that shells out to ``docker run``),
* the host ``--input`` override reaching a stage inside the container, and
* the nested-sandbox DooD path (a sandboxed shell command runs in a sibling
  container on the host daemon).

The whole module is **skipped automatically when Docker is not available**, so
``pytest tests/`` stays green on any machine without Docker. Run explicitly:

    pytest tests/packaging/test_docker_integration.py -v
    pytest -m docker          # only these
    pytest -m "not docker"    # everything else
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from armature.cli import app

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples" / "packages"
DOCKERFILE = REPO / "Dockerfile.runner"
IMAGE = "armature-runner:latest"

runner = CliRunner()


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30, check=True
        )
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="Docker daemon not available"),
]


@pytest.fixture(scope="module")
def runner_image():
    """Build the runner image once so the CLI's ensure_image() finds it ready.

    Building explicitly (rather than relying on ensure_image's lazy build) makes
    a build failure surface as a clear assertion instead of a CLI exit code, and
    guarantees the image matches this checkout regardless of any pre-existing
    armature-runner:latest on the host.
    """
    proc = subprocess.run(
        ["docker", "build", "-t", IMAGE, "-f", str(DOCKERFILE), str(DOCKERFILE.parent)],
        capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, f"runner image build failed:\n{proc.stderr[-3000:]}"
    return IMAGE


def _build_pkg(tmp_path: Path, example: str, *, tools: bool = False,
               runtime_inputs: str | None = None, input_kv: list[str] | None = None) -> Path:
    """Build an example package into tmp_path via the real CLI."""
    src = EXAMPLES / example
    pkg = tmp_path / f"{example}.pkg"
    args = ["package", "build", "--spec", str(src / "workflow.yaml"), "--out", str(pkg)]
    if (src / "destinations.yaml").exists():
        args += ["--destinations", str(src / "destinations.yaml")]
    if tools:
        assert (src / "tools").exists(), f"example {example} has no tools/ dir"
        args += ["--tools", str(src / "tools")]
    if runtime_inputs:
        args += ["--runtime-inputs", runtime_inputs]
    for kv in input_kv or []:
        args += ["--input", kv]
    res = runner.invoke(app, args)
    assert res.exit_code == 0, f"package build failed (rc={res.exit_code}):\n{res.stdout}"
    return pkg


def _run_pkg(pkg: Path, results: Path, *, input_kv: list[str] | None = None,
             include_trace: bool = False) -> tuple[int, str]:
    """Run a package in a container via the real CLI; return (exit_code, combined_output)."""
    # The container runs as non-root uid 1000; widen the results dir so the
    # bind-mounted output is writable across host/uid-mapping configs.
    results.mkdir(parents=True, exist_ok=True)
    try:
        results.chmod(0o777)
    except OSError:
        pass
    args = ["package", "run", str(pkg), "--results", str(results)]
    for kv in input_kv or []:
        args += ["--input", kv]
    if include_trace:
        args += ["--include-trace"]
    res = runner.invoke(app, args)
    # CliRunner mixes stderr into stdout by default; the container's own
    # stdout/stderr stream to this process and appear in pytest's captured
    # output on failure.
    return res.exit_code, res.stdout or ""


def _single_run_dir(results: Path) -> Path:
    runs = [d for d in results.iterdir() if d.is_dir() and d.name != "_pending"]
    assert len(runs) == 1, f"expected exactly one run dir under {results}, got {sorted(d.name for d in runs)}"
    return runs[0]


def test_echo_tool_runs_in_container(runner_image, tmp_path):
    """No-LLM package runs in a real container; host gets a receipt, artifact, and trace."""
    pkg = _build_pkg(tmp_path, "echo-tool", tools=True,
                     runtime_inputs="msg", input_kv=["msg=hello-default"])
    results = tmp_path / "results"
    rc, out = _run_pkg(pkg, results, input_kv=["msg=hello-via-container"], include_trace=True)
    assert rc == 0, f"container run failed (rc={rc}):\n{out}"

    run_dir = _single_run_dir(results)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["status"] == "complete", receipt
    assert (run_dir / "trace.jsonl").exists(), "--include-trace did not produce trace.jsonl"

    artifact = run_dir / "artifacts" / "echo.md"
    assert artifact.exists(), f"artifact not delivered: {run_dir / 'artifacts'}"
    assert "hello-via-container" in artifact.read_text(), "host --input override did not reach the stage"


def test_tampered_package_exits_nonzero_in_container(runner_image, tmp_path):
    """A tampered package run in a container must exit non-zero (R8 integrity -> failed)."""
    pkg = _build_pkg(tmp_path, "echo-tool", tools=True,
                     runtime_inputs="msg", input_kv=["msg=hello-default"])
    # Append a byte to workflow.yaml so manifest.sha256 no longer matches -> R1 fails
    # inside the container -> status='failed' receipt -> --direct exits 1 -> container exits 1.
    (pkg / "workflow.yaml").write_text((pkg / "workflow.yaml").read_text() + "\n# tampered\n")

    results = tmp_path / "results"
    rc, _ = _run_pkg(pkg, results)
    assert rc != 0, "expected non-zero exit from a tampered package run in a container"


def test_sandbox_shell_runs_in_sibling_container(runner_image, tmp_path):
    """sandbox.mode=docker: a shell command runs in a sibling container (DooD); stdout is captured."""
    pkg = _build_pkg(tmp_path, "sandbox-shell")
    results = tmp_path / "results"
    rc, out = _run_pkg(pkg, results)
    assert rc == 0, f"sandbox container run failed (rc={rc}):\n{out}"

    run_dir = _single_run_dir(results)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["status"] == "complete", receipt

    result_artifact = run_dir / "artifacts" / "result.json"
    assert result_artifact.exists(), "sandbox result artifact not delivered"
    data = json.loads(result_artifact.read_text())
    assert data["exit_code"] == 0, data
    assert "hello-from-sandbox" in data["stdout"], "sibling-container stdout not captured"
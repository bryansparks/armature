# tests/packaging/test_docker_runner.py
from pathlib import Path
from armature.packaging.docker_runner import DockerRunnerLauncher


def _make_pkg(tmp_path: Path, *, sandbox: bool) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    sb = "  mode: docker\n  image: alpine:3.20\n" if sandbox else ""
    (pkg / "workflow.yaml").write_text(
        f'name: demo\nversion: "1.0"\nsandbox:\n{sb}stages: []\n'
        if sandbox else
        'name: demo\nversion: "1.0"\nstages: []\n'
    )
    return pkg


def test_sandbox_pkg_mounts_socket_and_runs_as_root(tmp_path: Path):
    launcher = DockerRunnerLauncher(image="armature-runner:latest", runner=subprocess_runner())
    pkg = _make_pkg(tmp_path, sandbox=True)
    cmd = launcher.build_command(pkg=pkg, results=tmp_path / "out",
                                 profile=tmp_path / "p.env", inputs_override=tmp_path / "o.yaml",
                                 include_trace=True)
    joined = " ".join(cmd)
    assert "docker run --rm" in joined
    assert f"-v {tmp_path/'pkg'}:/package:ro" in joined
    assert f"-v {tmp_path/'out'}:/results" in joined
    assert "-v /var/run/docker.sock:/var/run/docker.sock" in joined
    assert "--user 0:0" in joined
    # ENTRYPOINT supplies `armature package run --direct`; do not duplicate it.
    assert "armature-runner:latest /package --results /results" in joined
    assert "package run --direct /package" not in joined
    assert "--include-trace" in joined
    assert "--secrets /secrets.env" in joined
    assert "--inputs-override /inputs-override.yaml" in joined


def test_non_sandbox_pkg_omits_socket_and_root(tmp_path: Path):
    launcher = DockerRunnerLauncher(image="armature-runner:latest", runner=subprocess_runner())
    pkg = _make_pkg(tmp_path, sandbox=False)
    cmd = launcher.build_command(pkg=pkg, results=tmp_path / "out",
                                 profile=None, inputs_override=None, include_trace=False)
    joined = " ".join(cmd)
    # Least privilege: no socket, no root, no secrets/override flags.
    assert "/var/run/docker.sock" not in joined
    assert "--user 0:0" not in joined
    assert "--secrets" not in joined
    assert "--inputs-override" not in joined
    assert "armature-runner:latest /package --results /results" in joined


def subprocess_runner():
    """Avoid shelling out in unit tests — capture, don't execute."""
    class _Stub:
        def __call__(self, cmd): return 0
    return _Stub()
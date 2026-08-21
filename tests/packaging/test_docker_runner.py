# tests/packaging/test_docker_runner.py
from pathlib import Path
from armature.packaging.docker_runner import DockerRunnerLauncher

def test_run_command_constructs_mounts(tmp_path: Path):
    launcher = DockerRunnerLauncher(image="armature-runner:latest", runner=subprocess_runner())
    cmd = launcher.build_command(pkg=tmp_path / "pkg", results=tmp_path / "out",
                                 profile=tmp_path / "p.env", inputs_override=tmp_path / "o.yaml",
                                 include_trace=True)
    joined = " ".join(cmd)
    assert "docker run --rm" in joined
    assert f"-v {tmp_path/'pkg'}:/package:ro" in joined
    assert f"-v {tmp_path/'out'}:/results" in joined
    assert "-v /var/run/docker.sock:/var/run/docker.sock" in joined
    assert "package run --direct /package --results /results" in joined
    assert "--include-trace" in joined
    assert "--secrets /secrets.env" in joined
    assert "--inputs-override /inputs-override.yaml" in joined

def subprocess_runner():
    """Avoid shelling out in unit tests — capture, don't execute."""
    class _Stub:
        def __call__(self, cmd): return 0
    return _Stub()
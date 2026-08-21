# armature/packaging/docker_runner.py
from __future__ import annotations
import shutil
import subprocess
from pathlib import Path


class DockerRunnerLauncher:
    def __init__(self, image: str = "armature-runner:latest", runner=None):
        self._image = image
        self._runner = runner  # callable(cmd: list[str]) -> int; None => subprocess

    def ensure_image(self, dockerfile: Path) -> None:
        """Build the runner image if it isn't present."""
        if not shutil.which("docker"):
            raise RuntimeError("docker not found on PATH")
        try:
            subprocess.run(["docker", "image", "inspect", self._image],
                           capture_output=True, timeout=30, check=True)
            return
        except Exception:
            pass
        ctx = dockerfile.parent
        subprocess.run(["docker", "build", "-t", self._image, "-f", str(dockerfile), str(ctx)],
                       check=True)

    def build_command(self, *, pkg: Path, results: Path, profile: Path | None,
                      inputs_override: Path | None, include_trace: bool) -> list[str]:
        cmd = ["docker", "run", "--rm",
               "-v", f"{pkg}:/package:ro",
               "-v", f"{results}:/results",
               "-v", "/var/run/docker.sock:/var/run/docker.sock"]
        if profile is not None:
            cmd += ["-v", f"{profile}:/secrets.env:ro"]
        if inputs_override is not None:
            cmd += ["-v", f"{inputs_override}:/inputs-override.yaml:ro"]
        cmd += [self._image, "package", "run", "--direct", "/package", "--results", "/results"]
        if profile is not None:
            cmd += ["--secrets", "/secrets.env"]
        if inputs_override is not None:
            cmd += ["--inputs-override", "/inputs-override.yaml"]
        if include_trace:
            cmd += ["--include-trace"]
        return cmd

    def run(self, *, pkg: Path, results: Path, profile: Path | None = None,
            inputs_override: Path | None = None, include_trace: bool = False) -> int:
        cmd = self.build_command(pkg=pkg, results=results, profile=profile,
                                 inputs_override=inputs_override, include_trace=include_trace)
        if self._runner is not None:
            return int(self._runner(cmd))
        return subprocess.run(cmd).returncode
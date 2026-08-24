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
        needs_docker = self._spec_uses_docker_sandbox(pkg)
        cmd = ["docker", "run", "--rm",
               "-v", f"{pkg}:/package:ro",
               "-v", f"{results}:/results"]
        if needs_docker:
            # DooD: mount the host Docker socket so DockerSandboxProvider can
            # spawn sibling containers on the host daemon, and run the runner as
            # root so it can reach the socket. (Orbstack restricts socket
            # access to root; --group-add is rejected there. On Linux a
            # docker-group member would also work, but root is the portable
            # choice for a trusted runner that deliberately mounts the socket.)
            # Non-sandbox packages skip both — least privilege.
            cmd += ["-v", "/var/run/docker.sock:/var/run/docker.sock", "--user", "0:0"]
        if profile is not None:
            cmd += ["-v", f"{profile}:/secrets.env:ro"]
        if inputs_override is not None:
            cmd += ["-v", f"{inputs_override}:/inputs-override.yaml:ro"]
        # The image ENTRYPOINT is `armature package run --direct`, so the args
        # after the image are just the package path + options — do NOT repeat
        # `package run --direct` here or it doubles up and `/package` becomes an
        # unexpected extra argument.
        cmd += [self._image, "/package", "--results", "/results"]
        if profile is not None:
            cmd += ["--secrets", "/secrets.env"]
        if inputs_override is not None:
            cmd += ["--inputs-override", "/inputs-override.yaml"]
        if include_trace:
            cmd += ["--include-trace"]
        return cmd

    @staticmethod
    def _spec_uses_docker_sandbox(pkg: Path) -> bool:
        """Peek at the bundled spec to decide whether the runner needs the Docker socket.

        Only packages declaring ``sandbox.mode: docker`` spawn sibling containers,
        so only they need the socket mounted and root privileges. Reading the
        bundled spec (default ``workflow.yaml``) on the host before launch keeps
        the runner least-privilege for every other package.
        """
        spec_path = pkg / "workflow.yaml"
        try:
            from ruamel.yaml import YAML
            doc = YAML().load(spec_path) or {}
            return str((doc.get("sandbox") or {}).get("mode", "")).lower() == "docker"
        except Exception:
            return False

    def run(self, *, pkg: Path, results: Path, profile: Path | None = None,
            inputs_override: Path | None = None, include_trace: bool = False) -> int:
        cmd = self.build_command(pkg=pkg, results=results, profile=profile,
                                 inputs_override=inputs_override, include_trace=include_trace)
        if self._runner is not None:
            return int(self._runner(cmd))
        return subprocess.run(cmd).returncode
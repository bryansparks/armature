"""Docker sandbox provider for Armature.

When enabled, wraps shell, file_write, and file_read tool handlers so
that shell commands run inside ephemeral Docker containers while file
operations work directly against the host workspace via bind mount.
"""
from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from armature.registry.registry import ToolRegistry
    from armature.spec.models import SandboxConfig


class DockerSandboxProvider:
    def __init__(self) -> None:
        self._stage_image: str | None = None

    def set_stage_image(self, image: str | None) -> None:
        """Override the Docker image for the next stage execution. Pass None to restore the spec default."""
        self._stage_image = image

    def wrap_registry(
        self,
        registry: "ToolRegistry",
        sandbox: "SandboxConfig",
        host_workspace: Path,
    ) -> None:
        """Replace shell/file_write/file_read handlers with sandboxed versions.

        When sandbox.mode is NONE, this is a no-op.
        """
        from armature.spec.models import SandboxMode

        if sandbox.mode != SandboxMode.DOCKER:
            return

        from armature.permissions.permissions import PermissionLevel

        shell_handler = self._make_shell_handler(sandbox, host_workspace)
        fw_handler = self._make_file_write_handler(host_workspace)
        fr_handler = self._make_file_read_handler(host_workspace)

        for name, handler, perm in [
            ("shell", shell_handler, PermissionLevel.WORKSPACE),
            ("file_write", fw_handler, PermissionLevel.WORKSPACE),
            ("file_read", fr_handler, PermissionLevel.READ_ONLY),
        ]:
            desc = registry.get(name)
            if desc is not None:
                # Preserve reversibility/postcondition so the safety hook sees
                # accurate `_tool_reversibility` for sandboxed tools; only the
                # handler and permission are overridden by the sandbox wrapper.
                registry.register(desc.model_copy(update={
                    "handler": handler,
                    "permission": perm,
                }))

    def _make_shell_handler(self, sandbox: "SandboxConfig", host_workspace: Path):
        provider = self

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            cmd = args.get("cmd", "")
            # Classify before launching the container: a destructive command
            # (rm -rf /, etc.) would trash the bind-mounted host workspace even
            # inside the sandbox. The container is containment, but the bind
            # mount is writable, so this is defense-in-depth at the boundary.
            from armature.permissions.permissions import classify_shell_command, requires_approval
            permission = classify_shell_command(cmd)
            if requires_approval(permission):
                return {
                    "stdout": "",
                    "stderr": f"shell command classified as DESTRUCTIVE and refused: {cmd[:60]!r}",
                    "exit_code": 126,
                }
            image = provider._stage_image if provider._stage_image is not None else sandbox.image
            docker_cmd = [sandbox.runtime, "run", "--rm"]

            if sandbox.platform is not None:
                docker_cmd += ["--platform", sandbox.platform]

            if not sandbox.allow_network:
                docker_cmd += ["--network", "none"]

            if sandbox.cpu_limit is not None:
                docker_cmd += ["--cpus", sandbox.cpu_limit]

            if sandbox.memory_limit is not None:
                docker_cmd += ["--memory", sandbox.memory_limit]

            docker_cmd += ["-v", f"{host_workspace}:{sandbox.workspace}"]

            for k, v in sandbox.env.items():
                docker_cmd += ["-e", f"{k}={v}"]

            docker_cmd += [image, "sh", "-c", cmd]

            proc = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=sandbox.timeout_s,
            )
            return {
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "exit_code": proc.returncode,
            }
        return handler

    @staticmethod
    def _resolve_within(host_workspace: Path, rel_path: str) -> tuple[Path, str | None]:
        """Resolve ``rel_path`` against ``host_workspace`` and contain it.

        Returns ``(target, None)`` if ``target`` is inside ``host_workspace``,
        else ``(target, error_message)``. An absolute ``rel_path`` discards the
        workspace (``Path("/ws") / "/etc/passwd" == "/etc/passwd"``) and ``..``
        segments traverse out, so both are rejected by resolving and checking
        ``is_relative_to``.
        """
        root = host_workspace.resolve()
        # Reject absolute paths explicitly: the workspace is the root; an
        # absolute path is an attempt to escape it.
        if rel_path and Path(rel_path).is_absolute():
            return root, f"path is absolute, must be relative to workspace: {rel_path!r}"
        target = (host_workspace / rel_path).resolve()
        if not target.is_relative_to(root):
            return target, f"path escapes workspace: {rel_path!r}"
        return target, None

    @staticmethod
    def _make_file_write_handler(host_workspace: Path):
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            rel_path = args.get("path", "")
            content = args.get("content", "")
            target, err = DockerSandboxProvider._resolve_within(host_workspace, rel_path)
            if err is not None:
                return {"error": err}
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return {"written": str(target)}
        return handler

    @staticmethod
    def _make_file_read_handler(host_workspace: Path):
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            rel_path = args.get("path", "")
            target, err = DockerSandboxProvider._resolve_within(host_workspace, rel_path)
            if err is not None:
                return {"error": err, "content": ""}
            if not target.exists():
                return {"error": f"File not found: {rel_path}", "content": ""}
            return {"content": target.read_text(encoding="utf-8")}
        return handler

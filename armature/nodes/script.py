from __future__ import annotations
import subprocess
from jinja2 import Environment, BaseLoader
from typing import Any
from armature.nodes.base import BaseNode
from armature.spec.models import Adapter
from armature.permissions.permissions import classify_shell_command, requires_approval


class ScriptNode(BaseNode):
    def __init__(self, adapter: Adapter):
        self._adapter = adapter

    async def execute(self, context: dict[str, Any]) -> Any:
        cmd = self._adapter.cmd or ""
        if "{{" in cmd:
            env = Environment(loader=BaseLoader(), variable_start_string="{{", variable_end_string="}}")
            cmd = env.from_string(cmd).render(**context)

        permission = classify_shell_command(cmd)
        if requires_approval(permission):
            raise PermissionError(
                f"Script command '{cmd[:60]}' is classified as DESTRUCTIVE and requires approval. "
                "Use a HumanGateNode before this stage or register a pre-tool hook."
            )

        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }

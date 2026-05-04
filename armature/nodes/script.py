from __future__ import annotations
import subprocess
from jinja2 import Environment, BaseLoader
from typing import Any
from armature.nodes.base import BaseNode
from armature.spec.models import Adapter


class ScriptNode(BaseNode):
    def __init__(self, adapter: Adapter):
        self._adapter = adapter

    async def execute(self, context: dict[str, Any]) -> Any:
        cmd = self._adapter.cmd or ""
        if "{{" in cmd:
            env = Environment(loader=BaseLoader(), variable_start_string="{{", variable_end_string="}}")
            cmd = env.from_string(cmd).render(**context)

        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }

from __future__ import annotations
import json
import os
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

        env = {**os.environ, "ARMATURE_CONTEXT": json.dumps(context, default=str)}
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=self._adapter.timeout, env=env)
        envelope = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
        if self._adapter.parse == "json":
            return self._parse_json(envelope)
        return envelope

    def _parse_json(self, envelope: dict[str, Any]) -> Any:
        """Turn a script's stdout into a structured stage result.

        With parse: json the script is a function: exit 0 with a JSON object
        on stdout. Anything else is a contract violation and raises, so the
        stage's normal failure handling (on_fail, fail_as_value) applies.
        Diagnostics belong on stderr.
        """
        if envelope["exit_code"] != 0:
            raise RuntimeError(
                f"Adapter '{self._adapter.name}' exited {envelope['exit_code']} "
                f"(parse: json): {envelope['stderr'].strip()[:500]}"
            )
        try:
            parsed = json.loads(envelope["stdout"])
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Adapter '{self._adapter.name}' has parse: json but stdout is not valid JSON: {e}"
            ) from e
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Adapter '{self._adapter.name}' has parse: json and must print a "
                f"top-level JSON object, got {type(parsed).__name__}"
            )
        return parsed

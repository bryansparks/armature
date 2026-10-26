from __future__ import annotations
import asyncio
import json
import os
import signal
import subprocess
from typing import Any
from armature.events.env import child_env
from armature.nodes.base import BaseNode
from armature.runtime.templating import text_env
from armature.spec.models import Adapter
from armature.permissions.permissions import classify_shell_command, requires_approval


class ScriptNode(BaseNode):
    def __init__(self, adapter: Adapter, env: dict[str, str] | None = None):
        self._adapter = adapter
        self._env = env or {}

    async def execute(self, context: dict[str, Any]) -> Any:
        cmd = self._adapter.cmd or ""
        # A pack's cmd is fixed at install, so nothing a workflow carries is rendered into it.
        if "{{" in cmd and self._adapter.pack_dir is None:
            cmd = text_env().from_string(cmd).render(**context)

        permission = classify_shell_command(cmd)
        if requires_approval(permission):
            raise PermissionError(
                f"Script command '{cmd[:60]}' is classified as DESTRUCTIVE and requires approval. "
                "Use a HumanGateNode before this stage or register a pre-tool hook."
            )

        env = child_env({**self._env, "ARMATURE_CONTEXT": json.dumps(context, default=str)})
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._adapter.timeout)
        except asyncio.TimeoutError:
            await self._kill_group(proc)
            raise subprocess.TimeoutExpired(cmd, self._adapter.timeout)
        except BaseException:
            await self._kill_group(proc)
            raise
        envelope = {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "exit_code": proc.returncode,
        }
        if self._adapter.parse == "json":
            return self._parse_json(envelope)
        return envelope

    @staticmethod
    async def _kill_group(proc: asyncio.subprocess.Process) -> None:
        # The shell's children, such as a CLI the script starts, share its session, so killing only the shell would orphan them.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()

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

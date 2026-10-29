from __future__ import annotations
import asyncio
import json
import os
import re
import signal
import subprocess
from collections.abc import Callable
from typing import Any
from armature.events.env import child_env
from armature.nodes.base import BaseNode
from armature.runtime.templating import text_env
from armature.spec.models import Adapter
from armature.permissions.permissions import classify_shell_command, requires_approval

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
LOG_LINE_CHARS = 2000
LOG_LINES_PER_S = 20
PARTIAL_FLUSH_S = 1.0
PARTIAL_FLUSH_BYTES = 4096


class ScriptNode(BaseNode):
    def __init__(
        self,
        adapter: Adapter,
        env: dict[str, str] | None = None,
        emit: Callable[..., None] | None = None,
        step_id: str | None = None,
    ):
        self._adapter = adapter
        self._env = env or {}
        self._emit = emit
        self._step_id = step_id
        self._window_start = 0.0
        self._window_lines = 0
        self._skipped = 0

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
            stdout, stderr = await asyncio.wait_for(self._communicate(proc), timeout=self._adapter.timeout)
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

    async def _communicate(self, proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        if self._emit is None:
            return await proc.communicate()
        stdout, stderr = await asyncio.gather(proc.stdout.read(), self._tail_stderr(proc.stderr))
        await proc.wait()
        return stdout, stderr

    async def _tail_stderr(self, stream: asyncio.StreamReader) -> bytes:
        """All of stderr, sending each line as a `log` event as it arrives."""
        loop = asyncio.get_running_loop()
        collected = bytearray()
        partial = b""
        partial_since = 0.0
        while True:
            timeout = max(0.0, partial_since + PARTIAL_FLUSH_S - loop.time()) if partial else None
            try:
                chunk = await asyncio.wait_for(stream.read(PARTIAL_FLUSH_BYTES), timeout)
            except asyncio.TimeoutError:
                self._log_line(partial)
                partial = b""
                continue
            if not chunk:
                break
            collected += chunk
            had_partial = bool(partial)
            *lines, rest = (partial + chunk).split(b"\n")
            for line in lines:
                self._log_line(line)
            if len(rest) >= PARTIAL_FLUSH_BYTES:
                self._log_line(rest)
                rest = b""
            if rest and (lines or not had_partial):
                partial_since = loop.time()
            partial = rest
        if partial:
            self._log_line(partial)
        self._report_skipped()
        return bytes(collected)

    def _log_line(self, raw: bytes) -> None:
        text = _ANSI.sub("", raw.decode(errors="replace")).rstrip()
        if not text.strip():
            return
        now = asyncio.get_running_loop().time()
        if now - self._window_start >= 1.0:
            self._report_skipped()
            self._window_start = now
            self._window_lines = 0
        if self._window_lines >= LOG_LINES_PER_S:
            self._skipped += 1
            return
        self._window_lines += 1
        self._send(text[:LOG_LINE_CHARS])

    def _report_skipped(self) -> None:
        if self._skipped:
            self._send(f"{self._skipped} lines skipped")
            self._skipped = 0

    def _send(self, message: str) -> None:
        try:
            self._emit("log", self._step_id, stream="stderr", message=message)
        except Exception:
            pass

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

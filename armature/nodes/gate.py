from __future__ import annotations
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any
from armature.events.sign import canonical
from armature.runtime.templating import text_env
from armature.nodes.base import BaseNode
from armature.spec.models import Stage

# Set by the server's worker or a runner. The run keeps its checkpoint there and pauses at a gate instead of asking.
GATE_DIR_ENV = "ARMATURE_GATE_DIR"
GATE_FILE = "gate.json"
CHECKPOINT_FILE = "checkpoint.json"


class GateSuspended(BaseException):
    """The run reached a human gate and pauses for a decision made on the server.

    A BaseException, so no retry or fail_as_value handler turns the pause into a stage failure.
    """

    def __init__(self, stage: Stage, key: str, present: str):
        super().__init__(f"Paused at gate {stage.id}")
        self.stage = stage
        self.key = key
        self.present = present


def gate_message(stage: Stage, context: dict[str, Any]) -> str:
    message = stage.present or "Review required."
    if "{{" in message or "{%" in message:
        message = text_env().from_string(message).render(**context)
    return message


def gate_digest(request: dict[str, Any]) -> str:
    """What a signed run_suspend vouches for in the gate request: its checkpoint and its question."""
    return hashlib.sha256(canonical({"checkpoint": request.get("checkpoint"),
                                     "present": request.get("present")})).hexdigest()


def kill_descendants() -> None:
    """Kill every process this one started that is still running, so none can touch the gate request it writes."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10,
                                 check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        pid, _, ppid = line.strip().partition(" ")
        if pid.isdigit() and ppid.strip().isdigit():
            children.setdefault(int(ppid.strip()), []).append(int(pid))
    stack, found = [os.getpid()], []
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    for pid in found:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def write_gate(directory: Path, request: dict[str, Any]) -> None:
    """Write the gate request beside the checkpoint, readable by the runner's group as the bundle's files are."""
    path, tmp = directory / GATE_FILE, directory / f"{GATE_FILE}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        os.fchmod(out.fileno(), 0o640)
        json.dump(request, out, default=str)
    tmp.rename(path)


def read_gate(directory: Path) -> dict[str, Any] | None:
    """The gate request a run that paused left in ``directory``, or None."""
    try:
        request = json.loads((directory / GATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return request if isinstance(request, dict) else None


class HumanGateNode(BaseNode):
    def __init__(self, stage: Stage):
        self._stage = stage

    async def execute(self, context: dict[str, Any]) -> Any:
        message = gate_message(self._stage, context)

        print(f"\n{'='*60}")
        print(f"HUMAN APPROVAL REQUIRED")
        print(f"{'='*60}")
        print(message)
        print(f"{'='*60}")

        response = input("Approve? [yes/no/feedback]: ").strip().lower()
        if response in ("yes", "y", "approve"):
            return {"approved": True, "feedback": None}
        else:
            feedback = input("Enter feedback (press Enter to skip): ").strip()
            return {"approved": False, "feedback": feedback or response}

from __future__ import annotations
from enum import Enum


class PermissionLevel(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE = "workspace"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


_READONLY_PREFIXES = (
    "ls", "cat", "grep", "find", "head", "tail", "wc", "echo",
    "pwd", "which", "env", "printenv",
    "git log", "git diff", "git status", "git show",
)
_DESTRUCTIVE_PREFIXES = (
    "rm", "sudo", "shutdown", "reboot", "mkfs", "dd ",
    "chmod 777", "chown", "kill", "killall", "pkill",
)


def classify_shell_command(cmd: str) -> PermissionLevel:
    stripped = cmd.strip()
    for prefix in _DESTRUCTIVE_PREFIXES:
        if stripped.startswith(prefix):
            return PermissionLevel.DESTRUCTIVE
    for prefix in _READONLY_PREFIXES:
        if stripped.startswith(prefix):
            return PermissionLevel.READ_ONLY
    return PermissionLevel.WORKSPACE


def requires_approval(level: PermissionLevel) -> bool:
    return level == PermissionLevel.DESTRUCTIVE

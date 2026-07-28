from __future__ import annotations
import os
import re
import shlex
from enum import Enum


class PermissionLevel(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE = "workspace"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


class Reversibility(str, Enum):
    FULL = "full"       # reads, creates with known rollback path
    PARTIAL = "partial" # overwrites, modifications
    NONE = "none"       # deletes, external sends, payments


# Bare command names (matched against the basename of the command token, so
# `/bin/rm` and `rm` are equivalent). The legacy prefix list matched `rm`,
# `sudo`, etc. as raw string prefixes, which any indirection defeated.
_READONLY_COMMANDS = frozenset({
    "ls", "cat", "grep", "find", "head", "tail", "wc", "echo",
    "pwd", "which", "env", "printenv",
})
_DESTRUCTIVE_COMMANDS = frozenset({
    "rm", "sudo", "shutdown", "reboot", "mkfs", "chown",
    "kill", "killall", "pkill", "dd",
})
_GIT_READONLY_SUBCOMMANDS = frozenset({"log", "diff", "status", "show"})
# Shells whose `-c` argument is itself a command to classify recursively.
_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})

# Split a command line into independent subcommands on shell control operators.
# This is a heuristic, not a full shell parser — it does not understand quoting
# around the operators themselves, but shlex tokenization of each subcommand
# still catches the common chaining bypasses (`;`, `&&`, `||`, `|`).
_CHAIN_SPLIT = re.compile(r"\s*(?:;|&&|\|\||\|)\s*")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Fail-safe recursion cap for `sh -c "sh -c '...'"`-style nesting. If a command
# nests deeper than this, treat it as destructive rather than risk missing a
# buried destructive call.
_MAX_INDIRECTION_DEPTH = 4


def _classify_token(command_token: str, rest: list[str]) -> PermissionLevel:
    """Classify a single, already-tokenized command (no chain operators)."""
    base = os.path.basename(command_token)

    if base in _DESTRUCTIVE_COMMANDS:
        return PermissionLevel.DESTRUCTIVE
    # `chmod 777 ...` is destructive; plain `chmod` is not.
    if base == "chmod" and "777" in rest:
        return PermissionLevel.DESTRUCTIVE
    if base in _READONLY_COMMANDS:
        return PermissionLevel.READ_ONLY
    if base == "git" and rest and rest[0] in _GIT_READONLY_SUBCOMMANDS:
        return PermissionLevel.READ_ONLY
    # `sh -c "<cmd>"` indirection: classify the inner command recursively.
    if base in _SHELL_WRAPPERS and "-c" in rest:
        idx = rest.index("-c")
        if idx + 1 < len(rest):
            return _classify_string(rest[idx + 1], depth=1)
    return PermissionLevel.WORKSPACE


def _classify_string(cmd: str, depth: int = 0) -> PermissionLevel:
    """Classify a raw command string, inspecting every subcommand in a chain."""
    if not cmd or not cmd.strip():
        return PermissionLevel.WORKSPACE
    if depth > _MAX_INDIRECTION_DEPTH:
        # Fail safe: deeply nested indirection is suspicious; treat as destructive.
        return PermissionLevel.DESTRUCTIVE

    worst = PermissionLevel.READ_ONLY  # lowest rank; upgraded as worse subcommands appear
    for part in _CHAIN_SPLIT.split(cmd.strip()):
        if not part.strip():
            continue
        level = _classify_subcommand(part, depth)
        if level == PermissionLevel.DESTRUCTIVE:
            return PermissionLevel.DESTRUCTIVE  # short-circuit
        if level == PermissionLevel.WORKSPACE:
            worst = PermissionLevel.WORKSPACE
    return worst


def _classify_subcommand(cmd: str, depth: int) -> PermissionLevel:
    """Tokenize one subcommand, strip leading env assignments, classify it."""
    try:
        tokens = shlex.split(cmd, comments=True)
    except ValueError:
        # Unbalanced quotes / unparseable — fall back to a conservative word
        # check so a malformed `rm -rf /` is not silently allowed.
        return _legacy_destructive_check(cmd)
    # Strip leading env assignments: `FOO=bar rm ...` -> `rm ...`
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return PermissionLevel.WORKSPACE
    return _classify_token(tokens[0], tokens[1:])


def _legacy_destructive_check(cmd: str) -> PermissionLevel:
    """Word-boundary fallback used when shlex cannot parse a subcommand."""
    for bad in _DESTRUCTIVE_COMMANDS:
        if re.search(rf"(^|[\s/&|;])({re.escape(bad)})(\s|$)", cmd):
            return PermissionLevel.DESTRUCTIVE
    return PermissionLevel.WORKSPACE


def classify_shell_command(cmd: str) -> PermissionLevel:
    """Classify a shell command string into a :class:`PermissionLevel`.

    Inspects every subcommand in a chain (``;``, ``&&``, ``||``, ``|``), strips
    leading environment assignments (``FOO=bar rm``), normalizes absolute command
    paths to their basename (``/bin/rm`` -> ``rm``), and recurses one level into
    ``sh -c "<cmd>"`` / ``bash -c "<cmd>"`` indirection.

    This is a heuristic denylist backstop, not a complete shell parser. It cannot
    see through command substitution (``$(...)``, backticks), ``eval``, ``exec``,
    or ``find -exec``. The robust enforcement path is the spec's ``safety_rules``
    (now applied at dispatch time) and, for shell stages, running classification
    inside the sandbox. Treat this as defense-in-depth, not a primary control.
    """
    return _classify_string(cmd, depth=0)


def requires_approval(level: PermissionLevel) -> bool:
    return level == PermissionLevel.DESTRUCTIVE
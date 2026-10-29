"""The command lines and environments of the coding-agent CLIs an ``agent:`` stage runs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MODELS_ENV = "ARMATURE_AGENT_MODELS"
SKILLS_ENV = "ARMATURE_SKILLS_DIR"
ROLES_ENV = "ARMATURE_ROLES_DIR"
AGENT_USER_ENV = "ARMATURE_AGENT_USER"
AGENT_DIR_ENV = "ARMATURE_AGENT_DIR"
SAME_USER_ENV = "ARMATURE_AGENT_SAME_USER"
SESSIONS_ENV = "ARMATURE_AGENT_SESSIONS"
GIT_HOSTS_ENV = "ARMATURE_GIT_HOSTS"
PROJECT_REPO_ENV = "ARMATURE_PROJECT_REPO"
# Only the runner or worker sets these, so a setting or secret of the same name never reaches the run.
HOST_ONLY_ENV = (AGENT_USER_ENV, AGENT_DIR_ENV, SAME_USER_ENV, SESSIONS_ENV, SKILLS_ENV, ROLES_ENV)
FILE_TOOLS = ("Read", "Grep", "Glob")
GIT_READS = ("log", "diff", "show", "status", "blame", "ls-files", "merge-base", "rev-list", "cat-file")
# Claude Code asks, so a headless session is denied, before any of these reads outside the working and added dirs.
# No find or rg, whose -exec and --pre run programs. Glob and Grep cover them.
SHELL_READS = ("cd", "ls", "wc", "cat", "head", "tail", "grep")
READ_TOOLS = (*(f"{tool}(./**)" for tool in FILE_TOOLS), "Skill", *(f"Bash(git {cmd}:*)" for cmd in GIT_READS),
              *(f"Bash({cmd}:*)" for cmd in SHELL_READS))
WRITE_TOOLS = (*READ_TOOLS, "Bash", "Edit", "Write")
SECRET_READS = ("Read(~/.claude.json)", "Read(~/.claude/**)", "Read(~/.aws/**)", "Read(~/.ssh/**)",
                *(f"{tool}(//{path}/**)" for path in ("proc", "sys", "etc/armature") for tool in FILE_TOOLS))
# Git options that write files, run programs, or set config, which a read-only git command could otherwise carry.
GIT_ESCAPES = ("Bash(git *--output*)", "Bash(git *--out*)", "Bash(git *--no-index*)", "Bash(git *--ext-diff*)",
               "Bash(git *--ext-d*)", "Bash(git *-c *)")
READ_DENIED = ("Edit", "Write", "NotebookEdit", *SECRET_READS, *GIT_ESCAPES)
WRITE_DENIED = ("NotebookEdit", *SECRET_READS, *GIT_ESCAPES, "Edit(./.claude/**)", "Write(./.claude/**)",
                "Edit(./.git/**)", "Write(./.git/**)")
WRITE_ENV = {"BASH_DEFAULT_TIMEOUT_MS": "1800000", "BASH_MAX_TIMEOUT_MS": "7300000"}
CLAUDE_CREDENTIALS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION",
                      "AWS_REGION_NAME", "AWS_BEARER_TOKEN_BEDROCK")
CODEX_CREDENTIALS = ("OPENAI_API_KEY",)


@dataclass(frozen=True)
class Launch:
    model: str
    effort: str | None
    access: str
    allowed: list[str]
    disallowed: list[str]
    add_dirs: list[Path]
    config_dir: Path
    resume: str | None = None
    memory: Path | None = None


class Harness(Protocol):
    binary: str
    credentials: tuple[str, ...]

    def argv(self, launch: Launch) -> list[str]: ...

    def config_env(self, launch: Launch) -> dict[str, str]: ...


class ClaudeCode:
    binary = "claude"
    credentials = CLAUDE_CREDENTIALS

    def argv(self, launch: Launch) -> list[str]:
        # The prompt goes on stdin, so no rendered text ever reaches the command line.
        argv = [self.binary, "-p", "--model", launch.model, "--output-format", "stream-json", "--verbose",
                "--permission-mode", "manual", "--permission-prompts", "none",
                "--settings", str(launch.config_dir / "settings.json"), "--setting-sources", "user",
                "--strict-mcp-config", "--allowed-tools", *launch.allowed, "--disallowed-tools", *launch.disallowed]
        if launch.effort:
            argv += ["--effort", launch.effort]
        for directory in launch.add_dirs:
            argv += ["--add-dir", str(directory)]
        if launch.memory:
            argv += ["--append-system-prompt-file", str(launch.memory)]
        if launch.resume:
            argv += ["--resume", launch.resume]
        return argv

    def config_env(self, launch: Launch) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": str(launch.config_dir)}


class Codex:
    """Experimental: the flags follow codex exec's documentation and are not checked against a real CLI."""

    binary = "codex"
    credentials = CODEX_CREDENTIALS

    def argv(self, launch: Launch) -> list[str]:
        sandbox = "workspace-write" if launch.access == "write" else "read-only"
        argv = [self.binary, "exec", "--json", "--sandbox", sandbox, "-m", launch.model]
        if launch.effort:
            argv += ["-c", f"model_reasoning_effort={launch.effort}"]
        if launch.resume:
            argv += ["resume", launch.resume]
        return [*argv, "-"]

    def config_env(self, launch: Launch) -> dict[str, str]:
        return {"CODEX_HOME": str(launch.config_dir)}


HARNESSES: dict[str, Harness] = {"claude-code": ClaudeCode(), "codex": Codex()}


def tools(access: str, allowed: list[str] | None, disallowed: list[str],
          data_dirs: list[Path] = ()) -> tuple[list[str], list[str]]:
    """The allowed and denied tools for a session. A stage's own denials add to the access level's."""
    base_allowed, base_denied = (WRITE_TOOLS, WRITE_DENIED) if access == "write" else (READ_TOOLS, READ_DENIED)
    reads = [f"{tool}(/{directory}/**)" for directory in data_dirs for tool in FILE_TOOLS]
    return [*(base_allowed if allowed is None else allowed), *reads], [*base_denied, *disallowed]

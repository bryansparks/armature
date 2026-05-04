from __future__ import annotations
from pathlib import Path
from typing import Any
from armature.spec.models import Role, RoleType

_ROLE_PREAMBLES = {
    RoleType.WORKER: "You are a focused task executor. Produce structured output that matches the required schema exactly.",
    RoleType.ORCHESTRATOR: "You are coordinating a multi-step workflow. Plan carefully, delegate to appropriate tools, and track progress.",
    RoleType.JUDGE: "You are evaluating output quality. Assess carefully, score objectively, and identify specific issues.",
    RoleType.RESEARCHER: "You are gathering and synthesizing information. Search broadly, filter for credibility, and structure your findings.",
}


class PromptAssembler:
    def __init__(
        self,
        static_prefix: str = "",
        instruction_dirs: list[Path] | None = None,
    ):
        self._static_prefix = static_prefix
        self._instruction_dirs = instruction_dirs or []

    def _load_instruction_files(self) -> str:
        parts = []
        for directory in self._instruction_dirs:
            for filename in ("HARNESS.md", "CLAUDE.md", "AGENTS.md"):
                path = Path(directory) / filename
                if path.exists():
                    parts.append(path.read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)

    def build(
        self,
        role: Role,
        tools: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> str:
        sections = []

        if self._static_prefix:
            sections.append(self._static_prefix)

        instructions = self._load_instruction_files()
        if instructions:
            sections.append(instructions)

        sections.append(_ROLE_PREAMBLES.get(role.type, ""))

        sections.append(f"## Your Role\n{role.description}")

        if tools:
            tool_lines = "\n".join(f"- {t['name']}: {t['description']}" for t in tools)
            sections.append(f"## Available Tools\n{tool_lines}")

        if context:
            ctx_items = "\n".join(f"- {k}: {v}" for k, v in context.items() if v is not None)
            if ctx_items:
                sections.append(f"## Current Context\n{ctx_items}")

        return "\n\n".join(s for s in sections if s)

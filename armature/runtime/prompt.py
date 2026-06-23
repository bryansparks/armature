from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING, Any
from armature.spec.models import Role, RoleType

if TYPE_CHECKING:
    from armature.adapters.registry import ResolvedAdapter
    from armature.spec.models import Signature, SkillDef

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
        signature: "Signature | None" = None,
        output_schema: dict[str, Any] | None = None,
        examples: list[dict] | None = None,
        skills: "list[SkillDef]" = [],
        mission_block: str = "",
        active_adapters: "dict[str, ResolvedAdapter] | None" = None,
        omitted_skills: "set[str] | None" = None,
    ) -> str:
        import json as _json

        sections = []

        if mission_block:
            sections.append(mission_block)

        if self._static_prefix:
            sections.append(self._static_prefix)

        instructions = self._load_instruction_files()
        if instructions:
            sections.append(instructions)

        sections.append(_ROLE_PREAMBLES.get(role.type, ""))

        # Compute visible context once — used for both description rendering and the
        # ## Current Context section so both see exactly the same filtered key set.
        if signature and signature.input:
            visible = {k: v for k, v in context.items() if k in signature.input}
        else:
            visible = context

        sections.append(f"## Your Role\n{_render_description(role.description, visible)}")

        if tools:
            tool_lines = "\n".join(f"- {t['name']}: {t['description']}" for t in tools)
            sections.append(f"## Available Tools\n{tool_lines}")

        active_adapters = active_adapters or {}
        omitted_skills = omitted_skills or set()
        if skills:
            skill_parts = []
            for skill in skills:
                if skill.id in omitted_skills:
                    continue
                resolved = active_adapters.get(skill.id)
                if resolved is not None:
                    if skill.adapter and skill.adapter.inject_metadata:
                        skill_parts.append(
                            f"### {skill.description}\n"
                            f"*Active via adapter {resolved.metadata.name}@{resolved.metadata.version}*"
                        )
                    # When an adapter is active (and metadata injection is off),
                    # the full skill text is omitted to save context window.
                    continue
                body = skill.content
                if body is None and skill.path:
                    body = Path(skill.path).read_text(encoding="utf-8").strip()
                skill_parts.append(f"### {skill.description}\n{body}")
            if skill_parts:
                sections.append("## Skills\n" + "\n\n".join(skill_parts))

            if active_adapters:
                adapter_lines = []
                for skill_id, resolved in active_adapters.items():
                    meta = resolved.metadata
                    adapter_lines.append(
                        f"- {meta.name}@{meta.version} "
                        f"(base: {meta.base_model}, rank: {meta.rank})"
                    )
                sections.append("## Active Adapters\n" + "\n".join(adapter_lines))

        if examples:
            ex_parts = []
            for i, ex in enumerate(examples, 1):
                inp = _json.dumps(ex.get("inputs", {}), indent=2)
                out = _json.dumps(ex.get("outputs", {}), indent=2)
                ex_parts.append(f"### Example {i}\nInputs:\n{inp}\nOutputs:\n{out}")
            sections.append("## Examples\n" + "\n\n".join(ex_parts))

        if visible:
            ctx_items = "\n".join(f"- {k}: {v}" for k, v in visible.items() if v is not None)
            if ctx_items:
                sections.append(f"## Current Context\n{ctx_items}")

        if output_schema is not None:
            schema_json = _json.dumps(output_schema, indent=2)
            sections.append(
                f"## Required Output Format\n"
                f"Return a JSON object matching this schema exactly. "
                f"The top-level value must be an object {{}}, not an array [].\n\n"
                f"{schema_json}"
            )

        return "\n\n".join(s for s in sections if s)


def _render_description(description: str, context: dict[str, Any]) -> str:
    """Jinja2-render role.description with the filtered context.

    {{ var }} and {{ obj.field }} syntax in descriptions is resolved against
    the same context that appears in ## Current Context — making data injection
    unambiguous rather than relying on the LLM to correlate template syntax with
    the context JSON. Undefined variables silently render as empty string.
    Template errors never raise; the raw description is returned as a fallback.
    """
    if "{{" not in description:
        return description
    try:
        from jinja2 import ChainableUndefined, Environment, BaseLoader
        env = Environment(
            loader=BaseLoader(),
            variable_start_string="{{",
            variable_end_string="}}",
            undefined=ChainableUndefined,
        )
        return env.from_string(description).render(**context)
    except Exception:
        return description

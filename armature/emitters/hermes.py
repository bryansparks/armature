"""Hermes-agent bundle emitter for Armature HarnessSpec."""
from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from armature.runtime.dag import topological_order
from armature.spec.models import HarnessSpec, Role, Stage


class HermesEmitter:
    """Compile a HarnessSpec into a Hermes-agent bundle directory.

    Output layout::

        {output_dir}/{spec.name}/
        ├── cli-config.yaml   delegation config + MCP servers
        ├── AGENTS.md         orchestrator instructions (stage DAG as prose)
        └── skills/
            └── {role_id}.md  one file per unique role
    """

    def emit(self, spec: HarnessSpec, output_dir: Path) -> Path:
        """Write the Hermes-agent bundle and return the bundle directory path."""
        bundle_dir = output_dir / spec.name
        (bundle_dir / "skills").mkdir(parents=True, exist_ok=True)
        self._write_cli_config(spec, bundle_dir)
        self._write_agents_md(spec, bundle_dir)
        self._write_skills(spec, bundle_dir)
        return bundle_dir

    # ── Private helpers ───────────────────────────────────────────────────────

    def _write_cli_config(self, spec: HarnessSpec, bundle_dir: Path) -> None:
        delegation: dict = {
            "orchestrator_enabled": True,
            "max_iterations": spec.contracts.max_iterations,
            "max_concurrent_children": self._max_concurrent(spec),
            "max_spawn_depth": 1,
            "subagent_auto_approve": False,
        }
        if spec.model_tiers.frontier:
            delegation["model"] = spec.model_tiers.frontier.model
            delegation["provider"] = spec.model_tiers.frontier.provider

        config: dict = {"delegation": delegation}

        if spec.mcp_servers:
            servers: dict = {}
            for mcp in spec.mcp_servers:
                entry: dict = {}
                if mcp.transport == "stdio" and mcp.command:
                    entry["command"] = mcp.command
                    if mcp.args:
                        entry["args"] = list(mcp.args)
                elif mcp.url:
                    entry["url"] = mcp.url
                if mcp.env:
                    entry["env"] = dict(mcp.env)
                if mcp.headers:
                    entry["headers"] = dict(mcp.headers)
                servers[mcp.name] = entry
            config["mcp"] = {"servers": servers}

        yaml = YAML()
        yaml.default_flow_style = False
        with open(bundle_dir / "cli-config.yaml", "w") as fh:
            yaml.dump(config, fh)

    def _write_agents_md(self, spec: HarnessSpec, bundle_dir: Path) -> None:
        roles = self._collect_roles(spec)
        stage_map = {s.id: s for s in spec.stages}
        deps = {s.id: list(s.depends_on) for s in spec.stages}
        ordered_ids = topological_order(deps)

        lines: list[str] = [
            f"# {spec.name}",
            "",
            spec.description,
            "",
            "## Workflow",
            "",
            "Execute the following stages in dependency order:",
            "",
        ]

        for i, stage_id in enumerate(ordered_ids, 1):
            if stage_id not in stage_map:
                continue
            stage = stage_map[stage_id]
            skill_id = self._stage_skill_id(stage, roles)
            deps_str = (
                ", ".join(f"`{d}`" for d in stage.depends_on)
                if stage.depends_on
                else "none"
            )
            lines += [
                f"### {i}. {stage.id}",
                "",
                f"- **Skill**: `{skill_id}`",
                f"- **Depends on**: {deps_str}",
                f"- **Output**: {stage.output_mode.value}",
                "",
            ]

        lines += [
            "## Execution Instructions",
            "",
            "1. Spawn subagents for each stage using the skill listed.",
            "2. Pass upstream stage outputs as context to dependent stages.",
            "3. Collect all stage outputs when the workflow completes.",
        ]

        (bundle_dir / "AGENTS.md").write_text("\n".join(lines) + "\n")

    def _write_skills(self, spec: HarnessSpec, bundle_dir: Path) -> None:
        roles = self._collect_roles(spec)
        skills_dir = bundle_dir / "skills"
        for role_id, role in roles.items():
            self._write_skill(role_id, role, skills_dir)

    def _write_skill(self, role_id: str, role: Role, skills_dir: Path) -> None:
        frontmatter_lines = [
            "---",
            f"name: {role.name}",
            f"description: {role.description[:120].strip()}",
        ]
        if role.tools:
            frontmatter_lines.append("tools:")
            for tool in role.tools:
                frontmatter_lines.append(f"  - {tool}")
        frontmatter_lines.append("---")

        body_lines: list[str] = ["", role.description]
        if role.tools:
            body_lines += ["", "## Available Tools", ""]
            for tool in role.tools:
                body_lines.append(f"- {tool}")

        content = "\n".join(frontmatter_lines) + "\n" + "\n".join(body_lines) + "\n"
        (skills_dir / f"{role_id}.md").write_text(content)

    def _collect_roles(self, spec: HarnessSpec) -> dict[str, Role]:
        """Build a {role_id: Role} map from spec.roles and inline stage roles."""
        roles: dict[str, Role] = dict(spec.roles)
        for stage in spec.stages:
            if stage.role is not None:
                role_id = self._role_id_for(stage.role, roles)
                if role_id not in roles:
                    roles[role_id] = stage.role
        return roles

    def _role_id_for(self, role: Role, existing: dict[str, Role]) -> str:
        """Return the existing key for a role (matched by name) or derive a new one."""
        for k, v in existing.items():
            if v.name == role.name:
                return k
        return role.name.lower().replace(" ", "_").replace("-", "_")

    def _stage_skill_id(self, stage: Stage, roles: dict[str, Role]) -> str:
        if stage.role is None:
            return "unknown"
        return self._role_id_for(stage.role, roles)

    def _max_concurrent(self, spec: HarnessSpec) -> int:
        root_count = sum(1 for s in spec.stages if not s.depends_on)
        return max(1, min(root_count, 3))

"""Tests for the Hermes-agent bundle emitter."""
from pathlib import Path
import pytest
from ruamel.yaml import YAML

from armature.spec.models import (
    Contract,
    HarnessSpec,
    MCPServerConfig,
    ModelTierConfig,
    ModelTiers,
    OutputMode,
    Role,
    RoleType,
    Stage,
)
from armature.emitters.hermes import HermesEmitter

_yaml = YAML()


def make_spec(
    name: str = "test-workflow",
    with_mcp: bool = True,
    contracts: Contract | None = None,
) -> HarnessSpec:
    researcher = Role(
        name="Researcher",
        type=RoleType.RESEARCHER,
        description="You are a research specialist who gathers information.",
        tools=["web_search", "read_file"],
    )
    writer = Role(
        name="Writer",
        type=RoleType.WORKER,
        description="You are a technical writer who produces clear documentation.",
        tools=["write_file"],
    )
    mcp_servers = (
        [MCPServerConfig(
            name="github",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": "$GITHUB_TOKEN"},
        )]
        if with_mcp else []
    )
    return HarnessSpec(
        name=name,
        description="A test research and writing workflow",
        stages=[
            Stage(id="research", role=researcher, depends_on=[]),
            Stage(id="write", role=writer, depends_on=["research"]),
        ],
        model_tiers=ModelTiers(
            small=ModelTierConfig(provider="anthropic", model="claude-haiku-4-5-20251001"),
            frontier=ModelTierConfig(provider="anthropic", model="claude-sonnet-4-6"),
        ),
        mcp_servers=mcp_servers,
        contracts=contracts or Contract(),
    )


# ── Bundle structure ──────────────────────────────────────────────────────────

def test_emit_creates_bundle_structure(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(), tmp_path)
    assert bundle_dir.is_dir()
    assert (bundle_dir / "cli-config.yaml").is_file()
    assert (bundle_dir / "AGENTS.md").is_file()
    assert (bundle_dir / "skills").is_dir()


def test_bundle_dir_is_named_after_spec(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(name="my-pipeline"), tmp_path)
    assert bundle_dir.name == "my-pipeline"


# ── Skill files ───────────────────────────────────────────────────────────────

def test_roles_become_skill_files(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(), tmp_path)
    skill_files = list((bundle_dir / "skills").glob("*.md"))
    assert len(skill_files) == 2
    names = {f.stem for f in skill_files}
    assert "researcher" in names
    assert "writer" in names


def test_skill_file_has_yaml_frontmatter(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(), tmp_path)
    content = (bundle_dir / "skills" / "researcher.md").read_text()
    assert content.startswith("---\n")
    parts = content.split("---\n", 2)
    assert len(parts) >= 3
    frontmatter = _yaml.load(parts[1])
    assert "name" in frontmatter
    assert "description" in frontmatter


def test_skill_body_contains_role_description(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(), tmp_path)
    content = (bundle_dir / "skills" / "researcher.md").read_text()
    assert "You are a research specialist who gathers information." in content


def test_skill_tools_in_frontmatter(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(), tmp_path)
    content = (bundle_dir / "skills" / "researcher.md").read_text()
    parts = content.split("---\n", 2)
    frontmatter = _yaml.load(parts[1])
    assert "tools" in frontmatter
    assert "web_search" in frontmatter["tools"]
    assert "read_file" in frontmatter["tools"]


def test_duplicate_roles_emit_single_skill(tmp_path):
    role = Role(name="Worker", type=RoleType.WORKER, description="Does work.", tools=[])
    spec = HarnessSpec(
        name="dup-test",
        stages=[
            Stage(id="step1", role=role, depends_on=[]),
            Stage(id="step2", role=role, depends_on=["step1"]),
        ],
    )
    bundle_dir = HermesEmitter().emit(spec, tmp_path)
    skill_files = list((bundle_dir / "skills").glob("*.md"))
    assert len(skill_files) == 1


# ── cli-config.yaml ───────────────────────────────────────────────────────────

def test_cli_config_uses_frontier_model(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(), tmp_path)
    config = _yaml.load((bundle_dir / "cli-config.yaml").read_text())
    assert config["delegation"]["model"] == "claude-sonnet-4-6"
    assert config["delegation"]["provider"] == "anthropic"


def test_cli_config_orchestrator_always_enabled(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(), tmp_path)
    config = _yaml.load((bundle_dir / "cli-config.yaml").read_text())
    assert config["delegation"]["orchestrator_enabled"] is True


def test_cli_config_delegation_limits(tmp_path):
    spec = make_spec(contracts=Contract(max_iterations=42))
    bundle_dir = HermesEmitter().emit(spec, tmp_path)
    config = _yaml.load((bundle_dir / "cli-config.yaml").read_text())
    assert config["delegation"]["max_iterations"] == 42


def test_mcp_servers_in_cli_config(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(with_mcp=True), tmp_path)
    config = _yaml.load((bundle_dir / "cli-config.yaml").read_text())
    assert "mcp" in config
    assert "servers" in config["mcp"]
    assert "github" in config["mcp"]["servers"]
    server = config["mcp"]["servers"]["github"]
    assert server["command"] == "npx"


def test_no_mcp_servers_omits_mcp_section(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(with_mcp=False), tmp_path)
    config = _yaml.load((bundle_dir / "cli-config.yaml").read_text())
    assert "mcp" not in config


# ── AGENTS.md ─────────────────────────────────────────────────────────────────

def test_agents_md_stages_in_topo_order(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(), tmp_path)
    content = (bundle_dir / "AGENTS.md").read_text()
    research_pos = content.index("research")
    write_pos = content.index("write")
    assert research_pos < write_pos


def test_agents_md_dependency_references(tmp_path):
    bundle_dir = HermesEmitter().emit(make_spec(), tmp_path)
    content = (bundle_dir / "AGENTS.md").read_text()
    # The 'write' stage depends on 'research' — must be referenced
    assert "research" in content
    assert "write" in content


# ── CLI integration ───────────────────────────────────────────────────────────

def test_cli_export_command(tmp_path):
    from typer.testing import CliRunner
    from armature.cli import app

    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(
        "name: cli-test\n"
        "version: '1.0'\n"
        "description: CLI test workflow\n"
        "stages:\n"
        "  - id: step1\n"
        "    role:\n"
        "      name: Worker\n"
        "      type: worker\n"
        "      description: Do some work\n"
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = CliRunner().invoke(
        app, ["export", str(spec_file), "--target", "hermes", "--output", str(output_dir)]
    )

    assert result.exit_code == 0, result.output
    bundle_dir = output_dir / "cli-test"
    assert bundle_dir.is_dir()
    assert (bundle_dir / "AGENTS.md").is_file()
    assert (bundle_dir / "cli-config.yaml").is_file()

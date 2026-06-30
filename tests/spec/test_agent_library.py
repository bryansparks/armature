"""Tests for the agent_library + Stage.agent feature.

agent_library lets a spec reference pre-built agent bundles instead of inlining
a role. Resolution happens in loader._resolve_agent_references at load time:
  - the bundle's role is copied onto the stage
  - the bundle's skill_library is merged into the spec's skill_library
  - stage.agent is cleared to None after resolution
"""
import pytest
from pathlib import Path
from armature.spec.loader import load_spec, _resolve_agent_references
from armature.spec.models import (
    AgentRef, CompiledAgent, HarnessSpec, Role, RoleType, SkillDef, Stage,
)
from armature.spec.validator import validate_spec


# ─── helpers ──────────────────────────────────────────────────────────────────

def _minimal_spec(**kwargs) -> HarnessSpec:
    defaults = dict(
        name="test",
        version="1.0",
        stages=[],
    )
    defaults.update(kwargs)
    return HarnessSpec.model_validate(defaults)


def _echo_role() -> Role:
    return Role(name="Echo", type=RoleType.WORKER, description="echo {{ topic }}")


def _echo_agent() -> CompiledAgent:
    return CompiledAgent(role=_echo_role())


# ─── model tests ──────────────────────────────────────────────────────────────

def test_agentref_model():
    ref = AgentRef(path="agents/echo/agent.yaml")
    assert ref.path == "agents/echo/agent.yaml"
    assert ref.version is None


def test_agentref_extra_allowed():
    ref = AgentRef(path="agents/echo/agent.yaml", tags=["v1", "stable"])
    assert ref.path == "agents/echo/agent.yaml"


def test_compiled_agent_model():
    agent = _echo_agent()
    assert agent.role.name == "Echo"
    assert agent.skill_library == {}


def test_compiled_agent_with_skills():
    agent = CompiledAgent(
        role=_echo_role(),
        skill_library={
            "plain": SkillDef(id="plain", description="plain language", content="keep it short"),
        },
    )
    assert "plain" in agent.skill_library


def test_role_extra_allowed():
    role = Role(name="X", type=RoleType.WORKER, description="d", author="alice")
    assert role.name == "X"


def test_skilldef_extra_allowed():
    sd = SkillDef(id="s", description="d", content="c", version="1.0")
    assert sd.id == "s"


def test_harness_spec_has_agent_library():
    spec = _minimal_spec(
        stages=[{"id": "s1", "role": {"name": "W", "type": "worker", "description": "d"}, "depends_on": []}],
    )
    assert spec.agent_library == {}


def test_stage_has_agent_field():
    stage = Stage(id="s1", agent="echo")
    assert stage.agent == "echo"
    assert stage.role is None


# ─── resolution tests ─────────────────────────────────────────────────────────

def test_resolve_copies_role_and_clears_agent(tmp_path):
    bundle_dir = tmp_path / "agents" / "echo"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "agent.yaml").write_text(
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
    )

    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: t\nversion: '1.0'\n"
        "agent_library:\n  echo:\n    path: agents/echo/agent.yaml\n"
        "stages:\n  - id: s1\n    agent: echo\n    depends_on: []\n"
    )
    spec = load_spec(spec_file)

    stage = spec.stages[0]
    assert stage.role is not None
    assert stage.role.name == "Echo"
    assert stage.agent is None  # cleared after resolution


def test_resolve_merges_skills(tmp_path):
    bundle_dir = tmp_path / "agents" / "echo"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "agent.yaml").write_text(
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "skill_library:\n"
        "  plain:\n    id: plain\n    description: plain\n    content: keep it short\n"
    )

    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: t\nversion: '1.0'\n"
        "agent_library:\n  echo:\n    path: agents/echo/agent.yaml\n"
        "stages:\n  - id: s1\n    agent: echo\n    depends_on: []\n"
    )
    spec = load_spec(spec_file)

    assert "plain" in spec.skill_library
    assert spec.skill_library["plain"].content == "keep it short"


def test_resolve_skill_path_normalized_to_absolute(tmp_path):
    bundle_dir = tmp_path / "agents" / "echo"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tips.md").write_text("be concise")
    (bundle_dir / "agent.yaml").write_text(
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "skill_library:\n"
        "  tips:\n    id: tips\n    description: writing tips\n    path: tips.md\n"
    )

    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: t\nversion: '1.0'\n"
        "agent_library:\n  echo:\n    path: agents/echo/agent.yaml\n"
        "stages:\n  - id: s1\n    agent: echo\n    depends_on: []\n"
    )
    spec = load_spec(spec_file)

    skill = spec.skill_library["tips"]
    assert Path(skill.path).is_absolute()
    assert Path(skill.path).exists()


def test_resolve_existing_skill_wins(tmp_path):
    bundle_dir = tmp_path / "agents" / "echo"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "agent.yaml").write_text(
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "skill_library:\n"
        "  plain:\n    id: plain\n    description: from bundle\n    content: bundle version\n"
    )

    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: t\nversion: '1.0'\n"
        "agent_library:\n  echo:\n    path: agents/echo/agent.yaml\n"
        "skill_library:\n"
        "  plain:\n    id: plain\n    description: from spec\n    content: spec version\n"
        "stages:\n  - id: s1\n    agent: echo\n    depends_on: []\n"
    )
    spec = load_spec(spec_file)

    assert spec.skill_library["plain"].content == "spec version"


def test_resolve_no_agent_library_is_noop(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: t\nversion: '1.0'\n"
        "stages:\n  - id: s1\n    role:\n      name: W\n      type: worker\n      description: d\n    depends_on: []\n"
    )
    spec = load_spec(spec_file)
    assert spec.agent_library == {}
    assert spec.stages[0].role is not None


def test_resolve_unknown_agent_raises(tmp_path):
    bundle_dir = tmp_path / "agents" / "echo"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "agent.yaml").write_text(
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
    )

    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: t\nversion: '1.0'\n"
        "agent_library:\n  echo:\n    path: agents/echo/agent.yaml\n"
        "stages:\n  - id: s1\n    agent: typo_agent\n    depends_on: []\n"
    )
    with pytest.raises(ValueError, match="unknown agent 'typo_agent'"):
        load_spec(spec_file)


def test_resolve_missing_bundle_file_raises(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: t\nversion: '1.0'\n"
        "agent_library:\n  echo:\n    path: agents/missing/agent.yaml\n"
        "stages:\n  - id: s1\n    agent: echo\n    depends_on: []\n"
    )
    with pytest.raises(FileNotFoundError, match="Agent bundle not found"):
        load_spec(spec_file)


# ─── validator tests ──────────────────────────────────────────────────────────

def test_validator_accepts_agent_as_execution_type():
    stage = Stage(id="s1", agent="echo")
    spec = _minimal_spec(
        agent_library={"echo": AgentRef(path="agents/echo/agent.yaml")},
        stages=[stage],
    )
    errors = validate_spec(spec, strict=False)
    no_exec_errors = [e for e in errors if e.code == "NO_EXECUTION_TYPE"]
    assert not no_exec_errors, f"Unexpected NO_EXECUTION_TYPE for agent stage: {no_exec_errors}"


def test_validator_still_flags_empty_stage():
    stage = Stage(id="s1")
    spec = _minimal_spec(stages=[stage])
    errors = validate_spec(spec, strict=False)
    assert any(e.code == "NO_EXECUTION_TYPE" for e in errors)


# ─── backward-compat ──────────────────────────────────────────────────────────

def test_existing_spec_without_agent_library_unchanged(tmp_path):
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: t\nversion: '1.0'\n"
        "stages:\n  - id: s1\n    role:\n      name: W\n      type: worker\n      description: d\n    depends_on: []\n"
    )
    spec = load_spec(spec_file)
    assert spec.stages[0].role.name == "W"
    assert spec.stages[0].agent is None


def test_example_fixture_loads(tmp_path):
    example = Path(__file__).parent.parent.parent / "examples" / "12_agent_reference.yml"
    assert example.exists(), "Example fixture is missing"
    spec = load_spec(example)
    assert spec.name == "agent-reference-demo"
    stage = spec.stages[0]
    assert stage.role is not None
    assert stage.role.name == "Echo"
    assert stage.agent is None
    assert "plain_language" in spec.skill_library


# ─── loader merge: agent block rules are a non-overridable floor ──────────────

def _write_agent(tmp_path, body):
    d = tmp_path / "agents" / "echo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.yaml").write_text(body, encoding="utf-8")


def _wf(tmp_path, extra=""):
    (tmp_path / "wf.yaml").write_text(
        "name: t\nversion: '1.0'\n"
        "agent_library:\n  echo:\n    path: agents/echo/agent.yaml\n"
        f"{extra}"
        "stages:\n  - id: s1\n    agent: echo\n    depends_on: []\n",
        encoding="utf-8",
    )
    return tmp_path / "wf.yaml"


def test_resolve_merges_agent_block_rules(tmp_path):
    _write_agent(tmp_path,
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "safety_rules:\n  - tool: merge_pr\n    condition: null\n    action: block\n    message: forbidden\n")
    spec = load_spec(_wf(tmp_path))
    rules = spec.safety_rules
    assert any(r.tool == "merge_pr" and r.action == "block" for r in rules)


def test_resolve_agent_block_drops_workflow_allow(tmp_path):
    _write_agent(tmp_path,
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "safety_rules:\n  - tool: merge_pr\n    condition: null\n    action: block\n    message: forbidden\n")
    spec = load_spec(_wf(tmp_path, extra=
        "safety_rules:\n  - tool: merge_pr\n    condition:\n      field: cmd\n      op: contains\n      value: x\n    action: allow\n    message: ok\n"))
    rules = spec.safety_rules
    assert not any(r.tool == "merge_pr" and r.action == "allow" for r in rules)
    assert any(r.tool == "merge_pr" and r.action == "block" for r in rules)


def test_resolve_agent_block_ordered_before_surviving_rules(tmp_path):
    _write_agent(tmp_path,
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "safety_rules:\n  - tool: merge_pr\n    condition: null\n    action: block\n    message: forbidden\n")
    spec = load_spec(_wf(tmp_path, extra=
        "safety_rules:\n  - tool: merge_pr\n    condition:\n      field: cmd\n      op: contains\n      value: x\n    action: warn\n    message: be careful\n"))
    rules = spec.safety_rules
    block_idx = next(i for i, r in enumerate(rules) if r.tool == "merge_pr" and r.action == "block")
    warn_idx = next(i for i, r in enumerate(rules) if r.tool == "merge_pr" and r.action == "warn")
    assert block_idx < warn_idx


def test_resolve_keeps_unconditional_workflow_block_dedups(tmp_path):
    _write_agent(tmp_path,
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "safety_rules:\n  - tool: merge_pr\n    condition: null\n    action: block\n    message: from bundle\n")
    spec = load_spec(_wf(tmp_path, extra=
        "safety_rules:\n  - tool: merge_pr\n    condition: null\n    action: block\n    message: from wf\n"))
    blocks = [r for r in spec.safety_rules if r.tool == "merge_pr" and r.action == "block"]
    assert len(blocks) == 1  # deduped by (tool, condition)


def test_resolve_dominates_conditional_workflow_block(tmp_path):
    _write_agent(tmp_path,
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "safety_rules:\n  - tool: merge_pr\n    condition: null\n    action: block\n    message: from bundle\n")
    spec = load_spec(_wf(tmp_path, extra=
        "safety_rules:\n  - tool: merge_pr\n    condition:\n      field: cmd\n      op: contains\n      value: force\n    action: block\n    message: from wf\n"))
    rules = spec.safety_rules
    blocks = [r for r in rules if r.tool == "merge_pr" and r.action == "block"]
    assert len(blocks) == 2  # both: unconditional (agent) + conditional (workflow)
    # agent's unconditional block must come first
    assert blocks[0].condition is None


def test_resolve_agent_no_safety_rules_leaves_spec_untouched(tmp_path):
    _write_agent(tmp_path, "role:\n  name: Echo\n  type: worker\n  description: hi\n")
    spec = load_spec(_wf(tmp_path, extra=
        "safety_rules:\n  - tool: x\n    condition: null\n    action: warn\n    message: m\n"))
    assert [r.tool for r in spec.safety_rules] == ["x"]


def test_resolve_agent_safety_idempotent_across_stages(tmp_path):
    _write_agent(tmp_path,
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "safety_rules:\n  - tool: merge_pr\n    condition: null\n    action: block\n    message: forbidden\n")
    (tmp_path / "wf.yaml").write_text(
        "name: t\nversion: '1.0'\n"
        "agent_library:\n  echo:\n    path: agents/echo/agent.yaml\n"
        "stages:\n  - id: s1\n    agent: echo\n    depends_on: []\n"
        "  - id: s2\n    agent: echo\n    depends_on: [s1]\n", encoding="utf-8")
    spec = load_spec(tmp_path / "wf.yaml")
    blocks = [r for r in spec.safety_rules if r.tool == "merge_pr" and r.action == "block"]
    assert len(blocks) == 1  # not duplicated across two references


def test_resolve_subagent_spec_inherits_safety(tmp_path):
    """Item #2: a child subagent_spec workflow referencing the agent via
    agent_library also gets the block rules merged at load."""
    _write_agent(tmp_path,
        "role:\n  name: Echo\n  type: worker\n  description: hi\n"
        "safety_rules:\n  - tool: merge_pr\n    condition: null\n    action: block\n    message: forbidden\n")
    child = tmp_path / "child.yaml"
    child.write_text(
        "name: child\nversion: '1.0'\n"
        "agent_library:\n  echo:\n    path: agents/echo/agent.yaml\n"
        "stages:\n  - id: c1\n    agent: echo\n    depends_on: []\n", encoding="utf-8")
    spec = load_spec(child)
    assert any(r.tool == "merge_pr" and r.action == "block" for r in spec.safety_rules)

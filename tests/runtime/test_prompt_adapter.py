"""Adapter-aware prompt assembly tests."""
from __future__ import annotations

from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry
from armature.runtime.prompt import PromptAssembler
from armature.spec.models import Role, RoleType, SkillDef, SkillAdapterRef


def _register_adapter(tmp_path, name="tdd-workflow", version="3") -> AdapterRegistry:
    """Create a registry with one adapter version registered."""
    reg = AdapterRegistry(base_dir=tmp_path / "adapters")
    artifact_dir = tmp_path / f"src-{name}-{version}"
    artifact_dir.mkdir()
    (artifact_dir / "adapter_config.json").write_text("{}")
    meta = AdapterMetadata(
        name=name,
        version=version,
        base_model="qwen/qwen3.6-27b",
        rank=16,
        alpha=32,
    )
    reg.register(meta, artifact_dir)
    return reg


def _make_role() -> Role:
    return Role(name="worker", type=RoleType.WORKER, description="Execute tasks.")


def test_omits_skill_text_when_adapter_active(tmp_path):
    """When an adapter is active, the full skill text is removed from the prompt."""
    reg = _register_adapter(tmp_path)
    resolved = reg.get("tdd-workflow", "3")
    skill = SkillDef(
        id="tdd",
        description="Test-driven development workflow",
        content="Write a failing test first, then the minimal code to pass it.",
        adapter=SkillAdapterRef(name="tdd-workflow", version="3"),
    )
    assembler = PromptAssembler()
    prompt = assembler.build(
        role=_make_role(),
        tools=[],
        context={},
        skills=[skill],
        active_adapters={"tdd": resolved},
    )
    assert "## Skills" not in prompt
    assert "failing test" not in prompt
    assert "## Active Adapters" in prompt
    assert "tdd-workflow@3" in prompt
    assert "qwen/qwen3.6-27b" in prompt


def test_injects_metadata_tag_when_enabled(tmp_path):
    """When inject_metadata is true, a short tag replaces the full skill body."""
    reg = _register_adapter(tmp_path)
    resolved = reg.get("tdd-workflow", "3")
    skill = SkillDef(
        id="tdd",
        description="Test-driven development workflow",
        content="Write a failing test first, then the minimal code to pass it.",
        adapter=SkillAdapterRef(name="tdd-workflow", version="3", inject_metadata=True),
    )
    assembler = PromptAssembler()
    prompt = assembler.build(
        role=_make_role(),
        tools=[],
        context={},
        skills=[skill],
        active_adapters={"tdd": resolved},
    )
    assert "## Skills" in prompt
    assert "Active via adapter tdd-workflow@3" in prompt
    assert "failing test" not in prompt
    assert "## Active Adapters" in prompt


def test_keeps_skill_text_when_adapter_inactive(tmp_path):
    """An unresolvable adapter with fallback=text keeps the original skill text."""
    skill = SkillDef(
        id="tdd",
        description="Test-driven development workflow",
        content="Write a failing test first, then the minimal code to pass it.",
        adapter=SkillAdapterRef(name="missing-adapter"),
    )
    assembler = PromptAssembler()
    prompt = assembler.build(
        role=_make_role(),
        tools=[],
        context={},
        skills=[skill],
        active_adapters={},
    )
    assert "## Skills" in prompt
    assert "failing test" in prompt
    assert "## Active Adapters" not in prompt


def test_omits_skill_when_fallback_none():
    """fallback=none means the skill is dropped entirely when the adapter is inactive."""
    skill = SkillDef(
        id="tdd",
        description="Test-driven development workflow",
        content="Write a failing test first, then the minimal code to pass it.",
        adapter=SkillAdapterRef(name="missing-adapter", fallback="none"),
    )
    assembler = PromptAssembler()
    prompt = assembler.build(
        role=_make_role(),
        tools=[],
        context={},
        skills=[skill],
        active_adapters={},
        omitted_skills={"tdd"},
    )
    assert "## Skills" not in prompt
    assert "failing test" not in prompt
    assert "## Active Adapters" not in prompt


def test_skills_without_adapter_render_normally():
    """Plain text skills without an adapter block render exactly as before."""
    skill = SkillDef(
        id="plain",
        description="Plain skill",
        content="Plain text content.",
    )
    assembler = PromptAssembler()
    prompt = assembler.build(
        role=_make_role(),
        tools=[],
        context={},
        skills=[skill],
    )
    assert "## Skills" in prompt
    assert "Plain text content." in prompt
    assert "## Active Adapters" not in prompt


def test_active_adapters_section_lists_multiple_adapters(tmp_path):
    """Multiple active adapters appear in the ## Active Adapters section."""
    reg = _register_adapter(tmp_path, name="skill-a", version="1")
    _register_adapter(tmp_path, name="skill-b", version="2")
    # Both registrations share the default registry dir under tmp_path.
    resolved_a = reg.get("skill-a", "1")
    resolved_b = reg.get("skill-b", "2")
    skill_a = SkillDef(
        id="a",
        description="Skill A",
        content="A body",
        adapter=SkillAdapterRef(name="skill-a", version="1"),
    )
    skill_b = SkillDef(
        id="b",
        description="Skill B",
        content="B body",
        adapter=SkillAdapterRef(name="skill-b", version="2"),
    )
    assembler = PromptAssembler()
    prompt = assembler.build(
        role=_make_role(),
        tools=[],
        context={},
        skills=[skill_a, skill_b],
        active_adapters={"a": resolved_a, "b": resolved_b},
    )
    assert "## Skills" not in prompt
    assert "## Active Adapters" in prompt
    assert "skill-a@1" in prompt
    assert "skill-b@2" in prompt

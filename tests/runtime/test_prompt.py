import pytest
from pathlib import Path
from armature.runtime.prompt import PromptAssembler
from armature.spec.models import Role, RoleType, Signature

def test_static_prefix_included():
    assembler = PromptAssembler(static_prefix="You are an ELF harness agent.")
    role = Role(name="worker1", type=RoleType.WORKER, description="Do structured tasks.")
    prompt = assembler.build(role=role, tools=[], context={})
    assert "You are an ELF harness agent." in prompt

def test_role_description_included():
    assembler = PromptAssembler()
    role = Role(name="researcher1", type=RoleType.RESEARCHER, description="Search for information on the topic.")
    prompt = assembler.build(role=role, tools=[], context={})
    assert "Search for information" in prompt

def test_tools_included():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    tools = [{"name": "shell", "description": "Run shell commands"}]
    prompt = assembler.build(role=role, tools=tools, context={})
    assert "shell" in prompt

def test_instruction_file_injected(tmp_path):
    harness_md = tmp_path / "HARNESS.md"
    harness_md.write_text("Always verify outputs before returning.")
    assembler = PromptAssembler(instruction_dirs=[tmp_path])
    role = Role(name="w", type=RoleType.WORKER, description="work")
    prompt = assembler.build(role=role, tools=[], context={})
    assert "Always verify outputs" in prompt


def test_signature_input_filters_context():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    sig = Signature(input={"topic": "The topic to research"})
    context = {"topic": "AI safety", "internal_id": "abc123", "run_id": "xyz"}
    prompt = assembler.build(role=role, tools=[], context=context, signature=sig)
    assert "AI safety" in prompt
    assert "internal_id" not in prompt
    assert "abc123" not in prompt


def test_no_signature_passes_all_context():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    context = {"topic": "AI safety", "internal_id": "abc123"}
    prompt = assembler.build(role=role, tools=[], context=context)
    assert "AI safety" in prompt
    assert "internal_id" in prompt


def test_empty_signature_input_passes_all_context():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    sig = Signature(input={})
    context = {"topic": "AI safety", "internal_id": "abc123"}
    prompt = assembler.build(role=role, tools=[], context=context, signature=sig)
    assert "AI safety" in prompt
    assert "internal_id" in prompt


def test_signature_with_multiple_allowed_keys():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    sig = Signature(input={"topic": "Topic", "depth": "Research depth"})
    context = {"topic": "quantum", "depth": "deep", "user_session": "secret"}
    prompt = assembler.build(role=role, tools=[], context=context, signature=sig)
    assert "quantum" in prompt
    assert "deep" in prompt
    assert "user_session" not in prompt
    assert "secret" not in prompt


# ── Fix #3: description Jinja2 rendering ────────────────────────────────────

def test_description_renders_context_variable():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="Analyze the topic: {{ topic }}")
    prompt = assembler.build(role=role, tools=[], context={"topic": "AI safety"})
    assert "Analyze the topic: AI safety" in prompt


def test_description_renders_nested_field():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER,
                description="Summary: {{ research.content }}")
    context = {"research": {"content": "key finding here"}}
    prompt = assembler.build(role=role, tools=[], context=context)
    assert "Summary: key finding here" in prompt


def test_description_undefined_variable_renders_empty():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER,
                description="Topic is {{ topic }} and ref is {{ does_not_exist }}")
    prompt = assembler.build(role=role, tools=[], context={"topic": "ethics"})
    assert "Topic is ethics" in prompt
    assert "does_not_exist" not in prompt


def test_description_no_template_unchanged():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="Plain text description.")
    prompt = assembler.build(role=role, tools=[], context={"topic": "x"})
    assert "Plain text description." in prompt


def test_description_renders_only_signature_visible_vars():
    """Template vars filtered by signature.input — hidden context not injectable."""
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER,
                description="Topic: {{ topic }}. Secret: {{ internal_id }}")
    sig = Signature(input={"topic": "The topic"})
    context = {"topic": "AI safety", "internal_id": "HIDDEN"}
    prompt = assembler.build(role=role, tools=[], context=context, signature=sig)
    assert "AI safety" in prompt
    # internal_id is outside signature.input — renders as empty string
    assert "HIDDEN" not in prompt


# ── Bug 2: output_schema injected into system prompt ────────────────────────

def test_output_schema_injects_required_format_section():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    prompt = assembler.build(role=role, tools=[], context={}, output_schema=schema)
    assert "## Required Output Format" in prompt
    assert '"score"' in prompt
    assert "not an array" in prompt


def test_no_output_schema_omits_format_section():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    prompt = assembler.build(role=role, tools=[], context={})
    assert "Required Output Format" not in prompt


def test_output_schema_appears_after_context():
    """Schema section must come after ## Current Context, not before."""
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    schema = {"type": "object"}
    prompt = assembler.build(role=role, tools=[], context={"topic": "AI"}, output_schema=schema)
    ctx_pos = prompt.index("Current Context")
    schema_pos = prompt.index("Required Output Format")
    assert schema_pos > ctx_pos


def test_output_schema_contains_json():
    """Schema JSON is embedded verbatim so the model can read it."""
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    schema = {
        "type": "object",
        "required": ["findings", "confidence"],
        "properties": {
            "findings": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
    }
    prompt = assembler.build(role=role, tools=[], context={}, output_schema=schema)
    assert '"findings"' in prompt
    assert '"confidence"' in prompt
    assert '"required"' in prompt


# ── Phase 1-b: Skills system ─────────────────────────────────────────────────

def test_skilldef_model_exists():
    from armature.spec.models import SkillDef
    skill = SkillDef(id="search", description="Search the web", content="Use a search engine.")
    assert skill.id == "search"
    assert skill.content == "Use a search engine."


def test_skilldef_path_or_content_required():
    """SkillDef must have either path or content, not neither."""
    from armature.spec.models import SkillDef
    import pydantic
    with pytest.raises((pydantic.ValidationError, ValueError)):
        SkillDef(id="bad", description="no source")


def test_harness_spec_has_skill_library():
    from armature.spec.models import HarnessSpec
    spec = HarnessSpec(
        name="wf",
        stages=[],
        skill_library={"search": {"id": "search", "description": "desc", "content": "do it"}},
    )
    assert "search" in spec.skill_library


def test_build_with_skills_injects_skill_content():
    from armature.spec.models import SkillDef
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work", skills=["search"])
    skill = SkillDef(id="search", description="Search the web", content="Use DuckDuckGo to find facts.")
    prompt = assembler.build(role=role, tools=[], context={}, skills=[skill])
    assert "Use DuckDuckGo to find facts." in prompt


def test_build_with_skills_uses_skill_description_header():
    from armature.spec.models import SkillDef
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work", skills=["search"])
    skill = SkillDef(id="search", description="Search the web", content="Use DuckDuckGo.")
    prompt = assembler.build(role=role, tools=[], context={}, skills=[skill])
    assert "Search the web" in prompt


def test_build_with_no_skills_omits_skills_section():
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work")
    prompt = assembler.build(role=role, tools=[], context={}, skills=[])
    assert "## Skills" not in prompt


def test_build_with_multiple_skills_injects_all():
    from armature.spec.models import SkillDef
    assembler = PromptAssembler()
    role = Role(name="w", type=RoleType.WORKER, description="work", skills=["a", "b"])
    skills = [
        SkillDef(id="a", description="Skill A", content="Do alpha tasks."),
        SkillDef(id="b", description="Skill B", content="Do beta tasks."),
    ]
    prompt = assembler.build(role=role, tools=[], context={}, skills=skills)
    assert "Do alpha tasks." in prompt
    assert "Do beta tasks." in prompt

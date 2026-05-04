import pytest
from pathlib import Path
from armature.runtime.prompt import PromptAssembler
from armature.spec.models import Role, RoleType

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

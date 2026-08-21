# tests/packaging/conftest.py
from pathlib import Path
import pytest

TINY_SPEC = """\
name: echo-demo
version: "1.0"
description: A no-LLM spec for package tests.
model_tiers:
  small:
    provider: openrouter
    model: qwen/qwen3.6-27b
    api_key_env: OPENROUTER_API_KEY
contracts:
  inputs:
    - name: topic
stages:
  - id: writer
    role: {name: Writer, type: worker, description: "Echo {{ topic }}"}
    output_mode: text
    depends_on: []
"""

@pytest.fixture
def tiny_spec(tmp_path: Path) -> Path:
    p = tmp_path / "workflow.yaml"
    p.write_text(TINY_SPEC)
    return p

NO_LLM_SPEC = """\
name: echo-tool
version: "1.0"
description: A no-LLM tool_call spec for e2e package tests.
contracts:
  inputs: []
tools:
  - module: echo_tool
stages:
  - id: echo
    tool_call:
      name: echo
      args:
        msg: hello-package
    depends_on: []
"""

ECHO_TOOL = '''\
"""A vendored no-op tool for package e2e tests."""
from armature.registry.registry import ToolDescriptor, PermissionLevel

def register(registry):
    async def echo(args):
        return {"content": args.get("msg", "")}
    registry.register(ToolDescriptor(
        name="echo",
        description="Echo the msg argument",
        permission=PermissionLevel.READ_ONLY,
        handler=echo,
        parameters={"msg": {"type": "string"}},
    ))
'''

@pytest.fixture
def no_llm_pkg(tmp_path: Path) -> Path:
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(NO_LLM_SPEC)
    tools = tmp_path / "tools" / "echo_tool"
    tools.mkdir(parents=True)
    (tools / "__init__.py").write_text(ECHO_TOOL)
    return spec_path, tmp_path / "tools"
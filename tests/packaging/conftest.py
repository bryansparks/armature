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
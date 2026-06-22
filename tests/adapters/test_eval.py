"""Tests for the adapter evaluation harness."""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, patch

from armature.adapters.eval import evaluate_adapter
from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry


def _make_spec(tmp_path) -> __import__("pathlib").Path:
    spec = tmp_path / "wf.yaml"
    spec.write_text(
        "name: eval-wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: openai\n"
        "    model: gpt-4o-mini\n"
        "contracts:\n"
        "  inputs:\n"
        "    - name: topic\n"
        "stages:\n"
        "  - id: worker\n"
        "    depends_on: []\n"
        "    output_mode: text\n"
        "    role:\n"
        "      name: W\n"
        "      type: worker\n"
        "      description: Write about {{ topic }}\n"
    )
    return spec


def _register_adapter(tmp_path, name="tdd", version="1") -> AdapterRegistry:
    reg = AdapterRegistry(base_dir=tmp_path / "adapters")
    artifact_dir = tmp_path / f"src-{name}-{version}"
    artifact_dir.mkdir()
    (artifact_dir / "adapter_config.json").write_text("{}")
    meta = AdapterMetadata(name=name, version=version, base_model="gpt-4o-mini")
    reg.register(meta, artifact_dir)
    return reg


def _mock_response(content: str) -> __import__("unittest.mock").MagicMock:
    from unittest.mock import MagicMock

    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    r.usage = MagicMock()
    r.usage.prompt_tokens = 5
    r.usage.completion_tokens = 5
    return r


async def test_eval_runs_with_and_without_adapter(tmp_path):
    spec = _make_spec(tmp_path)
    reg = _register_adapter(tmp_path)

    call_count = [0]

    async def fake_completion(**kwargs):
        call_count[0] += 1
        return _mock_response(json.dumps({"content": f"response-{call_count[0]}"}))

    with patch("armature.nodes.llm.litellm_completion", side_effect=fake_completion):
        result = await evaluate_adapter(
            registry=reg,
            name="tdd",
            version="1",
            spec_path=spec,
            inputs={"topic": "test"},
        )

    assert result.adapter_name == "tdd"
    assert result.adapter_version == "1"
    assert call_count[0] == 2  # one with, one without

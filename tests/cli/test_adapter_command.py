"""Tests for the `armature adapter create` CLI."""
from __future__ import annotations

from typer.testing import CliRunner
from armature.cli import app

runner = CliRunner()


def test_adapter_create_mock_backend(tmp_path):
    spec = tmp_path / "wf.yaml"
    spec.write_text(
        "name: wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: vllm\n"
        "    model: qwen/qwen2.5-7b\n"
        "    adapter_support: dynamic\n"
        "skill_library:\n"
        "  tdd:\n"
        "    id: tdd\n"
        "    description: Test-driven development\n"
        "    content: Write a failing test first.\n"
        "stages:\n"
        "  - id: s\n"
        "    depends_on: []\n"
        "    role:\n"
        "      name: R\n"
        "      type: worker\n"
        "      description: d\n"
    )
    result = runner.invoke(
        app,
        [
            "adapter", "create",
            "--spec", str(spec),
            "--skill", "tdd",
            "--backend", "mock",
            "--registry", str(tmp_path / "adapters"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created adapter tdd@1" in result.output


def test_adapter_create_missing_skill(tmp_path):
    spec = tmp_path / "wf.yaml"
    spec.write_text(
        "name: wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: openai\n"
        "    model: gpt-4o-mini\n"
        "stages:\n"
        "  - id: s\n"
        "    depends_on: []\n"
        "    role:\n"
        "      name: R\n"
        "      type: worker\n"
        "      description: d\n"
    )
    result = runner.invoke(app, ["adapter", "create", "--spec", str(spec), "--skill", "tdd", "--backend", "mock"])
    assert result.exit_code == 1
    assert "Skill 'tdd' not found" in result.output


def test_adapter_create_missing_spec():
    result = runner.invoke(app, ["adapter", "create", "--spec", "/nonexistent.yaml", "--skill", "tdd"])
    assert result.exit_code == 1
    assert "Spec file not found" in result.output


def test_adapter_create_from_traces(tmp_path):
    import json

    spec = tmp_path / "wf.yaml"
    spec.write_text(
        "name: wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: vllm\n"
        "    model: qwen/qwen2.5-7b\n"
        "    adapter_support: dynamic\n"
        "stages:\n"
        "  - id: s\n"
        "    depends_on: []\n"
        "    role:\n"
        "      name: R\n"
        "      type: worker\n"
        "      description: d\n"
    )
    traces = tmp_path / "traces.jsonl"
    with traces.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "messages": [
                        {"role": "system", "content": "sys"},
                        {"role": "user", "content": "hi"},
                        {"role": "assistant", "content": "hello"},
                    ]
                }
            )
            + "\n"
        )
    result = runner.invoke(
        app,
        [
            "adapter", "create",
            "--spec", str(spec),
            "--traces", str(traces),
            "--backend", "trace",
            "--registry", str(tmp_path / "adapters"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created adapter traces@1" in result.output


def test_adapter_create_requires_skill_or_traces(tmp_path):
    spec = tmp_path / "wf.yaml"
    spec.write_text(
        "name: wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: openai\n"
        "    model: gpt-4o-mini\n"
        "stages:\n"
        "  - id: s\n"
        "    depends_on: []\n"
        "    role:\n"
        "      name: R\n"
        "      type: worker\n"
        "      description: d\n"
    )
    result = runner.invoke(app, ["adapter", "create", "--spec", str(spec)])
    assert result.exit_code == 1
    assert "Either --skill or --traces is required" in result.output

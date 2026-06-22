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


def test_adapter_list_and_promote(tmp_path):
    adapters_dir = tmp_path / "adapters"
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    (artifact_dir / "adapter_config.json").write_text("{}")
    (artifact_dir / "adapter.safetensors").write_bytes(b"X")

    reg = runner.invoke(
        app,
        [
            "adapter", "register",
            "tdd", "2", str(artifact_dir),
            "--base-model", "qwen/qwen2.5-7b",
            "--registry", str(adapters_dir),
        ],
    )
    assert reg.exit_code == 0, reg.output

    reg2 = runner.invoke(
        app,
        [
            "adapter", "register",
            "tdd", "3", str(artifact_dir),
            "--base-model", "qwen/qwen2.5-7b",
            "--registry", str(adapters_dir),
        ],
    )
    assert reg2.exit_code == 0, reg2.output

    promote = runner.invoke(
        app, ["adapter", "promote", "tdd", "3", "--registry", str(adapters_dir)]
    )
    assert promote.exit_code == 0, promote.output
    assert "Promoted tdd@3" in promote.output

    lst = runner.invoke(app, ["adapter", "list", "--registry", str(adapters_dir)])
    assert lst.exit_code == 0, lst.output
    assert "tdd@3" in lst.output

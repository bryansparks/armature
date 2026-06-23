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


def test_adapter_merge_command(tmp_path):
    adapters_dir = tmp_path / "adapters"
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    (artifact_dir / "adapter_config.json").write_text("{}")
    (artifact_dir / "adapter.safetensors").write_bytes(b"X")

    reg1 = runner.invoke(
        app,
        [
            "adapter", "register",
            "skill-a", "1", str(artifact_dir),
            "--base-model", "qwen/qwen2.5-7b",
            "--registry", str(adapters_dir),
        ],
    )
    assert reg1.exit_code == 0, reg1.output

    reg2 = runner.invoke(
        app,
        [
            "adapter", "register",
            "skill-b", "1", str(artifact_dir),
            "--base-model", "qwen/qwen2.5-7b",
            "--registry", str(adapters_dir),
        ],
    )
    assert reg2.exit_code == 0, reg2.output

    merge = runner.invoke(
        app,
        [
            "adapter", "merge",
            "skill-a@1", "skill-b@1",
            "--name", "combo",
            "--registry", str(adapters_dir),
        ],
    )
    assert merge.exit_code == 0, merge.output
    assert "Merged adapter combo@1" in merge.output


def test_adapter_merge_requires_two_sources(tmp_path):
    adapters_dir = tmp_path / "adapters"
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    (artifact_dir / "adapter_config.json").write_text("{}")
    (artifact_dir / "adapter.safetensors").write_bytes(b"X")

    reg = runner.invoke(
        app,
        [
            "adapter", "register",
            "skill-a", "1", str(artifact_dir),
            "--base-model", "qwen/qwen2.5-7b",
            "--registry", str(adapters_dir),
        ],
    )
    assert reg.exit_code == 0, reg.output

    merge = runner.invoke(
        app,
        ["adapter", "merge", "skill-a@1", "--name", "combo", "--registry", str(adapters_dir)],
    )
    assert merge.exit_code == 1
    assert "At least two source adapters" in merge.output


def test_adapter_update_trains_new_version(tmp_path):
    import json

    adapters_dir = tmp_path / "adapters"
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    (artifact_dir / "adapter_config.json").write_text("{}")
    (artifact_dir / "adapter.safetensors").write_bytes(b"X")

    reg = runner.invoke(
        app,
        [
            "adapter", "register",
            "worker", "1", str(artifact_dir),
            "--base-model", "qwen/qwen2.5-7b",
            "--registry", str(adapters_dir),
        ],
    )
    assert reg.exit_code == 0, reg.output

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

    update = runner.invoke(
        app,
        [
            "adapter", "update",
            "worker", str(traces),
            "--base-model", "qwen/qwen2.5-7b",
            "--registry", str(adapters_dir),
            "--no-promote",
        ],
    )
    assert update.exit_code == 0, update.output
    assert "Updated adapter worker@2" in update.output

    lst = runner.invoke(app, ["adapter", "list", "--name", "worker", "--registry", str(adapters_dir)])
    assert lst.exit_code == 0, lst.output
    # Latest should still be 1 because we passed --no-promote.
    lines = [line for line in lst.output.splitlines() if line.startswith("worker@")]
    assert any("worker@1" in line for line in lines)


def test_adapter_eval_command(tmp_path):
    adapters_dir = tmp_path / "adapters"
    spec = tmp_path / "wf.yaml"
    spec.write_text(
        "name: wf\n"
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

    # Register a mock adapter so the eval can resolve it.
    artifact_dir = tmp_path / "src-tdd-1"
    artifact_dir.mkdir()
    (artifact_dir / "adapter_config.json").write_text("{}")
    reg = runner.invoke(
        app,
        [
            "adapter", "register",
            "tdd", "1", str(artifact_dir),
            "--base-model", "gpt-4o-mini",
            "--registry", str(adapters_dir),
        ],
    )
    assert reg.exit_code == 0, reg.output

    call_count = [0]

    def _mock_response(content: str):
        from unittest.mock import MagicMock
        r = MagicMock()
        r.choices = [MagicMock()]
        r.choices[0].message.content = content
        r.choices[0].message.tool_calls = None
        r.usage = MagicMock()
        r.usage.prompt_tokens = 5
        r.usage.completion_tokens = 5
        return r

    import json
    from unittest.mock import patch

    async def fake_completion(**kwargs):
        call_count[0] += 1
        return _mock_response(json.dumps({"content": f"response-{call_count[0]}"}))

    with patch("armature.nodes.llm.litellm_completion", side_effect=fake_completion):
        result = runner.invoke(
            app,
            [
                "adapter", "eval",
                "tdd", str(spec),
                "--registry", str(adapters_dir),
                "--input", "topic=test",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Evaluated tdd@1" in result.output
    assert call_count[0] == 2

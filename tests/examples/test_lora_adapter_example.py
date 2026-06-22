"""Smoke tests for the LoRA adapter example spec."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from armature.cli import app
from armature.spec.loader import load_spec
from armature.spec.validator import validate_spec


@pytest.fixture
def example_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "examples" / "07_lora_adapter.yml"


def test_example_spec_loads(example_path):
    spec = load_spec(example_path)
    assert spec.name == "lora_skill_adapter"
    assert "tdd" in spec.skill_library
    assert spec.skill_library["tdd"].adapter is not None
    assert spec.adapter_factory is not None
    assert spec.adapter_factory.backend == "mock"


def test_example_spec_validates(example_path):
    spec = load_spec(example_path)
    errors = validate_spec(spec, strict=False)
    assert not any(e.severity == "error" for e in errors)


def test_example_adapter_create_and_dry_run(example_path, tmp_path):
    runner = CliRunner()
    registry_dir = tmp_path / "adapters"

    create = runner.invoke(
        app,
        [
            "adapter", "create",
            "--spec", str(example_path),
            "--skill", "tdd",
            "--backend", "mock",
            "--registry", str(registry_dir),
        ],
    )
    assert create.exit_code == 0, create.output
    assert "Created adapter tdd@1" in create.output

    dry_run = runner.invoke(
        app,
        [
            "run", str(example_path),
            "--input", "feature=login",
            "--registry", str(registry_dir),
            "--dry-run",
        ],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert "valid" in dry_run.output.lower()


def test_example_adapter_merge_requires_two_sources(example_path, tmp_path):
    runner = CliRunner()
    registry_dir = tmp_path / "adapters"

    create = runner.invoke(
        app,
        [
            "adapter", "create",
            "--spec", str(example_path),
            "--skill", "tdd",
            "--backend", "mock",
            "--registry", str(registry_dir),
        ],
    )
    assert create.exit_code == 0, create.output

    merge = runner.invoke(
        app,
        [
            "adapter", "merge",
            "tdd@1",
            "--name", "combo",
            "--registry", str(registry_dir),
        ],
    )
    assert merge.exit_code == 1, merge.output
    assert "At least two source adapters" in merge.output

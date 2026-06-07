"""Tests for armature validate handling of warnings vs errors."""
import pytest
from typer.testing import CliRunner
from armature.cli import app

runner = CliRunner()


def test_validate_exits_0_with_only_warnings(tmp_path):
    """Spec with only warnings (GUIDED_JSON_LOW_TIER_RISK) exits 0, not 1."""
    spec = tmp_path / "warn.yaml"
    spec.write_text(
        "name: wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: openai\n"
        "    model: gpt-4o-mini\n"
        "stages:\n"
        "  - id: s\n"
        "    depends_on: []\n"
        "    output_mode: guided_json\n"
        "    output_schema:\n"
        "      type: object\n"
        "    role:\n"
        "      name: R\n"
        "      type: worker\n"
        "      description: d\n"
        "      model_tier: small\n"
    )
    result = runner.invoke(app, ["validate", str(spec)])
    assert result.exit_code == 0


def test_validate_shows_warning_text(tmp_path):
    """Warning message contains GUIDED_JSON_LOW_TIER_RISK code."""
    spec = tmp_path / "warn.yaml"
    spec.write_text(
        "name: wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: openai\n"
        "    model: gpt-4o-mini\n"
        "stages:\n"
        "  - id: s\n"
        "    depends_on: []\n"
        "    output_mode: guided_json\n"
        "    output_schema:\n"
        "      type: object\n"
        "    role:\n"
        "      name: R\n"
        "      type: worker\n"
        "      description: d\n"
        "      model_tier: small\n"
    )
    result = runner.invoke(app, ["validate", str(spec)])
    assert "GUIDED_JSON_LOW_TIER_RISK" in result.output


def test_validate_exits_1_when_errors_present_alongside_warnings(tmp_path):
    """Spec with both an error and a warning still exits 1."""
    spec = tmp_path / "bad.yaml"
    spec.write_text(
        "name: wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: openai\n"
        "    model: gpt-4o-mini\n"
        "stages:\n"
        "  - id: s\n"
        "    depends_on: [missing_stage]\n"  # UNDEFINED_DEPENDENCY error
        "    output_mode: guided_json\n"
        "    output_schema:\n"
        "      type: object\n"
        "    role:\n"
        "      name: R\n"
        "      type: worker\n"
        "      description: d\n"
        "      model_tier: small\n"
    )
    result = runner.invoke(app, ["validate", str(spec)])
    assert result.exit_code == 1

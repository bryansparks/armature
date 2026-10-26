"""armature validate accepts known reasoning_effort values and rejects others at load time."""
from typer.testing import CliRunner

from armature.cli import app

runner = CliRunner()


def _spec(tier_effort: str | None = None, role_effort: str | None = None) -> str:
    lines = [
        "name: wf",
        "model_tiers:",
        "  large:",
        "    provider: bedrock",
        "    model: bedrock/us.anthropic.claude-opus-5-5",
    ]
    if tier_effort is not None:
        lines.append(f"    reasoning_effort: {tier_effort}")
    lines += [
        "stages:",
        "  - id: s",
        "    depends_on: []",
        "    role:",
        "      name: R",
        "      type: judge",
        "      description: d",
        "      model_tier: large",
    ]
    if role_effort is not None:
        lines.append(f"      reasoning_effort: {role_effort}")
    return "\n".join(lines) + "\n"


def test_validate_accepts_tier_and_role_reasoning_effort(tmp_path):
    spec = tmp_path / "ok.yaml"
    spec.write_text(_spec(tier_effort="high", role_effort="max"))
    result = runner.invoke(app, ["validate", str(spec)])
    assert result.exit_code == 0, result.output


def test_validate_rejects_unknown_tier_reasoning_effort(tmp_path):
    spec = tmp_path / "bad.yaml"
    spec.write_text(_spec(tier_effort="extreme"))
    result = runner.invoke(app, ["validate", str(spec)])
    assert result.exit_code == 1
    assert "model_tiers.large.reasoning_effort" in result.output
    assert "'low', 'medium', 'high', 'xhigh' or 'max'" in result.output


def test_validate_rejects_unknown_role_reasoning_effort(tmp_path):
    spec = tmp_path / "bad.yaml"
    spec.write_text(_spec(role_effort="extreme"))
    result = runner.invoke(app, ["validate", str(spec)])
    assert result.exit_code == 1
    assert "role.reasoning_effort" in result.output

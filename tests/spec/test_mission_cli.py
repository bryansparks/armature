"""`armature mission validate` CLI (slice 1)."""
import re
from pathlib import Path
from typer.testing import CliRunner

from armature.cli import app

runner = CliRunner()


def plain(s: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', '', s)


VALID = """\
name: m
objective: Ship the thing.
work:
  - id: a
    title: Unit A
    workflow: registered-worker
"""

INVALID = """\
name: m
objective: Ship the thing.
work:
  - id: a
    title: Unit A
    workflow: registered-worker
    requires: [missing-unit]
"""


def test_mission_validate_ok(tmp_path):
    p = tmp_path / "m.mission.yml"
    p.write_text(VALID)
    result = runner.invoke(app, ["mission", "validate", str(p)])
    out = plain(result.output)
    assert result.exit_code == 0
    assert "'m' is valid" in out
    assert "1 work unit" in out


def test_mission_validate_reports_codes(tmp_path):
    p = tmp_path / "m.mission.yml"
    p.write_text(INVALID)
    result = runner.invoke(app, ["mission", "validate", str(p)])
    out = plain(result.output)
    assert result.exit_code == 1
    assert "[UNKNOWN_WORK_UNIT]" in out


def test_mission_validate_missing_file(tmp_path):
    result = runner.invoke(app, ["mission", "validate", str(tmp_path / "nope.yml")])
    assert result.exit_code == 1
    assert "not found" in plain(result.output)


def test_mission_validate_warnings_do_not_fail(tmp_path):
    p = tmp_path / "m.mission.yml"
    p.write_text(VALID)
    result = runner.invoke(app, ["mission", "validate", str(p)])
    out = plain(result.output)
    assert result.exit_code == 0          # WORKFLOW_UNVERIFIED_NAME is a warning
    assert "[WORKFLOW_UNVERIFIED_NAME]" in out
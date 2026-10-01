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

EXAMPLE = Path(__file__).parents[2] / "examples" / "missions" / "campaign-pretzel.mission.yml"


def test_shipped_example_mission_valid():
    assert EXAMPLE.exists(), f"missing shipped example: {EXAMPLE}"
    result = runner.invoke(app, ["mission", "validate", str(EXAMPLE)])
    out = plain(result.output)
    assert result.exit_code == 0, out
    assert "is valid" in out


# ── Slice 2: mission run + mission status ────────────────────────────────────

from armature.state.work import LocalWorkStore, WorkUnitState

OK_SPEC = """\
name: ok-flow
adapters:
  echo:
    name: echo
    type: script
    cmd: "echo '{\\"ok\\": true}'"
    parse: json
stages:
  - id: work
    adapter: echo
    depends_on: []
"""

FAIL_SPEC = """\
name: fail-flow
adapters:
  boom:
    name: boom
    type: script
    cmd: "exit 3"
    parse: json
stages:
  - id: work
    adapter: boom
    depends_on: []
"""

MISSION_TEXT = """\
name: m
objective: Ship it.
work:
  - id: a
    title: A
    workflow: wf.yml
  - id: b
    title: B
    workflow: wf.yml
    requires: [a]
    max_attempts: 2
"""


def _write_pair(tmp_path, mission_text, workflow_text):
    (tmp_path / "wf.yml").write_text(workflow_text)
    p = tmp_path / "m.mission.yml"
    p.write_text(mission_text)
    return p


def test_mission_run_success_reaches_done(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(tmp_path / "store")])
    out = plain(r.output)
    assert r.exit_code == 0, out
    assert "done" in out
    store = LocalWorkStore(tmp_path / "store")
    assert store.load("m", "a").state == WorkUnitState.DONE
    assert store.load("m", "a").attempts == 1


def test_mission_run_failure_lands_retry_pending_then_failed(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, FAIL_SPEC)
    s = tmp_path / "store"
    r1 = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(s)])
    assert r1.exit_code == 1
    store = LocalWorkStore(s)
    rec = store.load("m", "a")
    assert rec.state == WorkUnitState.RETRY_PENDING      # attempts 1 < max 2
    r2 = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(s)])
    assert r2.exit_code == 1
    assert store.load("m", "a").state == WorkUnitState.FAILED   # ceiling reached


def test_mission_run_refuses_unready_requires(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    r = runner.invoke(app, ["mission", "run", str(p), "b", "--store", str(tmp_path / "store")])
    out = plain(r.output)
    assert r.exit_code == 1
    assert "requires" in out or "a" in out
    store = LocalWorkStore(tmp_path / "store")
    assert store.load("m", "b").attempts == 0           # no attempt consumed


def test_mission_run_refuses_unknown_unit(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    r = runner.invoke(app, ["mission", "run", str(p), "nope", "--store", str(tmp_path / "store")])
    assert r.exit_code == 1
    assert "unknown unit" in plain(r.output)


def test_mission_run_injects_objective_into_context(tmp_path):
    spec = OK_SPEC.replace("name: ok-flow", "name: ok-flow\nmission_source: work_unit")
    p = _write_pair(tmp_path, MISSION_TEXT, spec)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(tmp_path / "store")])
    assert r.exit_code == 0, plain(r.output)
    # injection itself is asserted at engine level (Task 4); here: run completed
    # and the record's last_job_id was stamped
    assert LocalWorkStore(tmp_path / "store").load("m", "a").last_job_id


def test_mission_status_renders_states_and_transitions(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    s = tmp_path / "store"
    runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(s)])
    r = runner.invoke(app, ["mission", "status", str(p), "--store", str(s)])
    out = plain(r.output)
    assert r.exit_code == 0, out
    assert "a" in out and "done" in out
    assert "b" in out and "pending" in out
    assert "recent transitions" in out


def test_shipped_example_status_renders(tmp_path):
    r = runner.invoke(app, ["mission", "status", str(EXAMPLE), "--store", str(tmp_path)])
    assert r.exit_code == 0, plain(r.output)
    assert "hero-headlines" in plain(r.output)
    assert "brand-approval" in plain(r.output)
    assert "pending" in plain(r.output)
    out = plain(r.output)
    # strengthened (passed immediately off Task 5's command): per-unit rows
    # carry posture and the requires column
    assert "delegated" in out and "human-led" in out
    hero_row = next(l for l in out.splitlines() if l.startswith("  hero-headlines"))
    assert "brand-approval" in hero_row          # requires column
    assert "0/2" in hero_row                     # attempts/max column

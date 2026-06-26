import os
from pathlib import Path
import textwrap
from campaign_runner.plan import load_plan
from campaign_runner.sandbox import Sandbox


def _plan(tmp_path: Path):
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: t
        description: "x"
        workflow: specs/s.yml
        budget: {max_runs: 1}
        phases: [{id: p, lever: none, inputs: {}, repeats: 1}]
        verdicts: {}
    """))
    return load_plan(p)


def test_trace_db_lives_under_sandbox_home(tmp_path):
    plan = _plan(tmp_path)
    sb = Sandbox(plan, root=tmp_path / "out")
    assert sb.dir == tmp_path / "out" / "t"
    assert sb.trace_db == sb.dir / ".armature" / "traces.db"
    assert sb.working_spec == sb.dir / "spec_work.yml"
    assert sb.dir.exists()


def test_env_redirects_home_to_sandbox(tmp_path):
    plan = _plan(tmp_path)
    sb = Sandbox(plan, root=tmp_path / "out")
    env = sb.env()
    assert env["HOME"] == str(sb.dir)
    # preserves other env vars (API keys come from env)
    assert "PATH" in env


def test_copy_working_spec_round_trips(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    sb = Sandbox(plan, root=tmp_path / "out")
    source = tmp_path / "src.yml"
    source.write_text("name: x\n")
    sb.copy_working_spec(source)
    assert sb.working_spec.read_text() == "name: x\n"
    # a second copy is idempotent
    sb.copy_working_spec(source)
    assert sb.working_spec.read_text() == "name: x\n"
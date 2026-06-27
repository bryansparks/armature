import textwrap
import pytest
from pathlib import Path
from campaign_runner.plan import load_plan


def _soak_plan_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-t
        description: "x"
        workflow: specs/soak/synth_fanout_mid.yml
        purpose: "Soak test: prove the engine stays up under cron repetition."
        tier_override:
          apply: true
          tiers:
            tiny: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}
        budget: {max_runs: 5}
        phases:
          - id: mid
            lever: none
            inputs: {topic: "q"}
            repeats: 2
          - id: overlap
            workflow: specs/soak/synth_fanout_wide.yml
            lever: none
            concurrency: {workers: 3, driver: armature_loop, reps_per_worker: 5}
        soak_verdicts: {no_unclean_exits: {allowed_failures: 0}, agent_spawn_count: {min_total: 100}}
        verdicts: {}
    """))
    return p


def test_soak_plan_loads(tmp_path):
    plan = load_plan(_soak_plan_yaml(tmp_path))
    assert plan.purpose.startswith("Soak test")
    assert plan.tier_override.apply is True
    assert plan.tier_override.tiers["tiny"]["model"] == "qwen/qwen3.6-27b"
    assert plan.soak_verdicts is not None
    assert plan.soak_verdicts.agent_spawn_count == {"min_total": 100}
    assert plan.phases[0].workflow is None
    assert plan.phases[1].workflow == "specs/soak/synth_fanout_wide.yml"
    assert plan.phases[1].concurrency.workers == 3
    assert plan.phases[1].concurrency.driver == "armature_loop"


def test_concurrency_and_self_improve_conflict_rejected(tmp_path):
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-t
        workflow: specs/soak/synth_fanout_mid.yml
        budget: {max_runs: 5}
        phases:
          - id: x
            lever: none
            inputs: {topic: "q"}
            repeats: 1
            self_improve: {enabled: true, target_hqs: 0.75, min_traces: 1, max_rounds: 1, apply: false}
            concurrency: {workers: 2, driver: armature_loop}
        verdicts: {}
    """))
    with pytest.raises(Exception):
        load_plan(p)


def test_concurrency_bad_driver_rejected(tmp_path):
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-t
        workflow: specs/soak/synth_fanout_mid.yml
        budget: {max_runs: 5}
        phases:
          - id: x
            lever: none
            inputs: {topic: "q"}
            repeats: 1
            concurrency: {workers: 2, driver: bogus}
        verdicts: {}
    """))
    with pytest.raises(Exception):
        load_plan(p)


def test_working_spec_for_distinct_paths(tmp_path):
    from campaign_runner.sandbox import Sandbox
    p = tmp_path / "plan.yml"
    p.write_text("name: t\ndescription: x\nworkflow: s.yml\nbudget: {max_runs: 1}\n"
                 "phases: [{id: a, lever: none, inputs: {}, repeats: 1}]\nverdicts: {}\n")
    sb = Sandbox(load_plan(p), root=tmp_path / "out")
    assert sb.working_spec_for("a").name == "spec_work_a.yml"
    assert sb.working_spec_for("a") != sb.working_spec_for("b")


def test_apply_tier_override_rewrites_all_tiers_preserving_names(tmp_path):
    from campaign_runner.sandbox import Sandbox
    from campaign_runner.plan import TierOverride
    p = tmp_path / "plan.yml"
    p.write_text("name: t\ndescription: x\nworkflow: s.yml\nbudget: {max_runs: 1}\n"
                 "phases: [{id: a, lever: none, inputs: {}, repeats: 1}]\nverdicts: {}\n")
    sb = Sandbox(load_plan(p), root=tmp_path / "out")
    spec = tmp_path / "ws.yml"
    spec.write_text(textwrap.dedent("""
        name: wf
        version: "1.0"
        model_tiers:
          small: {provider: anthropic, model: claude-haiku, api_key_env: ANTHROPIC_API_KEY, temperature: 0.3, max_tokens: 1024}
          large: {provider: ollama, model: llama, api_key_env: ""}
        role_type_defaults: {worker: small, judge: large}
        stages: [{id: s1, role: {name: S, type: worker, description: x}, output_mode: text, depends_on: []}]
    """).strip() + "\n")
    ov = TierOverride(apply=True, tiers={"tiny": {"provider": "openrouter",
            "model": "qwen/qwen3.6-27b", "api_key_env": "OPENROUTER_API_KEY",
            "temperature": 0.2, "max_tokens": 512}})
    sb.apply_tier_override(spec, ov)
    import yaml
    parsed = yaml.safe_load(spec.read_text())
    assert set(parsed["model_tiers"].keys()) == {"small", "large"}   # names preserved
    for v in parsed["model_tiers"].values():
        assert v["model"] == "qwen/qwen3.6-27b"
        assert v["provider"] == "openrouter"
    # idempotent
    sb.apply_tier_override(spec, ov)
    assert yaml.safe_load(spec.read_text()) == parsed


def test_apply_tier_override_noop_when_no_override(tmp_path):
    from campaign_runner.sandbox import Sandbox
    p = tmp_path / "plan.yml"
    p.write_text("name: t\ndescription: x\nworkflow: s.yml\nbudget: {max_runs: 1}\n"
                 "phases: [{id: a, lever: none, inputs: {}, repeats: 1}]\nverdicts: {}\n")
    sb = Sandbox(load_plan(p), root=tmp_path / "out")
    spec = tmp_path / "ws.yml"
    spec.write_text("name: wf\nmodel_tiers: {small: {provider: anthropic, model: x}}\n")
    before = spec.read_text()
    sb.apply_tier_override(spec, None)
    assert spec.read_text() == before


def test_per_phase_workflow_resolution_and_tier_override(tmp_path, monkeypatch):
    from campaign_runner import runner
    # two self-contained workflow specs with distinct names + tiers
    wa = tmp_path / "wf_a.yml"
    wa.write_text('name: wf-a\nversion: "1.0"\nmodel_tiers:\n'
                  '  small: {provider: anthropic, model: claude-haiku, api_key_env: ANTHROPIC_API_KEY}\n'
                  'role_type_defaults: {worker: small}\n'
                  'contracts: {inputs: [{name: topic}]}\n'
                  'stages: [{id: s, role: {name: S, type: worker, description: "{{ topic }}"}, output_mode: text, depends_on: []}]\n')
    wb = tmp_path / "wf_b.yml"
    wb.write_text('name: wf-b\nversion: "1.0"\nmodel_tiers:\n'
                  '  small: {provider: ollama, model: llama, api_key_env: ""}\n'
                  'role_type_defaults: {worker: small}\n'
                  'contracts: {inputs: [{name: topic}]}\n'
                  'stages: [{id: s, role: {name: S, type: worker, description: "{{ topic }}"}, output_mode: text, depends_on: []}]\n')
    plan_p = tmp_path / "plan.yml"
    plan_p.write_text(textwrap.dedent(f"""
        name: soak-res
        description: x
        workflow: {wa}
        tier_override:
          apply: true
          tiers:
            tiny: {{provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}}
        budget: {{max_runs: 5}}
        phases:
          - {{id: a, lever: none, inputs: {{topic: "q"}}, repeats: 1}}
          - {{id: b, lever: none, workflow: {wb}, inputs: {{topic: "q"}}, repeats: 1}}
        verdicts: {{}}
    """))
    plan = load_plan(plan_p)

    captured = []
    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p):
            captured.append(("validate", str(p)))
            return True
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            captured.append(("run", str(spec), workflow_name))
            import sqlite3
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
            con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms,success,output_valid) VALUES (?,?,?,?,?,?,?,?,?)",
                        ("r1", workflow_name, "s", "worker", "m", "2026-01-01T00:00:01", 100.0, 1, 1))
            con.commit(); con.close()
            from campaign_runner import cli_driver
            return cli_driver.RunOutcome("r1", 0, "", "", {"run_id": "r1"},
                                         hqs_armature={"authoritative": 0.8, "dashboard": None} if tag == "main" else None)
        def improve(self, spec, **kw): ...
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    r = runner.CampaignRunner(plan, wa, root=tmp_path / "out")
    result = r.run()
    # two distinct per-phase working specs, distinct workflow names, tiers overridden to openrouter
    specs = [c[1] for c in captured if c[0] == "validate"]
    assert len(set(specs)) == 2
    import yaml
    for sp in set(specs):
        parsed = yaml.safe_load(Path(sp).read_text())
        assert parsed["model_tiers"]["small"]["provider"] == "openrouter"
    names = [c[2] for c in captured if c[0] == "run"]
    assert "wf-a" in names and "wf-b" in names
    assert len(result.rows) == 2


def test_self_improve_threads_per_phase_ws_not_shared(tmp_path, monkeypatch):
    """Regression: _do_improve and _memory_mode must operate on the per-phase
    working spec (ws), not the stale shared self.sb.working_spec. With the bug,
    FakeDrv.improve would edit the shared spec while spec_diff is computed
    against ws (empty diff), and _memory_mode would read the shared spec's
    memory block (no `fresh: true`) instead of the per-phase one — masking the
    H2 self_improve verdict as a false positive."""
    from campaign_runner import runner
    # Source spec (copied into the shared working_spec) — NO memory block, so a
    # stale _memory_mode read of self.sb.working_spec would yield None, not "cold".
    src = tmp_path / "src.yml"
    src.write_text('name: src-wf\nversion: "1.0"\nmodel_tiers:\n'
                   '  small: {provider: anthropic, model: claude-haiku, api_key_env: ANTHROPIC_API_KEY}\n'
                   'role_type_defaults: {worker: small}\n'
                   'contracts: {inputs: [{name: topic}]}\n'
                   'stages: [{id: s, role: {name: S, type: worker, description: "{{ topic }}"}, output_mode: text, depends_on: []}]\n')
    # Per-phase workflow spec — has memory: {fresh: true} so _memory_mode(ws) == "cold".
    ph = tmp_path / "ph.yml"
    ph.write_text('name: ph-wf\nversion: "1.0"\nmodel_tiers:\n'
                  '  small: {provider: anthropic, model: claude-haiku, api_key_env: ANTHROPIC_API_KEY}\n'
                  'role_type_defaults: {worker: small}\n'
                  'contracts: {inputs: [{name: topic}]}\n'
                  'memory: {fresh: true}\n'
                  'stages: [{id: s, role: {name: S, type: worker, description: "{{ topic }}"}, output_mode: text, depends_on: []}]\n')
    plan_p = tmp_path / "plan.yml"
    plan_p.write_text(textwrap.dedent(f"""
        name: si-reg
        description: x
        workflow: {src}
        budget: {{max_runs: 5}}
        phases:
          - id: x
            lever: none
            workflow: {ph}
            inputs: {{topic: "q"}}
            repeats: 1
            self_improve: {{enabled: true, target_hqs: 0.75, min_traces: 1, max_rounds: 1, apply: false}}
        verdicts: {{}}
    """))
    plan = load_plan(plan_p)

    captured = {"improve_specs": [], "run_specs": []}
    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p):
            return True
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            captured["run_specs"].append((str(spec), tag))
            import sqlite3
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
            rid = "r-main" if tag == "main" else "r-probe"
            con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms,success,output_valid) VALUES (?,?,?,?,?,?,?,?,?)",
                        (rid, workflow_name, "s", "worker", "m", "2026-01-01T00:00:01", 100.0, 1, 1))
            con.commit(); con.close()
            from campaign_runner import cli_driver
            return cli_driver.RunOutcome(rid, 0, "", "", {"run_id": rid},
                                         hqs_armature={"authoritative": 0.8, "dashboard": None} if tag == "main" else None)
        def improve(self, spec, **kw):
            captured["improve_specs"].append(str(spec))
            # Actually edit the spec file we were handed — append a marker comment.
            Path(spec).write_text(Path(spec).read_text() + "\n# improve-marker\n")
            from campaign_runner import cli_driver
            return cli_driver.ImproveOutcome(0, "", [{"needs_improvement": True, "hqs_before": 0.5}])
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    result = r.run()

    expected_ws = str(r.sb.working_spec_for("x"))
    shared_ws = str(r.sb.working_spec)

    # improve received the per-phase ws, not the shared working_spec
    assert captured["improve_specs"] == [expected_ws]
    assert captured["improve_specs"][0] != shared_ws

    # the recovery probe ran against the per-phase ws
    probe_specs = [s for s, t in captured["run_specs"] if t == "probe"]
    assert probe_specs == [expected_ws]
    assert probe_specs[0] != shared_ws

    # spec_diff is non-empty: improve edited ws, so ws changed between spec_before
    # and the post-improve read. With the bug (improve editing the shared spec),
    # ws would be untouched and spec_diff would be empty.
    assert result.rows, "expected one row"
    row = result.rows[0]
    assert row["spec_diff"].strip(), "spec_diff must be non-empty (improve edited ws)"
    assert "# improve-marker" in row["spec_diff"]

    # _memory_mode reflects the per-phase spec's memory block (fresh: true -> "cold").
    # A stale read of the shared working_spec (no memory block) would yield None.
    assert row["memory_mode"] == "cold"


def _soak_row(run_id, hqs=0.8, exit_code=0, latency_ms=100.0, role="worker"):
    return {"run_id": run_id, "phase_id": "p", "lever": "none", "inputs": {},
            "exit_code": exit_code, "is_concurrency_summary": False,
            "hqs_ours": {"authoritative": hqs, "rolling": None, "dashboard": None, "feedback": None},
            "hqs_armature": {"authoritative": None, "rolling": None, "dashboard": None, "feedback": None},
            "improve_log": [], "recovery_hqs_ours": None, "spec_diff": "", "memory_mode": None,
            "_latency_ms": latency_ms, "_role": role}


def test_verdict_no_unclean_exits(tmp_path):
    from campaign_runner import soak_verdicts as sv
    rows = [_soak_row("r1", exit_code=0), _soak_row("r2", exit_code=1)]
    name, status, detail = sv.verdict_no_unclean_exits(rows, {"allowed_failures": 0})
    assert (name, status) == ("no_unclean_exits", "FAIL")
    assert detail["n_unclean"] == 1


def test_verdict_trace_db_integrity(tmp_path):
    import sqlite3
    from campaign_runner import soak_verdicts as sv
    db = tmp_path / "traces.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
    con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms) VALUES (?,?,?,?,?,?,?)",
                ("r1", "wf", "s", "worker", "m", "2026-01-01T00:00:01", 100.0))
    con.commit(); con.close()
    name, status, detail = sv.verdict_trace_db_integrity([], {}, db)
    assert status == "PASS" and detail["integrity_check"] == "ok" and detail["n_null_run_id"] == 0


def test_verdict_no_row_loss_pass(tmp_path):
    from campaign_runner import soak_verdicts as sv
    rows = [{"is_concurrency_summary": True, "worker": 0, "run_ids": ["a", "b"],
             "exit_codes": [0], "n_trace_rows": 2, "sqlite_busy_count": 0},
            {"is_concurrency_summary": True, "worker": 1, "run_ids": ["c", "d"],
             "exit_codes": [0], "n_trace_rows": 2, "sqlite_busy_count": 0}]
    name, status, detail = sv.verdict_no_row_loss_under_concurrency(rows, {"expected": 4, "tolerance": 0})
    assert (name, status) == ("no_row_loss_under_concurrency", "PASS")
    assert detail["actual"] == 4 and detail["sqlite_busy_count"] == 0


def test_verdict_no_row_loss_busy_fails(tmp_path):
    from campaign_runner import soak_verdicts as sv
    rows = [{"is_concurrency_summary": True, "worker": 0, "run_ids": ["a"],
             "exit_codes": [0], "n_trace_rows": 1, "sqlite_busy_count": 1}]
    name, status, detail = sv.verdict_no_row_loss_under_concurrency(rows, {"expected": 4, "tolerance": 0})
    assert status == "FAIL" and detail["sqlite_busy_count"] == 1


def test_verdict_hqs_stability_no_drift(tmp_path):
    from campaign_runner import soak_verdicts as sv
    flat = [_soak_row(f"r{i}", hqs=0.80 + (i % 3) * 0.001) for i in range(20)]
    name, status, detail = sv.verdict_hqs_stability_no_drift(flat, {"max_mean_delta": 0.08})
    assert status == "PASS" and detail["delta"] <= 0.08
    drifty = [_soak_row(f"r{i}", hqs=0.5 + i * 0.05) for i in range(20)]
    _, status2, _ = sv.verdict_hqs_stability_no_drift(drifty, {"max_mean_delta": 0.08})
    assert status2 == "FAIL"


def test_verdict_wallclock_stability(tmp_path):
    import sqlite3
    from campaign_runner import soak_verdicts as sv
    db = tmp_path / "traces.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
    rows = []
    for i in range(10):
        rid = f"r{i}"
        rows.append(_soak_row(rid, latency_ms=100.0 + i * 0.5))   # ~0.5 ms/run slope -> PASS
        con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms) VALUES (?,?,?,?,?,?,?)",
                    (rid, "wf", "s", "worker", "m", "2026-01-01T00:00:01", 100.0 + i * 0.5))
    con.commit(); con.close()
    name, status, detail = sv.verdict_wallclock_stability(rows, {"max_latency_slope_ms_per_run": 5.0}, db)
    assert status == "PASS" and detail["slope_ms_per_run"] <= 5.0


def test_verdict_checkpoint_resume_correctness(tmp_path):
    from campaign_runner import soak_verdicts as sv
    rows = [_soak_row(f"r{i}") for i in range(5)]
    name, status, detail = sv.verdict_checkpoint_resume_correctness(rows, {"require_distinct_run_ids": True})
    assert status == "PASS" and detail["n_distinct_run_ids"] == 5
    dup = [_soak_row("r1"), _soak_row("r1")]
    _, status2, _ = sv.verdict_checkpoint_resume_correctness(dup, {"require_distinct_run_ids": True})
    assert status2 == "FAIL"


def test_verdict_budget_obeyed(tmp_path):
    from campaign_runner import soak_verdicts as sv
    plan = type("P", (), {"budget": type("B", (), {"max_runs": 5})(), "soak_verdicts": None})()
    rows = [_soak_row(f"r{i}") for i in range(4)]
    name, status, detail = sv.verdict_budget_obeyed(rows, {}, plan)
    assert status == "PASS" and detail["stop_reason"] == "completed"
    rows2 = [_soak_row(f"r{i}") for i in range(5)]
    _, status2, detail2 = sv.verdict_budget_obeyed(rows2, {}, plan)
    assert status2 == "PASS" and detail2["stop_reason"] == "budget"


def test_verdict_agent_spawn_count(tmp_path):
    import sqlite3
    from campaign_runner import soak_verdicts as sv
    db = tmp_path / "traces.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
    for i in range(10):
        con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp) VALUES (?,?,?,?,?,?)",
                    (f"r{i}", "wf", "s", "worker", "m", "2026-01-01T00:00:01"))
    con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp) VALUES (?,?,?,?,?,?)",
                ("rt", "wf", "st", "tool_call", "m", "2026-01-01T00:00:01"))  # excluded
    con.commit(); con.close()
    name, status, detail = sv.verdict_agent_spawn_count([], {"min_total": 10}, db)
    assert status == "PASS" and detail["total_agents"] == 10
    _, status2, _ = sv.verdict_agent_spawn_count([], {"min_total": 100}, db)
    assert status2 == "FAIL"


def test_all_soak_verdicts_dispatcher(tmp_path):
    from campaign_runner import soak_verdicts as sv
    from campaign_runner.plan import load_plan
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-t
        workflow: specs/soak/synth_fanout_mid.yml
        budget: {max_runs: 5}
        phases:
          - {id: a, lever: none, inputs: {topic: q}, repeats: 1}
          - id: b
            lever: none
            inputs: {topic: q}
            repeats: 1
            concurrency: {workers: 2, driver: armature_loop, reps_per_worker: 5}
        soak_verdicts: {no_unclean_exits: {allowed_failures: 0}, agent_spawn_count: {min_total: 1}}
        verdicts: {}
    """))
    plan = load_plan(p)
    rows = [_soak_row(f"r{i}") for i in range(3)]
    vs = sv.all_soak_verdicts(rows, plan, None)
    names = [v[0] for v in vs]
    assert "no_unclean_exits" in names and "agent_spawn_count" in names
    # expected derived from concurrency phase: 2 workers * 5 reps = 10
    nrl = [v for v in vs if v[0] == "no_row_loss_under_concurrency"][0]
    assert nrl[2]["expected"] == 10
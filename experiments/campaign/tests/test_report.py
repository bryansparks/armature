import re
from pathlib import Path
from campaign_runner import report


def _rows():
    return [{
        "run_id": "r1", "phase_id": "ramp", "lever": "input_difficulty_ramp",
        "inputs": {"difficulty": "1"}, "exit_code": 0,
        "hqs_ours": {"authoritative": 0.9, "rolling": 0.9, "dashboard": 0.9, "feedback": 0.9},
        "hqs_armature": {"authoritative": 0.9, "rolling": 0.9, "dashboard": 0.9, "feedback": 0.9},
        "improve_log": [], "recovery_hqs_ours": None, "spec_diff": "", "memory_mode": None,
    }]


def test_report_is_self_contained_with_all_sections(tmp_path):
    out = report.render_report(
        campaign={"name": "demo", "git_sha": "abc1234", "totals": {"runs": 1}},
        rows=_rows(),
        verdicts=[("hqs_tracks_difficulty", "PASS", {"spearman_rho": -0.7}),
                  ("self_improve_fires_and_recovers", "INCONCLUSIVE", {}),
                  ("hqs_formula_consistency", "PASS", {"max_delta": 0.0}),
                  ("memory_carry_forward_helps", "INCONCLUSIVE", {})],
        gaps=[{"want": "hqs_after", "needed": "recovery probe", "severity": "low"}],
        reproduce_cmd="python experiments/campaign/run.py plans/quick.yml --replay rec/",
        out_path=tmp_path / "report.html")
    html = out.read_text()
    # 7 sections present
    for marker in ["Campaign summary", "HQS over runs", "Formula-divergence",
                   "Fire", "Verdict table", "Observability gaps", "Reproduce this"]:
        assert marker in html, f"missing section: {marker}"
    # self-contained: no external assets
    assert 'href="' not in html and 'src="' not in html
    assert "<svg" in html
    # reproduce command present
    assert "--replay" in html


def test_report_escapes_user_controlled_strings(tmp_path):
    rows = [{
        "run_id": "r1", "phase_id": "ramp", "lever": "input_difficulty_ramp",
        "inputs": {"difficulty": "1"}, "exit_code": 0,
        "hqs_ours": {"authoritative": 0.9, "rolling": 0.9, "dashboard": 0.9, "feedback": 0.9},
        "hqs_armature": {"authoritative": 0.9, "rolling": 0.9, "dashboard": 0.9, "feedback": 0.9},
        "improve_log": [{"needs_improvement": True, "hqs_before": 0.4,
                         "applied": "<script>alert(1)</script>"}],
        "recovery_hqs_ours": None, "spec_diff": "", "memory_mode": None,
    }]
    out = report.render_report(
        campaign={"name": "<script>name</script>", "git_sha": "abc", "totals": {"runs": 1}},
        rows=rows,
        verdicts=[("hqs_tracks_difficulty", "PASS", {})],
        gaps=[{"want": "<b>gap</b>", "needed": "x", "severity": "low"}],
        reproduce_cmd="python run.py <img src=x>",
        out_path=tmp_path / "report.html")
    html = out.read_text()
    # raw script/img tags must NOT survive
    assert "<script>alert(1)</script>" not in html
    assert "<script>name</script>" not in html
    assert "<img src=x>" not in html
    # escaped forms should be present
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;gap&lt;/b&gt;" in html


def _agent_rows():
    """Three rows across two workflows with agents_run + workflow_name set, so
    the per-workflow tally table can be rendered + checked."""
    base = {"phase_id": "p", "lever": "none", "inputs": {}, "exit_code": 0,
            "hqs_ours": {"authoritative": 0.8, "rolling": 0.8, "dashboard": 0.8,
                         "feedback": 0.8},
            "hqs_armature": {"authoritative": 0.8, "rolling": 0.8, "dashboard": 0.8,
                             "feedback": None},
            "improve_log": [], "recovery_hqs_ours": None, "spec_diff": "",
            "memory_mode": None}
    return [
        dict(base, run_id="r1", workflow_name="wf-a", agents_run=4),
        dict(base, run_id="r2", workflow_name="wf-a", agents_run=6),
        dict(base, run_id="r3", workflow_name="wf-b", agents_run=10),
    ]


def test_report_renders_per_workflow_agent_tally(tmp_path):
    out = report.render_report(
        campaign={"name": "demo", "git_sha": "abc", "totals": {"runs": 3},
                  "agents_per_workflow": {"wf-a": {"runs": 2, "agents": 10},
                                          "wf-b": {"runs": 1, "agents": 10}},
                  "grand_total_agents": 20},
        rows=_agent_rows(),
        verdicts=[("hqs_formula_consistency", "PASS", {})],
        gaps=[],
        reproduce_cmd="python run.py x --replay r",
        out_path=tmp_path / "report.html")
    html = out.read_text()
    assert "Agents run per workflow" in html
    assert "wf-a" in html and "wf-b" in html
    # grand total row present with the summed count
    assert "grand total" in html.lower()
    assert "20" in html


def test_report_omits_agent_tally_when_no_data(tmp_path):
    out = report.render_report(
        campaign={"name": "demo", "git_sha": "abc", "totals": {"runs": 1}},
        rows=_rows(), verdicts=[], gaps=[], reproduce_cmd="x",
        out_path=tmp_path / "report.html")
    assert "Agents run per workflow" not in out.read_text()


def test_provider_health_banner_on_fail(tmp_path):
    from campaign_runner.report import render_report
    verdicts = [("provider_health", "FAIL",
                 {"account_scoped_runs": 3, "buckets": {"provider_credits": 3},
                  "models": ["openrouter/m"], "run_ids": ["r1", "r2", "r3"], "K": 3,
                  "aborted": True, "tripped_at": "r3", "abort_reason": "provider account exhausted"})]
    out = tmp_path / "report.html"
    render_report(campaign={"name": "t", "description": "", "purpose": "", "git_sha": "",
                            "date": "", "workflow": "", "tiers": [],
                            "totals": {"runs": 3, "phases": 1},
                            "agents_per_workflow": {}, "grand_total_agents": 0,
                            "aborted": True,
                            "abort_reason": "provider account exhausted"},
                  rows=[], verdicts=verdicts, gaps=[],
                  reproduce_cmd="python run.py t", out_path=out)
    html = out.read_text()
    assert "Provider account exhausted" in html
    assert "openrouter/m" in html
    assert "r1" in html and "r3" in html


def test_provider_health_banner_omitted_on_pass(tmp_path):
    from campaign_runner.report import render_report
    verdicts = [("provider_health", "PASS", {"account_scoped_runs": 0, "K": 3})]
    out = tmp_path / "report.html"
    render_report(campaign={"name": "t", "description": "", "purpose": "", "git_sha": "",
                            "date": "", "workflow": "", "tiers": [],
                            "totals": {"runs": 1, "phases": 1},
                            "agents_per_workflow": {}, "grand_total_agents": 0,
                            "aborted": False, "abort_reason": None},
                  rows=[], verdicts=verdicts, gaps=[],
                  reproduce_cmd="python run.py t", out_path=out)
    html = out.read_text()
    assert "Provider account exhausted" not in html
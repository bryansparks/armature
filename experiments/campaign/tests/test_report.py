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
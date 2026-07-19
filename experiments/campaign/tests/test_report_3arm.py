"""3-arm H4 report section: renders when the memory_carry_forward_helps verdict
is 3-arm (nav present); omits cleanly on the 2-arm fallback. Preview generator
produces a watermarked PREVIEW report.html with synthetic 3-arm data."""
from pathlib import Path
from campaign_runner import report


def _three_arm_verdict(result="PASS"):
    detail = {
        "signal": "quorum",
        "n_cold": 10, "n_warm": 10, "n_nav": 10,
        "n_excluded": {"cold": 0, "warm": 1, "nav": 0},
        "mean_quorum_cold": 0.50, "mean_quorum_warm": 0.86, "mean_quorum_nav": 0.85,
        "mean_latency_cold": 1200.0, "mean_latency_warm": 6000.0, "mean_latency_nav": 1400.0,
        "mean_input_tokens_cold": 200.0, "mean_input_tokens_warm": 1000.0, "mean_input_tokens_nav": 250.0,
        "warm_minus_cold_mean": 0.36, "warm_minus_cold_ci_low": 0.30,
        "nav_minus_cold_mean": 0.35, "nav_minus_cold_ci_low": 0.28,
        "nav_minus_warm_mean": -0.01, "nav_minus_warm_ci_low": -0.05,
    }
    return ("memory_carry_forward_helps", result, detail)


def _two_arm_verdict(result="PASS"):
    """2-arm fallback: no nav_* fields."""
    detail = {
        "signal": "quorum", "n_cold": 10, "n_warm": 10,
        "n_excluded": {"cold": 0, "warm": 0, "nav": 0},
        "mean_quorum_cold": 0.50, "mean_quorum_warm": 0.86,
        "mean_latency_cold": 1200.0, "mean_latency_warm": 6000.0, "mean_latency_nav": None,
        "mean_input_tokens_cold": 200.0, "mean_input_tokens_warm": 1000.0, "mean_input_tokens_nav": None,
        "mean_diff": 0.36, "ci_low": 0.30, "ci_high": 0.42,
    }
    return ("memory_carry_forward_helps", result, detail)


def test_three_arm_section_renders_when_nav_present():
    thresholds = {"warm_minus_cold_mean_ge": 0.05, "bootstrap_ci_lower_ge": 0.0,
                  "nav_minus_cold_mean_ge": 0.05, "nav_minus_warm_mean_ge": 0.0}
    html = report._three_arm_html([_three_arm_verdict("PASS")], thresholds)
    assert "cold" in html and "warm" in html and "nav" in html
    assert "<svg" in html  # at least one bar chart
    # pairwise diff rows
    assert "warm&minus;cold" in html or "warm-cold" in html
    assert "nav&minus;cold" in html or "nav-cold" in html
    assert "nav&minus;warm" in html or "nav-warm" in html
    # per-gate PASS/FAIL chips present
    assert "PASS" in html or "FAIL" in html


def test_three_arm_section_omitted_when_no_nav():
    thresholds = {"warm_minus_cold_mean_ge": 0.05, "bootstrap_ci_lower_ge": 0.0,
                  "nav_minus_cold_mean_ge": 0.05, "nav_minus_warm_mean_ge": 0.0}
    assert report._three_arm_html([_two_arm_verdict("PASS")], thresholds) == ""


def test_three_arm_section_omitted_when_no_h4_verdict():
    thresholds = {"warm_minus_cold_mean_ge": 0.05, "bootstrap_ci_lower_ge": 0.0}
    other = [("provider_health", "PASS", {"abort_reason": None})]
    assert report._three_arm_html(other, thresholds) == ""


def test_render_report_includes_three_arm_section(tmp_path):
    thresholds = {"warm_minus_cold_mean_ge": 0.05, "bootstrap_ci_lower_ge": 0.0,
                  "nav_minus_cold_mean_ge": 0.05, "nav_minus_warm_mean_ge": 0.0}
    campaign = {"name": "cold-vs-warm", "description": "d", "purpose": "p",
                "date": "2026-07-19", "workflow": "specs/x.yml", "git_sha": "abc",
                "totals": {"runs": 30}}
    rows = [{"run_id": "r1", "hqs_ours": {"authoritative": 0.7, "dashboard": 0.7},
             "hqs_armature": {"authoritative": 0.7}}]
    out = tmp_path / "report.html"
    report.render_report(campaign=campaign, rows=rows,
                         verdicts=[_three_arm_verdict("PASS")], gaps=[],
                         reproduce_cmd="python -m armature loop ...",
                         out_path=out, verdict_thresholds=thresholds)
    text = out.read_text()
    assert "3-arm comparison" in text.lower() or "three-arm" in text.lower()
    assert "<svg" in text


def test_preview_generator_writes_watermarked_html(tmp_path):
    out = tmp_path / "preview.html"
    report_path = report.build_3arm_preview(out_path=out)
    text = Path(report_path).read_text()
    assert "PREVIEW" in text
    assert "<svg" in text
    assert "synthetic" in text.lower() or "preview" in text.lower()


# ── Task 7c: 3-arm rows -> verdict -> report integration (Test C) ──


def _arm(mode, quorums, *, latency, tokens):
    """Build H4 rows for one memory arm with explicit latency + tokens so the
    per-arm means are deterministic (the report integration test asserts them)."""
    return [
        {"memory_mode": mode, "quorum_ours": q, "model_failed": False,
         "researcher_latency_ms": latency, "researcher_input_tokens": tokens}
        for q in quorums
    ]


def test_three_arm_rows_to_verdict_to_report_integration(tmp_path):
    """Realistic 3-arm rows -> verdict_h4 -> render_report: the 3-arm section
    renders and the verdict's per-arm means match the rows."""
    from campaign_runner import verdicts, report

    rows = [
        *_arm("cold", [0.50, 0.55], latency=1200, tokens=200),
        *_arm("warm", [0.85, 0.88], latency=6000, tokens=1000),
        *_arm("nav",  [0.85, 0.88], latency=1400, tokens=250),
    ]
    th = {"warm_minus_cold_mean_ge": 0.05, "bootstrap_ci_lower_ge": 0.0,
          "nav_minus_cold_mean_ge": 0.05, "nav_minus_warm_mean_ge": 0.0}
    name, result, detail = verdicts.verdict_h4(rows, th)
    assert name == "memory_carry_forward_helps"
    # nav matches warm (mean 0.865 == 0.865) -> nav_minus_warm_mean == 0.0
    # which satisfies nav_minus_warm_mean_ge=0.0 -> PASS.
    assert result == "PASS"
    assert detail["mean_quorum_nav"] == 0.865  # mean of [0.85, 0.88]
    assert detail["mean_latency_nav"] == 1400
    assert detail["mean_input_tokens_nav"] == 250

    # And the report renders the 3-arm section from this verdict.
    out = tmp_path / "report.html"
    campaign = {"name": "c", "description": "d", "purpose": "p",
                "date": "2026-07-19", "workflow": "specs/x.yml",
                "git_sha": "abc", "totals": {"runs": 6}}
    report.render_report(campaign=campaign, rows=rows,
                         verdicts=[(name, result, detail)], gaps=[],
                         reproduce_cmd="...", out_path=out,
                         verdict_thresholds=th)
    text = out.read_text()
    assert "3-arm comparison" in text.lower()
    assert "<svg" in text


# ── Task 7c: report chip fidelity (Test E) ──


def test_three_arm_chip_flips_when_threshold_tightens():
    """Tighten nav_minus_warm_mean_ge so a nav that barely matches warm flips
    the nav-warm chip from PASS to FAIL — the chips reflect the thresholds,
    not hardcoded labels."""
    import re
    from campaign_runner import report

    detail = {
        "signal": "quorum", "n_cold": 10, "n_warm": 10, "n_nav": 10,
        "n_excluded": {"cold": 0, "warm": 0, "nav": 0},
        "mean_quorum_cold": 0.50, "mean_quorum_warm": 0.86, "mean_quorum_nav": 0.86,
        "mean_latency_cold": 1200.0, "mean_latency_warm": 6000.0, "mean_latency_nav": 1400.0,
        "mean_input_tokens_cold": 200.0, "mean_input_tokens_warm": 1000.0, "mean_input_tokens_nav": 250.0,
        "warm_minus_cold_mean": 0.36, "warm_minus_cold_ci_low": 0.30,
        "nav_minus_cold_mean": 0.36, "nav_minus_cold_ci_low": 0.30,
        "nav_minus_warm_mean": 0.0, "nav_minus_warm_ci_low": -0.02,
    }
    loose = {"warm_minus_cold_mean_ge": 0.05, "bootstrap_ci_lower_ge": 0.0,
             "nav_minus_cold_mean_ge": 0.05, "nav_minus_warm_mean_ge": 0.0}
    tight = {**loose, "nav_minus_warm_mean_ge": 0.05}  # require nav to BEAT warm by 0.05
    html_loose = report._three_arm_html(
        [("memory_carry_forward_helps", "PASS", detail)], loose)
    html_tight = report._three_arm_html(
        [("memory_carry_forward_helps", "FAIL", detail)], tight)

    def _navwarm_chip(html):
        # Grab the nav-warm CHIP from the diff table — not the overall H4 verdict.
        # Anchoring on `<b>nav&minus;warm</b>` skips the thesis paragraph's
        # `nav&minus;warm` (wrapped as `<b>Headline gate: nav&minus;warm &ge; 0</b>`,
        # so `<b>nav&minus;warm</b>` does not match it). Requiring `</span>` after
        # the label skips the meta line's overall-H4 verdict `<b>{result}</b>`
        # (which is wrapped in `<b>`, not `<span>`). Without both anchors the regex
        # reads the verdict string this test injects, making the assertion vacuous.
        m = re.search(r"<b>nav&minus;warm</b>.*?(PASS|FAIL)</span>", html, re.S)
        assert m, "nav-warm chip row not found in 3-arm html"
        return m.group(1)

    # Under loose thresholds, nav-warm (mean 0.0 >= 0.0) -> PASS chip.
    assert _navwarm_chip(html_loose) == "PASS"
    # Under tight thresholds, nav-warm (mean 0.0 < 0.05) -> FAIL chip.
    assert _navwarm_chip(html_tight) == "FAIL"
from campaign_runner import hqs, trace_io


def _row(stage="s", valid=True, success=True, quorum=0.8, lat=1000.0, esc=0):
    return trace_io.TraceRow("r", "wf", stage, "worker", "m", 10, 20, lat,
                              success, valid, quorum, esc)


def test_authoritative_matches_handcomputed():
    # two rows: valid/success=1 each, quorums 0.8+0.9 avg=0.85, lat 1000+3000 avg=2000,
    # latency_score=max(0,1-2000/5000)=0.6, hfr=1/2=0.5 (row2 has esc=1)
    rows = [_row("a", quorum=0.8, lat=1000.0, esc=0),
            _row("b", quorum=0.9, lat=3000.0, esc=1)]
    # 0.35*1 + 0.25*1 + 0.20*0.85 + 0.10*0.6 + 0.10*0.5
    expected = 0.35 + 0.25 + 0.20 * 0.85 + 0.10 * 0.6 + 0.10 * 0.5
    assert abs(hqs.compute_authoritative(rows) - expected) < 1e-9


def test_authoritative_empty_returns_none():
    assert hqs.compute_authoritative([]) is None


def test_dashboard_uses_max_latency_and_no_hfr():
    rows = [_row("a", quorum=0.8, lat=1000.0, esc=0),
            _row("b", quorum=0.9, lat=3000.0, esc=1)]
    # max_lat=3000, latency_score=max(0,1-3000/60000)=0.95
    # 0.40*1 + 0.30*1 + 0.20*0.85 + 0.10*0.95  (no HFR term)
    expected = 0.40 + 0.30 + 0.20 * 0.85 + 0.10 * 0.95
    assert abs(hqs.compute_dashboard(rows) - expected) < 1e-9


def test_feedback_uses_fixed_quorum_and_no_hfr():
    rows = [_row("a", quorum=0.99, lat=1000.0, esc=0)]   # quorum ignored -> 0.5
    # avg_lat=1000, latency_score=max(0,1-1000/5000)=0.8
    # 0.40*1 + 0.30*1 + 0.20*0.5 + 0.10*0.8
    expected = 0.40 + 0.30 + 0.20 * 0.5 + 0.10 * 0.8
    assert abs(hqs.compute_feedback(rows) - expected) < 1e-9


def test_divergence_flags_drift():
    ours = {"authoritative": 0.80, "dashboard": 0.79}
    armature = {"authoritative": 0.80, "dashboard": 0.70}
    d = hqs.divergence(ours, armature)
    assert d["authoritative"] == 0.0
    assert abs(d["dashboard"] - 0.09) < 1e-9
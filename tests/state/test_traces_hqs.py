from armature.state.traces import TraceRecord, compute_hqs_from_traces


def _t(quorum=0.9, success=True, valid=True, latency=100.0, esc=0):
    return TraceRecord(
        run_id="r", workflow_name="wf", stage_id="s", role_type="judge",
        model="m", quorum_score=quorum, success=success, output_valid=valid,
        latency_ms=latency, escalation_count=esc,
    )


def test_compute_hqs_from_traces_matches_formula_a():
    traces = [_t(quorum=0.8, latency=1000.0), _t(quorum=0.6, latency=500.0, esc=1)]
    res = compute_hqs_from_traces(traces)
    # Reproduce formula A exactly:
    n = 2
    valid = 1.0
    success = 1.0
    avg_quorum = (0.8 + 0.6) / 2
    avg_lat = (1000.0 + 500.0) / 2
    latency_score = max(0.0, 1.0 - avg_lat / 5000.0)
    hfr = 1 / 2  # one trace has escalation_count == 0
    expected = 0.35 * valid + 0.25 * success + 0.20 * avg_quorum + 0.10 * latency_score + 0.10 * hfr
    assert res.hqs == expected
    assert res.hfr == hfr
    assert res.avg_quorum_score == avg_quorum
    assert res.n_traces == n


def test_compute_hqs_from_traces_no_quorum_defaults_half():
    traces = [_t(quorum=None), _t(quorum=None)]
    res = compute_hqs_from_traces(traces)
    assert res.avg_quorum_score == 0.5
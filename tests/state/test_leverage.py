from armature.state.leverage import _pearson_r


def test_pearson_perfect_positive():
    assert abs(_pearson_r([1, 2, 3, 4], [2, 4, 6, 8]) - 1.0) < 1e-9


def test_pearson_perfect_negative():
    assert abs(_pearson_r([1, 2, 3, 4], [8, 6, 4, 2]) - (-1.0)) < 1e-9


def test_pearson_zero_variance_returns_none():
    assert _pearson_r([1, 1, 1], [1, 2, 3]) is None
    assert _pearson_r([1, 2, 3], [5, 5, 5]) is None


def test_pearson_too_short_returns_none():
    assert _pearson_r([1], [2]) is None
    assert _pearson_r([], []) is None


def test_pearson_uncorrelated_near_zero():
    r = _pearson_r([1, 2, 3, 4, 5, 6, 7, 8], [3, 8, 2, 7, 4, 1, 6, 5])
    assert r is not None and abs(r) < 0.5  # weak/no linear relation


from armature.state.leverage import compute_leverage, LeverageReport
from armature.state.traces import TraceRecord


def _trace(run_id, stage_id, *, role="judge", quorum=0.5, success=True, valid=True, latency=100.0, esc=0):
    return TraceRecord(
        run_id=run_id, workflow_name="wf", stage_id=stage_id, role_type=role,
        model="m", quorum_score=quorum, success=success, output_valid=valid,
        latency_ms=latency, escalation_count=esc,
    )


# A strong-correlation fixture: judge_a quorum varies across runs and drives
# run HQS; worker is constant (zero variance).
_QUORUM_SERIES = [0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, 0.45]


def _strong_fixture(n_runs: int):
    traces = []
    for i, q in enumerate(_QUORUM_SERIES[:n_runs]):
        rid = f"r{i}"
        traces.append(_trace(rid, "judge_a", quorum=q))
        traces.append(_trace(rid, "worker", role="worker", quorum=0.5))  # constant
    return traces


def test_leverage_strong_stage_attribution():
    report = compute_leverage(_strong_fixture(10))
    a = report.stages["judge_a"]
    assert a.r is not None and a.r > 0.9
    assert a.sufficient is True
    assert a.signal_name == "quorum"
    # worker is constant -> zero variance -> r None, not sufficient
    w = report.stages["worker"]
    assert w.r is None
    assert w.sufficient is False
    assert report.n_runs == 10
    assert report.sufficient is True
    assert report.reason == "ok"


def test_leverage_min_runs_guard():
    report = compute_leverage(_strong_fixture(7), min_runs=8)
    assert report.n_runs == 7
    assert report.sufficient is False
    assert report.stages["judge_a"].sufficient is False  # strong r but too few runs
    assert "need" in report.reason


def test_leverage_min_abs_r_guard_via_param():
    # Strong fixture, but require an impossible threshold -> no stage qualifies.
    report = compute_leverage(_strong_fixture(10), min_abs_r=1.5)
    assert report.sufficient is False
    assert report.stages["judge_a"].sufficient is False
    assert "no stage" in report.reason


def test_leverage_noise_stage_not_sufficient():
    # judge_driver drives HQS (large-range monotonic quorum); judge_noise has a
    # tiny no-trend range -> uncorrelated with the driver-dominated run HQS.
    traces = []
    driver = [0.30, 0.40, 0.50, 0.60, 0.70, 0.78, 0.84, 0.88, 0.92, 0.95]
    noise = [0.50, 0.51, 0.49, 0.50, 0.51, 0.49, 0.50, 0.51, 0.49, 0.50]
    for i in range(10):
        rid = f"r{i}"
        traces.append(_trace(rid, "judge_driver", quorum=driver[i]))
        traces.append(_trace(rid, "judge_noise", quorum=noise[i]))
        traces.append(_trace(rid, "worker", role="worker", quorum=0.5))
    report = compute_leverage(traces)
    assert report.stages["judge_driver"].sufficient is True
    noise_r = report.stages["judge_noise"].r
    assert noise_r is not None and abs(noise_r) < 0.4
    assert report.stages["judge_noise"].sufficient is False
    assert report.sufficient is True  # driver qualifies


def test_leverage_non_judge_stage_uses_success_valid_signal():
    # A worker stage with no quorum; its success/valid tracks HQS via failures.
    traces = []
    # runs 0-4: worker succeeds; runs 5-9: worker fails (success=False).
    # worker success varies; run HQS varies with it.
    for i in range(10):
        rid = f"r{i}"
        ok = i < 5
        traces.append(_trace(rid, "worker", role="worker", quorum=None, success=ok, valid=ok))
        # a constant judge so HQS isn't only driven by worker
        traces.append(_trace(rid, "judge", quorum=0.7))
    report = compute_leverage(traces)
    w = report.stages["worker"]
    assert w.signal_name == "success_valid"
    assert w.r is not None  # defined (varies)


def test_leverage_empty_and_single_run():
    assert compute_leverage([]).sufficient is False
    one = [_trace("r0", "judge_a", quorum=0.8), _trace("r0", "worker", role="worker", quorum=0.5)]
    report = compute_leverage(one)
    assert report.sufficient is False
    assert report.reason == "need >=2 runs"

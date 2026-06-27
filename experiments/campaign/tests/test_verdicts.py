from campaign_runner import verdicts


def _row(run_id, phase_id="ramp", lever="input_difficulty_ramp", difficulty=1,
         hqs_auth=0.8, improve_log=None, recovery=None, memory_mode=None):
    return {
        "run_id": run_id, "phase_id": phase_id, "lever": lever,
        "inputs": {"difficulty": str(difficulty)},
        "exit_code": 0,
        "hqs_ours": {"authoritative": hqs_auth, "rolling": hqs_auth,
                     "dashboard": hqs_auth, "feedback": hqs_auth},
        "hqs_armature": {"authoritative": hqs_auth, "rolling": hqs_auth,
                         "dashboard": hqs_auth, "feedback": hqs_auth},
        "improve_log": improve_log or [],
        "recovery_hqs_ours": recovery,
        "spec_diff": "", "memory_mode": memory_mode,
    }


def test_h1_pass_when_hqs_falls_with_difficulty():
    rows = [_row("r1", difficulty=1, hqs_auth=0.9),
            _row("r2", difficulty=2, hqs_auth=0.7),
            _row("r3", difficulty=3, hqs_auth=0.5),
            _row("r4", difficulty=4, hqs_auth=0.4),
            _row("r5", difficulty=5, hqs_auth=0.3)]
    name, result, detail = verdicts.verdict_h1(rows, {"spearman_le": -0.5, "p_le": 0.05})
    assert result == "PASS"
    assert detail["spearman_rho"] <= -0.5


def test_h1_fail_when_hqs_flat():
    rows = [_row(f"r{i}", difficulty=i, hqs_auth=0.7) for i in range(1, 8)]
    _name, result, _detail = verdicts.verdict_h1(rows, {"spearman_le": -0.5, "p_le": 0.05})
    assert result == "FAIL"


def test_h2_pass_when_fires_and_recovers():
    improve_log = [{"needs_improvement": True, "n_traces": 5, "hqs_before": 0.4,
                    "applied": True, "diagnostics": []}]
    rows = [_row("r1", phase_id="corr", lever="spec_corruption", hqs_auth=0.4,
                 improve_log=improve_log, recovery={"authoritative": 0.8})]
    _name, result, detail = verdicts.verdict_h2(rows, {
        "fires_within_k_traces": 5, "edits_correct_surface": True,
        "recovers_above": 0.75, "within_r_runs": 5})
    assert result == "PASS"
    assert detail["recovered_to"] >= 0.75


def test_h2_inconclusive_when_no_improve_log():
    rows = [_row("r1", lever="spec_corruption", hqs_auth=0.4, improve_log=[])]
    _name, result, _detail = verdicts.verdict_h2(rows, {
        "fires_within_k_traces": 5, "edits_correct_surface": True,
        "recovers_above": 0.75, "within_r_runs": 5})
    assert result == "INCONCLUSIVE"


def test_h3_pass_when_formulas_agree():
    rows = [_row("r1") for _ in range(3)]     # ours == armature => delta 0
    _name, result, _detail = verdicts.verdict_h3(rows, {"max_abs_delta_le": 0.02})
    assert result == "PASS"


def test_h3_ignores_scope_mismatched_channels():
    # rolling & dashboard compare per-run rows (ours) vs across-run DB values
    # (armature) — different row sets by design. Their divergence must NOT
    # fail H3; only authoritative (same row set both sides) counts.
    rows = [_row("r1"), _row("r2"), _row("r3")]
    for r in rows:
        r["hqs_armature"]["rolling"] = r["hqs_ours"]["rolling"] + 0.5
        r["hqs_armature"]["dashboard"] = r["hqs_ours"]["dashboard"] + 0.6
    _name, result, detail = verdicts.verdict_h3(rows, {"max_abs_delta_le": 0.02})
    assert result == "PASS"
    assert detail["max_delta"] <= 0.02          # authoritative matched
    assert detail["excluded"]["dashboard"] >= 0.6
    assert detail["excluded"]["rolling"] >= 0.5


def test_h3_fails_on_authoritative_divergence():
    rows = [_row("r1"), _row("r2")]
    for r in rows:
        r["hqs_armature"]["authoritative"] = r["hqs_ours"]["authoritative"] + 0.5
    _name, result, detail = verdicts.verdict_h3(rows, {"max_abs_delta_le": 0.02})
    assert result == "FAIL"
    assert detail["max_delta"] >= 0.5
    assert detail["compared"] == "authoritative"


def test_h3_inconclusive_when_too_few_observations():
    # no authoritative observations -> cannot compare the apples-to-apples channel
    r = _row("r1")
    r["hqs_armature"] = {"authoritative": None, "rolling": None,
                         "dashboard": r["hqs_ours"]["dashboard"], "feedback": None}
    _name, result, detail = verdicts.verdict_h3([r], {"max_abs_delta_le": 0.02})
    assert result == "INCONCLUSIVE"
    assert detail["n_comparable"] == 0


def test_h4_pass_when_warm_beats_cold():
    rows = [_row("c1", lever="none", memory_mode="cold", hqs_auth=0.6),
            _row("c2", lever="none", memory_mode="cold", hqs_auth=0.6),
            _row("w1", lever="none", memory_mode="warm", hqs_auth=0.8),
            _row("w2", lever="none", memory_mode="warm", hqs_auth=0.8)]
    _name, result, _detail = verdicts.verdict_h4(rows, {
        "warm_minus_cold_mean_ge": 0.05, "bootstrap_ci_lower_ge": 0.0})
    assert result == "PASS"
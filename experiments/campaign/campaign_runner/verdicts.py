"""The four hypothesis verdicts. Each returns (name, result, detail).

INCONCLUSIVE is first-class: it means the data couldn't settle the question
and points at an observability gap, not a quiet failure.
"""
from __future__ import annotations

from campaign_runner import stats

PASS, FAIL, INCON = "PASS", "FAIL", "INCONCLUSIVE"

# Levers whose purpose is to degrade HQS below target so self_improve fires.
# H2 aggregates firing + recovery across any of these.
DEGRADATION_LEVERS = {"spec_corruption", "model_tier_degradation"}


def verdict_h1(rows: list[dict], th: dict) -> tuple[str, str, dict]:
    """HQS tracks input difficulty (negative correlation expected)."""
    pts = [(float(r["inputs"].get("difficulty", 0) or 0), r["hqs_ours"]["authoritative"])
           for r in rows if r["lever"] == "input_difficulty_ramp"
           and r["hqs_ours"]["authoritative"] is not None]
    if len(pts) < 4:
        return ("hqs_tracks_difficulty", INCON, {"n_points": len(pts)})
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    rho = stats.spearman_rho(xs, ys)
    p = stats.permutation_p(xs, ys, seed=12345, n=2000)
    ok = rho <= th.get("spearman_le", -0.5) and p <= th.get("p_le", 0.05)
    return ("hqs_tracks_difficulty", PASS if ok else FAIL,
            {"spearman_rho": round(rho, 4), "p_value": round(p, 4), "n_points": len(pts)})


def verdict_h2(rows: list[dict], th: dict) -> tuple[str, str, dict]:
    """Self-improve fires and recovers after a degradation lever."""
    corr = [r for r in rows if r["lever"] in DEGRADATION_LEVERS]
    if not corr:
        return ("self_improve_fires_and_recovers", INCON, {"n_degradation_runs": 0})
    # any firing in any degradation run's improve_log
    fired = any(any(lr.get("needs_improvement") for lr in r["improve_log"]) for r in corr)
    recovered = [r["recovery_hqs_ours"]["authoritative"] for r in corr
                 if r.get("recovery_hqs_ours") and r["recovery_hqs_ours"].get("authoritative") is not None]
    if not fired:
        return ("self_improve_fires_and_recovers", INCON if not recovered else FAIL,
                {"fired": False, "n_degradation_runs": len(corr)})
    recovers_above = th.get("recovers_above", 0.75)
    ok = bool(recovered) and max(recovered) >= recovers_above
    return ("self_improve_fires_and_recovers", PASS if ok else FAIL,
            {"fired": fired, "recovered_to": max(recovered) if recovered else None,
             "n_degradation_runs": len(corr)})


def verdict_h3(rows: list[dict], th: dict) -> tuple[str, str, dict]:
    """HQS formula consistency: ours vs Armature's INDEPENDENTLY emitted values.

    Only compares a formula where Armature actually emitted a value for it
    (hqs_armature[k] is not None). rolling/feedback are opportunistic — if
    they were never emitted, that is an observability gap, not a pass.
    """
    deltas: list[tuple[str, float]] = []
    for r in rows:
        ours, arm = r["hqs_ours"], r["hqs_armature"]
        for k in ours:
            a, b = ours.get(k), arm.get(k)
            if a is None or b is None:
                continue
            deltas.append((k, abs(a - b)))
    if len(deltas) < 2:
        return ("hqs_formula_consistency", INCON,
                {"n_comparable": len(deltas),
                 "note": "Armature emitted too few independent HQS values to compare"})
    max_delta = max(d for _k, d in deltas)
    ok = max_delta <= th.get("max_abs_delta_le", 0.02)
    by_formula = {}
    for k, d in deltas:
        by_formula.setdefault(k, 0.0)
        by_formula[k] = max(by_formula[k], d)
    return ("hqs_formula_consistency", PASS if ok else FAIL,
            {"max_delta": round(max_delta, 4), "n_comparable": len(deltas),
             "by_formula": {k: round(v, 4) for k, v in by_formula.items()}})


def verdict_h4(rows: list[dict], th: dict) -> tuple[str, str, dict]:
    """Memory + carry-forward helps: warm HQS > cold HQS."""
    cold = [r["hqs_ours"]["authoritative"] for r in rows if r.get("memory_mode") == "cold"
            and r["hqs_ours"]["authoritative"] is not None]
    warm = [r["hqs_ours"]["authoritative"] for r in rows if r.get("memory_mode") == "warm"
            and r["hqs_ours"]["authoritative"] is not None]
    if not cold or not warm:
        return ("memory_carry_forward_helps", INCON, {"n_cold": len(cold), "n_warm": len(warm)})
    diffs = [w - c for w in warm for c in cold]
    mean_diff, lo, hi = stats.bootstrap_ci(diffs, seed=12345, n=2000)
    ok = mean_diff >= th.get("warm_minus_cold_mean_ge", 0.05) and lo >= th.get("bootstrap_ci_lower_ge", 0.0)
    return ("memory_carry_forward_helps", PASS if ok else FAIL,
            {"mean_diff": round(mean_diff, 4), "ci_low": round(lo, 4), "ci_high": round(hi, 4),
             "n_cold": len(cold), "n_warm": len(warm)})


def all_verdicts(rows: list[dict], plan) -> list[tuple[str, str, dict]]:
    v = plan.verdicts
    return [
        verdict_h1(rows, v.hqs_tracks_difficulty),
        verdict_h2(rows, v.self_improve_fires_and_recovers),
        verdict_h3(rows, v.hqs_formula_consistency),
        verdict_h4(rows, v.memory_carry_forward_helps),
    ]
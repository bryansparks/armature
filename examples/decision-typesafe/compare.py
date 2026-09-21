#!/usr/bin/env python3
"""A/B comparison for the TypeSafe decision adapter spike (Layer 1).

Called by the Armature ScriptNode as the final stage of ab-demo.yml.
Reads ARMATURE_CONTEXT, which holds (index-aligned — fan-out preserves
item order):

  load_benchmark — {"states": [{id, text, labels}]}
  decide         — fan-in list of decide.py results per state
  llm_judge      — fan-in list of guided_json judge results per state

Prints a summary JSON object on stdout (parse: json): accuracy vs the
hand-labeled benchmark for both systems, decision-vs-judge agreement,
per-call decision latency, and estimated decision cost at $42/Btok
input tokens.

Ground truth caveat: labels are hand-authored for this spike, so the
absolute numbers are only as good as the labeling. The decision-vs-
judge agreement number is label-independent.
"""
from __future__ import annotations

import json
import os
import sys

# TypeSafe pricing: $42 per billion input tokens, output tokens free.
USD_PER_BTOK_INPUT = 42.0

DIMENSIONS = ("is_concrete", "severity", "actionability")


def _failed(result) -> bool:
    return isinstance(result, dict) and "_fan_out_error" in result


def noul_truth(answer: dict) -> float:
    """Read a noul answer's truth value.

    The live API nests it under 'noul'; 'truth' kept as fallback for older
    mocked shapes.
    """
    value = answer.get("noul", answer.get("truth", 0.0))
    return float(value)


def noul_correct(answer: dict, label: bool) -> bool:
    return (noul_truth(answer) >= 0.5) == bool(label)


def choice_correct(answer: dict, label: str) -> bool:
    return answer.get("choice") == label


def score_correct(answer: dict, label: int) -> bool:
    return round(float(answer.get("score", 0.0))) == int(label)


def _decide_answer(result: dict, dimension: str, label, labels: dict) -> bool | None:
    answer = result.get("answers", {}).get(dimension)
    if answer is None:
        return None
    if dimension == "is_concrete":
        return noul_correct(answer, label)
    if dimension == "severity":
        return choice_correct(answer, label)
    return score_correct(answer, label)


def _judge_correct(result: dict, dimension: str, label) -> bool | None:
    key = {"is_concrete": "concrete", "severity": "severity", "actionability": "actionability"}[dimension]
    if key not in result:
        return None
    if dimension == "is_concrete":
        return bool(result[key]) == bool(label)
    if dimension == "severity":
        return result[key] == label
    return round(float(result[key])) == int(label)


def _decide_flat(result: dict, dimension: str):
    answer = result.get("answers", {}).get(dimension)
    if answer is None:
        return None
    if dimension == "is_concrete":
        return noul_truth(answer) >= 0.5
    if dimension == "severity":
        return answer.get("choice")
    return round(float(answer.get("score", 0.0)))


def _judge_flat(result: dict, dimension: str):
    key = {"is_concrete": "concrete", "severity": "severity", "actionability": "actionability"}[dimension]
    if key not in result:
        return None
    if dimension == "is_concrete":
        return bool(result[key])
    if dimension == "severity":
        return result[key]
    return round(float(result[key]))


def _accuracy_per_dimension(hits: dict) -> dict:
    out = {}
    total = 0
    correct = 0
    for dim in DIMENSIONS:
        dim_hits = [h for h in hits[dim] if h is not None]
        out[dim] = (sum(dim_hits) / len(dim_hits)) if dim_hits else None
        total += len(dim_hits)
        correct += sum(dim_hits)
    out["overall"] = (correct / total) if total else None
    return out


def summarize(states: list, decide_results: list, judge_results: list) -> dict:
    """Compute the A/B summary. Lists must be index-aligned to `states`."""
    if len(states) != len(decide_results) or len(states) != len(judge_results):
        raise ValueError(
            f"Alignment broken: {len(states)} states, "
            f"{len(decide_results)} decide results, {len(judge_results)} judge results"
        )

    decision_hits = {d: [] for d in DIMENSIONS}
    judge_hits = {d: [] for d in DIMENSIONS}
    agree_hits = {d: [] for d in DIMENSIONS}
    latencies: list[float] = []
    input_tokens = 0
    pairs = 0
    decision_failures = 0
    judge_failures = 0
    per_state: list[dict] = []

    for state, dec, jud in zip(states, decide_results, judge_results):
        labels = state.get("labels", {})
        if _failed(dec):
            decision_failures += 1
        if _failed(jud):
            judge_failures += 1
        if _failed(dec) or _failed(jud):
            per_state.append({"id": state.get("id"), "failed": True})
            continue
        pairs += 1

        state_detail = {
            "id": state.get("id"),
            "label": {"is_concrete": labels.get("is_concrete"), "severity": labels.get("severity"), "actionability": labels.get("actionability")},
            "decision": {
                "is_concrete": _decide_flat(dec, "is_concrete"),
                "severity": _decide_flat(dec, "severity"),
                "actionability": _decide_flat(dec, "actionability"),
            },
            "judge": {
                "is_concrete": _judge_flat(jud, "is_concrete"),
                "severity": _judge_flat(jud, "severity"),
                "actionability": _judge_flat(jud, "actionability"),
            },
            "missed": [],
        }

        for dim in DIMENSIONS:
            label = labels.get(dim)
            d_hit = _decide_answer(dec, dim, label, labels)
            j_hit = _judge_correct(jud, dim, label)
            decision_hits[dim].append(d_hit)
            judge_hits[dim].append(j_hit)
            d_flat = _decide_flat(dec, dim)
            j_flat = _judge_flat(jud, dim)
            if d_flat is not None and j_flat is not None:
                agree_hits[dim].append(d_flat == j_flat)
            if d_hit is False:
                state_detail["missed"].append(dim)

        per_state.append(state_detail)
        latencies.append(float(dec.get("latency_ms", 0.0)))
        input_tokens += int(dec.get("usage", {}).get("input_tokens", 0))

    lat_sorted = sorted(latencies)
    n = len(lat_sorted)
    latency = {
        "mean": round(sum(lat_sorted) / n, 1) if n else None,
        "p50": round(lat_sorted[n // 2], 1) if n else None,
        "max": round(lat_sorted[-1], 1) if n else None,
    }

    return {
        "pairs": pairs,
        "decision_failures": decision_failures,
        "judge_failures": judge_failures,
        "decision": _accuracy_per_dimension(decision_hits),
        "judge": _accuracy_per_dimension(judge_hits),
        "agreement": _accuracy_per_dimension(agree_hits),
        "decision_latency_ms": latency,
        "decision_input_tokens": input_tokens,
        "decision_cost_usd": round(input_tokens * USD_PER_BTOK_INPUT / 1e9, 6),
        "per_state": per_state,
    }


def main() -> int:
    try:
        context = json.loads(os.environ.get("ARMATURE_CONTEXT", "{}"))
        states = context["load_benchmark"]["states"]
        decide_results = context["decide"]
        judge_results = context["llm_judge"]
        summary = summarize(states, decide_results, judge_results)
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"compare.py: {exc}", file=sys.stderr)
        return 1
    json.dump(summary, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

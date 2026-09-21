#!/usr/bin/env python3
"""TypeSafe AI decision adapter for Armature script stages (Layer 1 spike).

Called by the Armature ScriptNode. Reads the state to judge from
`--state-file`, `--state`, or ARMATURE_CONTEXT's `state_item.text`
(the partition_key used by ab-demo.yml's fan-out). Questions come from
`--questions-file` (shared battery for the A/B). Calls the TypeSafe
decision API (POST /v1/systemone, model jev-latest) and prints a
top-level JSON object on stdout for `parse: json`.

With --dry-run, returns canned type-correct answers without a key or
network — lets the whole workflow run offline.

Env vars used:
  ARMATURE_CONTEXT  — JSON blob with full workflow context (set by engine)
  TYPESAFE_API_KEY  — TypeSafe decision API key

Output contract (parse: json): exit 0 + JSON object on stdout:
  {model, answers, usage, latency_ms, [dry_run]}
Failures: exit 1, human-readable message on stderr, nothing on stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
REQUEST_TIMEOUT_SECONDS = 10.0
# Exponential backoff between retry attempts (429/529); overridden in tests.
RETRY_BACKOFF_SECONDS = [0.5, 1.0, 2.0]
MAX_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = {429, 529}
STATE_FALLBACK = os.environ.get("TYPESAFE_STATE", "")


class DecisionError(Exception):
    """Fatal adapter failure — surfaces on stderr with exit 1."""


def build_client(timeout: float) -> httpx.Client:
    """Client factory; tests swap this for httpx.MockTransport."""
    return httpx.Client(timeout=timeout)


def resolve_state(cli_state: str | None) -> str:
    """State text: --state flag > ARMATURE_CONTEXT state_item.text > --state-file."""
    if cli_state:
        return cli_state
    raw = os.environ.get("ARMATURE_CONTEXT", "")
    if raw:
        try:
            ctx = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DecisionError(f"ARMATURE_CONTEXT is not valid JSON: {exc}") from exc
        item = ctx.get("state_item")
        if isinstance(item, dict) and item.get("text"):
            return str(item["text"])
        if isinstance(item, str) and item:
            return item
    if STATE_FALLBACK:
        return STATE_FALLBACK
    raise DecisionError(
        "No state to judge: pass --state, --state-file, or run under "
        "Armature with partition_key: state_item"
    )


def _canned_answers(questions: dict) -> dict:
    """Type-correct placeholder answers matching the live API's answer shape."""
    answers: dict = {}
    for qid, q in questions.items():
        qtype = q.get("type")
        if qtype == "noul":
            answers[qid] = {"type": "noul", "noul": 0.5}
        elif qtype == "choice":
            options = list(q.get("criteria", {}))
            pick = options[0] if options else "unknown"
            answers[qid] = {
                "type": "choice",
                "choice": pick,
                "probabilities": {pick: 1.0},
                "confidence": 0.5,
            }
        elif qtype == "score":
            criteria = q.get("criteria", [])
            mid = len(criteria) // 2 if criteria else 0
            probs = {str(i): 0.0 for i in range(len(criteria))}
            if criteria:
                probs[str(min(mid, len(criteria) - 1))] = 1.0
            answers[qid] = {
                "type": "score",
                "score": float(mid),
                "legend": {str(i): c for i, c in enumerate(criteria)},
                "probabilities": probs,
                "confidence": 0.5,
            }
        else:
            raise DecisionError(f"Question '{qid}' has unknown type '{qtype}'")
    return answers


def run_decision(
    state: str,
    questions: dict,
    api_key: str | None,
    model: str = MODEL,
    dry_run: bool = False,
) -> dict:
    """One decision call: one state, all questions, ~100ms.

    Returns the answer dict plus latency_ms and usage for the A/B metrics.
    """
    if dry_run:
        return {
            "model": model,
            "answers": _canned_answers(questions),
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "latency_ms": 0,
            "dry_run": True,
        }

    if not api_key:
        raise DecisionError("TYPESAFE_API_KEY is not set (and --dry-run was not passed)")

    payload = {"state": state, "model": model, "questions": questions}
    started = time.perf_counter()
    client = build_client(REQUEST_TIMEOUT_SECONDS)
    with client:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = client.post(
                    API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.HTTPError as exc:
                raise DecisionError(f"TypeSafe API request failed: {exc}") from exc

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
                continue
            if response.status_code != 200:
                raise DecisionError(
                    f"TypeSafe API returned {response.status_code}: {response.text[:300]}"
                )
            body = response.json()
            break

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "model": body.get("model", model),
        "answers": body.get("answers", {}),
        "usage": body.get("usage", {}),
        "latency_ms": latency_ms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TypeSafe decision adapter for Armature stages")
    parser.add_argument("--questions-file", required=True, help="JSON file mapping question ids to question specs")
    parser.add_argument("--state", help="State text to judge (default: ARMATURE_CONTEXT state_item)")
    parser.add_argument("--state-file", help="File containing the state text")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--dry-run", action="store_true", help="Canned answers, no API call")
    args = parser.parse_args(argv)

    try:
        state = resolve_state(args.state)
        if args.state_file:
            with open(args.state_file) as fh:
                state = fh.read().strip()
        with open(args.questions_file) as fh:
            try:
                questions = json.load(fh)
            except json.JSONDecodeError as exc:
                raise DecisionError(f"Questions file {args.questions_file} is not valid JSON: {exc}") from exc
        if not isinstance(questions, dict) or not questions:
            raise DecisionError(f"Questions file {args.questions_file} must be a non-empty JSON object")
        result = run_decision(
            state=state,
            questions=questions,
            api_key=os.environ.get("TYPESAFE_API_KEY"),
            model=args.model,
            dry_run=args.dry_run,
        )
    except DecisionError as exc:
        print(f"decide.py: {exc}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

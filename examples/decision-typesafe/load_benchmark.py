#!/usr/bin/env python3
"""Benchmark loader for the TypeSafe decision A/B demo (Layer 1 spike).

Prints the benchmark states as {"states": [...]} on stdout for `parse: json`,
so the fan-out stages can partition over them. Optionally --limit N to
run a cheaper subset.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Load benchmark states for the A/B demo")
    parser.add_argument(
        "--file",
        default=str(Path(__file__).with_name("benchmark.json")),
        help="Path to the benchmark JSON (default: benchmark.json next to this script)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Only load the first N states")
    args = parser.parse_args()

    try:
        with open(args.file) as fh:
            benchmark = json.load(fh)
        states = benchmark["states"]
        if args.limit is not None:
            states = states[: args.limit]
    except (OSError, ValueError, KeyError) as exc:
        print(f"load_benchmark.py: {exc}", file=sys.stderr)
        return 1

    json.dump({"states": states}, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

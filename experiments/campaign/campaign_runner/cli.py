"""Entrypoint argparse: run a campaign, or replay a recording."""
from __future__ import annotations

import argparse
from pathlib import Path

from campaign_runner.plan import load_plan
from campaign_runner.runner import CampaignRunner

HARNESS_ROOT = Path(__file__).resolve().parent.parent   # experiments/campaign/


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run.py", description="Armature campaign runner")
    ap.add_argument("plan", nargs="?", default=None, help="path to a campaign plan YAML")
    ap.add_argument("--replay", metavar="DIR", help="replay a recording dir instead of running")
    ap.add_argument("--record", action="store_true", help="record a run for later replay")
    ap.add_argument("--out-dir", default=str(HARNESS_ROOT / "out"),
                    help="where to write campaign artifacts (default: experiments/campaign/out)")
    ap.add_argument("--build-index", metavar="OUT_DIR",
                    help="build out/index.html linking all reports under OUT_DIR, then exit")
    args = ap.parse_args(argv)

    if args.build_index:
        from campaign_runner.report import build_index
        idx = build_index(Path(args.build_index))
        print(f"index -> {idx}")
        return 0
    if args.plan is None:
        ap.error("plan is required unless --build-index is given")

    plan = load_plan(Path(args.plan))
    src = _resolve_source_spec(plan)
    if args.replay:
        r = CampaignRunner(plan, src, root=Path(args.out_dir))
        result = r.replay(Path(args.replay))
        print(f"replayed {len(result.rows)} runs -> {result.report_path}")
        return 0

    r = CampaignRunner(plan, src, root=Path(args.out_dir), record_mode=args.record)
    result = r.run()
    print(f"ran {len(result.rows)} runs -> {result.report_path}")
    return 0


def _resolve_source_spec(plan) -> Path:
    p = Path(plan.workflow)
    if not p.is_absolute():
        p = HARNESS_ROOT / p
    if not p.exists():
        p = HARNESS_ROOT / "tests" / "fixtures" / "sample_spec.yml"
    return p
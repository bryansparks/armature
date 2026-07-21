"""Generate a PREVIEW 3-arm report.html with synthetic data, so the report
format is visible before the live campaign run (which is deferred to OpenRouter
credits). Run: python experiments/campaign/preview_3arm_report.py --out <path>
(or: python -m campaign_runner.preview_3arm_report --out <path>)."""
import argparse
from pathlib import Path
from campaign_runner.report import build_3arm_preview


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a PREVIEW 3-arm report.html")
    ap.add_argument("--out", default=str(Path(".superpowers/sdd/preview-3arm-report.html")),
                    help="output path (default .superpowers/sdd/preview-3arm-report.html)")
    args = ap.parse_args(argv)
    p = build_3arm_preview(out_path=Path(args.out))
    print(f"wrote PREVIEW report -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
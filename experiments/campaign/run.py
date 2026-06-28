#!/usr/bin/env python3
"""Campaign runner entrypoint. Usage:
  python experiments/campaign/run.py plans/hqdynamics.yml
  python experiments/campaign/run.py plans/quick.yml --replay tests/fixtures/demo_recording
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from campaign_runner.cli import main

if __name__ == "__main__":
    sys.exit(main())
"""Record/replay payload: capture everything needed to regenerate campaign.jsonl
and report.html WITHOUT invoking Armature or any LLM. Deterministic replay is
the guarantee that 'rerun the whole thing' is real.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from campaign_runner import trace_io


class Recording:
    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "runs.jsonl"

    def record_run(self, run_id: str, argv: list[str], stdout: str, stderr: str,
                   exit_code: int, trace_rows: list[dict], sidecars: dict[str, str],
                   dashboard_json: dict) -> None:
        row = {"run_id": run_id, "argv": argv, "stdout": stdout, "stderr": stderr,
               "exit_code": exit_code, "trace_rows": trace_rows, "sidecars": sidecars,
               "dashboard_json": dashboard_json}
        with open(self.path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def replay(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


def capture_trace_rows(db_path: Path, run_id: str) -> list[dict]:
    return [asdict(r) for r in trace_io.read_rows_by_run(db_path, run_id)]
"""Subprocess wrappers around the Armature CLI. All isolation lives here:
every call gets HOME=<sandbox> so `armature run` writes to <sandbox>/.armature.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from campaign_runner import trace_io
from campaign_runner.record import Recording
from campaign_runner.sandbox import Sandbox


@dataclass
class RunOutcome:
    run_id: str | None
    exit_code: int
    stdout: str
    stderr: str
    result_json: dict | None
    # HQS Armature independently emitted for this run (authoritative via
    # `armature replay`, dashboard via `armature dashboard`). Populated for
    # main runs; None for recovery probes. Carried into the recording so
    # zero-cost replay can restore hqs_armature without re-invoking Armature.
    hqs_armature: dict | None = None


@dataclass
class ImproveOutcome:
    exit_code: int
    stderr: str
    improve_log: list[dict] = field(default_factory=list)
    pending: str | None = None
    applied: bool = False


class CliDriver:
    def __init__(self, sandbox: Sandbox, record: Recording | None = None) -> None:
        self.sb = sandbox
        self.record = record

    def _armature(self, args: list[str], *, capture_output: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["armature", *args], env=self.sb.env(),
                               capture_output=capture_output, text=True)

    def run(self, working_spec: Path, inputs: dict, workflow_name: str = "",
            tag: str = "main", meta: dict | None = None) -> RunOutcome:
        # --force bypasses armature's checkpoint/resume: without it, a run whose
        # (spec, inputs) match a prior run resumes from checkpoint and writes no
        # new trace rows / --output, so the harness would mis-attribute that
        # rep's HQS to a prior run. Every campaign rep must be a fresh, isolated
        # execution — the harness measures per-run HQS, not cached results.
        out_json = self.sb.dir / f"run_{abs(hash(tuple(sorted(inputs.items()))) ) % 100000}.json"
        args = ["run", str(working_spec), "--quiet", "--force", "--output", str(out_json)]
        for k, v in inputs.items():
            args += ["--input", f"{k}={v}"]
        cp = self._armature(args)
        result_json = None
        if out_json.exists():
            try:
                result_json = json.loads(out_json.read_text())
            except json.JSONDecodeError:
                result_json = None
        run_id = None
        if result_json and isinstance(result_json, dict) and result_json.get("run_id"):
            run_id = result_json["run_id"]
        else:
            run_id = trace_io.latest_run_id(self.sb.trace_db, workflow_name) if workflow_name else None
        # Capture the HQS values Armature independently emits for this run, so
        # the runner's hqs_armature column and the recording both come from one
        # source (no copy from hqs_ours). Only main runs need these; recovery
        # probes feed recovery_hqs_ours instead.
        hqs_armature: dict | None = None
        if tag == "main" and run_id:
            arm_auth = self.replay_hqs(run_id)
            dashboard_json = self.dashboard_json(workflow_name)
            hqs_armature = {"authoritative": arm_auth,
                            "dashboard": (dashboard_json or {}).get("current_hqs")}
        if self.record:
            from campaign_runner.record import capture_trace_rows
            trace_rows = (capture_trace_rows(self.sb.trace_db, run_id)
                          if run_id else [])
            dashboard_json = ({"current_hqs": hqs_armature["dashboard"]}
                              if hqs_armature else {})
            self.record.record_run(run_id, ["armature", *args], cp.stdout, cp.stderr,
                                    cp.returncode, trace_rows,
                                    {}, dashboard_json, tag=tag,
                                    hqs_armature=hqs_armature, meta=meta)
        return RunOutcome(run_id, cp.returncode, cp.stdout, cp.stderr, result_json,
                          hqs_armature=hqs_armature)

    def improve(self, working_spec: Path, *, target_hqs: float, min_traces: int,
                apply: bool) -> ImproveOutcome:
        stem = working_spec.stem
        args = ["improve", str(working_spec), "--traces", str(self.sb.trace_db),
                "--target-hqs", str(target_hqs), "--min-traces", str(min_traces),
                "--apply" if apply else "--no-apply"]
        cp = self._armature(args)
        parent = working_spec.parent
        log = trace_io.read_improve_log(parent / f"{stem}.improve_log.jsonl")
        pending = trace_io.read_pending(working_spec.with_suffix("").with_name(stem + ".pending.yaml"))
        applied = bool(log and log[-1].get("applied"))
        return ImproveOutcome(cp.returncode, cp.stderr, log, pending, applied)

    def dashboard_json(self, workflow_name: str) -> dict:
        cp = self._armature(["dashboard", "--workflow", workflow_name,
                             "--traces", str(self.sb.trace_db), "--format", "json"])
        try:
            return json.loads(cp.stdout) if cp.stdout else {}
        except json.JSONDecodeError:
            return {}

    def replay_hqs(self, run_id: str) -> float | None:
        cp = self._armature(["replay", run_id, "--traces", str(self.sb.trace_db)])
        return parse_hqs_from_text(cp.stdout)

    def validate(self, working_spec: Path) -> bool:
        cp = self._armature(["validate", str(working_spec)])
        return cp.returncode == 0


def parse_hqs_from_text(text: str) -> float | None:
    import re
    m = re.search(r"HQS:\s*([0-9]+\.?[0-9]*)", text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None
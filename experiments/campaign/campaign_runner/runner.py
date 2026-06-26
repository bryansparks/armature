"""CampaignRunner: the serial phase→drive→record→improve→recover→verdict loop.

In replay mode it reconstructs campaign.jsonl from a Recording without ever
invoking Armature or an LLM — the core 'mock operation mode'.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from campaign_runner import fault, hqs, trace_io, verdicts as verdicts_mod
from campaign_runner.cli_driver import CliDriver, parse_hqs_from_text
from campaign_runner.plan import CampaignPlan
from campaign_runner.record import Recording, capture_trace_rows
from campaign_runner.report import render_report
from campaign_runner.sandbox import Sandbox


@dataclass
class CampaignResult:
    rows: list[dict]
    verdicts: list[tuple[str, str, dict]] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    report_path: Path | None = None
    campaign_jsonl: Path | None = None
    gaps_jsonl: Path | None = None


class CampaignRunner:
    def __init__(self, plan: CampaignPlan, source_spec: Path, root: Path,
                 record_mode: bool = False) -> None:
        self.plan = plan
        self.source_spec = Path(source_spec)
        self.sb = Sandbox(plan, root=Path(root))
        self.sb.copy_working_spec(self.source_spec)
        self.record_mode = record_mode
        self.recording = Recording(self.sb.dir / "recording") if record_mode else None
        self.drv = CliDriver(self.sb, self.recording)
        self.workflow_name = self._workflow_name()
        self.campaign_jsonl = self.sb.dir / "campaign.jsonl"
        self.gaps_jsonl = self.sb.dir / "gaps.jsonl"

    def _workflow_name(self) -> str:
        import yaml
        return yaml.safe_load(self.source_spec.read_text()).get("name", "")

    def _row_from_run(self, run_id: str, phase_id: str, lever: str, inputs: dict,
                      exit_code: int, improve_log: list[dict], recovery: dict | None,
                      spec_diff: str, memory_mode: str | None, run_stderr: str = "",
                      gaps: list | None = None) -> dict:
        rows = trace_io.read_rows_by_run(self.sb.trace_db, run_id) if run_id else []
        ours = hqs.all_four(rows)
        # hqs_armature holds ONLY values Armature independently emits, never a
        # copy of ours. authoritative comes from `armature replay` output;
        # dashboard from `armature dashboard --format json`; feedback from the
        # hqs_feedback hook hint in the run's stderr. rolling (improve_log
        # hqs_before) is opportunistic — filled by the improve step, not here.
        arm_auth = self.drv.replay_hqs(run_id) if run_id else None
        # NOTE: hqs_ours["dashboard"] is computed from per-run trace rows
        # (trace_io.read_rows_by_run), while hqs_armature["dashboard"] comes from
        # `armature dashboard --format json`, which computes the HQS trend across
        # ALL workflow traces in the DB — not just this single run. So a persistent
        # dashboard delta in H3 reflects a row-set mismatch, not necessarily formula
        # drift. The formula reproduction itself is verified in tests/test_hqs.py.
        arm_dash = (self.drv.dashboard_json(self.workflow_name) or {}).get("current_hqs") if run_id else None
        arm_feedback = parse_hqs_from_text(run_stderr)
        hqs_armature = {"authoritative": arm_auth, "rolling": None,
                        "dashboard": arm_dash, "feedback": arm_feedback}
        if gaps is not None:
            if arm_auth is None and run_id:
                gaps.append({"want": "authoritative HQS via replay", "needed":
                              "armature replay <run_id> printed an HQS line",
                              "severity": "low", "run_id": run_id})
            if arm_feedback is None:
                gaps.append({"want": "feedback HQS via hook stderr", "needed":
                              "hqs_feedback hook fired and printed an HQS hint",
                              "severity": "low", "run_id": run_id})
        return {"run_id": run_id, "phase_id": phase_id, "lever": lever, "inputs": inputs,
                "exit_code": exit_code, "hqs_ours": ours, "hqs_armature": hqs_armature,
                "improve_log": improve_log, "recovery_hqs_ours": recovery,
                "spec_diff": spec_diff, "memory_mode": memory_mode}

    def run(self) -> CampaignResult:
        corpus_path = self.sb.dir.parent.parent / "corpora" / "difficulty.csv"
        corpus = fault.load_corpus(corpus_path) if corpus_path.exists() else []
        rows: list[dict] = []
        gaps: list[dict] = []
        t0 = time.monotonic()
        llm_calls = 0
        for pi, phase in enumerate(self.plan.phases):
            for rep in range(phase.repeats):
                if self._budget_exceeded(len(rows), llm_calls, time.monotonic() - t0,
                                         trace_io.total_tokens(self.sb.trace_db)):
                    gaps.append({"want": "budget", "needed": "stop before max_runs/llm/wallclock",
                                 "severity": "info"})
                    break
                spec_before = self.sb.working_spec.read_text()
                inputs = fault.apply_lever(phase, phase_index=pi, rep=rep, corpus=corpus,
                                           working_spec=self.sb.working_spec, rng_seed=1000)
                if not self.drv.validate(self.sb.working_spec):
                    gaps.append({"want": "valid spec after lever", "needed": "validate exit 0",
                                 "severity": "high", "phase": phase.id})
                    continue
                out = self.drv.run(self.sb.working_spec, inputs, workflow_name=self.workflow_name)
                llm_calls += 1
                improve_log, recovery, spec_diff = [], None, ""
                if phase.self_improve and phase.self_improve.enabled:
                    improve_log, recovery, improve_llm = self._do_improve(phase, gaps)
                    llm_calls += improve_llm
                    spec_diff = self._diff(spec_before, self.sb.working_spec.read_text())
                rows.append(self._row_from_run(out.run_id, phase.id, phase.lever, inputs,
                                               out.exit_code, improve_log, recovery,
                                               spec_diff, self._memory_mode(),
                                               run_stderr=out.stderr, gaps=gaps))
                # rolling (improve_log hqs_before) is the one Armature emission
                # available only after an improve cycle — fill it in if present.
                if improve_log:
                    rows[-1]["hqs_armature"]["rolling"] = improve_log[-1].get("hqs_before")
        return self._finalize(rows, gaps)

    def _do_improve(self, phase, gaps: list) -> tuple[list[dict], dict | None, int]:
        si = phase.self_improve
        log: list[dict] = []
        improve_rounds = 0
        for _round in range(si.max_rounds):
            imp = self.drv.improve(self.sb.working_spec, target_hqs=si.target_hqs,
                                   min_traces=si.min_traces, apply=si.apply)
            improve_rounds += 1
            log.extend(imp.improve_log)
            if not imp.improve_log or not imp.improve_log[-1].get("needs_improvement"):
                break
        # recovery probe: one more run after edits
        recovery = None
        probe_calls = 0
        if log:
            probe = self.drv.run(self.sb.working_spec, {}, workflow_name=self.workflow_name)
            probe_rows = trace_io.read_rows_by_run(self.sb.trace_db, probe.run_id) if probe.run_id else []
            recovery = hqs.all_four(probe_rows)
            probe_calls = 1
        if log and not any(lr.get("needs_improvement") for lr in log):
            gaps.append({"want": "self_improve firing", "needed": "needs_improvement=True in log",
                         "severity": "low"})
        # one LLM-invoking call per improve round + 1 for the recovery probe
        return log, recovery, improve_rounds + probe_calls

    def _diff(self, a: str, b: str) -> str:
        import difflib
        return "\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm=""))

    def _memory_mode(self) -> str | None:
        import yaml
        try:
            spec = yaml.safe_load(self.sb.working_spec.read_text())
        except Exception:
            return None
        mem = (spec or {}).get("memory")
        if isinstance(mem, dict):
            return "cold" if mem.get("fresh") else "warm"
        return None

    def _budget_exceeded(self, runs: int, llm_calls: int, wall_s: float,
                        tokens: int = 0) -> bool:
        b = self.plan.budget
        if runs >= b.max_runs:
            return True
        if b.max_llm_calls and llm_calls >= b.max_llm_calls:
            return True
        if b.max_tokens and tokens >= b.max_tokens:
            return True
        if b.max_wallclock_hours and wall_s >= b.max_wallclock_hours * 3600:
            return True
        return False

    def replay(self, recording_dir: Path) -> CampaignResult:
        """Reconstruct campaign.jsonl from a recording — zero Armature/LLM cost."""
        rec = Recording(Path(recording_dir))
        rows: list[dict] = []
        for r in rec.replay():
            tr = [trace_io.TraceRow(**t) for t in r["trace_rows"]]
            rows.append({"run_id": r["run_id"], "phase_id": "replay", "lever": "replay",
                         "inputs": {}, "exit_code": r["exit_code"],
                         "hqs_ours": hqs.all_four(tr),
                         "hqs_armature": {"authoritative": None, "rolling": None,
                                          "dashboard": r.get("dashboard_json", {}).get("current_hqs"),
                                          "feedback": None},
                         "improve_log": [], "recovery_hqs_ours": None,
                         "spec_diff": "", "memory_mode": None})
        return self._finalize(rows, [])

    def _finalize(self, rows: list[dict], gaps: list[dict]) -> CampaignResult:
        with open(self.campaign_jsonl, "w") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        with open(self.gaps_jsonl, "w") as f:
            for g in gaps:
                f.write(json.dumps(g, default=str) + "\n")
        vs = verdicts_mod.all_verdicts(rows, self.plan)
        report = render_report(
            campaign={"name": self.plan.name, "git_sha": _git_sha(),
                      "totals": {"runs": len(rows), "phases": len(self.plan.phases)}},
            rows=rows, verdicts=vs, gaps=gaps,
            reproduce_cmd=f"python experiments/campaign/run.py {self.plan.name} "
                          f"--replay {self.recording.dir if self.recording else '<recording>'}",
            out_path=self.sb.dir / "report.html")
        return CampaignResult(rows=rows, verdicts=vs, gaps=gaps,
                              report_path=report, campaign_jsonl=self.campaign_jsonl,
                              gaps_jsonl=self.gaps_jsonl)


def _git_sha() -> str:
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, check=False).stdout.strip()
    except Exception:
        return "unknown"
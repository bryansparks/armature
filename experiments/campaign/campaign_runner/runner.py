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

# experiments/campaign/ — used to resolve per-phase workflow paths that are
# specified relative to the harness root (e.g. "specs/soak/synth_fanout.yml").
HARNESS_ROOT = Path(__file__).resolve().parent.parent


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
        # last per-phase working spec used — _finalize reads tiers from here so
        # the report header reflects the final phase's (possibly overridden)
        # tiers rather than the stale pre-copy working_spec.
        self.last_working_spec = self.sb.working_spec
        self.campaign_jsonl = self.sb.dir / "campaign.jsonl"
        self.gaps_jsonl = self.sb.dir / "gaps.jsonl"

    def _workflow_name(self) -> str:
        import yaml
        return yaml.safe_load(self.source_spec.read_text()).get("name", "")

    def _resolve_phase_spec(self, phase) -> Path:
        """Resolve a phase's workflow spec path: phase.workflow falls back to
        plan.workflow, relative paths resolve against HARNESS_ROOT, and a
        missing path falls back to the sample fixture so H1–H4 plans (which
        point at a notional s.yml) keep working."""
        src = phase.workflow or self.plan.workflow
        p = Path(src)
        if not p.is_absolute():
            p = HARNESS_ROOT / src
        if not p.exists():
            p = HARNESS_ROOT / "tests" / "fixtures" / "sample_spec.yml"
        return p

    def _phase_workflow_name(self, ws: Path) -> str:
        """Parse the workflow `name` from a copied per-phase spec."""
        import yaml
        try:
            return (yaml.safe_load(ws.read_text()) or {}).get("name", "")
        except Exception:
            return ""

    def _row_from_run(self, run_id: str, phase_id: str, lever: str, inputs: dict,
                      exit_code: int, improve_log: list[dict], recovery: dict | None,
                      spec_diff: str, memory_mode: str | None, run_stderr: str = "",
                      gaps: list | None = None,
                      hqs_arm: dict | None = None) -> dict:
        rows = trace_io.read_rows_by_run(self.sb.trace_db, run_id) if run_id else []
        ours = hqs.all_four(rows)
        # hqs_armature holds ONLY values Armature independently emits, never a
        # copy of ours. authoritative + dashboard are captured once inside
        # CliDriver.run (from `armature replay` / `armature dashboard`) and
        # passed in as `hqs_arm` so the live row and the recording share one
        # source. feedback comes from the hqs_feedback hook hint in the run's
        # stderr. rolling (improve_log hqs_before) is opportunistic — filled by
        # the improve step, not here.
        arm = hqs_arm or {}
        arm_auth = arm.get("authoritative")
        # NOTE: hqs_ours["dashboard"] is computed from per-run trace rows
        # (trace_io.read_rows_by_run), while hqs_armature["dashboard"] comes from
        # `armature dashboard --format json`, which computes the HQS trend across
        # ALL workflow traces in the DB — not just this single run. So a persistent
        # dashboard delta in H3 reflects a row-set mismatch, not necessarily formula
        # drift. The formula reproduction itself is verified in tests/test_hqs.py.
        arm_dash = arm.get("dashboard")
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
            if phase.fresh_db:
                self.sb.reset_trace_db()
            # Concurrency phases are dispatched by Task 7; until then skip them
            # with a high-severity gap so a plan that opts in doesn't silently
            # fall through to the serial path.
            if phase.concurrency is not None:
                gaps.append({"want": "concurrency phase", "needed": "not yet implemented",
                             "severity": "high", "phase": phase.id})
                continue
            # Per-phase spec resolution: copy this phase's workflow (or the
            # plan-level default) into a phase-specific working spec, then apply
            # the campaign's tier override so every phase runs on the same model
            # config without mutating the shared working_spec.
            ws = self.sb.working_spec_for(phase.id)
            self.sb.copy_working_spec_to(self._resolve_phase_spec(phase), ws)
            if self.plan.tier_override:
                self.sb.apply_tier_override(ws, self.plan.tier_override)
            self.last_working_spec = ws
            wf_name = self._phase_workflow_name(ws)
            if not self.drv.validate(ws):
                gaps.append({"want": "valid spec after tier override", "needed": "validate exit 0",
                             "severity": "high", "phase": phase.id})
                continue
            for rep in range(phase.repeats):
                if self._budget_exceeded(len(rows), llm_calls, time.monotonic() - t0,
                                         trace_io.total_tokens(self.sb.trace_db)):
                    gaps.append({"want": "budget", "needed": "stop before max_runs/llm/wallclock",
                                 "severity": "info"})
                    break
                spec_before = ws.read_text()
                inputs = fault.apply_lever(phase, phase_index=pi, rep=rep, corpus=corpus,
                                           working_spec=ws, rng_seed=1000)
                out = self.drv.run(ws, inputs, workflow_name=wf_name,
                                   meta={"phase_id": phase.id, "lever": phase.lever,
                                         "inputs": inputs})
                llm_calls += 1
                improve_log, recovery, spec_diff = [], None, ""
                if phase.self_improve and phase.self_improve.enabled:
                    improve_log, recovery, improve_llm = self._do_improve(
                        phase, inputs, gaps, ws, wf_name)
                    llm_calls += improve_llm
                    spec_diff = self._diff(spec_before, ws.read_text())
                rows.append(self._row_from_run(out.run_id, phase.id, phase.lever, inputs,
                                               out.exit_code, improve_log, recovery,
                                               spec_diff, self._memory_mode(ws),
                                               run_stderr=out.stderr, gaps=gaps,
                                               hqs_arm=out.hqs_armature))
                # rolling (improve_log hqs_before) is the one Armature emission
                # available only after an improve cycle — fill it in if present.
                if improve_log:
                    rows[-1]["hqs_armature"]["rolling"] = improve_log[-1].get("hqs_before")
        return self._finalize(rows, gaps)

    def _do_improve(self, phase, inputs: dict, gaps: list, ws, wf_name) -> tuple[list[dict], dict | None, int]:
        si = phase.self_improve
        log: list[dict] = []
        improve_rounds = 0
        for _round in range(si.max_rounds):
            imp = self.drv.improve(ws, target_hqs=si.target_hqs,
                                   min_traces=si.min_traces, apply=si.apply)
            improve_rounds += 1
            log.extend(imp.improve_log)
            if not imp.improve_log or not imp.improve_log[-1].get("needs_improvement"):
                break
        # recovery probe: one more run after edits (tagged so replay folds it
        # into this run's recovery_hqs_ours instead of emitting a standalone row).
        # The probe's meta carries the improve_log so zero-cost replay can
        # restore it onto the parent main row (the main run is recorded before
        # improve runs, so its own record cannot carry the improve_log).
        recovery = None
        probe_calls = 0
        if log:
            probe = self.drv.run(ws, {}, workflow_name=wf_name,
                                  tag="probe",
                                  meta={"phase_id": phase.id, "lever": phase.lever,
                                        "inputs": inputs, "improve_log": log})
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

    def _memory_mode(self, ws) -> str | None:
        import yaml
        try:
            spec = yaml.safe_load(ws.read_text())
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
        """Reconstruct campaign.jsonl from a recording — zero Armature/LLM cost.

        Main runs become rows; a recovery probe (tag="probe") following its
        parent main run is folded into that row's recovery_hqs_ours, mirroring
        the live structure so replay reproduces the same row count. hqs_armature
        is restored from what CliDriver.run captured at record time (authoritative
        + dashboard), falling back to the legacy dashboard_json field for older
        recordings that predate hqs_armature capture.
        """
        rec = Recording(Path(recording_dir))
        rows: list[dict] = []
        for r in rec.replay():
            tr = [trace_io.TraceRow(**t) for t in r["trace_rows"]]
            meta = r.get("meta") or {}
            if r.get("tag", "main") == "probe":
                # Fold the probe into its parent main row: recovery_hqs_ours from
                # the probe's trace rows, and improve_log/phase context lifted
                # from the probe's meta (the main run is recorded before improve,
                # so its own meta lacks improve_log).
                if rows:
                    rows[-1]["recovery_hqs_ours"] = hqs.all_four(tr)
                    if "improve_log" in meta:
                        rows[-1]["improve_log"] = meta["improve_log"]
                    for k in ("phase_id", "lever", "inputs"):
                        if k in meta and not rows[-1].get(k):
                            rows[-1][k] = meta[k]
                continue
            arm = r.get("hqs_armature") or {}
            arm_dash = arm.get("dashboard")
            if arm_dash is None:
                arm_dash = r.get("dashboard_json", {}).get("current_hqs")
            # Restore phase context from the recording's meta so replayed rows
            # carry the same lever/inputs the live rows did — verdicts read these.
            # Fall back to "replay"/{} for older recordings that predate meta.
            rows.append({"run_id": r["run_id"],
                         "phase_id": meta.get("phase_id", "replay"),
                         "lever": meta.get("lever", "replay"),
                         "inputs": meta.get("inputs", {}),
                         "exit_code": r["exit_code"],
                         "hqs_ours": hqs.all_four(tr),
                         "hqs_armature": {"authoritative": arm.get("authoritative"),
                                          "rolling": None, "dashboard": arm_dash,
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
            campaign={"name": self.plan.name, "description": self.plan.description,
                      "git_sha": _git_sha(), "date": _now(),
                      "workflow": self.workflow_name,
                      "tiers": _spec_tiers(self.last_working_spec),
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


def _now() -> str:
    """UTC timestamp for the report header (report-only; not written to
    campaign.jsonl, so it does not affect replay determinism)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _spec_tiers(working_spec: Path) -> list[dict]:
    """Model-tiers summary parsed from the working spec, for the report header
    (provider + model per tier) so comparing runs across model swaps is easy."""
    try:
        import yaml
        tiers = (yaml.safe_load(working_spec.read_text()) or {}).get("model_tiers") or {}
        return [{"tier": t, "provider": v.get("provider"), "model": v.get("model")}
                for t, v in tiers.items()]
    except Exception:
        return []
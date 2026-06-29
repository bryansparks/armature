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
        self.aborted = False
        self.abort_reason: str | None = None

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
                      hqs_arm: dict | None = None,
                      workflow_name: str = "") -> dict:
        rows = trace_io.read_rows_by_run(self.sb.trace_db, run_id) if run_id else []
        agents_run = trace_io.count_agent_spawns(rows)
        quorum_ours = hqs.avg_quorum(rows)
        # model_failed: degenerate judge/researcher output (self-contradictory
        # judge verdict or empty researcher briefing). Computed from the run's
        # trace rows via the shared hqs.is_model_failed helper so replay()
        # reproduces it deterministically from the recorded rows. verdict_h4
        # excludes these so it measures memory coverage, not model reliability.
        model_failed = hqs.is_model_failed(rows) if rows else False
        # workflow_name: prefer the phase's spec name (passed in); fall back to the
        # trace rows' workflow_name (the name armature recorded for the run).
        wf = workflow_name or (rows[0].workflow_name if rows else "")
        acct = trace_io.account_scoped_rows(rows) if rows else []
        if acct and gaps is not None:
            gaps.append({"want": "funded provider account",
                         "needed": f"{acct[0].model} not 401/402/403 ({acct[0].error_kind})",
                         "severity": "high", "run_id": run_id})
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
        # feedback: armature's hqs_feedback hook prints only a CONDITIONAL prose
        # alert to stderr (HQS below 0.75) — it never emits a parseable `HQS:
        # <number>` value. So the feedback channel is structurally non-comparable;
        # we do not parse a value from stderr and never log a gap for it. H3
        # documents it as non_comparable. (parse_hqs_from_text stays in use for the
        # authoritative channel via replay_hqs.)
        hqs_armature = {"authoritative": arm_auth, "rolling": None,
                        "dashboard": arm_dash, "feedback": None}
        if gaps is not None:
            if arm_auth is None and run_id:
                gaps.append({"want": "authoritative HQS via replay", "needed":
                              "armature replay <run_id> printed an HQS line",
                              "severity": "low", "run_id": run_id})
        return {"run_id": run_id, "phase_id": phase_id, "lever": lever, "inputs": inputs,
                "exit_code": exit_code, "hqs_ours": ours, "hqs_armature": hqs_armature,
                "improve_log": improve_log, "recovery_hqs_ours": recovery,
                "spec_diff": spec_diff, "memory_mode": memory_mode,
                "agents_run": agents_run, "workflow_name": wf,
                "account_scoped": bool(acct),
                "account_scoped_kind": acct[0].error_kind if acct else None,
                "account_scoped_model": acct[0].model if acct else None,
                "quorum_ours": quorum_ours,
                "model_failed": model_failed}

    def _abort_k(self) -> int:
        return self.plan.abort.on_consecutive_account_errors if self.plan.abort else 3

    def _compute_abort(self, rows: list[dict]) -> None:
        """Deterministically set self.aborted/abort_reason by scanning rows for
        K consecutive account-scoped runs (skipping concurrency summaries).
        Used by replay() to reproduce the live abort decision from rows alone."""
        k = self._abort_k()
        consecutive = 0
        for r in rows:
            if r.get("is_concurrency_summary"):
                continue
            if r.get("account_scoped"):
                consecutive += 1
                if consecutive >= k:
                    self.aborted = True
                    self.abort_reason = "provider account exhausted"
                    return
            else:
                consecutive = 0

    def run(self) -> CampaignResult:
        corpus_path = self.sb.dir.parent.parent / "corpora" / "difficulty.csv"
        corpus = fault.load_corpus(corpus_path) if corpus_path.exists() else []
        rows: list[dict] = []
        gaps: list[dict] = []
        t0 = time.monotonic()
        llm_calls = 0
        self.aborted = False
        self.abort_reason = None
        K = self._abort_k()
        consecutive_account = 0
        for pi, phase in enumerate(self.plan.phases):
            if phase.fresh_db:
                self.sb.reset_trace_db()
            if phase.concurrency is not None:
                from campaign_runner import concurrency as conc_mod
                ws = self.sb.working_spec_for(phase.id)
                self.sb.copy_working_spec_to(self._resolve_phase_spec(phase), ws)
                if self.plan.tier_override:
                    self.sb.apply_tier_override(ws, self.plan.tier_override)
                self.last_working_spec = ws
                if not self.drv.validate(ws):
                    gaps.append({"want": "valid concurrency spec", "needed": "validate exit 0",
                                 "severity": "high", "phase": phase.id})
                    continue
                summaries = conc_mod.run_workers(self.sb, ws, phase.concurrency, phase.id,
                                                  self.recording)
                rows.extend(summaries)
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
                                               spec_diff, fault.memory_mode(ws),
                                               run_stderr=out.stderr, gaps=gaps,
                                               hqs_arm=out.hqs_armature,
                                               workflow_name=wf_name))
                # rolling (improve_log hqs_before) is the one Armature emission
                # available only after an improve cycle — fill it in if present.
                if improve_log:
                    rows[-1]["hqs_armature"]["rolling"] = improve_log[-1].get("hqs_before")
                # Circuit breaker: K consecutive account-scoped runs abort the
                # campaign so a drained provider account doesn't burn the rest
                # of the budget. A non-account-scoped run resets the streak.
                if rows[-1].get("account_scoped"):
                    consecutive_account += 1
                    if consecutive_account >= K:
                        self.aborted = True
                        self.abort_reason = "provider account exhausted"
                        gaps.append({"want": "campaign continued past exhaustion",
                                     "needed": f"abort after {K} consecutive account-scoped runs",
                                     "severity": "info"})
                        break
                else:
                    consecutive_account = 0
            if self.aborted:
                break
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
        if (log and phase.lever in verdicts_mod.DEGRADATION_LEVERS
                and not any(lr.get("needs_improvement") for lr in log)):
            # Only log a firing gap on phases whose purpose is to drop HQS below
            # target so self_improve fires (the degradation levers). On difficulty-
            # ramp / memory / none phases HQS sits above target, so NOT firing is
            # correct — logging a gap there was a false positive (20× in the
            # hqdynamics report).
            gaps.append({"want": "self_improve firing", "needed": "needs_improvement=True in log",
                         "severity": "low"})
        # one LLM-invoking call per improve round + 1 for the recovery probe
        return log, recovery, improve_rounds + probe_calls

    def _diff(self, a: str, b: str) -> str:
        import difflib
        return "\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm=""))

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
        self.aborted = False
        self.abort_reason = None
        # Every recorded entry — main, probe, and concurrency — carries the
        # trace rows it wrote to the live DB. Accumulate them so we can rebuild
        # the trace DB from the recording alone: that is what makes the
        # trace_db-dependent soak verdicts (trace_db_integrity /
        # agent_spawn_count / wallclock_stability) reproduce at zero cost,
        # instead of silently dropping to INCONCLUSIVE in a fresh replay
        # sandbox that never ran Armature.
        all_trace_rows: list[dict] = []
        for r in rec.replay():
            all_trace_rows.extend(r.get("trace_rows") or [])
            if r.get("tag") == "concurrency":
                rows.append((r.get("meta") or {}).get("summary", {}))
                continue
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
            acct = trace_io.account_scoped_rows(tr)
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
                         "spec_diff": "", "memory_mode": None,
                         "agents_run": trace_io.count_agent_spawns(tr),
                         "workflow_name": tr[0].workflow_name if tr else "",
                         "account_scoped": bool(acct),
                         "account_scoped_kind": acct[0].error_kind if acct else None,
                         "account_scoped_model": acct[0].model if acct else None,
                         "quorum_ours": hqs.avg_quorum(tr),
                         "model_failed": hqs.is_model_failed(tr) if tr else False})
        self._reconstruct_trace_db(self.sb.trace_db, all_trace_rows)
        self._compute_abort(rows)
        return self._finalize(rows, [])

    def _reconstruct_trace_db(self, db_path: Path, trace_rows: list[dict]) -> None:
        """Rebuild a read-only trace DB in the replay sandbox from recorded
        trace rows so the trace_db-dependent soak verdicts reproduce from the
        recording alone. Mirrors the columns those verdicts query
        (run_id / role_type / latency_ms) plus the rest of TraceRow for
        faithfulness. This is our own reconstruction in the replay sandbox —
        the live harness never writes to Armature's DB; replay never runs
        Armature. Idempotent: an existing file (e.g. a prior replay) is replaced."""
        import sqlite3
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.unlink()
        if not trace_rows:
            return
        # Backward compat: older recordings predate the error_kind and
        # outputs_json fields on TraceRow, so their asdict dicts lack the keys.
        # The named-param INSERT below binds :error_kind / :outputs_json, so
        # normalize missing keys to None rather than forcing every old recording
        # to be re-captured.
        for r in trace_rows:
            r.setdefault("error_kind", None)
            r.setdefault("outputs_json", None)
        con = sqlite3.connect(str(db_path))
        try:
            con.execute(
                "CREATE TABLE traces ("
                "run_id TEXT NOT NULL, workflow_name TEXT, stage_id TEXT, "
                "role_type TEXT, model TEXT, input_tokens INTEGER, "
                "output_tokens INTEGER, latency_ms REAL, success INTEGER, "
                "output_valid INTEGER, quorum_score REAL, escalation_count INTEGER, "
                "error_kind TEXT, outputs_json TEXT)")
            con.executemany(
                "INSERT INTO traces (run_id, workflow_name, stage_id, role_type, "
                "model, input_tokens, output_tokens, latency_ms, success, "
                "output_valid, quorum_score, escalation_count, error_kind, "
                "outputs_json) "
                "VALUES (:run_id, :workflow_name, :stage_id, :role_type, :model, "
                ":input_tokens, :output_tokens, :latency_ms, :success, "
                ":output_valid, :quorum_score, :escalation_count, :error_kind, "
                ":outputs_json)",
                trace_rows)
            con.commit()
        finally:
            con.close()

    def _finalize(self, rows: list[dict], gaps: list[dict]) -> CampaignResult:
        with open(self.campaign_jsonl, "w") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        with open(self.gaps_jsonl, "w") as f:
            for g in gaps:
                f.write(json.dumps(g, default=str) + "\n")
        from campaign_runner import soak_verdicts
        if self.plan.soak_verdicts is not None:
            vs = soak_verdicts.all_soak_verdicts(rows, self.plan, self.sb.trace_db)
        else:
            vs = verdicts_mod.all_verdicts(rows, self.plan)
        date_str = _now()
        purpose = self.plan.purpose or self.plan.description
        agents_per_workflow, grand_total_agents = _tally_agents(rows)
        meta = {"name": self.plan.name, "purpose": purpose, "date": date_str,
                "git_sha": _git_sha(),
                "totals": {"runs": len(rows), "phases": len(self.plan.phases)},
                "verdict_statuses": [{"name": n, "result": r} for n, r, _ in vs],
                "agents_per_workflow": agents_per_workflow,
                "grand_total_agents": grand_total_agents,
                "aborted": self.aborted,
                "abort_reason": self.abort_reason,
                "report": "report.html"}
        (self.sb.dir / "meta.json").write_text(json.dumps(meta, default=str))
        report = render_report(
            campaign={"name": self.plan.name, "description": self.plan.description,
                      "purpose": purpose, "git_sha": meta["git_sha"], "date": date_str,
                      "workflow": self.workflow_name,
                      "tiers": _spec_tiers(self.last_working_spec),
                      "totals": {"runs": len(rows), "phases": len(self.plan.phases)},
                      "agents_per_workflow": agents_per_workflow,
                      "grand_total_agents": grand_total_agents,
                      "aborted": self.aborted,
                      "abort_reason": self.abort_reason},
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


def _tally_agents(rows: list[dict]) -> tuple[dict, int]:
    """Group rows by workflow_name, summing agents_run + counting runs per
    workflow, plus a grand total across all rows (normal + concurrency). This
    is the per-workflow agent-spawn breakdown shown in reports + meta; the grand
    total should match the DB-wide agent_spawn_count soak verdict."""
    apw: dict[str, dict] = {}
    grand = 0
    for r in rows:
        wf = r.get("workflow_name") or "(unknown)"
        e = apw.setdefault(wf, {"runs": 0, "agents": 0})
        e["runs"] += 1
        a = int(r.get("agents_run") or 0)
        e["agents"] += a
        grand += a
    return dict(sorted(apw.items())), grand


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
"""SelfImproveRunner — closes the self-improvement loop for Armature workflows.

Analyzes accumulated traces for a workflow, diagnoses failure signatures,
proposes a targeted spec revision via SpecRefiner, and optionally applies it.
Every analysis cycle is logged to a JSONL file for traceability.

Each proposed revision also declares which failure signatures it predicts will
be resolved (predicted_fixes) and which might temporarily worsen
(predicted_regressions).  The next cycle verifies those predictions against the
observed diagnostic shift, building an accountability record over time.

Usage:
    runner = SelfImproveRunner("monitoring.yaml", "~/.armature/traces.db")
    report = await runner.analyze()
    # report.applied tells you if the spec was updated
    # report.proposed_spec has the revised HarnessSpec (even if not applied)
    # report.verified_fixes shows which previous predictions came true

CLI:
    armature improve monitoring.yaml --traces ~/.armature/traces.db
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import litellm

from armature.spec.models import HarnessSpec
from armature.spec.loader import load_spec
from armature.spec.validator import validate_spec, SpecValidationError
from armature.state.diagnostics import DiagnosticAnalyzer, DiagnosticResult
from armature.state.traces import TraceStore, IhrResult


async def llm_completion(**kwargs) -> Any:
    return await litellm.acompletion(**kwargs)


# ── SpecRefiner ───────────────────────────────────────────────────────────────

_REFINER_SYSTEM = """\
You are an expert at refining Armature workflow specs to address performance issues.

You will receive:
1. The current YAML spec
2. Diagnostic failure signatures (stage IDs + failure codes)
3. IHR (Implicit Harness Rating) breakdown — output validity, success rate, quorum score
4. Optionally: improvement suggestions from a post-run analysis stage

Your task: produce a revised YAML that addresses the diagnosed issues.

Rules:
- Make TARGETED changes only. Do not rewrite stages that are performing well.
- You MAY modify: role.description, output_schema, on_fail.loop.max, model_tier, stage timeout_s
- Do NOT add or remove stages. Do NOT change stage IDs or role names.
- If a stage has LOW_CONFIDENCE: enrich its description with explicit evaluation criteria.
- If a stage has OUTPUT_INVALID: relax or correct the output_schema required fields.
- If a stage has HIGH_ESCALATION: increase on_fail.loop.max or upgrade model_tier.
- If a stage has STAGE_FAILED: add a timeout_s or upgrade model_tier.

Output format — two sections, in order:
1. The complete revised YAML (no markdown fences, no explanation).
2. The literal separator line (nothing else on that line):
   ---PREDICTIONS---
3. A single JSON object declaring your falsifiable contract:
   {"predicted_fixes": [...], "predicted_regressions": [...]}

   predicted_fixes: list of "code:stage_id" strings you expect to resolve
   predicted_regressions: list of "code:stage_id" strings that might temporarily worsen
   Valid codes: stage_failed, output_invalid, low_confidence, high_escalation
   Example: {"predicted_fixes": ["output_invalid:analyst"], "predicted_regressions": []}
   Use [] for empty lists. These predictions will be verified in the next cycle.
"""


@dataclass
class RefinerResult:
    """Parsed output from SpecRefiner.refine()."""
    spec: HarnessSpec
    yaml_text: str
    predicted_fixes: list[str] = field(default_factory=list)
    predicted_regressions: list[str] = field(default_factory=list)


class SpecRefiner:
    """Calls a frontier LLM to produce a targeted revision of an existing spec."""

    def __init__(self, model: str) -> None:
        self._model = model

    async def refine(
        self,
        spec_yaml: str,
        diagnostics: list[DiagnosticResult],
        ihr: "IhrResult | None",
        refiner_suggestions: str | None = None,
    ) -> RefinerResult | None:
        """Return RefinerResult (spec, yaml_text, predictions) or None if unparseable."""
        diag_lines = "\n".join(
            f"  [{d.stage_id}] {d.code.value}: {d.details}" for d in diagnostics
        ) or "  (none)"

        if ihr:
            ihr_lines = (
                f"  IHR: {ihr.ihr:.2f}  "
                f"(output_valid={ihr.output_valid_rate:.0%}, "
                f"success={ihr.success_rate:.0%}, "
                f"avg_quorum={ihr.avg_quorum_score:.2f})"
            )
        else:
            ihr_lines = "  IHR: unavailable"

        user_content = (
            f"Current spec:\n```yaml\n{spec_yaml}\n```\n\n"
            f"Failure signatures:\n{diag_lines}\n\n"
            f"Quality metrics:\n{ihr_lines}"
        )
        if refiner_suggestions:
            user_content += f"\n\nPost-run analysis suggestions:\n{refiner_suggestions}"

        response = await llm_completion(
            model=self._model,
            messages=[
                {"role": "system", "content": _REFINER_SYSTEM},
                {"role": "user", "content": user_content},
            ],
        )
        raw = response.choices[0].message.content or ""
        return self._parse(raw)

    @staticmethod
    def _parse(text: str) -> RefinerResult | None:
        import yaml as _yaml

        # Split off predictions block before any other processing
        parts = text.split("---PREDICTIONS---", 1)
        yaml_part = parts[0]

        predicted_fixes: list[str] = []
        predicted_regressions: list[str] = []
        if len(parts) > 1:
            try:
                preds = json.loads(parts[1].strip())
                predicted_fixes = preds.get("predicted_fixes", [])
                predicted_regressions = preds.get("predicted_regressions", [])
            except (json.JSONDecodeError, AttributeError):
                pass

        # Strip markdown fences from YAML
        yaml_text = yaml_part.strip()
        if yaml_text.startswith("```"):
            first_nl = yaml_text.find("\n")
            if first_nl != -1:
                inner = yaml_text[first_nl + 1:]
                end = inner.rfind("```")
                yaml_text = inner[:end].strip() if end != -1 else inner.strip()

        try:
            data = _yaml.safe_load(yaml_text)
        except _yaml.YAMLError:
            return None

        if not isinstance(data, dict) or "stages" not in data:
            return None

        try:
            spec = HarnessSpec(**data, validate=False)
            validate_spec(spec)
        except Exception:
            return None

        return RefinerResult(
            spec=spec,
            yaml_text=yaml_text,
            predicted_fixes=predicted_fixes,
            predicted_regressions=predicted_regressions,
        )


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ImprovementReport:
    workflow_name: str
    spec_path: Path
    n_traces: int
    ihr_before: float | None
    needs_improvement: bool
    applied: bool
    diagnostics: list[DiagnosticResult]
    proposed_spec: HarnessSpec | None = None
    proposed_yaml: str | None = None
    log_path: Path | None = None
    # Prediction fields (from AHE prediction-verification loop)
    predicted_fixes: list[str] = field(default_factory=list)
    predicted_regressions: list[str] = field(default_factory=list)
    # Verification fields — populated by comparing against previous cycle's predictions
    verified_fixes: list[str] = field(default_factory=list)
    missed_predictions: list[str] = field(default_factory=list)
    unexpected_regressions: list[str] = field(default_factory=list)


# ── SelfImproveRunner ─────────────────────────────────────────────────────────

class SelfImproveRunner:
    """
    Analyzes accumulated traces for a workflow and proposes/applies spec improvements.

    Flow:
        1. Load last log entry to retrieve previous cycle's predictions
        2. Load traces for the workflow from the trace DB
        3. Compute rolling IHR across all loaded traces
        4. Run DiagnosticAnalyzer to identify failure signatures
        5. Verify previous predictions against current diagnostic state
        6. If IHR < target_ihr AND n_traces >= min_traces:
           a. Call SpecRefiner with current spec + diagnostics
           b. If refiner returns a valid revised spec:
              - Apply if auto_apply=True (overwrite spec file)
              - Extract predictions from RefinerResult
        7. Log the cycle (always) including predictions and verification
        8. Return ImprovementReport
    """

    def __init__(
        self,
        spec_path: Path | str,
        trace_db: Path | str | None = None,
        *,
        model: str = "claude-sonnet-4-6",
        target_ihr: float = 0.90,
        min_traces: int = 3,
        auto_apply: bool = True,
        log_path: Path | str | None = None,
    ) -> None:
        self._spec_path = Path(spec_path)
        if trace_db:
            self._trace_db = Path(trace_db)
        else:
            self._trace_db = Path("~/.armature/traces.db").expanduser()
        self._model = model
        self._target_ihr = target_ihr
        self._min_traces = min_traces
        self._auto_apply = auto_apply
        if log_path:
            self._log_path = Path(log_path)
        else:
            stem = self._spec_path.stem
            self._log_path = self._spec_path.parent / f"{stem}.improve_log.jsonl"

    async def analyze(self) -> ImprovementReport:
        # Load previous cycle's predictions for verification
        prev_entry = self._load_last_log_entry()

        spec = load_spec(self._spec_path)
        store = TraceStore(self._trace_db)

        traces = await store.query(workflow_name=spec.name, limit=200)
        n = len(traces)

        ihr_before: float | None = None
        diagnostics: list[DiagnosticResult] = []
        needs_improvement = False
        applied = False
        proposed_spec: HarnessSpec | None = None
        proposed_yaml: str | None = None
        predicted_fixes: list[str] = []
        predicted_regressions: list[str] = []

        if n > 0:
            ihr_result = self._compute_ihr(traces)
            ihr_before = ihr_result.ihr
            diagnostics = DiagnosticAnalyzer(traces).analyze()
            needs_improvement = n >= self._min_traces and ihr_before < self._target_ihr

        # Compute verification against previous cycle's predictions
        curr_diag_keys = self._diag_keys(diagnostics)
        verified_fixes, missed_predictions, unexpected_regressions = self._verify_predictions(
            prev_diag_keys=set(prev_entry.get("diagnostics_keys", [])) if prev_entry else set(),
            predicted_fixes=prev_entry.get("predicted_fixes", []) if prev_entry else [],
            predicted_regressions=prev_entry.get("predicted_regressions", []) if prev_entry else [],
            curr_diag_keys=curr_diag_keys,
        )

        if needs_improvement:
            refiner = SpecRefiner(self._model)
            spec_yaml = self._spec_path.read_text(encoding="utf-8")
            ihr_obj = self._compute_ihr(traces) if traces else None
            result = await refiner.refine(
                spec_yaml=spec_yaml,
                diagnostics=diagnostics,
                ihr=ihr_obj,
            )
            if result is not None:
                proposed_spec = result.spec
                proposed_yaml = result.yaml_text
                predicted_fixes = result.predicted_fixes
                predicted_regressions = result.predicted_regressions
                if self._auto_apply:
                    self._spec_path.write_text(proposed_yaml, encoding="utf-8")
                    applied = True

        self._write_log(
            workflow_name=spec.name,
            n_traces=n,
            ihr_before=ihr_before,
            diagnostics=diagnostics,
            diagnostics_keys=sorted(curr_diag_keys),
            needs_improvement=needs_improvement,
            applied=applied,
            predicted_fixes=predicted_fixes,
            predicted_regressions=predicted_regressions,
            verified_fixes=verified_fixes,
            missed_predictions=missed_predictions,
            unexpected_regressions=unexpected_regressions,
        )

        return ImprovementReport(
            workflow_name=spec.name,
            spec_path=self._spec_path,
            n_traces=n,
            ihr_before=ihr_before,
            needs_improvement=needs_improvement,
            applied=applied,
            diagnostics=diagnostics,
            proposed_spec=proposed_spec,
            proposed_yaml=proposed_yaml,
            log_path=self._log_path,
            predicted_fixes=predicted_fixes,
            predicted_regressions=predicted_regressions,
            verified_fixes=verified_fixes,
            missed_predictions=missed_predictions,
            unexpected_regressions=unexpected_regressions,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _diag_keys(diagnostics: list[DiagnosticResult]) -> set[str]:
        return {f"{d.code.value}:{d.stage_id}" for d in diagnostics}

    @staticmethod
    def _verify_predictions(
        *,
        prev_diag_keys: set[str],
        predicted_fixes: list[str],
        predicted_regressions: list[str],
        curr_diag_keys: set[str],
    ) -> tuple[list[str], list[str], list[str]]:
        """Return (verified_fixes, missed_predictions, unexpected_regressions)."""
        resolved = prev_diag_keys - curr_diag_keys
        new_issues = curr_diag_keys - prev_diag_keys

        fixes_set = set(predicted_fixes)
        regressions_set = set(predicted_regressions)

        verified_fixes = sorted(fixes_set & resolved)
        missed_predictions = sorted(fixes_set & curr_diag_keys)
        unexpected_regressions = sorted(new_issues - regressions_set)

        return verified_fixes, missed_predictions, unexpected_regressions

    def _load_last_log_entry(self) -> dict | None:
        if not self._log_path.exists():
            return None
        try:
            lines = [
                line for line in self._log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return json.loads(lines[-1]) if lines else None
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _compute_ihr(traces: list) -> IhrResult:
        from armature.state.traces import IhrResult
        n = len(traces)
        output_valid_rate = sum(1 for t in traces if t.output_valid) / n
        success_rate = sum(1 for t in traces if t.success) / n
        qs = [t.quorum_score for t in traces if t.quorum_score is not None]
        avg_quorum = sum(qs) / len(qs) if qs else 0.5
        avg_latency = sum(t.latency_ms for t in traces) / n
        latency_score = max(0.0, 1.0 - avg_latency / 5000.0)
        ihr = 0.40 * output_valid_rate + 0.30 * success_rate + 0.20 * avg_quorum + 0.10 * latency_score
        return IhrResult(
            run_id="rolling",
            ihr=ihr,
            output_valid_rate=output_valid_rate,
            success_rate=success_rate,
            avg_quorum_score=avg_quorum,
            latency_score=latency_score,
            n_traces=n,
        )

    def _write_log(
        self,
        *,
        workflow_name: str,
        n_traces: int,
        ihr_before: float | None,
        diagnostics: list[DiagnosticResult],
        diagnostics_keys: list[str],
        needs_improvement: bool,
        applied: bool,
        predicted_fixes: list[str],
        predicted_regressions: list[str],
        verified_fixes: list[str],
        missed_predictions: list[str],
        unexpected_regressions: list[str],
    ) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workflow_name": workflow_name,
            "n_traces": n_traces,
            "ihr_before": ihr_before,
            "target_ihr": self._target_ihr,
            "needs_improvement": needs_improvement,
            "applied": applied,
            "diagnostics": [
                {"code": d.code.value, "stage_id": d.stage_id, "details": d.details}
                for d in diagnostics
            ],
            # Store "code:stage_id" keys so next cycle can compute verification
            "diagnostics_keys": diagnostics_keys,
            # Prediction-verification (AHE falsifiable contract)
            "predicted_fixes": predicted_fixes,
            "predicted_regressions": predicted_regressions,
            "verified_fixes": verified_fixes,
            "missed_predictions": missed_predictions,
            "unexpected_regressions": unexpected_regressions,
        }
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

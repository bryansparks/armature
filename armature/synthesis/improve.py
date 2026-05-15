"""SelfImproveRunner — closes the self-improvement loop for Armature workflows.

Analyzes accumulated traces for a workflow, diagnoses failure signatures,
proposes a targeted spec revision via SpecRefiner, and optionally applies it.
Every analysis cycle is logged to a JSONL file for traceability.

Usage:
    runner = SelfImproveRunner("monitoring.yaml", "~/.armature/traces.db")
    report = await runner.analyze()
    # report.applied tells you if the spec was updated
    # report.proposed_spec has the revised HarnessSpec (even if not applied)

CLI:
    armature improve monitoring.yaml --traces ~/.armature/traces.db
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
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
- Return ONLY the complete revised YAML — no markdown fences, no explanation.
"""


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
    ) -> "tuple[HarnessSpec, str] | None":
        """Return (HarnessSpec, raw_yaml) or None if the LLM output can't be parsed."""
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
        spec = self._parse(raw)
        if spec is None:
            return None
        return spec, raw

    @staticmethod
    def _parse(yaml_text: str) -> HarnessSpec | None:
        import yaml as _yaml

        text = yaml_text.strip()
        if text.startswith("```"):
            first_nl = text.find("\n")
            if first_nl != -1:
                inner = text[first_nl + 1:]
                end = inner.rfind("```")
                text = inner[:end].strip() if end != -1 else inner.strip()

        try:
            data = _yaml.safe_load(text)
        except _yaml.YAMLError:
            return None

        if not isinstance(data, dict) or "stages" not in data:
            return None

        try:
            spec = HarnessSpec(**data, validate=False)
            validate_spec(spec)
            return spec
        except Exception:
            return None


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
    proposed_yaml: str | None = None    # raw YAML text for review/diff
    log_path: Path | None = None


# ── SelfImproveRunner ─────────────────────────────────────────────────────────

class SelfImproveRunner:
    """
    Analyzes accumulated traces for a workflow and proposes/applies spec improvements.

    Flow:
        1. Load traces for the workflow from the trace DB
        2. Compute rolling IHR across all loaded traces
        3. Run DiagnosticAnalyzer to identify failure signatures
        4. If IHR < target_ihr AND n_traces >= min_traces:
           a. Call SpecRefiner with current spec + diagnostics
           b. If refiner returns a valid revised spec:
              - Apply if auto_apply=True (overwrite spec file)
              - Log the cycle to log_path
        5. Return ImprovementReport
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
        spec = load_spec(self._spec_path)
        store = TraceStore(self._trace_db)

        traces = await store.query(workflow_name=spec.name, limit=200)
        n = len(traces)

        ihr_before: float | None = None
        diagnostics: list[DiagnosticResult] = []
        needs_improvement = False
        applied = False
        proposed_spec: HarnessSpec | None = None

        if n > 0:
            ihr_result = self._compute_ihr(traces)
            ihr_before = ihr_result.ihr
            diagnostics = DiagnosticAnalyzer(traces).analyze()
            needs_improvement = n >= self._min_traces and ihr_before < self._target_ihr

        proposed_yaml: str | None = None
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
                proposed_spec, proposed_yaml = result
                if self._auto_apply:
                    self._spec_path.write_text(proposed_yaml, encoding="utf-8")
                    applied = True

        self._write_log(
            workflow_name=spec.name,
            n_traces=n,
            ihr_before=ihr_before,
            diagnostics=diagnostics,
            needs_improvement=needs_improvement,
            applied=applied,
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
        )

    # ── helpers ───────────────────────────────────────────────────────────────

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
        needs_improvement: bool,
        applied: bool,
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
        }
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

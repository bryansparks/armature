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

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import litellm

from armature.spec.models import HarnessSpec, EditableSurface
from armature.spec.loader import load_spec
from armature.spec.validator import validate_spec, SpecValidationError
from armature.state.diagnostics import DiagnosticAnalyzer, DiagnosticResult
from armature.state.traces import TraceStore, HqsResult, TraceRecord, compute_hqs_from_traces
from armature.state.leverage import compute_leverage, LeverageReport


async def llm_completion(**kwargs) -> Any:
    return await litellm.acompletion(**kwargs)


# ── Refiner model resolution ──────────────────────────────────────────────────
# Priority: ARMATURE_REFINER_MODEL env var → spec's top tier → fallback

_REFINER_ENV_VAR = "ARMATURE_REFINER_MODEL"
_TIER_PRIORITY = ("frontier", "large", "medium", "small", "tiny")


def _resolve_refiner_model(spec: HarnessSpec) -> str:
    """Return the litellm model string to use for the SpecRefiner.

    Checks ARMATURE_REFINER_MODEL env var first, then falls back to the spec's
    highest available tier so improve/optimize work with whatever provider the
    user already configured (OpenRouter, OpenAI, Ollama, etc.).
    """
    env_val = os.environ.get(_REFINER_ENV_VAR)
    if env_val:
        return env_val

    tiers = spec.model_tiers
    cfg = None
    for name in _TIER_PRIORITY:
        cfg = getattr(tiers, name, None)
        if cfg is not None:
            break
    if cfg is None and tiers.__pydantic_extra__:
        cfg = next(iter(tiers.__pydantic_extra__.values()), None)

    if cfg is None:
        return "claude-sonnet-4-6"  # last-resort fallback

    return f"{cfg.provider}/{cfg.model}" if cfg.provider else cfg.model


# ── Trigger override resolution ────────────────────────────────────────────────
# The spec's self_improvement block may declare target_hqs / min_traces. A CLI flag
# (if not None) wins; otherwise the spec field wins; otherwise the caller's hard
# default. This makes the spec the single source of truth for *when* improvement
# fires, while keeping CLI flags as an explicit override.

def resolve_trigger_overrides(
    cli_target_hqs: float | None,
    cli_min_traces: int | None,
    cli_drift_threshold: float | None,
    spec: "HarnessSpec",
    *,
    default_target_hqs: float,
    default_min_traces: int,
    default_drift_threshold: float = 0.5,
) -> tuple[float, int, float]:
    """Return (target_hqs, min_traces, drift_threshold) honoring CLI flag > spec field > default."""
    si = spec.self_improvement
    target_hqs = cli_target_hqs if cli_target_hqs is not None else (
        si.target_hqs if si.target_hqs is not None else default_target_hqs
    )
    min_traces = cli_min_traces if cli_min_traces is not None else (
        si.min_traces if si.min_traces is not None else default_min_traces
    )
    drift_threshold = cli_drift_threshold if cli_drift_threshold is not None else (
        si.drift_threshold if si.drift_threshold is not None else default_drift_threshold
    )
    return target_hqs, min_traces, drift_threshold


# ── SpecRefiner ───────────────────────────────────────────────────────────────

_REFINER_BASE = """\
You are an expert at refining Armature workflow specs to address performance issues.

You will receive:
1. The current YAML spec
2. Diagnostic failure signatures (stage IDs + failure codes + causal attribution)
3. HQS (Harness Quality Score) breakdown — output validity, success rate, quorum score
4. Optionally: improvement suggestions from a post-run analysis stage

Your task: produce a revised YAML that addresses the diagnosed issues.

Rules:
- Make TARGETED changes only. Do not rewrite stages that are performing well.
- Use the causal_status to guide your fix:
    spec_problem → improve role.description or relax output_schema
    model_problem → upgrade model_tier or increase on_fail.loop.max
    tool_problem → review tool usage in role.description
- If a stage has LOW_CONFIDENCE: enrich its description with explicit evaluation criteria.
- If a stage has OUTPUT_INVALID (spec_problem): relax or correct the output_schema required fields.
- If a stage has OUTPUT_INVALID (model_problem): upgrade model_tier.
- If a stage has HIGH_ESCALATION: increase on_fail.loop.max or upgrade model_tier.
- If a stage has STAGE_FAILED (spec_problem/timeout): add or increase timeout_s.
- If a stage has STAGE_FAILED (model_problem): upgrade model_tier.
- If a stage has LOW_SKILL_ACTIVATION: strengthen role.description to explicitly instruct tool use; list tools by name and when to invoke them.
- Do NOT add or remove stages. Do NOT change stage IDs or role names.

Output format — two sections, in order:
1. The complete revised YAML (no markdown fences, no explanation).
2. The literal separator line (nothing else on that line):
   ---PREDICTIONS---
3. A single JSON object declaring your falsifiable contract:
   {"predicted_fixes": [...], "predicted_regressions": [...]}

   predicted_fixes: list of "code:stage_id" strings you expect to resolve
   predicted_regressions: list of "code:stage_id" strings that might temporarily worsen
   Valid codes: stage_failed, output_invalid, low_confidence, high_escalation, low_skill_activation
   Example: {"predicted_fixes": ["output_invalid:analyst"], "predicted_regressions": []}
   Use [] for empty lists. These predictions will be verified in the next cycle.
"""


def _make_refiner_system_prompt(
    editable_surfaces: list[str] | None = None,
    diversity_hint: str | None = None,
) -> str:
    prompt = _REFINER_BASE
    if editable_surfaces:
        all_surfaces = {s.value for s in EditableSurface}
        locked = sorted(all_surfaces - set(editable_surfaces))
        prompt += f"\nEditable surfaces (ONLY these may be changed): {', '.join(sorted(editable_surfaces))}\n"
        if locked:
            prompt += f"DO NOT modify: {', '.join(locked)}\n"
    if diversity_hint:
        prompt += f"\n{diversity_hint}\n"
    return prompt


_DIVERSITY_HINTS = [
    "Focus your proposal on improving role.description clarity and specificity.",
    "Focus your proposal on adjusting on_fail retry limits and model_tier upgrades.",
    "Focus your proposal on relaxing or tightening output_schema constraints.",
    "Focus your proposal on adding timeouts and escalation recovery.",
]


@dataclass
class RefinerResult:
    """Parsed output from SpecRefiner.refine()."""
    spec: HarnessSpec
    yaml_text: str
    predicted_fixes: list[str] = field(default_factory=list)
    predicted_regressions: list[str] = field(default_factory=list)


class SpecRefiner:
    """Calls a medium-tier LLM to produce a targeted revision of an existing spec.

    Per arXiv:2605.30621v1, medium-tier models achieve equivalent spec-evolution
    quality to frontier models (≤3.1pp difference) at substantially lower cost.
    """

    def __init__(self, model: str) -> None:
        self._model = model

    async def refine(
        self,
        spec_yaml: str,
        diagnostics: list[DiagnosticResult],
        hqs: "HqsResult | None",
        refiner_suggestions: str | None = None,
        editable_surfaces: list[str] | None = None,
        diversity_hint: str | None = None,
    ) -> RefinerResult | None:
        """Return RefinerResult (spec, yaml_text, predictions) or None if unparseable."""
        diag_lines = "\n".join(
            f"  [{d.stage_id}] {d.code.value}"
            + (
                f" [{d.causal_attribution.causal_status.value}/{d.causal_attribution.mechanism.value}]"
                if d.causal_attribution else ""
            )
            + f": {d.details}"
            for d in diagnostics
        ) or "  (none)"

        if hqs:
            hqs_lines = (
                f"  HQS: {hqs.hqs:.2f}  "
                f"(output_valid={hqs.output_valid_rate:.0%}, "
                f"success={hqs.success_rate:.0%}, "
                f"avg_quorum={hqs.avg_quorum_score:.2f})"
            )
        else:
            hqs_lines = "  HQS: unavailable"

        user_content = (
            f"Current spec:\n```yaml\n{spec_yaml}\n```\n\n"
            f"Failure signatures:\n{diag_lines}\n\n"
            f"Quality metrics:\n{hqs_lines}"
        )
        if refiner_suggestions:
            user_content += f"\n\nContext from previous cycles and post-run analysis:\n{refiner_suggestions}"

        system_prompt = _make_refiner_system_prompt(editable_surfaces, diversity_hint)

        response = await llm_completion(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
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

    async def refine_many(
        self,
        spec_yaml: str,
        diagnostics: list[DiagnosticResult],
        hqs: "HqsResult | None",
        refiner_suggestions: str | None = None,
        editable_surfaces: list[str] | None = None,
        n_proposals: int = 3,
    ) -> list[RefinerResult]:
        """Generate n_proposals candidate revisions in parallel, returning only valid ones."""
        tasks = [
            self.refine(
                spec_yaml=spec_yaml,
                diagnostics=diagnostics,
                hqs=hqs,
                refiner_suggestions=refiner_suggestions,
                editable_surfaces=editable_surfaces,
                diversity_hint=_DIVERSITY_HINTS[i % len(_DIVERSITY_HINTS)],
            )
            for i in range(n_proposals)
        ]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]


def _pick_best_proposal(
    candidates: list[RefinerResult],
    diagnostics: list[DiagnosticResult],
    old_spec: "HarnessSpec",
    *,
    latency_tolerance: int = 1,
    leverage: dict[str, float] | None = None,
) -> "RefinerResult | None":
    """Select the best candidate using an ε-band fuzzy tiebreak.

    Coverage is primary: a candidate ≥2 predicted fixes behind the top can never
    win. When ``leverage`` is a stage->weight map, coverage is leverage-weighted
    (a fix on a high-leverage stage scores higher); when ``leverage is None``,
    coverage is the plain count — identical to the prior behavior. Among
    candidates within ``latency_tolerance`` (ε) of the top weighted coverage,
    the lowest structural ``latency_risk`` wins — coverage is the final tiebreak.
    """
    if not candidates:
        return None
    diag_keys = {f"{d.code.value}:{d.stage_id}" for d in diagnostics}

    def coverage(r: RefinerResult) -> float:
        fixes = set(r.predicted_fixes) & diag_keys
        if leverage is None:
            return float(len(fixes))
        return sum(leverage.get(fix.split(":")[1], 1.0) for fix in fixes)

    def risk(r: RefinerResult) -> float:
        return _latency_risk(old_spec, r.spec)

    best_cov = max(coverage(c) for c in candidates)
    tied = [c for c in candidates if best_cov - coverage(c) <= latency_tolerance]
    return min(tied, key=lambda r: (risk(r), -coverage(r)))


def _healthy_stage_ids(
    traces: list[TraceRecord],
    diagnostics: list[DiagnosticResult],
) -> set[str]:
    """Return stage IDs that appear in traces but have NO diagnostics (healthy)."""
    failing = {d.stage_id for d in diagnostics}
    all_ids = {t.stage_id for t in traces}
    return all_ids - failing


def _proposal_regression_risk(
    candidate: RefinerResult,
    old_spec: "HarnessSpec",
    healthy_stage_ids: set[str],
) -> bool:
    """Return True if the proposal modifies any stage that is currently healthy."""
    if not healthy_stage_ids:
        return False
    auto, review = _classify_changes(old_spec, candidate.spec)
    # Only stage-level changes (format "field:stage_id") are checked; structural
    # changes (stages_added/removed) are governed separately and intentionally excluded.
    changed_stages = {
        k.split(":")[1] for k in {**auto, **review}
        if ":" in k
    }
    return bool(changed_stages & healthy_stage_ids)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ImprovementReport:
    workflow_name: str
    spec_path: Path
    n_traces: int
    hqs_before: float | None
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
    drift_score: float = 0.0
    # Governance fields — set when proposed change requires human review
    requires_review: bool = False
    pending_path: Path | None = None
    # K-proposal fields
    n_proposals_generated: int = 0
    regression_risk_count: int = 0
    # Editable-surfaces gate — surfaces a proposal touched that were NOT in the
    # spec's editable_surfaces (locked). Such proposals are rejected (dropped),
    # not applied and not written to pending.
    rejected_locked_surfaces: list[str] = field(default_factory=list)
    rejected_proposals: int = 0
    # #5 drift trigger — set when the cycle fired because drift_score crossed the
    # threshold while HQS was healthy (oscillation). Such proposals are forced to
    # review (auto-apply suppressed) to avoid worsening the oscillation.
    triggered_by_drift: bool = False
    escalated_oscillation: bool = False
    # #6 latency-aware selection — structural latency-risk of the selected proposal
    # (proxy for the HQS latency-cancel tradeoff). 0.0 when no proposal was produced.
    latency_risk: float = 0.0
    # Stage credit attribution — the LeverageReport carried for visibility/logging.
    # None when no traces; a LeverageReport (possibly insufficient) otherwise.
    leverage: LeverageReport | None = None


_AUTO_APPLY_FIELDS = {"description", "on_fail", "model_tier", "timeout_s", "loop"}

_REVIEW_REQUIRED_FIELDS = {"stages_added", "stages_removed", "output_schema", "safety_rules"}


def _classify_changes(
    old_spec: "HarnessSpec", new_spec: "HarnessSpec"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Classify spec changes into auto-apply vs review-required categories.

    Returns (auto_changes, review_changes) where each is a dict of {description: detail}.
    Auto-apply: role description, on_fail tweaks, model_tier, timeout_s changes.
    Review-required: adding/removing stages, output_schema changes, safety_rules modifications.
    """
    auto: dict[str, Any] = {}
    review: dict[str, Any] = {}

    old_ids = {s.id for s in old_spec.stages}
    new_ids = {s.id for s in new_spec.stages}

    added = new_ids - old_ids
    removed = old_ids - new_ids

    if added:
        review["stages_added"] = sorted(added)
    if removed:
        review["stages_removed"] = sorted(removed)

    old_rules = [r.model_dump() for r in old_spec.safety_rules]
    new_rules = [r.model_dump() for r in new_spec.safety_rules]
    if old_rules != new_rules:
        review["safety_rules"] = "modified"

    old_stages = {s.id: s for s in old_spec.stages}
    new_stages = {s.id: s for s in new_spec.stages}
    for sid in old_ids & new_ids:
        old_s = old_stages[sid]
        new_s = new_stages[sid]
        if old_s.role and new_s.role and old_s.role.description != new_s.role.description:
            auto[f"description:{sid}"] = "changed"
        if old_s.role and new_s.role and old_s.role.model_tier != new_s.role.model_tier:
            auto[f"model_tier:{sid}"] = "changed"
        if old_s.output_schema != new_s.output_schema:
            review[f"output_schema:{sid}"] = "modified"
        if old_s.timeout_s != new_s.timeout_s:
            auto[f"timeout_s:{sid}"] = "changed"
        if old_s.on_fail != new_s.on_fail:
            auto[f"on_fail:{sid}"] = "changed"

    # Global model_tiers block (tier definitions): a change here redefines a tier
    # for every stage using it, so detect it explicitly rather than letting it
    # slip through unclassified.
    if old_spec.model_tiers.model_dump() != new_spec.model_tiers.model_dump():
        auto["model_tiers_block"] = "modified"

    return auto, review


def _touched_surfaces(old_spec: "HarnessSpec", new_spec: "HarnessSpec") -> set[str]:
    """Return the set of EditableSurface values touched by old_spec → new_spec.

    Structural changes (stage add/remove, safety_rules) map to no surface — they
    are governed by `_classify_changes` (always review), not by the
    `editable_surfaces` lock. This is the set the lock gate checks: any surface
    touched but not in the spec's editable_surfaces is a locked-surface violation.
    """
    from armature.spec.models import EditableSurface

    touched: set[str] = set()

    old_ids = {s.id for s in old_spec.stages}
    new_ids = {s.id for s in new_spec.stages}
    old_stages = {s.id: s for s in old_spec.stages}
    new_stages = {s.id: s for s in new_spec.stages}

    for sid in old_ids & new_ids:
        old_s = old_stages[sid]
        new_s = new_stages[sid]
        if old_s.role and new_s.role:
            if old_s.role.description != new_s.role.description:
                touched.add(EditableSurface.DESCRIPTIONS.value)
            if old_s.role.model_tier != new_s.role.model_tier:
                touched.add(EditableSurface.MODEL_TIERS.value)
        if old_s.output_schema != new_s.output_schema:
            touched.add(EditableSurface.SCHEMAS.value)
        if old_s.timeout_s != new_s.timeout_s:
            touched.add(EditableSurface.TIMEOUTS.value)
        if old_s.on_fail != new_s.on_fail:
            touched.add(EditableSurface.RETRY_COUNTS.value)

    # Global model_tiers block change also touches the model_tiers surface.
    if old_spec.model_tiers.model_dump() != new_spec.model_tiers.model_dump():
        touched.add(EditableSurface.MODEL_TIERS.value)

    return touched


# Tier rank for latency-risk escalation/demotion. Custom/unknown tier names
# (anything not in the canonical tiny..frontier ladder) map to None and contribute
# nothing — their latency direction is uncertain in v1.
_TIER_RANK = {"tiny": 0, "small": 1, "medium": 2, "large": 3, "frontier": 4}


def _latency_risk(old_spec: "HarnessSpec", new_spec: "HarnessSpec") -> float:
    """Predict relative latency impact of old_spec → new_spec from the structural diff.

    A proxy heuristic (no post-apply measurement): lower = safer for latency.
    Unchanged spec → 0.0. Used by ``_pick_best_proposal`` as the ε-band tiebreak
    so the coverage-winning candidate isn't systematically the highest-latency one
    (the H4-v2 latency-cancel mechanism).

    Contributions:
      +1.0 / stage added,  −1.0 / stage removed
      +1.0 / tier escalation, −1.0 / demotion (only when both ranks are known)
      +0.5 / on_fail.loop.max increase
      +0.25 / timeout_s increase
      +0.5 for a model_tiers block redefinition (flat; direction uncertain in v1)
    """
    risk = 0.0

    old_ids = {s.id for s in old_spec.stages}
    new_ids = {s.id for s in new_spec.stages}
    risk += 1.0 * len(new_ids - old_ids)      # stages added
    risk -= 1.0 * len(old_ids - new_ids)      # stages removed

    old_stages = {s.id: s for s in old_spec.stages}
    new_stages = {s.id: s for s in new_spec.stages}
    for sid in old_ids & new_ids:
        old_s = old_stages[sid]
        new_s = new_stages[sid]
        if old_s.role and new_s.role:
            old_rank = _TIER_RANK.get(old_s.role.model_tier) if old_s.role.model_tier else None
            new_rank = _TIER_RANK.get(new_s.role.model_tier) if new_s.role.model_tier else None
            if old_rank is not None and new_rank is not None:
                if new_rank > old_rank:
                    risk += 1.0
                elif new_rank < old_rank:
                    risk -= 1.0
        # on_fail.loop.max increase
        old_loop = old_s.on_fail.loop if old_s.on_fail else None
        new_loop = new_s.on_fail.loop if new_s.on_fail else None
        if old_loop and new_loop and new_loop.max > old_loop.max:
            risk += 0.5
        # timeout_s increase
        if old_s.timeout_s and new_s.timeout_s and new_s.timeout_s > old_s.timeout_s:
            risk += 0.25

    # Global model_tiers block redefinition — flat, uncertain direction in v1.
    if old_spec.model_tiers.model_dump() != new_spec.model_tiers.model_dump():
        risk += 0.5

    return risk


def _build_refiner_suggestions(
    prev_entry: dict | None, traces: list,
    optimizer_proposals: list | None = None,
) -> str | None:
    """Build the refiner_suggestions string from prior-cycle verification feedback
    and any post-run ``improvement_suggestions`` captured in traces.

    Closes the prediction-verification loop: the refiner learns which of its
    previous predicted fixes were missed or caused unexpected regressions, plus
    any explicit suggestions a post-run analyst stage emitted. Returns None when
    there is nothing to feed back (so the prompt stays unchanged).

    ``optimizer_proposals`` (reverse direction of #7) carries A/B-tested proposals
    from ``armature optimize``'s ProposalStore so the refiner doesn't re-propose
    diffs the meta-workflow already scored and can build on accepted ones.
    """
    prev = prev_entry or {}
    lines: list[str] = []

    missed = prev.get("missed_predictions") or []
    unexpected = prev.get("unexpected_regressions") or []
    verified = prev.get("verified_fixes") or []
    drift = float(prev.get("drift_score") or 0.0)

    if missed or unexpected or verified or drift > 0.0:
        lines.append("Previous-cycle feedback:")
        if missed:
            lines.append("- Missed predictions (predicted to fix, still failing): "
                         + ", ".join(missed))
        if unexpected:
            lines.append("- Unexpected regressions (new failures not predicted): "
                         + ", ".join(unexpected))
        if verified:
            lines.append("- Verified fixes (predicted and resolved): "
                         + ", ".join(verified))
        if drift > 0.0:
            lines.append(f"- Drift score: {drift:.2f} (previously fixed issues reappeared — "
                         "the system may be oscillating; consider a different lever).")

    # Post-run analyst stages emit improvement_suggestions in their trace outputs.
    suggestions = []
    for t in traces:
        outs = getattr(t, "outputs", None) or {}
        s = outs.get("improvement_suggestions")
        if s:
            suggestions.append(str(s).strip())
    if suggestions:
        if lines:
            lines.append("")
        lines.append("Post-run analysis suggestions:")
        for s in suggestions:
            lines.append(f"- {s}")

    if optimizer_proposals:
        if lines:
            lines.append("")
        lines.append("Prior A/B-tested proposals from `armature optimize`:")
        for p in optimizer_proposals:
            verdict = "ACCEPTED" if getattr(p, "accepted", False) else "REJECTED"
            score = getattr(p, "score", None)
            score_str = f" score={score:.2f}" if isinstance(score, (int, float)) else ""
            lines.append(f"- [{verdict}{score_str}] {getattr(p, 'rationale', '')}")

    return "\n".join(lines) if lines else None


async def _load_cross_engine_history(
    improvement_db_path: Path | str | None, workflow_stem: str, limit: int = 10
) -> list:
    """Read ``armature optimize``'s A/B-tested records for a workflow stem from the
    unified ``ImprovementStore``.

    Reverse direction of #7: lets ``improve``'s refiner see the diffs the
    meta-workflow already A/B-tested (accepted/rejected, score, rationale) so it
    doesn't re-propose rejected diffs and can build on accepted ones. Filters to
    ``source="optimize"`` records, keyed by the spec file stem (the same key both
    engines use).

    Advisory: a missing DB or any read error returns an empty list — never blocks
    improvement. The DB is **only** read here, never created — the file-existence
    guard means ``improve`` never materializes ``~/.armature/improvements.db``; only
    ``optimize`` (which must write) does.
    """
    if not improvement_db_path:
        return []
    path = Path(improvement_db_path)
    if not path.exists():
        return []
    try:
        from armature.state.improvement_store import ImprovementStore
        store = ImprovementStore(path)
        await store.init()  # idempotent — table already exists when optimize ran
        return await store.load_history(workflow_stem, source="optimize", limit=limit)
    except Exception:
        return []


def _load_all_verified_fixes(log_path: Path) -> set[str]:
    """Read all JSONL log entries and collect every verified_fix ever recorded."""
    if not log_path.exists():
        return set()
    result: set[str] = set()
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            result.update(entry.get("verified_fixes") or [])
        except (json.JSONDecodeError, AttributeError):
            pass
    return result


# ── SelfImproveRunner ─────────────────────────────────────────────────────────

class SelfImproveRunner:
    """
    Analyzes accumulated traces for a workflow and proposes/applies spec improvements.

    Flow:
        1. Load last log entry to retrieve previous cycle's predictions
        2. Load traces for the workflow from the trace DB
        3. Compute rolling HQS across all loaded traces
        4. Run DiagnosticAnalyzer to identify failure signatures
        5. Verify previous predictions against current diagnostic state
        6. If HQS < target_hqs AND n_traces >= min_traces:
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
        model: str | None = None,
        target_hqs: float = 0.90,
        min_traces: int = 3,
        auto_apply: bool = True,
        log_path: Path | str | None = None,
        n_proposals: int = 1,
        drift_threshold: float = 0.5,
        improvement_db_path: Path | str | None = None,
    ) -> None:
        self._spec_path = Path(spec_path)
        if trace_db:
            self._trace_db = Path(trace_db)
        else:
            self._trace_db = Path("~/.armature/traces.db").expanduser()
        self._model = model  # None → resolved from spec + env in analyze()
        self._target_hqs = target_hqs
        self._min_traces = min_traces
        self._drift_threshold = drift_threshold
        self._auto_apply = auto_apply
        if log_path:
            self._log_path = Path(log_path)
        else:
            stem = self._spec_path.stem
            self._log_path = self._spec_path.parent / f"{stem}.improve_log.jsonl"
        self._n_proposals = n_proposals
        # #7 (Option 4): read optimize's A/B-tested records from the unified
        # ImprovementStore so the refiner doesn't re-propose scored diffs, and write
        # this cycle's record back so optimize can see it. CLI supplies the default
        # (~/.armature/improvements.db); None → no read/write (keeps
        # programmatic/test use free of the store).
        self._improvement_db_path = (
            Path(improvement_db_path) if improvement_db_path else None
        )

    async def analyze(self) -> ImprovementReport:
        # Load previous cycle's predictions for verification
        prev_entry = self._load_last_log_entry()

        spec = load_spec(self._spec_path)
        # Resolve refiner model lazily so it picks up the spec's own provider/model
        _model = self._model or _resolve_refiner_model(spec)
        store = TraceStore(self._trace_db)

        traces = await store.query(workflow_name=spec.name, limit=200)
        n = len(traces)

        hqs_before: float | None = None
        diagnostics: list[DiagnosticResult] = []
        needs_improvement = False
        applied = False
        requires_review = False
        _pending_path: Path | None = None
        proposed_spec: HarnessSpec | None = None
        proposed_yaml: str | None = None
        predicted_fixes: list[str] = []
        predicted_regressions: list[str] = []
        n_proposals_generated = 0
        regression_risk_count = 0
        rejected_locked_surfaces: list[str] = []
        rejected_proposals = 0
        triggered_by_drift = False
        escalated_oscillation = False
        drift_score = 0.0
        latency_risk = 0.0

        # Stage credit attribution — computed once over all loaded traces.
        # compute_leverage([]) returns a valid insufficient LeverageReport, so the
        # n=0 path is safe. `leverage_weights` (the dict) feeds _pick_best_proposal;
        # `leverage_report` (the report) feeds _write_log + ImprovementReport.
        leverage_report = compute_leverage(traces)
        if leverage_report.sufficient:
            leverage_weights = {
                sid: (1.0 + abs(s.r)) if s.sufficient else 1.0
                for sid, s in leverage_report.stages.items()
            }
        else:
            leverage_weights = None

        # Drift score: fraction of current failures that were previously "fixed".
        # Computed against the union of all prior cycles' verified_fixes so it
        # detects oscillation (a fixed issue reappearing) across the whole log.
        ever_verified = _load_all_verified_fixes(self._log_path)

        if n > 0:
            hqs_result = compute_hqs_from_traces(traces)
            hqs_before = hqs_result.hqs
            diagnostics = DiagnosticAnalyzer(traces).analyze()
            curr_diag_keys = self._diag_keys(diagnostics)
            regressed = curr_diag_keys & ever_verified
            drift_score = len(regressed) / max(len(curr_diag_keys), 1) if curr_diag_keys else 0.0
            # #5: drift is an implicit trigger independent of HQS. A workflow can
            # oscillate (fix A → break B → fix B → break A) while staying above
            # target_hqs; drift_score is the canonical signal for that. When drift
            # crosses the threshold, improvement fires even with healthy HQS.
            triggered_by_drift = (
                (hqs_before is None or hqs_before >= self._target_hqs)
                and drift_score >= self._drift_threshold
            )
            needs_improvement = (
                n >= self._min_traces
                and (hqs_before < self._target_hqs or drift_score >= self._drift_threshold)
            )
        else:
            curr_diag_keys = set()

        # Compute verification against previous cycle's predictions

        verified_fixes, missed_predictions, unexpected_regressions = self._verify_predictions(
            prev_diag_keys=set(prev_entry.get("diagnostics_keys", [])) if prev_entry else set(),
            predicted_fixes=prev_entry.get("predicted_fixes", []) if prev_entry else [],
            predicted_regressions=prev_entry.get("predicted_regressions", []) if prev_entry else [],
            curr_diag_keys=curr_diag_keys,
        )

        if needs_improvement:
            refiner = SpecRefiner(_model)
            spec_yaml = self._spec_path.read_text(encoding="utf-8")
            hqs_obj = compute_hqs_from_traces(traces) if traces else None
            editable_surfaces = [s.value for s in spec.self_improvement.editable_surfaces]
            # Close the loop: feed prior-cycle verification feedback and any post-run
            # analyst suggestions into the refiner so it can correct missed predictions.
            # #7 (Option 4): also surface optimize's A/B-tested records from the
            # unified ImprovementStore.
            optimizer_proposals = await _load_cross_engine_history(
                self._improvement_db_path, self._spec_path.stem
            )
            refiner_suggestions = _build_refiner_suggestions(
                prev_entry, traces, optimizer_proposals
            )

            if self._n_proposals > 1:
                candidates = await refiner.refine_many(
                    spec_yaml=spec_yaml,
                    diagnostics=diagnostics,
                    hqs=hqs_obj,
                    refiner_suggestions=refiner_suggestions,
                    editable_surfaces=editable_surfaces,
                    n_proposals=self._n_proposals,
                )
                n_proposals_generated = len(candidates)
                # Hard gate: drop any candidate that touches a surface not in
                # editable_surfaces (a locked surface). The lock is binding — a
                # refiner that ignores "DO NOT modify" has its work discarded.
                allowed_candidates = [
                    c for c in candidates
                    if _touched_surfaces(spec, c.spec) <= set(editable_surfaces)
                ]
                rejected_proposals = len(candidates) - len(allowed_candidates)
                if rejected_proposals:
                    rejected_locked_surfaces = sorted({
                        surf
                        for c in candidates
                        for surf in (_touched_surfaces(spec, c.spec) - set(editable_surfaces))
                    })
                healthy = _healthy_stage_ids(traces, diagnostics)
                safe_candidates = [
                    c for c in allowed_candidates
                    if not _proposal_regression_risk(c, spec, healthy)
                ]
                regression_risk_count = len(allowed_candidates) - len(safe_candidates)
                result = _pick_best_proposal(safe_candidates or allowed_candidates, diagnostics, spec, leverage=leverage_weights)
            else:
                result = await refiner.refine(
                    spec_yaml=spec_yaml,
                    diagnostics=diagnostics,
                    hqs=hqs_obj,
                    refiner_suggestions=refiner_suggestions,
                    editable_surfaces=editable_surfaces,
                )
                n_proposals_generated = 1 if result is not None else 0
                # In single-proposal mode, regression risk is informational only — the proposal
                # is still applied (we don't discard the only candidate).
                if result is not None:
                    healthy = _healthy_stage_ids(traces, diagnostics)
                    regression_risk_count = 1 if _proposal_regression_risk(result, spec, healthy) else 0
                else:
                    regression_risk_count = 0

            if result is not None:
                proposed_spec = result.spec
                proposed_yaml = result.yaml_text
                predicted_fixes = result.predicted_fixes
                predicted_regressions = result.predicted_regressions
                # #6: structural latency-risk of the selected proposal (both paths).
                latency_risk = _latency_risk(spec, result.spec)
                # Hard gate (single proposal): reject if it touches a locked surface.
                violations = _touched_surfaces(spec, result.spec) - set(editable_surfaces)
                if violations:
                    rejected_locked_surfaces = sorted(violations)
                    rejected_proposals = 1
                    # Locked-surface violation: do not apply, do not write pending.
                    # proposed_spec stays set so the caller can inspect what was rejected.
                elif triggered_by_drift:
                    # #5: drift-triggered proposals always require review — auto-apply
                    # is suppressed even when self._auto_apply is set and the change
                    # would otherwise be auto-eligible. Oscillation is the symptom of
                    # blind auto-apply; auto-applying here would worsen the latency
                    # churn (H4-v2 constraint). Write the proposal to .pending.yaml.
                    pending_path = self._spec_path.with_suffix("").with_name(
                        self._spec_path.stem + ".pending.yaml"
                    )
                    pending_path.write_text(proposed_yaml, encoding="utf-8")
                    requires_review = True
                    escalated_oscillation = True
                    _pending_path = pending_path
                elif self._auto_apply:
                    _, review_changes = _classify_changes(spec, result.spec)
                    if review_changes:
                        pending_path = self._spec_path.with_suffix("").with_name(
                            self._spec_path.stem + ".pending.yaml"
                        )
                        pending_path.write_text(proposed_yaml, encoding="utf-8")
                        requires_review = True
                        _pending_path = pending_path
                    else:
                        self._write_spec_history(self._spec_path.read_text(encoding="utf-8"))
                        self._spec_path.write_text(proposed_yaml, encoding="utf-8")
                        applied = True

        self._write_log(
            workflow_name=spec.name,
            n_traces=n,
            hqs_before=hqs_before,
            diagnostics=diagnostics,
            diagnostics_keys=sorted(curr_diag_keys),
            needs_improvement=needs_improvement,
            applied=applied,
            predicted_fixes=predicted_fixes,
            predicted_regressions=predicted_regressions,
            verified_fixes=verified_fixes,
            missed_predictions=missed_predictions,
            unexpected_regressions=unexpected_regressions,
            drift_score=drift_score,
            regression_risk_count=regression_risk_count,
            n_proposals_generated=n_proposals_generated,
            rejected_locked_surfaces=rejected_locked_surfaces,
            rejected_proposals=rejected_proposals,
            triggered_by_drift=triggered_by_drift,
            drift_threshold=self._drift_threshold,
            escalated_oscillation=escalated_oscillation,
            latency_risk=latency_risk,
            leverage=leverage_report,
        )
        # #7 (Option 4): also record this cycle to the unified ImprovementStore so
        # `armature optimize` can see improve's verified/missed/regressed work. The
        # JSONL log above stays the local audit + dashboard source; this record is
        # the cross-engine substrate. Advisory — never block improvement on a DB
        # write failure.
        if self._improvement_db_path is not None:
            try:
                await self._write_improvement_record(
                    workflow_stem=self._spec_path.stem,
                    predicted_fixes=predicted_fixes,
                    predicted_regressions=predicted_regressions,
                    verified_fixes=verified_fixes,
                    missed_predictions=missed_predictions,
                    unexpected_regressions=unexpected_regressions,
                    applied=applied,
                    hqs_before=hqs_before,
                    drift_score=drift_score,
                    triggered_by_drift=triggered_by_drift,
                    escalated_oscillation=escalated_oscillation,
                    latency_risk=latency_risk,
                )
            except Exception:
                pass

        return ImprovementReport(
            workflow_name=spec.name,
            spec_path=self._spec_path,
            n_traces=n,
            hqs_before=hqs_before,
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
            drift_score=drift_score,
            requires_review=requires_review,
            pending_path=_pending_path,
            n_proposals_generated=n_proposals_generated,
            regression_risk_count=regression_risk_count,
            rejected_locked_surfaces=rejected_locked_surfaces,
            rejected_proposals=rejected_proposals,
            triggered_by_drift=triggered_by_drift,
            escalated_oscillation=escalated_oscillation,
            latency_risk=latency_risk,
            leverage=leverage_report,
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

    def _write_spec_history(self, yaml_text: str) -> None:
        history_path = self._spec_path.parent / f"{self._spec_path.stem}.spec_history.jsonl"
        entry = json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "yaml": yaml_text,
        })
        with history_path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")

    def _write_log(
        self,
        *,
        workflow_name: str,
        n_traces: int,
        hqs_before: float | None,
        diagnostics: list[DiagnosticResult],
        diagnostics_keys: list[str],
        needs_improvement: bool,
        applied: bool,
        predicted_fixes: list[str],
        predicted_regressions: list[str],
        verified_fixes: list[str],
        missed_predictions: list[str],
        unexpected_regressions: list[str],
        drift_score: float = 0.0,
        regression_risk_count: int = 0,
        n_proposals_generated: int = 0,
        rejected_locked_surfaces: list[str] | None = None,
        rejected_proposals: int = 0,
        triggered_by_drift: bool = False,
        drift_threshold: float = 0.5,
        escalated_oscillation: bool = False,
        latency_risk: float = 0.0,
        leverage: LeverageReport | None = None,
    ) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workflow_name": workflow_name,
            "n_traces": n_traces,
            "hqs_before": hqs_before,
            "target_hqs": self._target_hqs,
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
            "drift_score": drift_score,
            "regression_risk_count": regression_risk_count,
            "n_proposals_generated": n_proposals_generated,
            "rejected_locked_surfaces": rejected_locked_surfaces or [],
            "rejected_proposals": rejected_proposals,
            # #5 drift-trigger audit trail
            "triggered_by_drift": triggered_by_drift,
            "drift_threshold": drift_threshold,
            "escalated_oscillation": escalated_oscillation,
            # #6 latency-aware selection — structural latency-risk of the selected proposal
            "latency_risk": latency_risk,
            # Stage credit attribution — leverage report (sufficient flag + per-stage r).
            "leverage_sufficient": (leverage.sufficient if leverage is not None else False),
            "leverage_stages": (
                {sid: {"r": s.r, "n_runs": s.n_runs, "sufficient": s.sufficient}
                 for sid, s in leverage.stages.items()}
                if leverage is not None else {}
            ),
        }
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    async def _write_improvement_record(
        self,
        *,
        workflow_stem: str,
        predicted_fixes: list[str],
        predicted_regressions: list[str],
        verified_fixes: list[str],
        missed_predictions: list[str],
        unexpected_regressions: list[str],
        applied: bool,
        hqs_before: float | None,
        drift_score: float,
        triggered_by_drift: bool,
        escalated_oscillation: bool,
        latency_risk: float,
    ) -> None:
        """Record this improve cycle to the unified ``ImprovementStore``.

        The cross-engine substrate (Option 4): optimize reads these
        ``source="improve"`` records to avoid re-proposing verified fixes and to
        target missed predictions. improve doesn't produce a diff/rationale/score,
        so the optimize-side fields stay at defaults; ``accepted`` mirrors
        ``applied`` and ``score`` carries ``hqs_before`` as the cycle's HQS context.
        """
        from armature.state.improvement_store import (
            ImprovementRecord, ImprovementStore,
        )
        store = ImprovementStore(self._improvement_db_path)
        await store.init()
        await store.record(ImprovementRecord(
            record_id=str(uuid.uuid4()),
            workflow_stem=workflow_stem,
            source="improve",
            accepted=applied,
            score=hqs_before if hqs_before is not None else 0.0,
            applied=applied,
            hqs_before=hqs_before,
            predicted_fixes=predicted_fixes,
            predicted_regressions=predicted_regressions,
            verified_fixes=verified_fixes,
            missed_predictions=missed_predictions,
            unexpected_regressions=unexpected_regressions,
            drift_score=drift_score,
            triggered_by_drift=triggered_by_drift,
            escalated_oscillation=escalated_oscillation,
            latency_risk=latency_risk,
        ))

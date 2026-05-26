"""DashboardData — multi-run aggregated data model for the Rich dashboard."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from armature.state.traces import TraceRecord


@dataclass
class StageStats:
    stage_id: str
    role_type: str
    run_count: int
    failure_rate: float
    avg_latency_ms: float
    avg_quorum: float | None
    escalation_rate: float
    is_post_run: bool


@dataclass
class ImprovementCycle:
    cycle_number: int
    timestamp: str
    ihr_before: float
    drift_score: float
    applied: bool
    requires_review: bool
    verified_fixes: int
    missed_predictions: int
    unexpected_regressions: int
    predicted_fixes: list[str]
    predicted_regressions: list[str]


@dataclass
class SafetyStats:
    warn_hits: int
    block_hits: int
    approval_hits: int
    postcondition_failures: int
    current_policy_version: str | None
    stale_memory_count: int


@dataclass
class DashboardData:
    workflow_name: str
    total_runs: int
    traces: list[TraceRecord]
    stage_stats: dict[str, StageStats]
    improvement_cycles: list[ImprovementCycle]
    safety_stats: SafetyStats
    ihr_trend: list[float]  # ordered oldest → newest
    last_run_id: str | None

    @property
    def current_ihr(self) -> float | None:
        return self.ihr_trend[-1] if self.ihr_trend else None

    @property
    def health_color(self) -> str:
        ihr = self.current_ihr
        if ihr is None:
            return "dim"
        if ihr >= 0.85:
            return "green"
        if ihr >= 0.70:
            return "yellow"
        return "red"

    @property
    def ihr_delta(self) -> float | None:
        if len(self.ihr_trend) < 2:
            return None
        return self.ihr_trend[-1] - self.ihr_trend[-2]


# ── builders ──────────────────────────────────────────────────────────────────

def build_stage_stats(traces: list[TraceRecord]) -> dict[str, StageStats]:
    """Aggregate per-stage stats from a list of trace records."""
    from collections import defaultdict

    buckets: dict[str, list[TraceRecord]] = defaultdict(list)
    for t in traces:
        buckets[t.stage_id].append(t)

    stats: dict[str, StageStats] = {}
    for stage_id, stage_traces in buckets.items():
        n = len(stage_traces)
        failures = sum(1 for t in stage_traces if not t.success or not t.output_valid)
        avg_lat = sum(t.latency_ms for t in stage_traces) / n

        quorum_vals = [t.quorum_score for t in stage_traces if t.quorum_score is not None]
        avg_quorum = sum(quorum_vals) / len(quorum_vals) if quorum_vals else None

        # Escalation: count traces whose model differs from the most common model
        models = [t.model for t in stage_traces]
        if models:
            baseline = max(set(models), key=models.count)
            escalated = sum(1 for m in models if m != baseline)
        else:
            escalated = 0

        is_post_run = any(t.role_type == "post_run" for t in stage_traces)

        stats[stage_id] = StageStats(
            stage_id=stage_id,
            role_type=stage_traces[0].role_type,
            run_count=n,
            failure_rate=failures / n,
            avg_latency_ms=avg_lat,
            avg_quorum=avg_quorum,
            escalation_rate=escalated / n,
            is_post_run=is_post_run,
        )
    return stats


def load_improvement_cycles(log_path: Path) -> list[ImprovementCycle]:
    """Read a JSONL improvement log and return cycles newest-first."""
    if not log_path.exists():
        return []
    cycles: list[ImprovementCycle] = []
    for i, line in enumerate(log_path.read_text().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        cycles.append(ImprovementCycle(
            cycle_number=i + 1,
            timestamp=entry.get("timestamp", ""),
            ihr_before=float(entry.get("ihr_before", 0.0)),
            drift_score=float(entry.get("drift_score", 0.0)),
            applied=bool(entry.get("applied", False)),
            requires_review=bool(entry.get("requires_review", False)),
            verified_fixes=len(entry.get("verified_fixes", [])),
            missed_predictions=len(entry.get("missed_predictions", [])),
            unexpected_regressions=len(entry.get("unexpected_regressions", [])),
            predicted_fixes=entry.get("predicted_fixes", []),
            predicted_regressions=entry.get("predicted_regressions", []),
        ))
    # Newest first (last line in JSONL is most recent)
    return list(reversed(cycles))


def load_safety_stats(traces: list[TraceRecord]) -> SafetyStats:
    """Derive safety/governance stats from trace records."""
    postcondition_failures = sum(
        1 for t in traces if t.error_type == "PostconditionFailed"
    )

    # Extract policy version from most recent trace that has one
    current_policy_version: str | None = None
    for t in reversed(traces):
        pv = getattr(t, "policy_version", None)
        if pv:
            current_policy_version = pv
            break

    # Count stale_memory labels in inputs_provenance
    stale_memory_count = 0
    for t in traces:
        provenance = getattr(t, "inputs_provenance", {}) or {}
        if any(v == "stale_memory" for v in provenance.values()):
            stale_memory_count += 1

    return SafetyStats(
        warn_hits=0,    # derived from session log; not yet tracked in TraceRecord
        block_hits=0,
        approval_hits=0,
        postcondition_failures=postcondition_failures,
        current_policy_version=current_policy_version,
        stale_memory_count=stale_memory_count,
    )

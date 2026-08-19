"""DashboardData — multi-run aggregated data model for the Rich dashboard."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from armature.state.leverage import LeverageReport
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
    avg_tools_declared: float = 0.0
    avg_tools_called: float = 0.0
    fan_out_per_run: int = 1


@dataclass
class ImprovementCycle:
    cycle_number: int
    timestamp: str
    hqs_before: float
    drift_score: float
    applied: bool
    requires_review: bool
    verified_fixes: int
    missed_predictions: int
    unexpected_regressions: int
    predicted_fixes: list[str]
    predicted_regressions: list[str]
    escalated_oscillation: bool = False
    latency_risk: float = 0.0


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
    hqs_trend: list[float]  # ordered oldest → newest
    last_run_id: str | None
    last_run_at: str | None = None
    leverage: LeverageReport | None = None

    @property
    def current_hqs(self) -> float | None:
        return self.hqs_trend[-1] if self.hqs_trend else None

    @property
    def health_color(self) -> str:
        hqs = self.current_hqs
        if hqs is None:
            return "dim"
        if hqs >= 0.85:
            return "green"
        if hqs >= 0.70:
            return "yellow"
        return "red"

    @property
    def hqs_delta(self) -> float | None:
        if len(self.hqs_trend) < 2:
            return None
        return self.hqs_trend[-1] - self.hqs_trend[-2]


# ── builders ──────────────────────────────────────────────────────────────────

def build_stage_stats(traces: list[TraceRecord]) -> dict[str, StageStats]:
    """Aggregate per-stage stats from a list of trace records."""
    from collections import defaultdict

    buckets: dict[str, list[TraceRecord]] = defaultdict(list)
    for t in traces:
        buckets[t.stage_id].append(t)

    # Per-run counts for fan-out detection: {stage_id: {run_id: count}}
    per_run_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in traces:
        per_run_counts[t.stage_id][t.run_id] += 1

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

        tool_traces = [t for t in stage_traces if t.tools_declared]
        if tool_traces:
            avg_tools_declared = sum(len(t.tools_declared) for t in tool_traces) / len(tool_traces)
            avg_tools_called = sum(len(t.tools_called) for t in tool_traces) / len(tool_traces)
        else:
            avg_tools_declared = 0.0
            avg_tools_called = 0.0

        fan_out_per_run = max(per_run_counts[stage_id].values(), default=1)

        stats[stage_id] = StageStats(
            stage_id=stage_id,
            role_type=stage_traces[0].role_type,
            run_count=n,
            failure_rate=failures / n,
            avg_latency_ms=avg_lat,
            avg_quorum=avg_quorum,
            escalation_rate=escalated / n,
            is_post_run=is_post_run,
            avg_tools_declared=avg_tools_declared,
            avg_tools_called=avg_tools_called,
            fan_out_per_run=fan_out_per_run,
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
            hqs_before=float(entry.get("hqs_before", 0.0)),
            drift_score=float(entry.get("drift_score", 0.0)),
            applied=bool(entry.get("applied", False)),
            requires_review=bool(entry.get("requires_review", False)),
            verified_fixes=len(entry.get("verified_fixes", [])),
            missed_predictions=len(entry.get("missed_predictions", [])),
            unexpected_regressions=len(entry.get("unexpected_regressions", [])),
            predicted_fixes=entry.get("predicted_fixes", []),
            predicted_regressions=entry.get("predicted_regressions", []),
            escalated_oscillation=bool(entry.get("escalated_oscillation", False)),
            latency_risk=float(entry.get("latency_risk", 0.0)),
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

    approval_hits = sum(1 for t in traces if t.role_type == "gate")

    return SafetyStats(
        warn_hits=0,    # derived from session log; not yet tracked in TraceRecord
        block_hits=0,
        approval_hits=approval_hits,
        postcondition_failures=postcondition_failures,
        current_policy_version=current_policy_version,
        stale_memory_count=stale_memory_count,
    )

"""ReportBuilder — diagnostic run report answering "is this working well?"""
from __future__ import annotations
import json
import textwrap
from dataclasses import dataclass, field
from typing import Any

from armature.state.evaluator import EvaluationResult
from armature.state.knowledge import KnowledgeRecord
from armature.state.session import SessionEvent
from armature.state.traces import IhrResult, TraceRecord

_WIDE = 72
_SEP = "═" * _WIDE
_SEP2 = "─" * _WIDE
_SLOW_MS = 30_000      # stages slower than this get a SLOW flag
_OUTPUT_LIMIT = 500    # chars to show for long text values


@dataclass
class ReportData:
    run_id: str
    workflow_name: str
    traces: list[TraceRecord] = field(default_factory=list)
    events: list[SessionEvent] = field(default_factory=list)
    evaluations: list[EvaluationResult] = field(default_factory=list)
    knowledge: list[KnowledgeRecord] = field(default_factory=list)
    ihr: IhrResult | None = None


class ReportBuilder:
    def __init__(self, data: ReportData) -> None:
        self._d = data

    def build(self) -> str:
        parts: list[str] = [self._header()]

        failures = [t for t in self._d.traces if not t.success or not t.output_valid]
        if failures or self._d.ihr:
            parts += ["", self._health(failures)]

        if failures:
            parts += ["", self._issues(failures)]

        parts += ["", self._stage_timeline()]

        decision_traces = [t for t in self._d.traces if t.role_type in ("judge", "orchestrator")]
        if decision_traces:
            parts += ["", self._quality_signals(decision_traces)]

        parts += ["", self._key_outputs()]

        if self._d.evaluations:
            parts += ["", self._evaluation_section()]
        if self._d.knowledge:
            parts += ["", self._knowledge_section()]

        parts.append(_SEP)
        return "\n".join(p for p in parts if p != "" or True)

    # ── sections ──────────────────────────────────────────────────────────────

    def _header(self) -> str:
        d = self._d
        n = len(d.traces)
        total_tokens = sum(t.input_tokens + t.output_tokens for t in d.traces)
        total_ms = sum(t.latency_ms for t in d.traces)
        elapsed = f"{total_ms / 1000:.1f}s" if d.traces else "—"
        run_short = d.run_id[:8]
        ts = d.traces[0].timestamp[:16].replace("T", " ") if d.traces else "—"
        line1 = f"  {d.workflow_name}  ·  run {run_short}  ·  {ts} UTC"
        line2 = f"  {n} stage{'s' if n != 1 else ''}  ·  {elapsed}  ·  {total_tokens:,} tokens"
        return "\n".join([_SEP, line1, line2, _SEP])

    def _health(self, failures: list[TraceRecord]) -> str:
        d = self._d
        ok = len(d.traces) - len(failures)
        status = "✓ OK" if not failures else f"⚠ {len(failures)} failure{'s' if len(failures) > 1 else ''}"
        parts = [f"  {status}  ·  {ok}/{len(d.traces)} stages succeeded"]
        if d.ihr:
            r = d.ihr
            parts[0] += f"  ·  IHR {r.ihr:.2f}"
        return "\n".join(parts)

    def _issues(self, failures: list[TraceRecord]) -> str:
        lines = ["Issues", _SEP2]
        for t in failures:
            reason = ""
            if not t.success:
                ec = t.outputs.get("exit_code")
                err = t.outputs.get("stderr") or t.outputs.get("error") or t.error_type or ""
                if ec is not None:
                    reason = f"exit_code={ec}"
                if err:
                    snippet = str(err)[:80].replace("\n", " ")
                    reason += (f" — {snippet}" if reason else snippet)
            label = "✗ failed" if not t.success else "✗ invalid output"
            lines.append(f"  {label}  [{t.stage_id}]  {reason}".rstrip())
        return "\n".join(lines)

    def _stage_timeline(self) -> str:
        header = f"  {'stage':<22} {'role':<14} {'latency':>9}  {'tokens':>7}  {'ok':>3}"
        rows = [header, "  " + _SEP2]
        for t in self._d.traces:
            ok = "✓" if t.success and t.output_valid else "✗"
            lat = f"{int(t.latency_ms)}ms"
            tokens = t.input_tokens + t.output_tokens
            slow = "  SLOW" if t.latency_ms > _SLOW_MS else ""
            quorum = f"  q={t.quorum_score:.2f}" if t.quorum_score is not None else ""
            rows.append(
                f"  {t.stage_id:<22} {t.role_type:<14} {lat:>9}  {tokens:>7}  {ok:>3}{quorum}{slow}"
            )
        return "Stage Timeline\n" + "\n".join(rows)

    def _quality_signals(self, decision_traces: list[TraceRecord]) -> str:
        lines = ["Quality Signals — Decisions & Deliberations", _SEP2]
        for t in decision_traces:
            out = t.outputs
            # Decision badge
            if "accept" in out:
                decision = "✓ ACCEPTED" if out["accept"] else "✗ REJECTED"
            elif "decision" in out:
                decision = f"→ {out['decision']}"
            else:
                decision = ""

            conf_raw = out.get("confidence")
            conf = f"  confidence={float(conf_raw):.2f}" if conf_raw is not None else ""
            score_raw = out.get("score")
            score = f"  score={float(score_raw):.2f}" if score_raw is not None else ""
            header = f"\n  [{t.stage_id}]  {t.role_type}{('  ' + decision) if decision else ''}{conf}{score}"
            lines.append(header)

            # Show key text fields: notes, feedback, reasoning, rationale, summary
            for key in ("notes", "feedback", "reasoning", "rationale", "summary"):
                if key in out and out[key]:
                    text = self._wrap(str(out[key]), indent=4, limit=400)
                    lines.append(f"    {key}: {text}")

            # For orchestrators without text keys, show the output summary
            shown_keys = {"accept", "decision", "confidence", "score", "notes",
                          "feedback", "reasoning", "rationale", "summary"}
            for key, val in out.items():
                if key in shown_keys:
                    continue
                summary = self._summarize_value(key, val)
                if summary:
                    lines.append(f"    {key}: {summary}")

        return "\n".join(lines)

    def _key_outputs(self) -> str:
        lines = ["Key Outputs", _SEP2]
        _decision_keys = {"accept", "decision", "confidence", "notes", "feedback",
                          "reasoning", "rationale", "score"}
        any_shown = False

        for t in self._d.traces:
            out = t.outputs
            if not out:
                continue

            # Script stages: only show if there's an error
            if t.role_type == "script":
                ec = out.get("exit_code")
                stderr = str(out.get("stderr", "") or "").strip()
                ok_exit = (str(ec) == "0" or ec == 0)
                if ok_exit and not stderr:
                    continue
                lines.append(f"\n  [{t.stage_id}]  script")
                if not ok_exit and ec is not None:
                    lines.append(f"    exit_code: {ec}")
                if stderr:
                    lines.append(f"    stderr: {self._wrap(stderr, indent=4, limit=200)}")
                any_shown = True
                continue

            # Judge/orchestrator: already shown in Quality Signals — skip entirely here
            if t.role_type in ("judge", "orchestrator"):
                continue

            # Worker stages: show their meaningful outputs
            output_lines: list[str] = []
            for key, val in out.items():
                summary = self._summarize_value(key, val)
                if summary:
                    output_lines.append(f"    {key}: {summary}")

            if output_lines:
                lines.append(f"\n  [{t.stage_id}]  {t.role_type}")
                lines.extend(output_lines)
                any_shown = True

        if not any_shown:
            lines.append("  (no notable outputs)")

        return "\n".join(lines)

    def _evaluation_section(self) -> str:
        lines = ["Evaluation Scores", _SEP2]
        for ev in self._d.evaluations:
            lines.append(f"\n  {ev.stage_id}  (score: {ev.score:.2f})")
            for c in ev.criteria_passed:
                lines.append(f"    ✓  {c}")
            for c in ev.criteria_failed:
                lines.append(f"    ✗  {c}")
            if ev.notes:
                lines.append(f"    note: {ev.notes}")
        return "\n".join(lines)

    def _knowledge_section(self) -> str:
        lines = ["Knowledge Extracted This Run", _SEP2]
        for k in self._d.knowledge:
            lines.append(f"  [{k.entity}]  {k.fact}  (conf: {k.confidence:.2f})")
        return "\n".join(lines)

    # ── formatting helpers ────────────────────────────────────────────────────

    @staticmethod
    def _wrap(text: str, indent: int, limit: int = _OUTPUT_LIMIT) -> str:
        text = text.strip().replace("\n", " ")
        if len(text) > limit:
            text = text[:limit] + "…"
        pad = " " * indent
        return textwrap.fill(text, width=_WIDE - indent, subsequent_indent=pad)

    @classmethod
    def _summarize_value(cls, key: str, val: Any) -> str:
        """Return a human-readable one-or-few-line summary of a field value."""
        if val is None:
            return ""

        if isinstance(val, str):
            v = val.strip()
            if not v:
                return ""
            return cls._wrap(v, indent=4 + len(key) + 2, limit=_OUTPUT_LIMIT)

        if isinstance(val, bool):
            return str(val)

        if isinstance(val, (int, float)):
            return str(val)

        if isinstance(val, dict):
            # Compact summary: "key=val, key=val, …" for small dicts
            parts = []
            for k, v in list(val.items())[:6]:
                parts.append(f"{k}={v}")
            summary = ", ".join(parts)
            if len(summary) > _OUTPUT_LIMIT:
                summary = summary[:_OUTPUT_LIMIT] + "…"
            return summary

        if isinstance(val, list):
            n = len(val)
            if n == 0:
                return "(empty list)"
            if all(isinstance(i, str) for i in val):
                sample = ", ".join(val[:3])
                return f"[{sample}{'…' if n > 3 else ''}]  ({n} items)"
            # List of dicts — show first item summary + count
            first = val[0]
            if isinstance(first, dict):
                preview = ", ".join(f"{k}={v}" for k, v in list(first.items())[:3])
                if len(preview) > 120:
                    preview = preview[:120] + "…"
                suffix = f"  (+{n-1} more)" if n > 1 else ""
                return f"{{{preview}}}{suffix}"
            return f"({n} items)"

        return ""


# ── async loader ─────────────────────────────────────────────────────────────

async def load_report_data(
    run_id: str,
    traces_db: "Path | str | None" = None,
    evals_db: "Path | str | None" = None,
    knowledge_db: "Path | str | None" = None,
    session_log: "Path | str | None" = None,
) -> ReportData | None:
    """Load all stores and return a ReportData. Returns None if no traces found."""
    from pathlib import Path
    from armature.state.traces import TraceStore

    db = Path(traces_db) if traces_db else Path(f"~/.armature/runs/{run_id}/traces.db").expanduser()
    if not db.exists():
        return None

    store = TraceStore(db)
    traces = await store.query_by_run(run_id)
    if not traces:
        return None

    workflow_name = traces[0].workflow_name
    ihr = await store.compute_ihr(run_id)

    evaluations: list[EvaluationResult] = []
    if evals_db:
        edb = Path(evals_db)
        if edb.exists():
            from armature.state.evaluator import EvaluationStore
            estore = EvaluationStore(edb)
            evaluations = await estore.load_for_run(run_id)

    knowledge: list[KnowledgeRecord] = []
    if knowledge_db:
        kdb = Path(knowledge_db)
        if kdb.exists():
            from armature.state.knowledge import KnowledgeStore
            kstore = KnowledgeStore(kdb)
            knowledge = await kstore.load(workflow_name)
            knowledge = [k for k in knowledge if k.source_run_id == run_id]

    events: list[SessionEvent] = []
    if session_log:
        slog_path = Path(session_log)
        if slog_path.exists():
            from armature.state.session import SessionLog
            slog = SessionLog(slog_path)
            events = await slog.read_all()

    return ReportData(
        run_id=run_id,
        workflow_name=workflow_name,
        traces=traces,
        events=events,
        evaluations=evaluations,
        knowledge=knowledge,
        ihr=ihr,
    )

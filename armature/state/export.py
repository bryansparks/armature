"""TraceExporter — export high-quality traces as SFT/DPO training data.

Formats supported:
  chat      OpenAI ChatML (also Qwen, LLaMA instruction tuning)
  alpaca    Stanford Alpaca instruction format
  sharegpt  ShareGPT conversation format

Each trace becomes one JSONL record. The prompt is reconstructed from the
trace's inputs dict (infra keys stripped) and the assistant completion from
the outputs dict. When a spec is available, pass system_prompt to supply
accurate stage descriptions; without it the role_type is used as a fallback.

For DPO/GRPO training, export_dpo() pairs high-quality (chosen) traces with
low-quality (rejected) traces from the same stage, producing the
{prompt, chosen, rejected} format expected by most DPO trainers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from armature.state.traces import TraceRecord, TraceStore

ExportFormat = Literal["chat", "alpaca", "sharegpt"]

# Context keys that are infrastructure, not meaningful training signal
_INFRA_KEYS = frozenset({
    "run_id", "_memory", "_knowledge", "_transcript", "_diagnostics",
})


@dataclass
class ExportSummary:
    total_exported: int
    output_path: Path
    format: str
    workflow_name: str
    min_quorum_score: float


class TraceExporter:
    """Export traces from a TraceStore as SFT or DPO training data."""

    def __init__(self, store: TraceStore) -> None:
        self._store = store

    async def export(
        self,
        workflow_name: str,
        output_path: Path | str,
        *,
        format: ExportFormat = "chat",
        min_quorum_score: float = 0.85,
        role_types: list[str] | None = None,
        system_prompt: str | None = None,
        limit: int = 1000,
    ) -> ExportSummary:
        """Export high-quality traces as JSONL training records.

        Args:
            workflow_name: Filter to this workflow.
            output_path: Where to write the JSONL file.
            format: Output format — "chat", "alpaca", or "sharegpt".
            min_quorum_score: Only include traces at or above this quality threshold.
            role_types: If set, only include traces whose role_type is in this list.
            system_prompt: Override the system/instruction field for all records.
                           When None, uses "You are a {role_type} agent."
            limit: Maximum number of traces to fetch from the store.
        """
        traces = await self._store.query(
            workflow_name=workflow_name,
            min_quorum_score=min_quorum_score,
            limit=limit,
        )

        if role_types:
            traces = [t for t in traces if t.role_type in role_types]

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        with out.open("w", encoding="utf-8") as f:
            for trace in traces:
                record = self._to_record(trace, format=format, system_prompt=system_prompt)
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return ExportSummary(
            total_exported=len(traces),
            output_path=out,
            format=format,
            workflow_name=workflow_name,
            min_quorum_score=min_quorum_score,
        )

    async def export_dpo(
        self,
        workflow_name: str,
        output_path: Path | str,
        *,
        chosen_min_score: float = 0.85,
        rejected_max_score: float = 0.30,
        system_prompt: str | None = None,
        limit: int = 500,
    ) -> ExportSummary:
        """Export chosen/rejected pairs for DPO or GRPO training.

        Pairs are matched by stage_id: for each high-quality (chosen) trace,
        find a low-quality (rejected) trace from the same stage. Unpaired
        chosen traces are omitted. If the same stage has multiple rejected
        traces, the lowest-scoring one is selected.
        """
        chosen = await self._store.query(
            workflow_name=workflow_name,
            min_quorum_score=chosen_min_score,
            limit=limit,
        )

        # Fetch all traces and keep only low-quality ones for rejection pool
        all_traces = await self._store.query(
            workflow_name=workflow_name,
            limit=limit * 10,
        )
        rejected_by_stage: dict[str, list[TraceRecord]] = {}
        for t in all_traces:
            score = t.quorum_score if t.quorum_score is not None else 1.0
            if score <= rejected_max_score:
                rejected_by_stage.setdefault(t.stage_id, []).append(t)

        # Sort rejected pools: lowest score first (worst outputs make strongest signal)
        for pool in rejected_by_stage.values():
            pool.sort(key=lambda t: t.quorum_score or 0.0)

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        pairs_written = 0
        with out.open("w", encoding="utf-8") as f:
            for c in chosen:
                pool = rejected_by_stage.get(c.stage_id, [])
                if not pool:
                    continue
                r = pool[0]
                prompt = self._build_user_content(c)
                if system_prompt:
                    prompt = f"{system_prompt}\n\n{prompt}"
                record = {
                    "prompt": prompt,
                    "chosen": self._build_assistant_content(c),
                    "rejected": self._build_assistant_content(r),
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                pairs_written += 1

        return ExportSummary(
            total_exported=pairs_written,
            output_path=out,
            format="dpo",
            workflow_name=workflow_name,
            min_quorum_score=chosen_min_score,
        )

    # ── internal helpers ──────────────────────────────────────────────────────

    def _build_user_content(self, trace: TraceRecord) -> str:
        parts = []
        for key, val in trace.inputs.items():
            if key in _INFRA_KEYS or key.startswith("_"):
                continue
            parts.append(f"{key}: {val}")
        return "\n".join(parts) if parts else "(no inputs)"

    def _build_assistant_content(self, trace: TraceRecord) -> str:
        out = {k: v for k, v in trace.outputs.items() if not k.startswith("_")}
        if not out:
            return ""
        # Single 'content' key → emit raw string (text-mode worker output)
        if len(out) == 1 and "content" in out:
            return str(out["content"])
        return json.dumps(out, ensure_ascii=False, indent=2)

    def _system_content(self, trace: TraceRecord, system_prompt: str | None) -> str:
        if system_prompt:
            return system_prompt
        return f"You are a {trace.role_type} agent. Complete the task described by the user."

    def _to_record(
        self,
        trace: TraceRecord,
        *,
        format: ExportFormat,
        system_prompt: str | None,
    ) -> dict[str, Any]:
        system = self._system_content(trace, system_prompt)
        user = self._build_user_content(trace)
        assistant = self._build_assistant_content(trace)

        if format == "chat":
            return {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": assistant},
                ]
            }
        if format == "alpaca":
            return {"instruction": system, "input": user, "output": assistant}
        if format == "sharegpt":
            return {
                "conversations": [
                    {"from": "human", "value": f"{system}\n\n{user}"},
                    {"from": "gpt", "value": assistant},
                ]
            }
        raise ValueError(f"Unknown export format: {format!r}")

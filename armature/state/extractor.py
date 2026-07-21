"""KnowledgeExtractor — calls an LLM to extract structured facts from raw memories.

Memories are labeled by (stage_id, capture_key) in the prompt; the LLM is asked to cite
the source per fact. Extracted candidates are reconciled (dedup/update/supersede/merge)
against existing records when reconcile=True.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any
import litellm
from armature.state.knowledge import KnowledgeRecord, MemoryType, KnowledgeStore
from armature.state.reconcile import Reconciler


async def litellm_completion(**kwargs) -> Any:
    return await litellm.acompletion(**kwargs)


_SYSTEM_PROMPT = """\
You are a knowledge extraction assistant. Given raw captured data from a workflow run, \
extract structured factual insights that would be useful to remember for future runs.

Each memory entry is labeled with its source stage and capture key. For each fact you \
extract, cite the source_stage and source_key it came from, and classify its type as one \
of: fact, event, instruction, preference.

Return a JSON array only, no other text:
[{"entity": "subject of the fact", "fact": "the fact itself", "confidence": 0.0-1.0, \
  "source_stage": "stage_id or null", "source_key": "capture_key or null", "type": "fact"}]

If there is nothing meaningful to extract, return an empty array: []
"""


def _label_memories(memories: dict) -> str:
    """Render memories as labeled text so the LLM can cite source_stage/source_key."""
    lines: list[str] = []
    for stage_id, keys in (memories or {}).items():
        for capture_key, values in (keys or {}).items():
            lines.append(f"[source_stage={stage_id} source_key={capture_key}] {json.dumps(values, default=str)}")
    return "\n".join(lines) if lines else "(no memories captured)"


def _known_sources(memories: dict) -> set[tuple[str, str]]:
    known: set[tuple[str, str]] = set()
    for stage_id, keys in (memories or {}).items():
        for capture_key in (keys or {}):
            known.add((stage_id, capture_key))
    return known


class KnowledgeExtractor:
    def __init__(
        self,
        model: str,
        knowledge_store: KnowledgeStore | None = None,
        embedder=None,
        reconcile: bool = True,
        reconcile_llm: bool = False,
    ):
        self._model = model
        self._knowledge_store = knowledge_store
        self._embedder = embedder
        self._reconcile = reconcile
        self._reconcile_llm = reconcile_llm

    async def extract(
        self,
        memories: dict[str, Any],
        workflow_name: str,
        run_id: str,
    ) -> list[KnowledgeRecord]:
        if not memories:
            return []

        known = _known_sources(memories)
        now = datetime.now(timezone.utc).isoformat()
        try:
            response = await litellm_completion(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Workflow captured data:\n{_label_memories(memories)}"},
                ],
            )
            content = response.choices[0].message.content or ""
            raw = json.loads(content)
            if not isinstance(raw, list):
                return []
        except Exception:
            return []

        records: list[KnowledgeRecord] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                stage = item.get("source_stage")
                key = item.get("source_key")
                # Validate cited source against actual memories; drop if hallucinated.
                if stage is not None and key is not None and (stage, key) not in known:
                    stage, key = None, None
                elif stage is not None and key is None:
                    key = None
                provenance = [{
                    "run_id": run_id,
                    "stage_id": stage,
                    "capture_key": key,
                    "ts": now,
                }]
                rec = KnowledgeRecord(
                    workflow_name=workflow_name,
                    entity=str(item.get("entity", "unknown")),
                    fact=str(item.get("fact", "")),
                    confidence=float(item.get("confidence", 0.5)),
                    source_run_id=run_id,
                    source_stage_id=stage,
                    source_capture_key=key,
                    type=MemoryType(item.get("type", "fact")),
                    provenance=provenance,
                )
                records.append(rec)
            except Exception:
                continue

        await self._persist(records, workflow_name, run_id)
        return records

    async def _persist(self, records: list[KnowledgeRecord], workflow_name: str, run_id: str) -> None:
        if not records:
            return
        try:
            if self._knowledge_store is not None and self._reconcile:
                reconciler = Reconciler(self._knowledge_store, embedder=self._embedder)
                await reconciler.reconcile_batch(records)
            elif self._knowledge_store is not None:
                for rec in records:
                    try:
                        await self._knowledge_store.record(rec, embedder=self._embedder)
                    except Exception:
                        pass  # storage failure must never block execution
        except Exception:
            pass  # extraction must never block execution
"""KnowledgeExtractor — calls an LLM to extract structured facts from raw memories."""
from __future__ import annotations
import json
from typing import Any
import litellm
from armature.state.knowledge import KnowledgeRecord, KnowledgeStore


async def litellm_completion(**kwargs) -> Any:
    return await litellm.acompletion(**kwargs)


_SYSTEM_PROMPT = """\
You are a knowledge extraction assistant. Given raw captured data from a workflow run, \
extract structured factual insights that would be useful to remember for future runs.

Focus on:
- Domain facts learned during this run
- User preferences or behaviors observed
- Patterns or constraints discovered

Return a JSON array only, no other text:
[{"entity": "subject of the fact", "fact": "the fact itself", "confidence": 0.0-1.0}]

If there is nothing meaningful to extract, return an empty array: []
"""


class KnowledgeExtractor:
    """Extracts structured knowledge from raw MemoryStore outputs using an LLM."""

    def __init__(self, model: str, knowledge_store: KnowledgeStore | None = None):
        self._model = model
        self._knowledge_store = knowledge_store

    async def extract(
        self,
        memories: dict[str, Any],
        workflow_name: str,
        run_id: str,
    ) -> list[KnowledgeRecord]:
        """Call the LLM to extract facts from raw memories; returns empty list on any failure."""
        if not memories:
            return []

        memories_text = json.dumps(memories, indent=2, default=str)

        try:
            response = await litellm_completion(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Workflow captured data:\n{memories_text}"},
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
                rec = KnowledgeRecord(
                    workflow_name=workflow_name,
                    entity=str(item.get("entity", "unknown")),
                    fact=str(item.get("fact", "")),
                    confidence=float(item.get("confidence", 0.5)),
                    source_run_id=run_id,
                )
                records.append(rec)
            except Exception:
                continue

        if self._knowledge_store is not None:
            for rec in records:
                try:
                    await self._knowledge_store.record(rec)
                except Exception:
                    pass  # storage failure must never block execution

        return records

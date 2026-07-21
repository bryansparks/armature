"""Reconciler — dedup/update/supersede/merge new candidate records against existing ones.

Heuristic classification (Phase 1): text similarity via difflib, entity/type match,
confidence delta. Semantic embeddings (when available) are used by find_neighbors to
*retrieve* candidates; the similarity *score* uses difflib so the classifier works
with or without sentence-transformers installed.
"""
from __future__ import annotations
import difflib
from datetime import datetime, timezone
from typing import Literal
from armature.state.knowledge import KnowledgeRecord

Decision = Literal["STORE", "SKIP", "UPDATE", "SUPERSEDE", "MERGE"]


def _text_sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _entity_match(c: KnowledgeRecord, n: KnowledgeRecord) -> bool:
    ce, ne = (c.entity or "").lower(), (n.entity or "").lower()
    return ce == ne or (ce and ne and (ce in ne or ne in ce))


def _union_provenance(a: list[dict], b: list[dict]) -> list[dict]:
    """Union two provenance lists by run_id, merging per-run fields."""
    by_run: dict[str, dict] = {}
    for entry in (a or []) + (b or []):
        run_id = entry.get("run_id")
        if run_id is None:
            continue
        if run_id not in by_run:
            by_run[run_id] = dict(entry)
        else:
            for k, v in entry.items():
                if k == "run_id":
                    continue
                if v is not None and (by_run[run_id].get(k) is None):
                    by_run[run_id][k] = v
    return list(by_run.values())


def classify(candidate: KnowledgeRecord, neighbors: list[KnowledgeRecord]) -> tuple[Decision, KnowledgeRecord | None]:
    """First-match-wins classification over neighbors sorted by text similarity desc."""
    scored = sorted(
        ((_text_sim(candidate.fact, n.fact), n) for n in neighbors),
        key=lambda x: x[0], reverse=True,
    )
    for tsim, n in scored:
        ematch = _entity_match(candidate, n)
        tmatch = candidate.type == n.type
        dconf = candidate.confidence - n.confidence
        if tsim >= 0.92 and ematch and dconf <= 0:
            return "SKIP", n
        if tsim >= 0.92 and ematch and dconf > 0.15 and tmatch:
            return "SUPERSEDE", n
        if tsim >= 0.92 and ematch and dconf > 0:
            return "UPDATE", n
        if 0.65 <= tsim < 0.92 and (ematch or tmatch):
            return "MERGE", n
    return "STORE", None


class Reconciler:
    def __init__(self, knowledge_store, embedder=None, tiebreak=None, max_tiebreak_calls: int = 3):
        self._store = knowledge_store
        self._embedder = embedder
        self._tiebreak = tiebreak  # async (candidate, neighbor) -> "store"|"merge"|None
        self._tiebreak_calls = 0

    async def reconcile_batch(self, candidates: list[KnowledgeRecord]) -> None:
        for c in candidates:
            try:
                await self._reconcile_one(c)
            except Exception:
                pass  # storage failure must never block execution

    async def _reconcile_one(self, candidate: KnowledgeRecord) -> None:
        if not (candidate.fact or "").strip():
            raise ValueError("empty fact")  # swallowed by reconcile_batch
        neighbors = await self._store.find_neighbors(
            candidate.workflow_name, candidate, self._embedder, k=10
        )
        decision, target = classify(candidate, neighbors)
        now = datetime.now(timezone.utc).isoformat()

        if decision == "STORE":
            await self._store.record(candidate, embedder=self._embedder)
        elif decision == "SKIP":
            return
        elif decision == "UPDATE":
            await self._store.update_record(
                target.id,
                confidence=max(target.confidence, candidate.confidence),
                provenance=_union_provenance(target.provenance, candidate.provenance),
                updated_at=now,
            )
        elif decision == "SUPERSEDE":
            new_id = await self._store.record(candidate, embedder=self._embedder)
            await self._store.set_superseded(target.id, new_id)
        elif decision == "MERGE":
            await self._store.update_record(
                target.id,
                confidence=max(target.confidence, candidate.confidence),
                provenance=_union_provenance(target.provenance, candidate.provenance),
                updated_at=now,
            )
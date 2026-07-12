"""Memory-navigation tools (Phase 2 — read tools only).

`register_memory_tools` is a per-run factory called by `Harness.__init__`
(gated by `MemoryConfig.navigation_tools`). Handlers close over per-run store
handles; `ToolRegistry` is per-Harness so there is no cross-run leakage.

Write tools (`memory.write_track`, `memory.write_profile`) and the curator
stage arrive in Phase 3. `read_track` / `read_profile` return null gracefully
until Phase 3 populates L2/L3.
"""
from __future__ import annotations
from typing import Any

from armature.registry.registry import ToolDescriptor, ToolRegistry
from armature.permissions.permissions import PermissionLevel, Reversibility


def reciprocal_rank_fusion(
    bm25_results: list, semantic_results: list, k: int = 60, top_k: int = 5,
) -> list[tuple[int, float]]:
    """Fuse BM25 and semantic result lists by Reciprocal Rank Fusion (k=60)."""
    scores: dict[int, float] = {}
    for rank, rec in enumerate(bm25_results):
        rid = getattr(rec, "id", None)
        if rid is None:
            continue
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
    for rank, rec in enumerate(semantic_results):
        rid = getattr(rec, "id", None)
        if rid is None:
            continue
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:top_k]


def _record_to_dict(rec: Any) -> dict:
    return {
        "id": rec.id,
        "type": rec.type.value,
        "entity": rec.entity,
        "fact": rec.fact,
        "confidence": rec.confidence,
        "provenance": rec.provenance,
        "timestamp": rec.timestamp,
    }


async def _search_records(args, knowledge_store, embedder, workflow_name):
    if knowledge_store is None:
        return {"records": [], "note": "knowledge store unavailable"}
    query = args.get("query", "")
    top_k = int(args.get("top_k", 5))
    mem_type = args.get("type")
    bm25 = await knowledge_store.search(workflow_name, query, top_k=top_k)
    semantic: list = []
    if embedder is not None:
        try:
            semantic = await knowledge_store.semantic_search(workflow_name, query, embedder, top_k=top_k)
        except Exception:
            semantic = []
    fused = reciprocal_rank_fusion(bm25, semantic, k=60, top_k=top_k)
    records: list[dict] = []
    for rid, _score in fused:
        rec = await knowledge_store.get_by_id(rid)
        if rec is None or rec.superseded_by is not None:
            continue
        if mem_type and rec.type.value != mem_type:
            continue
        records.append(_record_to_dict(rec))
    return {"records": records}


async def _get_records(args, knowledge_store, _workflow_name):
    if knowledge_store is None:
        return {"records": []}
    ids = args.get("ids", []) or []
    out: list[dict] = []
    for rid in ids:
        try:
            rec = await knowledge_store.get_by_id(int(rid))
        except (TypeError, ValueError):
            continue
        if rec is None or rec.superseded_by is not None:
            continue
        out.append(_record_to_dict(rec))
    return {"records": out}


def register_memory_tools(
    registry: ToolRegistry,
    *,
    memory_store,
    knowledge_store,
    trace_store,
    embedder,
    workflow_name: str,
    run_id: str,
    track_store=None,
    profile_store=None,
) -> None:
    """Register memory-navigation tools on `registry` for one run.

    `track_store` / `profile_store` are accepted for forward compatibility
    with Phase 3 (write tools + curator); unused in Phase 2.
    """
    registry.register(ToolDescriptor(
        name="memory.search_records",
        description=(
            "Hybrid BM25 + semantic RRF search over L1 knowledge records. "
            "Returns id/type/entity/fact/confidence/provenance/timestamp. "
            "If embeddings are unavailable, ranking is keyword-only (BM25)."
        ),
        permission=PermissionLevel.READ_ONLY,
        reversibility=Reversibility.FULL,
        handler=lambda args: _search_records(args, knowledge_store, embedder, workflow_name),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "type": {
                    "type": "string",
                    "enum": ["fact", "event", "instruction", "preference"],
                    "description": "Optional record-type filter",
                },
                "top_k": {"type": "integer", "description": "Max results (default 5)"},
            },
            "required": ["query"],
        },
    ))
    registry.register(ToolDescriptor(
        name="memory.get_records",
        description="Fetch L1 knowledge records by id. Used to resolve full "
                    "provenance after search_records or a track's evidence_links.",
        permission=PermissionLevel.READ_ONLY,
        reversibility=Reversibility.FULL,
        handler=lambda args: _get_records(args, knowledge_store, workflow_name),
        parameters={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"},
                         "description": "Record ids to fetch"},
            },
            "required": ["ids"],
        },
    ))
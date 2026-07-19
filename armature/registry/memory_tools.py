"""Memory-navigation tools (Phase 2/3 — read + curator write tools).

`register_memory_tools` is a per-run factory called by `Harness.__init__`
(gated by `MemoryConfig.navigation_tools`). Handlers close over per-run store
handles; `ToolRegistry` is per-Harness so there is no cross-run leakage.

Read tools are always registered. Write tools (`memory.write_track`,
`memory.write_profile`) are registered only when `curator_stage` is set and
the corresponding L2/L3 store is provided.
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


async def _read_track(args, track_store, workflow_name):
    if track_store is None:
        return {"tracks": []}
    track_id = args.get("track_id")
    if track_id:
        return {"track": await track_store.get_track(workflow_name, track_id)}
    return {"tracks": await track_store.list_tracks(workflow_name)}


async def _read_profile(args, profile_store, workflow_name):
    if profile_store is None:
        return {"content": None}
    return {"content": await profile_store.get_profile(workflow_name)}


async def _write_track(args, track_store, workflow_name, track_char_budget, track_budget):
    if track_store is None:
        return {"error": "track store unavailable"}
    return await track_store.upsert_track(
        workflow_name=workflow_name,
        track_id=args["track_id"],
        title=args["title"],
        summary=args["summary"],
        narrative=args.get("narrative"),
        evidence_links=args.get("evidence_links") or [],
        char_budget=track_char_budget,
        track_budget=track_budget,
    )


async def _write_profile(args, profile_store, workflow_name, profile_budget):
    if profile_store is None:
        return {"error": "profile store unavailable"}
    return await profile_store.upsert_profile(
        workflow_name=workflow_name,
        content=args["content"],
        char_budget=profile_budget,
    )


async def _search_conversation(args, memory_store, workflow_name):
    if memory_store is None:
        return {"captures": []}
    query = args.get("query", "")
    stage_id = args.get("stage_id")
    top_k = int(args.get("top_k", 10))
    rows = await memory_store.search_conversation(workflow_name, query, stage_id=stage_id, top_k=top_k)
    return {"captures": rows}


async def _get_run_trace(args, trace_store, run_id):
    if trace_store is None:
        return {"traces": []}
    rid = args.get("run_id") or run_id
    stage_id = args.get("stage_id")
    traces = await trace_store.query_by_run(rid)
    out: list[dict] = []
    for t in traces:
        if stage_id and t.stage_id != stage_id:
            continue
        out.append({
            "stage_id": t.stage_id,
            "role_type": t.role_type,
            "model": t.model,
            "success": t.success,
            "outputs": t.outputs,
            "timestamp": t.timestamp,
        })
    return {"traces": out}


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
    curator_stage: str | None = None,
    track_budget: int = 20,
    track_char_budget: int = 2000,
    profile_budget: int = 2000,
) -> None:
    """Register memory-navigation tools on `registry` for one run.

    Read tools are always registered. `memory.write_track` / `memory.write_profile`
    are registered only when `curator_stage` is set (and the corresponding store
    is provided) — a spec without a curator can read tracks/profile but not write.
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
            "query": {"type": "string", "description": "Search query"},
            "type": {
                "type": "string",
                "enum": ["fact", "event", "instruction", "preference"],
                "description": "Optional record-type filter",
                "optional": True,
            },
            "top_k": {"type": "integer", "description": "Max results (default 5)", "optional": True},
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
            "ids": {"type": "array", "items": {"type": "integer"},
                     "description": "Record ids to fetch"},
        },
    ))
    registry.register(ToolDescriptor(
        name="memory.read_track",
        description=(
            "Read an L2 topic track. With track_id set, returns the full "
            "track ({\"track\": {...}}). Without track_id, lists all tracks "
            "({\"tracks\": [...]}): titles+summaries+evidence_links (≤20). "
            "Returns {\"tracks\": []} until the curator writes tracks."
        ),
        permission=PermissionLevel.READ_ONLY,
        reversibility=Reversibility.FULL,
        handler=lambda args: _read_track(args, track_store, workflow_name),
        parameters={
            "track_id": {"type": "string", "description": "Optional specific track slug; omit to list all tracks", "optional": True},
        },
    ))
    registry.register(ToolDescriptor(
        name="memory.read_profile",
        description="Read the L3 team profile (markdown, ≤2000 chars). "
                    "Returns {\"content\": null} until the curator writes a profile (Phase 3).",
        permission=PermissionLevel.READ_ONLY,
        reversibility=Reversibility.FULL,
        handler=lambda args: _read_profile(args, profile_store, workflow_name),
        parameters={},
    ))
    registry.register(ToolDescriptor(
        name="memory.search_conversation",
        description="Keyword scan over L0 raw stage captures (this workflow's "
                    "captured outputs). Optional stage_id filter.",
        permission=PermissionLevel.READ_ONLY,
        reversibility=Reversibility.FULL,
        handler=lambda args: _search_conversation(args, memory_store, workflow_name),
        parameters={
            "query": {"type": "string", "description": "Keyword to search for"},
            "stage_id": {"type": "string", "description": "Optional stage filter", "optional": True},
            "top_k": {"type": "integer", "description": "Max results (default 10)", "optional": True},
        },
    ))
    registry.register(ToolDescriptor(
        name="memory.get_run_trace",
        description="Pull a prior run's stage outputs (defaults to current run). "
                    "The Armature analog of NapMem's raw-conversation access.",
        permission=PermissionLevel.READ_ONLY,
        reversibility=Reversibility.FULL,
        handler=lambda args: _get_run_trace(args, trace_store, run_id),
        parameters={
            "run_id": {"type": "string", "description": "Run id (defaults to current run)", "optional": True},
            "stage_id": {"type": "string", "description": "Optional stage filter", "optional": True},
        },
    ))

    # ── Phase 3: write tools (curator only) ──
    if curator_stage is not None:
        if track_store is not None:
            registry.register(ToolDescriptor(
                name="memory.write_track",
                description=(
                    "Create or update an L2 topic track (a ≤ char_budget markdown "
                    "summary citing L1 record ids). Upserts on track_id. Evidence "
                    "links to superseded or nonexistent records are dropped and "
                    "reported back. The 21st distinct track in a workflow is rejected."
                ),
                permission=PermissionLevel.WORKSPACE,
                reversibility=Reversibility.PARTIAL,
                handler=lambda args: _write_track(
                    args, track_store, workflow_name, track_char_budget, track_budget),
                parameters={
                    "track_id": {"type": "string", "description": "Stable slug; upserts existing track"},
                    "title": {"type": "string", "description": "Human-readable track title"},
                    "summary": {"type": "string", "description": "Track body (≤ track_char_budget chars)"},
                    "narrative": {"type": "string", "description": "Optional longer markdown", "optional": True},
                    "evidence_links": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "knowledge record ids cited", "optional": True,
                    },
                },
            ))
        if profile_store is not None:
            registry.register(ToolDescriptor(
                name="memory.write_profile",
                description=(
                    "Create or replace the L3 team profile (a ≤ profile_budget markdown "
                    "capturing stable team attributes: domain, constraints, recurring "
                    "failure modes). Upserts on workflow_name."
                ),
                permission=PermissionLevel.WORKSPACE,
                reversibility=Reversibility.PARTIAL,
                handler=lambda args: _write_profile(
                    args, profile_store, workflow_name, profile_budget),
                parameters={
                    "content": {"type": "string", "description": "Profile markdown (≤ profile_budget chars)"},
                },
            ))
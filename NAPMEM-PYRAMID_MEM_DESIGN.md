# Memory Pyramid for Armature — Design

**Status:** Design only (no implementation). Lives on private local-only branch `worktree-memory-pyramid-design`; this file is under `docs/superpowers/` which is gitignored — it never reaches public `origin`, never PR'd to `main`.
**Inspiration:** NapMem — "From Passive Retrieval to Active Memory Navigation" (arXiv:2607.05794, Xu et al., July 2026).
**Date:** 2026-07-11

---

## Context

Armature's agentic teams currently get memory by **passive injection**: at run start the engine loads every captured stage output (`MemoryStore`) and every LLM-extracted fact (`KnowledgeStore`) and dumps them into each stage's context as `_memory` / `_knowledge`. The agent has zero control over what memory it consumes. Bryan's own soak data shows this *hurts*: "memory broadens judge coverage 0.78→0.90 but an HQS latency term cancels the gain" and "memory carry-forward degraded performance by 11%." Passive injection buys coverage with context/latency cost that outpaces quality.

NapMem's thesis: treat long-term memory as a **structured action space the agent navigates**, organized as a 4-layer pyramid linked by provenance, exposed via memory tools — instead of passively-injected context. Their ablation shows the structural pyramid + *untrained* tool-based navigation already beats passive retrieval (their RL policy adds more, but RL is not portable to a harness).

**Intended outcome:** Replace the passive dump with active navigation over a typed, provenance-linked, reconciled memory pyramid, while leaving existing specs' behavior byte-for-byte unchanged (additive opt-in). This targets the measured latency/quality tradeoff directly: stages pull only what they need.

### Locked decisions (user-confirmed)

1. **Scope — full pyramid:** typed records + provenance + reconciliation (L1), topic tracks (L2), team profile (L3), and memory-navigation tools.
2. **Location — private layer:** design + implementation live on a private local-only armature branch; never pushed to public `origin`, never PR'd to `main`.
3. **Inject policy — additive/opt-in:** existing `_memory`/`_knowledge` injection is untouched. Stages opt into navigation by declaring `memory.*` tools in `role.tools`.
4. **Migration — auto-migrate on first run:** add columns, backfill `type='fact'` and provenance, rebuild FTS. No data loss, idempotent.

### What already exists in Armature (verified)

- `KnowledgeStore` already has an `embedding` BLOB column, FTS5/BM25 `search()`, `semantic_search()` (cosine), and a `LocalEmbedder` (sentence-transformers, all-MiniLM-L6-v2, 384-dim). **Vector infra exists — no new vector store needed.**
- `nodes/llm.py` already does native LLM tool-calling: `role.tools: list[str]` filters which tools a stage sees (`llm.py:398-401`), `tool_choice="auto"` when the tier supports it (`llm.py:522-542`), and a ReAct loop dispatches tool calls (`llm.py:551-578`). **Navigation tools need zero engine tool-calling changes** — just register tools, stages opt in.
- `ToolRegistry` is per-`Harness` (instantiated in `engine.py:147`, not module-global) — so closure-capturing per-run store handles into tool handlers is safe.
- `KnowledgeExtractor` already runs post-run (`engine.py:1068-1078`); the `self_analyst` / `post_run: true` stage pattern already exists (`engine.py:1088-1099`).

### North star

Passive dump → active navigation. The agent treats memory as an action space over a 4-layer pyramid: **L0** raw stage captures (`MemoryStore`), **L1** typed records (`knowledge` table, extended), **L2** topic tracks (≤20 markdown), **L3** team profile (single markdown, ≤2000 chars). Provenance links connect layers. Reconciliation (store/skip/update/merge) keeps L1 clean. The RL policy is explicitly out of scope.

---

## 1. Data model / schemas

All schemas extend the existing `KnowledgeStore` SQLite DB. `MemoryStore` (L0) is structurally unchanged. Existing embedding-BLOB + FTS5 patterns are reused.

### 1.1 Schema versioning

`PRAGMA user_version` (integer) on the knowledge DB. Existing DBs = `0`; new schema = `1`. `KnowledgeStore.init()` reads it; if `< 1`, run migration then `PRAGMA user_version = 1`. This is the idempotency gate; `ALTER TABLE ADD COLUMN` wrapped in try/except is a secondary guard (matches the existing pattern at `knowledge.py:54-58`).

### 1.2 Extended `knowledge` table (L1)

```sql
CREATE TABLE IF NOT EXISTS knowledge (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name      TEXT NOT NULL,
    entity             TEXT NOT NULL,
    fact               TEXT NOT NULL,
    confidence         REAL NOT NULL,
    source_run_id      TEXT NOT NULL,
    source_stage_id    TEXT,                 -- NEW provenance
    source_capture_key TEXT,                 -- NEW provenance
    source_msg_id      TEXT,                  -- NEW, nullable (see §10.1)
    type               TEXT NOT NULL DEFAULT 'fact',  -- NEW: fact|event|instruction|preference
    provenance         TEXT,                  -- NEW: JSON array of {run_id, stage_id, capture_key, ts}
    timestamp          TEXT NOT NULL,
    updated_at         TEXT,                   -- NEW: set on update/merge
    superseded_by      INTEGER,               -- NEW: FK→knowledge.id; NULL=live
    embedding          BLOB
);
CREATE INDEX IF NOT EXISTS idx_knowledge_wf_type    ON knowledge(workflow_name, type)    WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_knowledge_wf_entity  ON knowledge(workflow_name, entity) WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_knowledge_superseded ON knowledge(superseded_by)         WHERE superseded_by IS NOT NULL;
```

`knowledge_fts` (FTS5, content=knowledge) unchanged in shape; migration **drops and recreates** it so external-content pointers stay valid after `ALTER TABLE`, then backfills via `INSERT INTO knowledge_fts(rowid, fact) SELECT id, fact FROM knowledge`.

**Provenance storage:** record→source stored as a JSON array column `provenance` on the record (denormalized, queryable via JSON1). Track→record links stored as JSON array `evidence_links` on `topic_tracks` (§1.3). Keeps the write path single-row; "which tracks cite record X" requires a scan of `topic_tracks.evidence_links` — acceptable because tracks are ≤20/workflow.

### 1.3 `topic_tracks` table (L2)

```sql
CREATE TABLE IF NOT EXISTS topic_tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name   TEXT NOT NULL,
    track_id        TEXT NOT NULL,            -- slug, e.g. "auth-patterns"
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,            -- ≤ char_budget (default 2000); the "track body"
    narrative       TEXT,                      -- optional longer markdown (≤8000)
    evidence_links  TEXT NOT NULL DEFAULT '[]', -- JSON array of knowledge.id
    char_budget     INTEGER NOT NULL DEFAULT 2000,
    updated_at      TEXT NOT NULL,
    UNIQUE(workflow_name, track_id)
);
CREATE INDEX IF NOT EXISTS idx_tracks_wf ON topic_tracks(workflow_name);
```

Budget enforced in code: `len(summary) <= char_budget`. Track count enforced in code: `COUNT(*) WHERE workflow_name=? <= track_budget` (default 20) before a *new* insert; updates to existing `track_id` are exempt.

### 1.4 `team_profile` table (L3)

Single logical row per workflow; `UNIQUE(workflow_name)` so the engine upserts.

```sql
CREATE TABLE IF NOT EXISTS team_profile (
    workflow_name TEXT PRIMARY KEY,
    content       TEXT NOT NULL,              -- markdown, ≤2000 chars (NapMem budget)
    char_budget   INTEGER NOT NULL DEFAULT 2000,
    updated_at    TEXT NOT NULL
);
```

### 1.5 Pydantic models (extend `armature/state/knowledge.py`)

Extend `KnowledgeRecord` **in place** (do not create a parallel model — `KnowledgeExtractor.extract()` and `KnowledgeStore.record()`/`_row_to_record()` already construct it; a parallel model would bifurcate read/write paths). Add new fields: `id: int | None`, `type: MemoryType = FACT`, `source_stage_id`, `source_capture_key`, `source_msg_id`, `provenance: list[dict]`, `updated_at`, `superseded_by`. Add `MemoryType(str, Enum)` (fact/event/instruction/preference). Add new `TopicTrack` and `TeamProfile` models.

> **Integration risk found:** `_row_to_record` (`knowledge.py:167-176`) currently discards the row `id`. RRF, `get_records`, and `evidence_links` all need it — **must add `id` to `KnowledgeRecord` and populate it in `_row_to_record`** (low-risk additive change).

---

## 2. Reconciliation algorithm

New module `armature/state/reconcile.py`, invoked from the (extended) `KnowledgeExtractor.extract()` path. Default: cheap heuristics; optional LLM tie-breaker gated by `MemoryConfig.reconcile_llm` (default off — extra LLM calls cost latency, per soak data).

### 2.1 Pipeline (per candidate record from extraction)

```
for candidate in extracted_records:
    neighbors = await knowledge_store.find_neighbors(workflow, candidate, k=10)  # §2.2
    decision = classify(candidate, neighbors)                                    # §2.3
    match decision:
      STORE   → insert
      SKIP    → drop (strict duplicate, ≤ confidence)
      UPDATE  → in place: set updated_at, union provenance; bump confidence if higher
      SUPERSEDE → insert candidate fresh; set old.superseded_by = candidate.id
      MERGE   → in place: union provenance + timestamps; confidence = max
```

### 2.2 `find_neighbors` (reuses existing infra)

1. `KnowledgeStore.semantic_search(workflow, candidate.fact, embedder, top_k=10)` — cosine over embedding blobs; only if embedder available, else skipped.
2. `KnowledgeStore.search(workflow, candidate.fact, top_k=10)` — BM25.

Union + dedupe by `id`; restrict to `superseded_by IS NULL` (never reconcile against superseded rows).

### 2.3 Heuristic classification (`classify`)

Per neighbor `n`, compute: `text_sim` (cosine if both embedded, else `difflib.SequenceMatcher` ratio), `entity_match` (exact or substring), `type_match`, `conf_delta = candidate.confidence - n.confidence`. First match wins:

| Condition | Decision |
|---|---|
| `text_sim >= 0.92` AND `entity_match` AND `conf_delta <= 0` | **SKIP** |
| `text_sim >= 0.92` AND `entity_match` AND `conf_delta > 0.15` AND `type_match` | **SUPERSEDE** |
| `text_sim >= 0.92` AND `entity_match` AND `conf_delta > 0` | **UPDATE** in place |
| `0.65 <= text_sim < 0.92` AND (`entity_match` OR `type_match`) | **MERGE** |
| `text_sim < 0.65` OR no neighbors | **STORE** |

All scoped to `workflow_name` AND `superseded_by IS NULL`.

### 2.4 Optional LLM tie-breaker

Only when `reconcile_llm=True` AND `0.65 <= text_sim < 0.92` AND `not entity_match` AND `type_match` (the ambiguous band). Single short prompt to the small tier; expects `{"action":"store"|"merge"}`. On any failure → fall back to **STORE** (never block on the LLM). Cap ≤3 tie-breaker calls per `extract()` run to bound latency.

### 2.5 Supersession semantics

`superseded_by` is a **soft-delete**: the old row stays (preserving provenance history) but is excluded from `load()`, `search()`, `semantic_search()`, `find_neighbors()` via `WHERE superseded_by IS NULL` added to all four. Rollback = null the column. Matches NapMem's "roll back invalid writes" without deleting data.

### 2.6 Where it runs

`KnowledgeExtractor.extract()` calls `Reconciler(...).reconcile_batch(candidates)` **instead of** the current append-only loop at `extractor.py:79-84`. The "storage failure must never block execution" invariant (`extractor.py:84`) is preserved — wrap reconcile in try/except, log, continue. Thread `source_stage_id` / `source_capture_key` from the memory entries (which already carry them at `memory.py:51-55`) into the records.

---

## 3. Memory-navigation tools

### 3.1 The ownership problem (solved first)

`ToolRegistry` is per-`Harness` (constructed `engine.py:147`), not module-global; `MemoryStore`/`KnowledgeStore` are also per-`Harness` (`engine.py:213-220`). Within a single run they're singletons; the CLI shells out fresh per run, so one process = one Harness in practice.

**Solution — per-run tool factory bound at init:** new `armature/registry/memory_tools.py` exposes `register_memory_tools(registry, memory_store, knowledge_store, track_store, profile_store, embedder) -> None`. The engine calls it *after* constructing the stores (gated by `mem_cfg.navigation_tools`). Each handler is a closure over the per-run store handles. No cross-run leakage (registry isn't module-global). Stages opt in via `role.tools`. Trade-off: tools registered even for stages that won't use them — but `llm.py:398-401` filters by `role.tools`, so unused descriptors are inert; cost is a few dict entries.

This avoids a registry-scoped context object (more invasive) and late-bound store lookups by name (race-prone).

### 3.2 Module location

`armature/registry/memory_tools.py` (sibling of `builtins.py`). **Not** loaded via the spec `tools:` mechanism (those are user-declared module paths); loaded explicitly by the engine so it can inject store handles — mirroring how `register_builtins` is called at `engine.py:148`.

### 3.3 Tool descriptors (5 read + 2 write)

All read tools are `PermissionLevel.READ_ONLY`. Write tools (`§4.2`) are `WORKSPACE` / `Reversibility.PARTIAL`.

- **`memory.search_records`** — hybrid RRF search over L1. Params: `query: string`, `type?` (enum), `top_k?` (default 5). Returns records with `id,type,entity,fact,confidence,provenance,timestamp`. RRF over `search()` (BM25) + `semantic_search()` (cosine) — see §8.
- **`memory.get_records`** — fetch by id. Params: `ids: int[]`. `SELECT ... WHERE id IN (?) AND superseded_by IS NULL`. Used after `search_records` for full provenance, or to resolve a track's `evidence_links`.
- **`memory.read_track`** — read an L2 track. Params: `track_id?`, `list?` (default false → titles+ids only). Default returns all tracks' titles+summaries (≤20, small).
- **`memory.read_profile`** — read L3. Params: `{}`. Returns `{"content": "..."}` or `{"content": null}`.
- **`memory.search_conversation`** — keyword scan over L0 raw captures. Params: `query`, `stage_id?`, `top_k?`. Searches `memories.value` (LIKE), optionally filtered by `stage_id`.
- **`memory.get_run_trace`** — pull a prior run's stage outputs. Params: `run_id?` (defaults to current), `stage_id?`. Delegates to `TraceStore.query_by_run(run_id)` (already used at `engine.py:1083`). This is the Armature analog of NapMem's "raw conversations."

### 3.4 Navigation step budget

NapMem caps ~4 steps. Armature's ReAct loop already has `self._max_tool_iterations` (`llm.py:551`). No new mechanism — the existing cap governs. Recommend specs set `contracts.max_iterations` conservatively for navigation-heavy stages; the engine imposes nothing extra (would over-constrain non-memory tool use).

---

## 4. Topic-track + profile lifecycle

### 4.1 Writer — a dedicated `post_run: true` curator stage

Reuse the existing post-run mechanism (`models.py:179`, executed `engine.py:1088-1099`). The engine never auto-creates stages. `MemoryConfig.curator_stage: str | None = None`: if set, the engine validates it exists, is `post_run: true`, and declares the write tools (§5.3). If `None`, tracks/profile are never written — navigation still works over L1. The curator reads records via `memory.search_records` / `get_records`, groups by type+entity, drafts ≤20 tracks and the profile, and writes them.

### 4.2 Write tools

- **`memory.write_track`** — params: `track_id` (slug; upserts existing), `title`, `summary` (≤ char_budget), `narrative?` (≤8000), `evidence_links: int[]` (knowledge.id cited). Handler validates: `len(summary) <= char_budget`; `COUNT(* WHERE workflow=? AND track_id != ?) < track_budget` (updates to existing `track_id` allowed); every `evidence_links` id must exist AND `superseded_by IS NULL` — invalid ids **dropped** (write succeeds) and returned in the response so the agent gets feedback. Upsert on `(workflow, track_id)`.
- **`memory.write_profile`** — params: `content` (≤ profile_budget). Validates length; upsert on `workflow_name`.

### 4.3 Update / merge / rollback

- **Update:** `write_track` with existing `track_id` overwrites `summary/narrative/evidence_links`, bumps `updated_at`. Old track content is not versioned (tracks are summaries; L1 is source of truth). A "worse" track is cheaply fixed by re-running the curator — L1 records are untouched. This is the cheap rollback.
- **Invalid-write rollback:** hallucinated `evidence_links` are dropped (not the whole write) — a hallucinated citation cannot poison the track, and the agent gets retry feedback. This implements NapMem's "roll back invalid writes" as input validation rather than post-hoc undo.

### 4.4 Refresh triggers (hints, not gates)

At end of `Harness.run()`, after reconcile, before the post_run loop (`engine.py:1088`):
- **Track refresh** if `new_live_records >= track_refresh_threshold` (default 10) since last track `updated_at`.
- **Profile refresh** if `track_writes >= profile_refresh_threshold` (default 3) OR `new_live_records >= 50`.

The engine injects `_memory_index_refresh_hint: True` so the curator prompt knows whether there's enough delta to bother — keeps it from rewriting identical tracks every run (latency). The curator stage runs whenever it's declared `post_run: true`; the hint only tells it whether to run.

### 4.5 Budget enforcement summary

| Constraint | Enforced in |
|---|---|
| ≤20 tracks | `write_track` `COUNT(*)` check |
| summary ≤2000 chars | `write_track` `len()` check |
| profile ≤2000 chars | `write_profile` `len()` check |
| evidence_links validity | `write_track` `WHERE id IN (...) AND superseded_by IS NULL` |

---

## 5. Spec / validator changes

### 5.1 `MemoryConfig` extensions (additive — all new fields have defaults; existing behavior unchanged)

```python
class MemoryConfig(BaseModel):
    enabled: bool = True
    fresh: bool = False
    capture: list[MemoryCapture] = Field(default_factory=list)
    inject_as: str = "_memory"
    db: str | None = None
    extract_knowledge: bool = False
    inject_knowledge_as: str = "_knowledge"
    # ── NEW (memory pyramid) ──
    navigation_tools: bool = False       # register memory.* tools for this run
    curator_stage: str | None = None     # post_run stage id that writes tracks/profile
    track_budget: int = 20
    profile_budget: int = 2000
    track_char_budget: int = 2000
    reconcile: bool = True               # dedup/merge on extraction (Phase 1)
    reconcile_llm: bool = False          # optional LLM tie-breaker
    track_refresh_threshold: int = 10
    profile_refresh_threshold: int = 3
```

`navigation_tools=False` → no tools registered. `reconcile=True` is the only behavior change but only fires when `extract_knowledge=True` (off by default) — so existing specs are byte-for-byte unchanged.

### 5.2 New harness-injected context key `_memory_index`

A **lightweight** (~500 chars) navigation table-of-contents injected when `navigation_tools=True`: track titles+ids (≤20 lines), profile char count + first 200 chars, live-record count by type. This is the *only* passive injection navigation-enabled stages receive — it replaces the full `_knowledge` dump and tells the agent which `memory.read_*` tool to call.

**Per-stage suppression:** when `navigation_tools=True` and a stage declares any `memory.*` tool in `role.tools`, the existing full `_knowledge` injection (`engine.py:996-1004`, `top_k=10`) is **suppressed for that stage only** — non-navigation stages keep it (zero behavior change). Implementation: localized ~3-line filter in `LLMNode.execute()` (`llm.py:394`) after resolving `tools_declared`, strip the `inject_knowledge_as` key from that stage's visible context.

Add `_memory_index` to `harness_injected_keys` in `validator.py:225-230`.

> **Integration risk:** a stage declaring a memory tool but *also* expecting `_knowledge` will lose it. This is the *intent* (don't double-inject); document it. The stage can still `memory.search_records` for the same data.

### 5.3 New validator codes (appended in the existing `POST_RUN_TRANSCRIPT_OVERFLOW_RISK` pattern, `validator.py:243-254`)

| Code | Severity | Condition |
|---|---|---|
| `MEMORY_CURATOR_STAGE_MISSING` | error | `curator_stage` set but no such stage |
| `MEMORY_CURATOR_NOT_POST_RUN` | error | curator stage not `post_run: true` |
| `MEMORY_CURATOR_MISSING_TOOLS` | warning | curator lacks `memory.write_track`/`memory.write_profile` in `role.tools` |
| `MEMORY_NAV_TOOLS_REQUIRES_EXTRACT` | warning | `navigation_tools=True` but `extract_knowledge=False` (L1 empty) |
| `MEMORY_BUDGET_OUT_OF_RANGE` | error | `track_budget > 20` or `profile_budget > 5000` |
| `MEMORY_RECONCILE_LLM_WITHOUT_EXTRACT` | warning | `reconcile_llm=True` but `extract_knowledge=False` |

---

## 6. Engine wiring

Minimal, localized changes to `runtime/engine.py`. The "memory capture must never block execution" invariant (`extractor.py:84`, `engine.py:1077`) is preserved everywhere.

- **`__init__` (around `engine.py:206-233`):** after the existing `extract_knowledge` block, construct `TrackStore` / `ProfileStore` (sharing the knowledge DB file — one aiosqlite connection per call, so co-location is fine; keeps footprint at two DBs: `{name}.db` + `{name}_knowledge.db`). If `navigation_tools`: instantiate `LocalEmbedder` if available, call `register_memory_tools(...)`. Else set the stores to `None`.
- **`run()` init (`engine.py:980-1004`):** `await self._knowledge_store.init()` already runs — extend `init()` with migration (§7). If `navigation_tools`: build `_memory_index` via new `KnowledgeStore.index_summary(workflow)` and inject as `context["_memory_index"]`. Keep existing `_knowledge` injection for non-navigation backward compat.
- **KnowledgeExtractor path (`engine.py:1068-1078`):** no engine change — the change is inside `KnowledgeExtractor` (now uses `Reconciler`, §2.6). Thread `source_stage_id`/`source_capture_key` from memory entries into records.
- **Curator (`engine.py:1088-1099`):** the existing post_run loop already runs all `post_run: true` stages sequentially — **no engine change** for the curator to run. Only addition: compute `_memory_index_refresh_hint` before the loop (`new_count = await knowledge_store.count_since(workflow, last_track_updated_at)`).
- **Dynamic-registration vs `role.tools` filter:** `register_memory_tools` runs in `__init__` before any stage executes, so descriptors are present when `LLMNode.execute()` runs — no race. The filter is by stable string name (`"memory.search_records"`); no per-run name mangling. This is the key reason the per-run-factory approach works.

---

## 7. Migration (auto, idempotent, in `KnowledgeStore.init()`)

```python
async def init(self) -> None:
    self._path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(self._path) as db:
        await db.execute("PRAGMA journal_mode=WAL")   # §10.2 concurrency
        await db.execute(_CREATE_SQL_V2); await db.execute(_CREATE_FTS_SQL)
        cur = await db.execute("PRAGMA user_version"); version = (await cur.fetchone())[0]
        if version < 1:
            # add columns idempotently (try/except per existing pattern)
            for col, ddl in [("type","TEXT NOT NULL DEFAULT 'fact'"),
                             ("source_stage_id","TEXT"),("source_capture_key","TEXT"),
                             ("source_msg_id","TEXT"),("provenance","TEXT"),
                             ("updated_at","TEXT"),("superseded_by","INTEGER")]:
                try: await db.execute(f"ALTER TABLE knowledge ADD COLUMN {col} {ddl}")
                except Exception: pass
            await db.execute("UPDATE knowledge SET type='fact' WHERE type IS NULL OR type=''")
            await db.execute("UPDATE knowledge SET provenance = json_array(json_object('run_id', source_run_id)) WHERE provenance IS NULL")
            # rebuild FTS (external-content pointers invalidated by ALTER)
            await db.execute("DROP TABLE IF EXISTS knowledge_fts"); await db.execute(_CREATE_FTS_SQL)
            await db.execute("INSERT INTO knowledge_fts(rowid, fact) SELECT id, fact FROM knowledge")
            # L2/L3 tables + indexes
            await db.execute(_CREATE_TRACKS_SQL); await db.execute(_CREATE_PROFILE_SQL)
            for idx in _INDEXES: await db.execute(idx)
            await db.execute("PRAGMA user_version = 1")
        await db.commit()
```

**Idempotency:** `user_version` is the primary gate (runs ≤ once); `ALTER ADD COLUMN` try/except is the secondary guard (survives a crash mid-migration that didn't bump `user_version`); `CREATE … IF NOT EXISTS` makes L2/L3 creation safe to repeat. FTS rebuild is the only destructive step, bracketed by `user_version`. **"Already ran"** = `user_version >= 1` (cheap, persistent, single-row read). `MemoryStore` migration is unchanged (no new L0 columns in Phases 1–3).

---

## 8. Hybrid retrieval (RRF)

```python
def reciprocal_rank_fusion(bm25_results, semantic_results, k=60, top_k=5):
    scores: dict[int, float] = {}
    for rank, rec in enumerate(bm25_results):    scores[rec.id] = scores.get(rec.id, 0) + 1/(k+rank+1)
    for rank, rec in enumerate(semantic_results): scores[rec.id] = scores.get(rec.id, 0) + 1/(k+rank+1)
    return sorted(scores.items(), key=lambda kv: (-kv[1],))[:top_k]
```

`k=60` standard RRF constant. BM25 via existing `search()` (`knowledge.py:101-126`); semantic via `semantic_search()` (`knowledge.py:128-165`). Both return `KnowledgeRecord` — need their `id` (§1.5 `id` addition). Add `superseded_by IS NULL` to both queries (additive). Optional `type` filter pushed into both source queries.

**No-embedder fallback:** if `LocalEmbedder.is_available()` is False, embeddings are NULL (`record()` swallows embedding failure at `knowledge.py:72-73`), the handler skips the semantic arm — RRF degenerates to BM25-only. Document in the tool description so the agent knows ranking is keyword-only.

### 8.1 The O(N) semantic scan — risk + recommendation

**Current:** `semantic_search` (`knowledge.py:147-165`) does `SELECT * WHERE workflow_name=? AND embedding IS NOT NULL`, loads *all* rows into Python, unpacks each BLOB, computes cosine in a loop — O(N) per call. At soak scale (~hundreds of records) this is microseconds; at thousands, milliseconds; at 10k+, a real latency tax per `search_records` call, and navigation means several calls per run.

**Recommendation:** keep for Phases 1–2 but **cap the scan**:

```sql
SELECT * FROM knowledge
WHERE workflow_name=? AND embedding IS NOT NULL AND superseded_by IS NULL
ORDER BY confidence DESC, timestamp DESC
LIMIT 500
```

Bounds Python-side work to 500 cosines. Combined with the `superseded_by IS NULL` filter, keeps the live set small.

**Phase 3+ option (not now):** `sqlite-vec` loadable extension gives KNN over BLOB vectors in SQL. Trade-off: native extension (build/dependency complexity, platform binaries). **Defer until a workflow exceeds ~1000 live records — measure first.** The char-budgeted L2/L3 pyramid naturally caps L1 growth (the curator compresses records into tracks), so L1 staying huge indicates a missing-curator problem, not a retrieval-engine problem. **Do not** introduce a separate vector index service (Chroma/Qdrant) — violates Armature's local-first, single-process, aiosqlite-only architecture.

---

## 9. Testing strategy

New tests under `tests/` (pytest-asyncio, matching `tests/state/test_knowledge.py`): `tests/state/test_reconcile.py`, `tests/state/test_tracks_profile.py`, `tests/state/test_migration.py`, `tests/registry/test_memory_tools.py`, `tests/integration/test_memory_navigation.py`.

- **Reconcile state machine** — parametrize `classify` over the §2.3 decision table (STORE/SKIP/UPDATE/SUPERSEDE/MERGE); assert `superseded_by` set correctly; assert old row still loadable via direct SELECT (not via `load()`); assert reconcile never raises on a bad candidate (swallowed → STORE).
- **RRF ranking** — plant 5 records (3 keyword-relevant, 2 semantic, 1 overlap); assert overlap ranks #1, union has no dupes; assert BM25-only path when `embedder=None`; assert `superseded_by IS NULL` excludes superseded rows.
- **Budget enforcement** — `write_track` rejects `summary > 2000`; 21st track rejected, update to existing `track_id` allowed; `write_profile` rejects `content > 2000`; invalid `evidence_links` id → write succeeds, id dropped + returned; superseded id treated as invalid.
- **Migration idempotency** — create legacy DB (original columns, 3 rows, `user_version=0`) → `init()` → assert new columns exist (`PRAGMA table_info`), `type='fact'` backfilled, `provenance` non-null, `superseded_by` NULL, FTS searchable, `user_version==1`; run `init()` again → no error, `user_version` still 1, FTS row count == knowledge row count (no dupes); insert post-migration → appears in `search()`.
- **Tool dispatch via mock registry** — build `ToolRegistry`, register with mock stores (AsyncMock), dispatch each tool, assert handler args + return shape; **closure isolation**: two registries with two store mocks → dispatch returns different data (no cross-contamination).
- **End-to-end** — minimal spec with `extract_knowledge: true`, `navigation_tools: true`, `curator_stage: curator`, a worker declaring `memory.search_records`/`read_track`, a curator declaring `memory.write_track`/`write_profile`/`search_records`/`get_records`. Mock the LLM (patch `litellm.acompletion`) so worker extracts one fact, extractor reconciles, curator writes one track + profile. Assert: `knowledge` has 1 live row (`type='fact'`, `source_stage_id='worker'`); `topic_tracks` has 1 row whose `evidence_links` contains the knowledge id; `team_profile` content ≤2000; a second run where the worker declaring `memory.search_records` *actually calls* the tool (assert via the mocked LLM seeing the tool-call in `messages`) and the worker's context does **not** contain the full `_knowledge` dump (suppressed per §5.2).
- **Regression** — existing `tests/state/test_knowledge.py` passes unchanged (additive `id`/`type` fields with defaults; `record()`/`search()` signatures backward-compatible); existing `tests/state/test_memory.py` untouched (L0 unchanged); `tests/test_tool_modules.py` untouched (memory tools registered engine-side, not via `tools:` spec).

---

## 10. Risks & non-goals

### 10.1 Non-goals

- **RL navigation policy.** NapMem's headline win is RL-trained tool selection. **Not portable** to a harness — requires a training loop, reward signal, offline trajectory dataset Armature doesn't have. The ablation shows structure + untrained navigation already beats passive retrieval; that's the accessible win. RL is out.
- **Per-user memory.** NapMem models a single user. Armature is **per-workflow/team**. Mapping: NapMem "user profile" → Armature "team profile" (stable attributes of *this workflow* — its domain, constraints, recurring failure modes). `workflow_name` is the partition key throughout. Multi-tenant user-level memory is a future design.
- **Decay / forgetting.** None (NapMem has none; Armature keeps none). Supersession handles correction, not aging. The 30-day staleness flag on `MemoryStore` stays as-is.
- **Per-message provenance.** NapMem links records to message IDs. Armature traces don't have stable message IDs — the ReAct loop (`llm.py:551-578`) mutates a `messages` list in place with no persistent IDs. **The provenance unit in Armature is `(run_id, stage_id, capture_key)`** — the `MemoryStore` row's natural key — not a message id. `source_msg_id` is kept nullable for future use but not populated by default.

### 10.2 Integration risks found in the current code

1. **Global-registry-vs-per-run-stores** — solved by per-run factory (§3.1); `ToolRegistry` is per-`Harness`, not module-level, so closure capture is safe.
2. **`role.tools` filtering** (`llm.py:398-401`) — filters by string name; dynamically-registered per-run tools appear in `descriptors()` because registration happens in `__init__` before any stage runs. No race, no special-casing.
3. **O(N) semantic scan** — flagged §8.1; mitigated by row cap + `superseded_by IS NULL`; defer `sqlite-vec` until measured.
4. **`KnowledgeRecord` has no `id`** — `_row_to_record` discards it; RRF/`get_records`/`evidence_links` need it; must add `id` (low-risk additive).
5. **FTS5 external-content invalidation on ALTER** — handled by DROP+recreate+backfill in migration.
6. **Two stores, one DB file** — `TrackStore`/`ProfileStore` share the knowledge DB; each aiosqlite call opens its own connection, so no connection-sharing contention; concurrent curator writes vs navigation reads could hit SQLite write-lock contention → **add `PRAGMA journal_mode=WAL`** in `init()` (single line, big concurrency win, low risk).
7. **Passive-injection suppression is per-stage** — a stage declaring a memory tool but expecting `_knowledge` will lose it; this is the intent (don't double-inject); document it; the stage can still `memory.search_records`.
8. **Reconcile LLM tie-breaker latency** — default off; when on, cap ≤3 tie-breaker calls per `extract()`.

### 10.3 Context-cost tradeoff

Passive `_knowledge` injects up to 10 facts (`engine.py:998`, `top_k=10`) as JSON every run. Navigation replaces this with a ~500-char `_memory_index` + on-demand tool calls. Net: ~10 facts × ~100 chars ≈ ~1k chars removed per stage, replaced by ~500 chars once + zero for non-navigation stages. The soak data's "HQS latency term cancels the gain" should improve because navigation fetches *only* what the agent decides it needs, not a fixed dump.

### 10.4 Concurrency on writes

- `write_track`/`write_profile` run only in the curator post_run stage, which executes **sequentially** (`engine.py:1096-1099`); no concurrent curator writes.
- Navigation reads can run concurrently across fan_out workers; SQLite WAL handles read-during-write.
- Reconcile writes happen post-run, before post_run stages (`engine.py:1068` vs `1088`) — serialized by the run flow.

---

## 11. Phased rollout

Each phase is independently shippable and leaves existing behavior unchanged unless a spec opts in.

### Phase 1 — Typed records, provenance, migration, reconcile
Extend `KnowledgeRecord` (`+id,type,provenance,superseded_by,updated_at`); migration in `KnowledgeStore.init()` (§7); `Reconciler` in `armature/state/reconcile.py` wired into `KnowledgeExtractor` (§2.6); `MemoryConfig.reconcile`/`reconcile_llm` (§5.1); WAL pragma; `superseded_by IS NULL` filters on `load`/`search`/`semantic_search`.
**Acceptance:** `test_migration.py` passes (legacy upgrades cleanly, idempotent); `test_reconcile.py` passes (all 5 decision cases); existing `test_knowledge.py` passes unchanged; a spec with `extract_knowledge: true` (no other new fields) stores reconciled, typed, provenance-linked records; superseded rows excluded from search, old rows still present via direct SELECT.

### Phase 2 — Navigation tools (additive opt-in)
`armature/registry/memory_tools.py` with `register_memory_tools` factory (§3.1); 5 read tools (RRF `search_records`, `get_records`, `read_track`, `read_profile`, `search_conversation`, `get_run_trace`); `MemoryConfig.navigation_tools`; engine registration in `__init__` (§6); `_memory_index` injection + per-stage `_knowledge` suppression (§5.2); validator codes for nav misconfig (§5.3).
**Acceptance:** `test_memory_tools.py` passes (dispatch, closure isolation, RRF ranking, no-embedder fallback); `test_memory_navigation.py` Phase-2 subset passes (worker declaring `memory.search_records` receives the tool, calls it via mocked LLM, context lacks full `_knowledge` dump); `navigation_tools: false`/unset is byte-identical to today; `read_track`/`read_profile` return `{"content": null}` gracefully until Phase 3.

### Phase 3 — Topic tracks, profile, curator
`TrackStore`/`ProfileStore` + L2/L3 tables (created in Phase-1 migration, populated now); `memory.write_track`/`write_profile` tools (§4.2); `MemoryConfig.curator_stage`, budgets, refresh thresholds (§5.1); refresh-hint computation in `run()` (§6); validator codes for curator misconfig (§5.3).
**Acceptance:** `test_tracks_profile.py` passes (budget enforcement, evidence validation, upsert); full `test_memory_navigation.py` passes (curator writes track+profile; second run navigates them); a spec without `curator_stage` runs unchanged (tracks/profile tables exist but stay empty; `read_track`/`read_profile` return null); profile ≤2000 chars enforced; ≤20 tracks enforced.

---

## Critical files for implementation

- `armature/state/knowledge.py` — extend `KnowledgeRecord` (`+id,type,provenance,superseded_by,updated_at,source_*`); extend `init()` with migration (§7); add `superseded_by IS NULL` filters to `load`/`search`/`semantic_search`; add row cap to `semantic_search`; add WAL pragma; add `index_summary()`, `count_since()`, `find_neighbors()` helpers; populate `id` in `_row_to_record`.
- `armature/state/extractor.py` — replace the append-only `record()` loop (`extractor.py:79-84`) with `Reconciler.reconcile_batch()`; thread `source_stage_id`/`source_capture_key` from memory entries into records.
- `armature/runtime/engine.py` — `__init__`: construct `TrackStore`/`ProfileStore`, call `register_memory_tools` when `navigation_tools` (§6); `run()`: inject `_memory_index`, compute refresh hint (§6).
- `armature/spec/models.py` — extend `MemoryConfig` with the 9 new additive fields (§5.1).
- `armature/spec/validator.py` — add `_memory_index` to `harness_injected_keys`; add the 6 new validator codes (§5.3).
- `armature/registry/memory_tools.py` (new, sibling of `builtins.py`) — `register_memory_tools()` factory + 7 tool handlers (5 read + 2 write) and the RRF helper (§3, §4, §8).
- `armature/state/reconcile.py` (new) — `Reconciler` + `classify` + `find_neighbors`.
- `armature/state/tracks.py` (new) — `TrackStore`.
- `armature/state/profile.py` (new) — `ProfileStore`.

## Verification (end-to-end)

1. `pytest tests/state/test_migration.py tests/state/test_reconcile.py tests/state/test_tracks_profile.py tests/registry/test_memory_tools.py tests/integration/test_memory_navigation.py` — all green.
2. `pytest tests/state/test_knowledge.py tests/state/test_memory.py tests/test_tool_modules.py` — existing tests still green (regression).
3. `armature validate` on a spec using the new `memory:` fields passes (no new validator errors on a well-formed spec; errors fire on the misconfig cases in §5.3).
4. Manual smoke: run the §9.6 integration spec against a real (mocked-LLM) harness; confirm `~/.armature/memory/nav_test_knowledge.db` has typed records, a track, and a profile after one run; run a second time and confirm the worker's `memory.search_records` tool-call appears in the trace and `_knowledge` is absent from its stage context.
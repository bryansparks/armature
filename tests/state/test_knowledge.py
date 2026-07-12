"""Tests for KnowledgeStore and KnowledgeExtractor.

KnowledgeStore persists LLM-extracted facts across workflow runs.
KnowledgeExtractor calls a small LLM to extract structured knowledge from raw memories.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


# ── KnowledgeStore ────────────────────────────────────────────────────────────

async def test_knowledge_store_record_and_load(tmp_path):
    """record() stores a KnowledgeRecord; load() retrieves it."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    rec = KnowledgeRecord(
        workflow_name="wf",
        entity="user",
        fact="prefers concise responses",
        confidence=0.9,
        source_run_id="r1",
    )
    await store.record(rec)

    results = await store.load("wf")
    assert len(results) == 1
    assert results[0].entity == "user"
    assert results[0].fact == "prefers concise responses"
    assert results[0].confidence == pytest.approx(0.9)


async def test_knowledge_store_load_filters_by_workflow(tmp_path):
    """load() only returns records for the requested workflow."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    await store.record(KnowledgeRecord(
        workflow_name="wf_a", entity="domain", fact="uses REST APIs", confidence=0.8, source_run_id="r1",
    ))
    await store.record(KnowledgeRecord(
        workflow_name="wf_b", entity="user", fact="prefers JSON", confidence=0.9, source_run_id="r2",
    ))

    results = await store.load("wf_a")
    assert len(results) == 1
    assert results[0].workflow_name == "wf_a"


async def test_knowledge_store_empty_returns_empty(tmp_path):
    """No records stored → empty list, no error."""
    from armature.state.knowledge import KnowledgeStore

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    results = await store.load("unknown_workflow")
    assert results == []


async def test_knowledge_store_search_keyword_match(tmp_path):
    """search() returns records whose fact contains the query keyword."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="user", fact="prefers concise responses", confidence=0.9, source_run_id="r1",
    ))
    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="domain", fact="all APIs require authentication", confidence=0.8, source_run_id="r1",
    ))

    results = await store.search("wf", "authentication")
    assert len(results) == 1
    assert "authentication" in results[0].fact


async def test_knowledge_store_search_no_match_returns_empty(tmp_path):
    """search() returns empty when no fact contains the query."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="user", fact="prefers short answers", confidence=0.9, source_run_id="r1",
    ))

    results = await store.search("wf", "blockchain")
    assert results == []


async def test_knowledge_store_search_top_k_limits_results(tmp_path):
    """search() respects top_k limit."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    for i in range(10):
        await store.record(KnowledgeRecord(
            workflow_name="wf", entity=f"e{i}", fact=f"fact about topic {i}", confidence=0.9, source_run_id="r1",
        ))

    results = await store.search("wf", "topic", top_k=3)
    assert len(results) <= 3


async def test_knowledge_store_multiple_records_multiple_runs(tmp_path):
    """Facts accumulate across multiple runs."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    for i, run_id in enumerate(["r1", "r2", "r3"]):
        await store.record(KnowledgeRecord(
            workflow_name="wf", entity="domain", fact=f"insight from run {i}", confidence=0.8, source_run_id=run_id,
        ))

    results = await store.load("wf")
    assert len(results) == 3


async def test_knowledge_store_search_multi_word_query(tmp_path):
    """FTS5 search handles multi-word queries as implicit AND."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="user", fact="prefers concise responses", confidence=0.9, source_run_id="r1",
    ))
    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="domain", fact="concise documentation is required", confidence=0.8, source_run_id="r1",
    ))
    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="system", fact="always validate authentication tokens", confidence=0.85, source_run_id="r1",
    ))

    results = await store.search("wf", "concise responses")
    assert len(results) >= 1
    assert any("concise" in r.fact and "responses" in r.fact for r in results)


async def test_knowledge_store_search_returns_highest_relevance_first(tmp_path):
    """FTS5 search ranks records by BM25 relevance, most relevant first."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="a", fact="authentication is required for all endpoints", confidence=0.9, source_run_id="r1",
    ))
    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="b", fact="authentication authentication authentication security", confidence=0.7, source_run_id="r1",
    ))

    results = await store.search("wf", "authentication", top_k=5)
    assert len(results) == 2
    assert results[0].entity == "b"  # higher term frequency → higher BM25 rank


# ── KnowledgeStore semantic search ───────────────────────────────────────────


class _FakeEmbedder:
    """Deterministic fake embedder for testing semantic search without a real model."""
    _vectors: dict[str, list[float]] = {
        "dogs are loyal animals":  [1.0, 0.0, 0.0, 0.0],
        "cats are independent":    [0.0, 1.0, 0.0, 0.0],
        "birds can fly":           [0.0, 0.0, 1.0, 0.0],
    }
    _default = [0.25, 0.25, 0.25, 0.25]

    def embed(self, text: str) -> list[float]:
        for key, vec in self._vectors.items():
            if key == text:
                return vec
        # Queries: map known synonyms
        if "canine" in text or "dog" in text:
            return [0.95, 0.05, 0.0, 0.0]
        if "feline" in text or "cat" in text:
            return [0.05, 0.95, 0.0, 0.0]
        return list(self._default)


async def test_knowledge_semantic_search_finds_related_fact(tmp_path):
    """semantic_search() returns the most cosine-similar fact, not keyword match."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    embedder = _FakeEmbedder()

    for fact in ("dogs are loyal animals", "cats are independent", "birds can fly"):
        await store.record(
            KnowledgeRecord(workflow_name="wf", entity="animals", fact=fact,
                            confidence=0.9, source_run_id="r1"),
            embedder=embedder,
        )

    # "canine" has no keyword overlap with "dogs are loyal animals" but high cosine similarity
    results = await store.semantic_search("wf", "canine", embedder=embedder, top_k=1)
    assert len(results) == 1
    assert "dog" in results[0].fact


async def test_knowledge_semantic_search_respects_top_k(tmp_path):
    """semantic_search() returns at most top_k results."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    embedder = _FakeEmbedder()

    for fact in ("dogs are loyal animals", "cats are independent", "birds can fly"):
        await store.record(
            KnowledgeRecord(workflow_name="wf", entity="animals", fact=fact,
                            confidence=0.9, source_run_id="r1"),
            embedder=embedder,
        )

    results = await store.semantic_search("wf", "dog canine", embedder=embedder, top_k=2)
    assert len(results) <= 2


async def test_knowledge_semantic_search_filters_by_workflow(tmp_path):
    """semantic_search() only returns records for the requested workflow."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    embedder = _FakeEmbedder()

    await store.record(
        KnowledgeRecord(workflow_name="wf_a", entity="a", fact="dogs are loyal animals",
                        confidence=0.9, source_run_id="r1"),
        embedder=embedder,
    )
    await store.record(
        KnowledgeRecord(workflow_name="wf_b", entity="b", fact="cats are independent",
                        confidence=0.9, source_run_id="r1"),
        embedder=embedder,
    )

    results = await store.semantic_search("wf_a", "canine", embedder=embedder, top_k=5)
    assert all(r.workflow_name == "wf_a" for r in results)


async def test_knowledge_semantic_search_skips_records_without_embeddings(tmp_path):
    """Records inserted without embedder are skipped in semantic_search()."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    embedder = _FakeEmbedder()

    # Insert without embedder (no embedding blob)
    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="old", fact="dogs are loyal animals",
        confidence=0.9, source_run_id="r1",
    ))
    # Insert with embedder
    await store.record(
        KnowledgeRecord(workflow_name="wf", entity="new", fact="cats are independent",
                        confidence=0.9, source_run_id="r1"),
        embedder=embedder,
    )

    results = await store.semantic_search("wf", "feline", embedder=embedder, top_k=5)
    # Only the record with an embedding is returned
    assert len(results) == 1
    assert results[0].entity == "new"


async def test_knowledge_semantic_search_empty_store_returns_empty(tmp_path):
    """semantic_search() on an empty store returns []."""
    from armature.state.knowledge import KnowledgeStore

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()

    results = await store.semantic_search("wf", "canine", embedder=_FakeEmbedder(), top_k=5)
    assert results == []


# ── KnowledgeExtractor ────────────────────────────────────────────────────────

_MEMORIES = {
    "researcher": {
        "brief": [{"topic": "climate", "summary": "CO2 is rising"}]
    }
}

_EXTRACTOR_RESPONSE = '[{"entity": "domain", "fact": "CO2 is rising significantly", "confidence": 0.9}]'


async def test_extractor_returns_knowledge_records():
    """extract() returns KnowledgeRecord list parsed from LLM JSON."""
    from armature.state.extractor import KnowledgeExtractor

    extractor = KnowledgeExtractor(model="claude-haiku-4-5-20251001")

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = _EXTRACTOR_RESPONSE
        return resp

    with patch("armature.state.extractor.litellm_completion", side_effect=mock_completion):
        records = await extractor.extract(_MEMORIES, workflow_name="wf", run_id="r1")

    assert len(records) == 1
    assert records[0].entity == "domain"
    assert "CO2" in records[0].fact
    assert records[0].workflow_name == "wf"
    assert records[0].source_run_id == "r1"


async def test_extractor_handles_empty_memories():
    """extract() with empty memories returns empty list without error."""
    from armature.state.extractor import KnowledgeExtractor

    extractor = KnowledgeExtractor(model="claude-haiku-4-5-20251001")

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "[]"
        return resp

    with patch("armature.state.extractor.litellm_completion", side_effect=mock_completion):
        records = await extractor.extract({}, workflow_name="wf", run_id="r1")

    assert records == []


async def test_extractor_passes_memories_to_llm():
    """The LLM prompt includes the raw memory content."""
    from armature.state.extractor import KnowledgeExtractor

    extractor = KnowledgeExtractor(model="claude-haiku-4-5-20251001")
    captured = {}

    async def mock_completion(**kwargs):
        captured["messages"] = kwargs.get("messages", [])
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = _EXTRACTOR_RESPONSE
        return resp

    with patch("armature.state.extractor.litellm_completion", side_effect=mock_completion):
        await extractor.extract(_MEMORIES, workflow_name="wf", run_id="r1")

    prompt_text = " ".join(m["content"] for m in captured["messages"])
    assert "CO2" in prompt_text or "climate" in prompt_text or "researcher" in prompt_text


async def test_extractor_handles_llm_failure_gracefully():
    """LLM exception → empty list, no exception propagated."""
    from armature.state.extractor import KnowledgeExtractor

    extractor = KnowledgeExtractor(model="claude-haiku-4-5-20251001")

    async def mock_completion(**kwargs):
        raise RuntimeError("API down")

    with patch("armature.state.extractor.litellm_completion", side_effect=mock_completion):
        records = await extractor.extract(_MEMORIES, workflow_name="wf", run_id="r1")

    assert records == []


async def test_extractor_handles_invalid_json():
    """LLM returns non-JSON → empty list, no exception."""
    from armature.state.extractor import KnowledgeExtractor

    extractor = KnowledgeExtractor(model="claude-haiku-4-5-20251001")

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "Sorry, I cannot extract facts from this."
        return resp

    with patch("armature.state.extractor.litellm_completion", side_effect=mock_completion):
        records = await extractor.extract(_MEMORIES, workflow_name="wf", run_id="r1")

    assert records == []


async def test_extractor_stores_results_in_knowledge_store(tmp_path):
    """When a KnowledgeStore is provided, extracted facts are persisted."""
    from armature.state.extractor import KnowledgeExtractor
    from armature.state.knowledge import KnowledgeStore

    store = KnowledgeStore(tmp_path / "knowledge.db")
    await store.init()

    extractor = KnowledgeExtractor(model="claude-haiku-4-5-20251001", knowledge_store=store)

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = _EXTRACTOR_RESPONSE
        return resp

    with patch("armature.state.extractor.litellm_completion", side_effect=mock_completion):
        await extractor.extract(_MEMORIES, workflow_name="wf", run_id="r1")

    stored = await store.load("wf")
    assert len(stored) == 1
    assert stored[0].source_run_id == "r1"


_LABELED_RESPONSE = (
    '[{"entity": "domain", "fact": "CO2 is rising significantly", "confidence": 0.9, '
    '"source_stage": "researcher", "source_key": "brief", "type": "fact"}]'
)


async def test_extractor_threads_provenance_from_labeled_memories():
    """extract() labels memories by stage/key and threads them into record.provenance."""
    from armature.state.extractor import KnowledgeExtractor

    extractor = KnowledgeExtractor(model="claude-haiku-4-5-20251001", reconcile=False)
    captured = {}

    async def mock_completion(**kwargs):
        captured["messages"] = kwargs.get("messages", [])
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = _LABELED_RESPONSE
        return resp

    with patch("armature.state.extractor.litellm_completion", side_effect=mock_completion):
        records = await extractor.extract(_MEMORIES, workflow_name="wf", run_id="r1")

    prompt_text = " ".join(m["content"] for m in captured["messages"])
    assert "researcher" in prompt_text and "brief" in prompt_text  # memories labeled
    assert len(records) == 1
    rec = records[0]
    assert rec.source_stage_id == "researcher"
    assert rec.source_capture_key == "brief"
    assert rec.type.value == "fact"
    assert any(p.get("run_id") == "r1" and p.get("stage_id") == "researcher" for p in rec.provenance)


async def test_extractor_drops_unmatched_source_label():
    """If the LLM cites a stage/key not in the memories, source fields are nulled."""
    from armature.state.extractor import KnowledgeExtractor

    extractor = KnowledgeExtractor(model="claude-haiku-4-5-20251001", reconcile=False)
    bad_response = (
        '[{"entity": "x", "fact": "y", "confidence": 0.9, '
        '"source_stage": "hallucinated_stage", "source_key": "no"}]'
    )

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = bad_response
        return resp

    with patch("armature.state.extractor.litellm_completion", side_effect=mock_completion):
        records = await extractor.extract(_MEMORIES, workflow_name="wf", run_id="r1")

    assert len(records) == 1
    assert records[0].source_stage_id is None
    assert records[0].source_capture_key is None


async def test_extractor_reconcile_path_calls_reconciler(tmp_path):
    """With reconcile=True + a knowledge_store, extract() delegates writes to Reconciler."""
    from armature.state.extractor import KnowledgeExtractor
    from armature.state.knowledge import KnowledgeStore
    from unittest.mock import AsyncMock

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    extractor = KnowledgeExtractor(
        model="claude-haiku-4-5-20251001", knowledge_store=store, reconcile=True
    )
    # Spy on Reconciler.reconcile_batch
    with patch("armature.state.extractor.Reconciler") as RMock:
        instance = RMock.return_value
        instance.reconcile_batch = AsyncMock()
        async def mock_completion(**kwargs):
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = _LABELED_RESPONSE
            return resp
        with patch("armature.state.extractor.litellm_completion", side_effect=mock_completion):
            await extractor.extract(_MEMORIES, workflow_name="wf", run_id="r1")
    instance.reconcile_batch.assert_awaited_once()
    candidates = instance.reconcile_batch.await_args.args[0]
    assert len(candidates) == 1
    assert candidates[0].source_stage_id == "researcher"


async def test_knowledge_record_round_trips_type_provenance_and_id(tmp_path):
    """record() persists type/provenance/source_*; load() returns them with id."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    rec = KnowledgeRecord(
        workflow_name="wf", entity="user", fact="prefers dark mode", confidence=0.9,
        source_run_id="r1", source_stage_id="researcher", source_capture_key="brief",
        type=MemoryType.PREFERENCE,
        provenance=[{"run_id": "r1", "stage_id": "researcher", "capture_key": "brief"}],
    )
    rid = await store.record(rec)
    assert rid is not None

    results = await store.load("wf")
    assert len(results) == 1
    out = results[0]
    assert out.id == rid
    assert out.type == MemoryType.PREFERENCE
    assert out.source_stage_id == "researcher"
    assert out.source_capture_key == "brief"
    assert out.provenance == [{"run_id": "r1", "stage_id": "researcher", "capture_key": "brief"}]


async def test_superseded_rows_excluded_from_load_search_and_semantic(tmp_path):
    """Rows with superseded_by set are hidden from load/search/semantic_search."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord

    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    embedder = _FakeEmbedder()
    old_id = await store.record(KnowledgeRecord(
        workflow_name="wf", entity="user", fact="prefers light theme", confidence=0.7, source_run_id="r1",
    ), embedder=embedder)
    new_id = await store.record(KnowledgeRecord(
        workflow_name="wf", entity="user", fact="prefers dark theme", confidence=0.95, source_run_id="r2",
    ), embedder=embedder)
    import aiosqlite
    async with aiosqlite.connect(tmp_path / "k.db") as db:
        await db.execute("UPDATE knowledge SET superseded_by=? WHERE id=?", (new_id, old_id))
        await db.commit()

    loaded = await store.load("wf")
    assert len(loaded) == 1
    assert loaded[0].id == new_id

    found = await store.search("wf", "theme")
    assert all(r.id != old_id for r in found)

    # semantic_search must also exclude the superseded row.
    sem = await store.semantic_search("wf", "theme", embedder=embedder, top_k=5)
    assert all(r.id != old_id for r in sem)
    assert len(sem) == 1 and sem[0].id == new_id


async def test_get_by_id_returns_record(tmp_path):
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord
    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    rid = await store.record(KnowledgeRecord(
        workflow_name="wf", entity="e", fact="a fact", confidence=0.8, source_run_id="r1"))
    got = await store.get_by_id(rid)
    assert got is not None and got.id == rid and got.fact == "a fact"
    assert await store.get_by_id(999999) is None


async def test_update_record_bumps_confidence_provenance_and_updated_at(tmp_path):
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord
    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    rid = await store.record(KnowledgeRecord(
        workflow_name="wf", entity="e", fact="a fact", confidence=0.7, source_run_id="r1",
        provenance=[{"run_id": "r1"}]))
    await store.update_record(rid, confidence=0.9, provenance=[{"run_id": "r1"}, {"run_id": "r2"}])
    got = await store.get_by_id(rid)
    assert got.confidence == 0.9
    assert {"run_id": "r2"} in got.provenance
    assert got.updated_at is not None


async def test_set_superseded_marks_old_row(tmp_path):
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord
    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    old = await store.record(KnowledgeRecord(
        workflow_name="wf", entity="e", fact="old fact", confidence=0.5, source_run_id="r1"))
    new = await store.record(KnowledgeRecord(
        workflow_name="wf", entity="e", fact="new fact", confidence=0.95, source_run_id="r2"))
    await store.set_superseded(old, new)
    got = await store.get_by_id(old)
    assert got.superseded_by == new


async def test_find_neighbors_unions_bm25_and_semantic(tmp_path):
    """find_neighbors returns the union of BM25 + semantic hits, deduped by id."""
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord
    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    embedder = _FakeEmbedder()  # defined earlier in test_knowledge.py
    for fact in ["dogs are loyal animals", "cats are independent pets", "python is a programming language"]:
        await store.record(KnowledgeRecord(
            workflow_name="wf", entity="e", fact=fact, confidence=0.8, source_run_id="r1"),
            embedder=embedder)
    cand = KnowledgeRecord(workflow_name="wf", entity="e", fact="canine companions", confidence=0.9, source_run_id="r2")
    neighbors = await store.find_neighbors("wf", cand, embedder=embedder, k=10)
    ids = [n.id for n in neighbors]
    assert len(ids) == len(set(ids))  # deduped
    assert any("dog" in n.fact for n in neighbors)


async def test_find_neighbors_without_embedder_uses_bm25_only(tmp_path):
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord
    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="e", fact="prefers concise answers", confidence=0.9, source_run_id="r1"))
    cand = KnowledgeRecord(workflow_name="wf", entity="e", fact="prefers concise answers", confidence=0.9, source_run_id="r2")
    neighbors = await store.find_neighbors("wf", cand, embedder=None, k=10)
    assert len(neighbors) == 1
    assert "concise" in neighbors[0].fact


async def test_index_summary_counts_by_type(tmp_path):
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType
    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="a", fact="fact one", confidence=0.9,
        source_run_id="r1", type=MemoryType.FACT))
    await store.record(KnowledgeRecord(
        workflow_name="wf", entity="b", fact="an event", confidence=0.9,
        source_run_id="r1", type=MemoryType.EVENT))
    summary = await store.index_summary("wf")
    assert summary["records_by_type"]["fact"] == 1
    assert summary["records_by_type"]["event"] == 1
    assert summary["tracks"] == []
    assert summary["profile_chars"] == 0
    assert summary["profile_preview"] == ""


async def test_index_summary_missing_db_returns_empty():
    from armature.state.knowledge import KnowledgeStore
    store = KnowledgeStore(Path("/nonexistent/does_not_exist.db"))
    summary = await store.index_summary("wf")
    assert summary == {"records_by_type": {}, "tracks": [],
                       "profile_chars": 0, "profile_preview": ""}

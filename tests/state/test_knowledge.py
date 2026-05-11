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

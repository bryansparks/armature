from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _harness_with_nav(
    tmp_path,
    navigation_tools: bool = True,
    extract_knowledge: bool = True,
    curator_stage: str | None = None,
):
    from armature.runtime.engine import Harness
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        MemoryConfig, Contract,
    )
    stages = [Stage(id="worker", role=Role(
        name="Worker", type=RoleType.WORKER, description="do {{ topic }}",
        model_tier="small", tools=["memory.search_records"],
    ))]
    if curator_stage is not None:
        stages.append(Stage(
            id=curator_stage, post_run=True,
            role=Role(
                name="Curator", type=RoleType.JUDGE,
                description="Curate tracks and profile for {{ topic }}.",
                model_tier="small",
                tools=["memory.write_track", "memory.write_profile"],
            ),
        ))
    spec = HarnessSpec(
        name="nav_reg", version="1.0",
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        role_type_defaults={"worker": "small", "orchestrator": "small", "judge": "small", "researcher": "small"},
        contracts=Contract(inputs=[{"name": "topic"}]),
        memory=MemoryConfig(
            enabled=True, db=str(tmp_path / "mem.db"),
            extract_knowledge=extract_knowledge, navigation_tools=navigation_tools,
            curator_stage=curator_stage,
        ),
        stages=stages,
    )
    return Harness(spec=spec, session_dir=tmp_path)


async def _mock_completion(**kwargs):
    r = MagicMock(); r.choices = [MagicMock()]
    r.choices[0].message.content = "done"
    r.choices[0].message.tool_calls = None
    r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
    return r


def test_navigation_tools_registers_memory_tools(tmp_path):
    h = _harness_with_nav(tmp_path, navigation_tools=True)
    names = {d["name"] for d in h._registry.descriptors()}
    assert "memory.search_records" in names
    assert "memory.get_records" in names
    assert "memory.read_track" in names
    assert "memory.read_profile" in names
    assert "memory.search_conversation" in names
    assert "memory.get_run_trace" in names


def test_navigation_tools_off_does_not_register(tmp_path):
    h = _harness_with_nav(tmp_path, navigation_tools=False)
    names = {d["name"] for d in h._registry.descriptors()}
    assert not any(n.startswith("memory.") for n in names)


def test_navigation_tools_works_without_extract_knowledge(tmp_path):
    # navigation_tools=True but extract_knowledge=False: tools still registered,
    # knowledge_store constructed for navigation reads (L1 empty).
    h = _harness_with_nav(tmp_path, navigation_tools=True, extract_knowledge=False)
    names = {d["name"] for d in h._registry.descriptors()}
    assert "memory.search_records" in names
    assert h._knowledge_store is not None

async def test_memory_index_injected_when_navigation_tools(tmp_path):
    # Pre-seed the knowledge db with one record so index_summary is non-empty.
    from armature.state.knowledge import KnowledgeStore, KnowledgeRecord, MemoryType
    h = _harness_with_nav(tmp_path, navigation_tools=True, extract_knowledge=True)
    await h._knowledge_store.init()
    await h._knowledge_store.record(KnowledgeRecord(
        workflow_name="nav_reg", entity="e", fact="f", confidence=0.9,
        source_run_id="r0", type=MemoryType.FACT))
    # Stub the LLM so the run completes without network.
    from unittest.mock import MagicMock, patch
    async def mock_completion(**kwargs):
        r = MagicMock(); r.choices = [MagicMock()]
        r.choices[0].message.content = "done"
        r.choices[0].message.tool_calls = None
        r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
        return r
    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        ctx = await h.run({"topic": "x"})
    assert "_memory_index" in ctx
    assert ctx["_memory_index"]["records_by_type"].get("fact") == 1


async def test_curator_stage_constructs_track_profile_stores(tmp_path, monkeypatch):
    h = _harness_with_nav(tmp_path, curator_stage="curator")
    assert h._track_store is not None
    assert h._profile_store is not None


async def test_no_curator_stage_no_write_tools(tmp_path):
    h = _harness_with_nav(tmp_path, curator_stage=None)
    names = {d["name"] for d in h._registry.descriptors()}
    assert "memory.write_track" not in names
    assert "memory.write_profile" not in names
    # read tools still present
    assert "memory.read_track" in names


async def test_refresh_hint_injected_when_curator(tmp_path):
    h = _harness_with_nav(tmp_path, curator_stage="curator")
    with patch("armature.nodes.llm.litellm_completion", side_effect=_mock_completion):
        await h.run({"topic": "x"})
    # The refresh hint is computed and stored in provenance before the
    # post-run loop when curator_stage is set.
    assert "_memory_index_refresh_hint" in h._provenance


async def test_refresh_hint_not_injected_without_curator(tmp_path):
    h = _harness_with_nav(tmp_path, curator_stage=None)
    with patch("armature.nodes.llm.litellm_completion", side_effect=_mock_completion):
        await h.run({"topic": "x"})
    assert "_memory_index_refresh_hint" not in h._provenance


async def test_embedder_shared_between_extractor_and_nav(tmp_path, monkeypatch):
    """When extract_knowledge + navigation_tools both on, only one LocalEmbedder."""
    calls = {"n": 0}

    class CountingEmbedder:
        @staticmethod
        def is_available():
            return True

        def __init__(self):
            calls["n"] += 1

    monkeypatch.setattr("armature.state.embedder.LocalEmbedder", CountingEmbedder)
    h = _harness_with_nav(tmp_path, curator_stage=None, extract_knowledge=True)
    assert calls["n"] == 1, f"expected 1 embedder, got {calls['n']}"

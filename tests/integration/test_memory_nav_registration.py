from __future__ import annotations
from pathlib import Path

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _harness_with_nav(tmp_path, navigation_tools: bool, extract_knowledge: bool = True):
    from armature.runtime.engine import Harness
    from armature.spec.models import (
        HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig,
        MemoryConfig, Contract,
    )
    spec = HarnessSpec(
        name="nav_reg", version="1.0",
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        role_type_defaults={"worker": "small", "orchestrator": "small", "judge": "small", "researcher": "small"},
        contracts=Contract(inputs=[{"name": "topic"}]),
        memory=MemoryConfig(
            enabled=True, db=str(tmp_path / "mem.db"),
            extract_knowledge=extract_knowledge, navigation_tools=navigation_tools,
        ),
        stages=[Stage(id="worker", role=Role(
            name="Worker", type=RoleType.WORKER, description="do {{ topic }}",
            model_tier="small", tools=["memory.search_records"],
        ))],
    )
    return Harness(spec=spec, session_dir=tmp_path)


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
import pytest
from pathlib import Path
from armature.state.artifacts import ArtifactStore

@pytest.fixture
def store(tmp_path):
    return ArtifactStore(base_dir=tmp_path / "artifacts")

async def test_write_and_read_json(store):
    await store.write("result", {"decision": "approve", "confidence": 0.9})
    data = await store.read("result")
    assert data["confidence"] == 0.9

async def test_write_and_read_text(store):
    await store.write_text("brief", "This is a research brief.")
    text = await store.read_text("brief")
    assert "research brief" in text

async def test_list_artifacts(store):
    await store.write("a", {"x": 1})
    await store.write("b", {"y": 2})
    names = await store.list()
    assert "a" in names and "b" in names

async def test_missing_artifact_returns_none(store):
    result = await store.read("nonexistent")
    assert result is None

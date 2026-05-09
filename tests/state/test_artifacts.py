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


async def test_missing_text_artifact_returns_none(store):
    result = await store.read_text("nonexistent")
    assert result is None


async def test_write_returns_path(store):
    path = await store.write("output", {"x": 42})
    assert path.exists()
    assert path.suffix == ".json"


async def test_write_text_returns_path(store):
    path = await store.write_text("notes", "some notes")
    assert path.exists()
    assert path.suffix == ".md"


async def test_write_overwrites_existing(store):
    await store.write("item", {"v": 1})
    await store.write("item", {"v": 2})
    data = await store.read("item")
    assert data["v"] == 2


async def test_list_empty_store(store):
    names = await store.list()
    assert names == []


async def test_write_nested_data_preserved(store):
    payload = {"list": [1, 2, 3], "nested": {"key": "value"}}
    await store.write("complex", payload)
    data = await store.read("complex")
    assert data["list"] == [1, 2, 3]
    assert data["nested"]["key"] == "value"


async def test_base_dir_created_on_init(tmp_path):
    new_dir = tmp_path / "new" / "nested" / "dir"
    ArtifactStore(base_dir=new_dir)
    assert new_dir.exists()


async def test_read_text_finds_txt_extension(tmp_path):
    """read_text falls back to .txt when no .md file exists."""
    store = ArtifactStore(base_dir=tmp_path)
    (tmp_path / "notes.txt").write_text("from txt file", encoding="utf-8")
    text = await store.read_text("notes")
    assert text == "from txt file"


async def test_read_text_prefers_md_over_txt(tmp_path):
    """When both .md and .txt exist, .md wins."""
    store = ArtifactStore(base_dir=tmp_path)
    (tmp_path / "notes.md").write_text("from md file", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("from txt file", encoding="utf-8")
    text = await store.read_text("notes")
    assert text == "from md file"

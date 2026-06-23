"""Tests for adapter manifest read/write."""
import pytest
from pathlib import Path

from armature.adapters.manifest import Manifest, AdapterMetadata


def test_manifest_starts_empty(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    assert m.versions() == {}
    assert m.latest_version() is None


def test_manifest_add_and_latest(tmp_path):
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    meta = AdapterMetadata(name="x", version="1", base_model="qwen/qwen2.5-7b")
    m.add(meta)
    assert m.latest_version() == "1"
    assert m.versions()["1"].name == "x"
    assert path.exists()


def test_manifest_promote(tmp_path):
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    m.add(AdapterMetadata(name="x", version="1", base_model="qwen/qwen2.5-7b"))
    m.add(AdapterMetadata(name="x", version="2", base_model="qwen/qwen2.5-7b"))
    m.set_latest("1")
    assert m.latest_version() == "1"


def test_manifest_promote_unknown_fails(tmp_path):
    path = tmp_path / "manifest.json"
    m = Manifest(path)
    m.add(AdapterMetadata(name="x", version="1", base_model="qwen/qwen2.5-7b"))
    with pytest.raises(ValueError):
        m.set_latest("99")

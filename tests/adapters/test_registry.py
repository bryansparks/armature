"""Tests for the local adapter registry."""
import pytest
from pathlib import Path

from armature.adapters.registry import AdapterRegistry, ResolvedAdapter
from armature.adapters.manifest import AdapterMetadata


def _meta(name: str, version: str, base_model: str = "qwen/qwen2.5-7b") -> AdapterMetadata:
    return AdapterMetadata(name=name, version=version, base_model=base_model)


def test_registry_empty_raises(tmp_path):
    reg = AdapterRegistry(base_dir=tmp_path)
    with pytest.raises(ValueError, match="not found"):
        reg.get("missing")


def test_registry_register_and_get(tmp_path):
    reg = AdapterRegistry(base_dir=tmp_path)
    artifact = tmp_path / "incoming"
    artifact.mkdir()
    (artifact / "adapter_config.json").write_text("{}")

    meta = _meta("tdd-workflow", "1")
    reg.register(meta, artifact)

    resolved = reg.get("tdd-workflow")
    assert resolved.metadata == meta
    assert resolved.artifact_dir.exists()
    assert (resolved.artifact_dir / "adapter_config.json").exists()


def test_registry_get_specific_version(tmp_path):
    reg = AdapterRegistry(base_dir=tmp_path)
    for v in ["1", "2"]:
        d = tmp_path / f"v{v}"
        d.mkdir()
        (d / "x.txt").write_text(v)
        reg.register(_meta("skill", v), d)

    assert reg.get("skill", "1").metadata.version == "1"
    assert reg.get("skill").metadata.version == "2"  # latest


def test_registry_promote(tmp_path):
    reg = AdapterRegistry(base_dir=tmp_path)
    for v in ["1", "2"]:
        d = tmp_path / f"v{v}"
        d.mkdir()
        reg.register(_meta("skill", v), d)

    reg.promote("skill", "1")
    assert reg.get("skill").metadata.version == "1"


def test_registry_list(tmp_path):
    reg = AdapterRegistry(base_dir=tmp_path)
    d = tmp_path / "a1"
    d.mkdir()
    reg.register(_meta("a", "1"), d)
    d2 = tmp_path / "a2"
    d2.mkdir()
    reg.register(_meta("a", "2"), d2)

    results = list(reg.list("a"))
    assert len(results) == 2
    assert {r[0].version for r in results} == {"1", "2"}


def test_registry_list_all(tmp_path):
    reg = AdapterRegistry(base_dir=tmp_path)
    for name, version in [("a", "1"), ("b", "1")]:
        d = tmp_path / f"{name}-{version}"
        d.mkdir()
        reg.register(_meta(name, version), d)

    results = list(reg.list())
    assert {r[0].name for r in results} == {"a", "b"}

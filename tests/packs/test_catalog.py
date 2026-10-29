import os

import pytest

from armature.packs import (
    BUILTIN_DIR,
    PACKS_PATH_ENV,
    PackError,
    catalog,
    load_pack,
    search_path,
    version_key,
)
from tests.packs.support import HELLO, write_pack


def test_loads_the_example_pack():
    pack = load_pack(HELLO)
    assert pack.name == "hello" and pack.version == "1.0.0"
    assert sorted(pack.adapters) == ["greet", "wait"]
    assert pack.dir == HELLO
    assert pack.adapters["greet"].params["required"] == ["name"]


@pytest.mark.parametrize("cmd", ["echo {{ name }}", "{% if x %}rm -rf /{% endif %}", "a{{b"])
def test_refuses_template_markers_in_cmd(tmp_path, cmd):
    write_pack(tmp_path / "p", greet={"cmd": cmd})
    with pytest.raises(PackError, match="template markers"):
        load_pack(tmp_path / "p")


@pytest.mark.parametrize("name", ["Hello", "-x", "a_b", "a" * 41, ""])
def test_refuses_bad_pack_names(tmp_path, name):
    write_pack(tmp_path / "p", name=name)
    with pytest.raises(PackError, match="pack name"):
        load_pack(tmp_path / "p")


@pytest.mark.parametrize("adapter", ["Greet", "a/b", "_x", "a" * 65])
def test_refuses_bad_adapter_names(tmp_path, adapter):
    write_pack(tmp_path / "p", **{adapter: {"cmd": "true"}})
    with pytest.raises(PackError, match="adapter names"):
        load_pack(tmp_path / "p")


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "01.0.0", "latest"])
def test_refuses_non_semver(tmp_path, version):
    write_pack(tmp_path / "p", version=version)
    with pytest.raises(PackError, match="semantic version"):
        load_pack(tmp_path / "p")


def test_refuses_bad_params_schema_and_unknown_keys(tmp_path):
    write_pack(tmp_path / "a", x={"cmd": "true", "params": {"type": "array"}})
    with pytest.raises(PackError, match="type: object"):
        load_pack(tmp_path / "a")
    write_pack(tmp_path / "b", x={"cmd": "true", "params": {"type": "object", "required": "name"}})
    with pytest.raises(PackError, match="not a valid JSON Schema"):
        load_pack(tmp_path / "b")
    write_pack(tmp_path / "c", x={"cmd": "true", "shell": "zsh"})
    with pytest.raises(PackError, match="shell"):
        load_pack(tmp_path / "c")
    write_pack(tmp_path / "d", x={"cmd": "true", "secrets": ["gh token"]})
    with pytest.raises(PackError, match="secret names"):
        load_pack(tmp_path / "d")


def test_missing_manifest_names_no_path(tmp_path):
    with pytest.raises(PackError) as caught:
        load_pack(tmp_path)
    assert str(tmp_path) not in str(caught.value)


def test_search_path_order_and_first_pack_wins(tmp_path):
    first = write_pack(tmp_path / "one" / "demo", version="1.0.0")
    write_pack(tmp_path / "two" / "demo", version="2.0.0")
    write_pack(tmp_path / "two" / "other", name="other")
    cat = catalog([tmp_path / "one", tmp_path / "two"])
    assert [p.name for p in cat.packs()] == ["demo", "other"]
    assert cat.pack("demo").version == "1.0.0" and cat.pack("demo").dir == first.resolve()
    assert len(cat.errors) == 1 and "duplicate pack 'demo'" in cat.errors[0].message
    assert cat.errors[0].path == tmp_path / "two" / "demo"


def test_entry_may_be_a_pack_or_a_directory_of_packs(tmp_path):
    write_pack(tmp_path / "single", name="single")
    write_pack(tmp_path / "many" / "a", name="a")
    write_pack(tmp_path / "many" / ".hidden", name="hidden")
    cat = catalog([tmp_path / "single", tmp_path / "many", tmp_path / "missing"])
    assert [p.name for p in cat.packs()] == ["a", "single"]
    assert cat.errors == []


def test_broken_pack_is_an_error_not_a_crash(tmp_path):
    write_pack(tmp_path / "root" / "good", name="good")
    write_pack(tmp_path / "root" / "bad", name="bad", x={"cmd": "echo {{ x }}"})
    cat = catalog([tmp_path / "root"])
    assert [p.name for p in cat.packs()] == ["good"]
    assert [e.path.name for e in cat.errors] == ["bad"]


def test_default_search_path_reads_builtins_then_env_then_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(PACKS_PATH_ENV, os.pathsep.join([str(tmp_path / "a"), "", str(tmp_path / "b")]))
    assert search_path() == [BUILTIN_DIR, tmp_path / "a", tmp_path / "b",
                             tmp_path / "home" / ".armature" / "packs"]
    write_pack(tmp_path / "home" / ".armature" / "packs" / "demo")
    write_pack(tmp_path / "b" / "demo", version="3.0.0")
    assert catalog().pack("demo").version == "3.0.0"


def test_get_resolve_and_allowlists(tmp_path):
    write_pack(tmp_path / "r" / "demo")
    (tmp_path / "r" / "demo" / "pack.yaml").write_text(
        "name: demo\nversion: 1.0.0\ntools_allowlist: [Read, Grep]\nmcp_allowlist: [jira]\n"
        "adapters:\n  echo:\n    cmd: 'true'\n")
    write_pack(tmp_path / "r" / "zed", name="zed")
    (tmp_path / "r" / "zed" / "pack.yaml").write_text(
        "name: zed\nversion: 1.0.0\ntools_allowlist: [Grep, Glob]\nadapters: {}\n")
    cat = catalog([tmp_path / "r"])
    assert cat.get("demo/echo").cmd == "true"
    assert cat.get("demo/nope") is None and cat.get("nope/echo") is None and cat.get("demo") is None
    assert cat.tools_allowlist() == ["Read", "Grep", "Glob"]
    assert cat.mcp_allowlist() == ["jira"]


def test_version_order():
    ordered = ["0.9.9", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0",
               "1.0.1", "1.10.0"]
    assert sorted(ordered, key=version_key) == ordered

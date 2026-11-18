import os

import pytest

import armature.packs as packs_module
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


@pytest.fixture
def parses(monkeypatch):
    calls = []
    real = packs_module._parse_pack

    def counting(raw):
        calls.append(raw)
        return real(raw)

    monkeypatch.setattr(packs_module, "_parse_pack", counting)
    return calls


def test_a_second_load_does_not_reparse(tmp_path, parses):
    write_pack(tmp_path / "p")
    first, second = load_pack(tmp_path / "p"), load_pack(tmp_path / "p")
    assert len(parses) == 1
    assert first == second and first is not second
    assert second.dir == (tmp_path / "p").resolve()


def test_editing_the_manifest_reloads(tmp_path, parses):
    write_pack(tmp_path / "p", version="1.0.0")
    assert load_pack(tmp_path / "p").version == "1.0.0"
    write_pack(tmp_path / "p", version="10.0.0")
    assert load_pack(tmp_path / "p").version == "10.0.0"
    assert len(parses) == 2


def test_mutating_a_loaded_pack_leaves_the_next_load_alone(tmp_path, parses):
    write_pack(tmp_path / "p")
    pack = load_pack(tmp_path / "p")
    pack.adapters["echo"].params["properties"]["n"]["type"] = "string"
    pack.adapters["extra"] = pack.adapters["echo"]
    pack.name, pack.dir = "changed", tmp_path
    again = load_pack(tmp_path / "p")
    assert len(parses) == 1
    assert again.name == "demo" and sorted(again.adapters) == ["echo"]
    assert again.adapters["echo"].params["properties"]["n"]["type"] == "integer"
    assert again.dir == (tmp_path / "p").resolve()


def test_a_swapped_in_copy_with_the_old_mtime_reloads(tmp_path, parses):
    write_pack(tmp_path / "old", version="1.0.0")
    write_pack(tmp_path / "new", version="2.0.0")
    old = (tmp_path / "old" / "pack.yaml").stat()
    os.utime(tmp_path / "new" / "pack.yaml", ns=(old.st_atime_ns, old.st_mtime_ns))
    (tmp_path / "old").rename(tmp_path / "p")
    assert load_pack(tmp_path / "p").version == "1.0.0"
    (tmp_path / "p").rename(tmp_path / "gone")
    (tmp_path / "new").rename(tmp_path / "p")
    assert load_pack(tmp_path / "p").version == "2.0.0"



def test_a_same_size_rewrite_with_the_old_mtime_reloads(tmp_path, parses):
    manifest = write_pack(tmp_path / "p", version="1.0.0") / "pack.yaml"
    before = manifest.stat()
    assert load_pack(tmp_path / "p").version == "1.0.0"
    manifest.write_text(manifest.read_text().replace("1.0.0", "2.0.0"))
    os.utime(manifest, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = manifest.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert load_pack(tmp_path / "p").version == "2.0.0"
    assert len(parses) == 2

def test_a_cached_failure_keeps_its_cause(tmp_path):
    write_pack(tmp_path / "p", x={"cmd": "echo {{ x }}"})
    for _ in range(2):
        with pytest.raises(PackError) as caught:
            load_pack(tmp_path / "p")
        assert caught.value.__cause__ is not None


def test_a_manifest_that_is_not_utf8_is_a_pack_error(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "pack.yaml").write_bytes(b"name: d\xe9mo\nversion: 1.0.0\n")
    with pytest.raises(PackError, match="not UTF-8"):
        load_pack(tmp_path / "p")


def test_a_broken_manifest_that_gets_fixed_loads(tmp_path, parses):
    write_pack(tmp_path / "p", x={"cmd": "echo {{ x }}"})
    for _ in range(2):
        with pytest.raises(PackError, match="template markers"):
            load_pack(tmp_path / "p")
    assert len(parses) == 1
    write_pack(tmp_path / "p")
    assert sorted(load_pack(tmp_path / "p").adapters) == ["echo"]
    assert len(parses) == 2


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

import shutil
import subprocess

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from armature.cli import app
from armature.packs import PACKS_PATH_ENV
from armature.server.app import routes_packs
from armature.server.db import make_engine, migrate
from armature.server.db.schema import audit, packs
from tests.packs.support import HELLO, write_pack
from tests.server.app_support import Clock, add_user, login, make_client


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(PACKS_PATH_ENV, raising=False)


@pytest.fixture
def server_config(tmp_path):
    url = f"sqlite:///{tmp_path / 'server.db'}"
    migrate(url)
    config = tmp_path / "server.yaml"
    config.write_text(f"database_url: {url}\npacks_dir: {tmp_path / 'installed'}\n")
    return config, url, tmp_path / "installed"


def _cli(config, *args):
    return CliRunner().invoke(app, ["server", "pack", *args, "--config", str(config)])


def _rows(url, table):
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(select(table)).all()
    finally:
        engine.dispose()


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(HELLO, repo)
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v1")
    return repo


def test_install_from_a_directory(server_config):
    config, url, installed = server_config
    result = _cli(config, "install", str(HELLO))
    assert result.exit_code == 0, result.output
    assert (installed / "hello" / "pack.yaml").is_file()
    assert (installed / "hello" / "adapters" / "greet.py").is_file()
    [row] = _rows(url, packs)
    assert (row.name, row.version, row.source, row.installed_by) == ("hello", "1.0.0", str(HELLO), "cli")
    assert row.pinned_ref.startswith("sha256:") and len(row.pinned_ref) == 71
    [entry] = _rows(url, audit)
    assert entry.action == "pack.install" and entry.target_id == row.id and entry.detail["version"] == "1.0.0"


def test_install_from_git_pinned_to_a_sha(server_config, repo):
    config, url, installed = server_config
    sha = _git(repo, "rev-parse", "HEAD")
    result = _cli(config, "install", f"{repo}@{sha}")
    assert result.exit_code == 0, result.output
    assert not (installed / "hello" / ".git").exists()
    [row] = _rows(url, packs)
    assert row.pinned_ref == sha and row.source == str(repo)


@pytest.mark.parametrize("ref", ["main", "abc1234", "HEAD"])
def test_git_install_needs_a_full_sha(server_config, repo, ref):
    config, url, installed = server_config
    result = _cli(config, "install", f"{repo}@{ref}")
    assert result.exit_code == 1 and "40-character commit sha" in result.output
    assert _rows(url, packs) == [] and not installed.exists()


def test_git_install_of_an_unknown_sha_fails(server_config, repo):
    config, url, _ = server_config
    result = _cli(config, "install", f"{repo}@{'0' * 40}")
    assert result.exit_code == 1 and "git failed" in result.output
    assert _rows(url, packs) == []


def test_downgrade_is_refused_without_force(server_config, repo):
    config, url, installed = server_config
    first = _git(repo, "rev-parse", "HEAD")
    assert _cli(config, "install", f"{repo}@{first}").exit_code == 0
    manifest = repo / "pack.yaml"
    manifest.write_text(manifest.read_text().replace("version: 1.0.0", "version: 0.9.0"))
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "older")
    older = _git(repo, "rev-parse", "HEAD")

    refused = _cli(config, "install", f"{repo}@{older}")
    assert refused.exit_code == 1 and "downgrade" in refused.output
    assert _rows(url, packs)[0].version == "1.0.0"
    assert "version: 1.0.0" in (installed / "hello" / "pack.yaml").read_text()

    forced = _cli(config, "install", f"{repo}@{older}", "--force")
    assert forced.exit_code == 0, forced.output
    [row] = _rows(url, packs)
    assert (row.version, row.pinned_ref) == ("0.9.0", older)
    assert "version: 0.9.0" in (installed / "hello" / "pack.yaml").read_text()
    assert [e.detail["previous_version"] for e in _rows(url, audit)] == [None, "1.0.0"]


def test_install_refuses_a_templated_cmd_and_symlinks(server_config, tmp_path):
    config, url, installed = server_config
    write_pack(tmp_path / "bad", x={"cmd": "echo {{ x }}"})
    result = _cli(config, "install", str(tmp_path / "bad"))
    assert result.exit_code == 1 and "template markers" in result.output
    linked = write_pack(tmp_path / "linked", name="linked")
    (linked / "secret").symlink_to(tmp_path / "server.yaml")
    result = _cli(config, "install", str(linked))
    assert result.exit_code == 1 and "symlink" in result.output
    assert _rows(url, packs) == [] and not list(installed.iterdir())


def test_remove(server_config):
    config, url, installed = server_config
    assert _cli(config, "install", str(HELLO)).exit_code == 0
    result = _cli(config, "remove", "hello")
    assert result.exit_code == 0 and "Removed hello 1.0.0" in result.output
    assert _rows(url, packs) == [] and not (installed / "hello").exists()
    assert [e.action for e in _rows(url, audit)] == ["pack.install", "pack.remove"]
    assert _cli(config, "remove", "hello").exit_code == 1
    assert _cli(config, "remove", "../x").exit_code == 1


def _client(sqlite_engine, keyring, clock, packs_dir):
    client = make_client(sqlite_engine, keyring, clock, packs_dir=str(packs_dir))
    if not any(getattr(route, "path", None) == "/api/packs" for route in client.app.routes):
        client.app.include_router(routes_packs.router)
    return client


def test_routes_need_a_session(sqlite_engine, keyring, tmp_path):
    client = _client(sqlite_engine, keyring, Clock(), tmp_path / "installed")
    assert client.get("/api/packs").status_code == 401
    assert client.get("/api/packs/hello").status_code == 401


def test_routes_list_packs_adapters_and_errors(sqlite_engine, keyring, tmp_path, monkeypatch):
    clock = Clock()
    installed = tmp_path / "installed"
    shutil.copytree(HELLO, installed / "hello")
    write_pack(installed / "broken", name="broken", x={"cmd": "echo {{ x }}"})
    write_pack(tmp_path / "env" / "hello", name="hello", version="5.0.0")
    monkeypatch.setenv(PACKS_PATH_ENV, str(tmp_path / "env"))
    client = _client(sqlite_engine, keyring, clock, installed)
    _, secret = add_user(sqlite_engine, keyring, clock, "viewer")
    assert login(client, "viewer", clock.code(secret)).status_code == 200

    body = client.get("/api/packs").json()
    [pack] = body["packs"]
    assert (pack["name"], pack["version"], pack["source"], pack["pinned_ref"]) == ("hello", "5.0.0", None, None)
    errors = {e["pack"]: e["message"] for e in body["errors"]}
    assert set(errors) == {"broken", "hello"}
    assert "template markers" in errors["broken"] and "duplicate pack 'hello'" in errors["hello"]
    assert str(tmp_path) not in str(body)

    monkeypatch.delenv(PACKS_PATH_ENV)
    body = client.get("/api/packs").json()
    [pack] = body["packs"]
    greet = next(a for a in pack["adapters"] if a["ref"] == "hello/greet")
    assert greet["params"]["required"] == ["name"] and greet["timeout"] == 30
    assert greet["writes"] is False and greet["secrets"] == [] and greet["outputs"]["type"] == "object"
    assert client.get("/api/packs/hello").json()["pack"]["name"] == "hello"
    assert client.get("/api/packs/nope").status_code == 404


def test_routes_show_the_install_record_and_see_new_installs(sqlite_engine, keyring, tmp_path):
    clock = Clock()
    installed = tmp_path / "installed"
    client = _client(sqlite_engine, keyring, clock, installed)
    _, secret = add_user(sqlite_engine, keyring, clock, "viewer")
    assert login(client, "viewer", clock.code(secret)).status_code == 200
    assert client.get("/api/packs").json() == {"packs": [], "errors": []}

    from armature.server.packs import install
    install(sqlite_engine, installed, str(HELLO), now=clock())
    pack = client.get("/api/packs/hello").json()["pack"]
    assert pack["source"] == HELLO.name and pack["pinned_ref"].startswith("sha256:")


def test_server_catalog_is_the_shared_catalog_type(tmp_path):
    from armature.packs import Catalog
    from armature.server import packs as server_packs
    shutil.copytree(HELLO, tmp_path / "installed" / "hello")
    cat = server_packs.catalog(tmp_path / "installed")
    assert isinstance(cat, Catalog) and cat.get("hello/greet") is not None
    assert server_packs.catalog(tmp_path / "installed") is cat

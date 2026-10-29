"""The first-run setup app: loopback only, a one-time token, the database and admin steps, and the swap."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from armature.cli import app as cli_app
from armature.server.app import create_app
from armature.server.app.routes_setup import Swappable, can_swap, create_setup_app, setup_origin
from armature.server.config import ServerConfig, load_config
from armature.server.crypto import keyring_from_source
from armature.server.db import make_engine
from armature.server.db.schema import users
from armature.server.setup import needs_setup, prepare
from armature.server.users import create_admin

ORIGIN = "http://127.0.0.1:8098"
TOKEN = "t" * 43
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("ARMATURE_SERVER_DATABASE_URL", "ARMATURE_SERVER_MASTER_KEY", "ARMATURE_SERVER_REQUIRE_TOTP"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def config(tmp_path):
    return tmp_path / ".armature" / "server.yaml"


class Finish:
    def __init__(self, swap: bool = True):
        self.swap = swap
        self.seen = []

    async def __call__(self, cfg: ServerConfig) -> bool:
        self.seen.append(cfg)
        return self.swap


def client_for(config, finish=None, *, peer=("127.0.0.1", 50000), base_url=ORIGIN) -> TestClient:
    app = create_setup_app(config, origin=ORIGIN, token=TOKEN, finish=finish or Finish())
    return TestClient(app, base_url=base_url, client=peer, follow_redirects=False)


def headers(token=TOKEN):
    return {"X-Setup-Token": token, "Origin": ORIGIN}


def setup_database(client):
    response = client.post("/setup/api/database", json={"url": None}, headers=headers())
    assert response.status_code == 200, response.text
    return response.json()


def test_page_and_assets_are_served_to_loopback(config):
    client = client_for(config)
    page = client.get("/setup")
    assert page.status_code == 200 and "Set up Armature" in page.text
    assert client.get("/static/setup.js").status_code == 200
    assert client.get("/static/app.js").status_code == 404
    assert client.get("/api/session").status_code == 404
    assert client.get("/").status_code == 404


@pytest.mark.parametrize("peer", [("10.0.0.5", 50000), ("192.168.1.9", 50000), ("testclient", 50000)])
def test_a_remote_peer_sees_nothing(config, peer):
    client = client_for(config, peer=peer)
    assert client.get("/setup").status_code == 404
    assert client.get("/setup/api/state", headers=headers()).status_code == 404
    assert client.post("/setup/api/database", json={}, headers=headers()).status_code == 404


def test_a_forwarded_header_does_not_make_a_peer_loopback(config):
    client = client_for(config, peer=("10.0.0.5", 50000))
    response = client.get("/setup/api/state", headers={**headers(), "X-Real-IP": "127.0.0.1",
                                                      "X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 404


def test_another_host_name_sees_nothing(config):
    client = client_for(config, base_url="http://rebound.example:8098")
    assert client.get("/setup").status_code == 404


def test_the_api_needs_the_token(config):
    client = client_for(config)
    assert client.get("/setup/api/state").status_code == 403
    assert client.get("/setup/api/state", headers=headers("wrong")).status_code == 403
    assert client.get("/setup/api/state", headers=headers(TOKEN[:-1])).status_code == 403
    state = client.get("/setup/api/state", headers=headers())
    assert state.status_code == 200
    assert state.json()["step"] == "database" and state.json()["config_exists"] is False


def test_a_post_needs_this_origin(config):
    client = client_for(config)
    response = client.post("/setup/api/database", json={}, headers={**headers(), "Origin": "http://evil.test"})
    assert response.status_code == 403
    assert not config.exists()


def test_database_test_refuses_what_is_not_a_database(config):
    client = client_for(config)
    for url in ("not a url", "http://example.com/x", "oracle://u:p@h/db"):
        response = client.post("/setup/api/database/test", json={"url": url}, headers=headers())
        assert response.status_code == 400
        assert "p@h" not in response.text
    ok = client.post("/setup/api/database/test", json={"url": f"sqlite:///{config.parent / 'probe.db'}"},
                     headers=headers())
    assert ok.status_code == 200


def test_database_step_writes_config_migrates_and_makes_the_key(config, home):
    client = client_for(config)
    saved = setup_database(client)
    assert saved["new_key"] is True
    key = home / ".armature" / "master.key"
    assert saved["key_file"] == str(key) and key.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o600
    assert key.read_text().strip() not in str(saved)
    assert key.read_text().strip() not in client.get("/setup/api/state", headers=headers()).text
    assert client.get("/setup/api/state", headers=headers()).json()["step"] == "admin"
    assert needs_setup(load_config(config))


def test_database_step_takes_a_url(config, tmp_path):
    url = f"sqlite:///{tmp_path / 'other.db'}"
    client = client_for(config)
    response = client.post("/setup/api/database", json={"url": url}, headers=headers())
    assert response.status_code == 200, response.text
    assert load_config(config).database_url == url
    assert (tmp_path / "other.db").exists()


def test_admin_step_creates_the_admin_signs_in_and_ends_setup(config):
    finish = Finish(swap=True)
    client = client_for(config, finish)
    assert client.post("/setup/api/admin", json={"username": "ada", "password": PASSWORD},
                       headers=headers()).status_code == 409
    setup_database(client)
    short = client.post("/setup/api/admin", json={"username": "ada", "password": "short"}, headers=headers())
    assert short.status_code == 400 and "12" in short.json()["error"]
    created = client.post("/setup/api/admin", json={"username": "Ada", "password": PASSWORD}, headers=headers())
    assert created.status_code == 200, created.text
    assert created.json() == {"next": "/#/setup", "sign_in": None}
    assert "armature-local=" in created.headers["set-cookie"]
    assert len(finish.seen) == 1 and finish.seen[0].database_url == load_config(config).database_url
    engine = make_engine(load_config(config).database_url)
    with engine.connect() as conn:
        row = conn.execute(select(users)).one()
    engine.dispose()
    assert row.username == "ada" and row.is_admin and row.totp_secret_enc is None
    assert not needs_setup(load_config(config))
    for path in ("/setup", "/static/setup.js"):
        assert client.get(path).status_code == 404
    assert client.get("/setup/api/state", headers=headers()).status_code == 404
    again = client.post("/setup/api/admin", json={"username": "eve", "password": PASSWORD}, headers=headers())
    assert again.status_code == 404


def test_admin_step_without_a_swap_points_to_sign_in(config):
    client = client_for(config, Finish(swap=False))
    setup_database(client)
    created = client.post("/setup/api/admin", json={"username": "ada", "password": PASSWORD}, headers=headers())
    assert created.json() == {"next": None, "sign_in": f"{ORIGIN}/login"}
    assert "set-cookie" not in created.headers


def test_setup_shuts_off_when_an_admin_appears_elsewhere(config):
    client = client_for(config)
    setup_database(client)
    cfg = load_config(config)
    engine = make_engine(cfg.database_url)
    with engine.begin() as conn:
        create_admin(conn, keyring_from_source(cfg.master_key), username="cli", password=PASSWORD,
                     require_totp=False)
    engine.dispose()
    assert client.get("/setup/api/state", headers=headers()).status_code == 404
    assert client.post("/setup/api/admin", json={"username": "eve", "password": PASSWORD},
                       headers=headers()).status_code == 404


def test_admin_step_refuses_when_codes_are_required(config, monkeypatch):
    client = client_for(config)
    setup_database(client)
    monkeypatch.setenv("ARMATURE_SERVER_REQUIRE_TOTP", "true")
    response = client.post("/setup/api/admin", json={"username": "ada", "password": PASSWORD}, headers=headers())
    assert response.status_code == 409 and "create-admin" in response.json()["error"]


def test_swap_serves_the_real_app_and_runs_its_lifespan(config, monkeypatch):
    monkeypatch.setenv("ARMATURE_SERVER_EVENTS_SOCKET", "")
    holder = None

    async def finish(cfg: ServerConfig) -> bool:
        assert can_swap(cfg, ORIGIN)
        real = create_app(cfg, make_engine(cfg.database_url), keyring_from_source(cfg.master_key),
                          worker_interval=None)
        await holder.swap(real)
        return True

    holder = Swappable(create_setup_app(config, origin=ORIGIN, token=TOKEN, finish=finish))
    with TestClient(holder, base_url=ORIGIN, client=("127.0.0.1", 50000), follow_redirects=False) as client:
        assert client.get("/api/session").status_code == 404
        setup_database(client)
        created = client.post("/setup/api/admin", json={"username": "ada", "password": PASSWORD}, headers=headers())
        assert created.status_code == 200, created.text
        real = holder.app
        assert real.state.live.running
        session = client.get("/api/session").json()
        assert session["user"]["username"] == "ada" and session["user"]["is_admin"]
        assert client.get("/").status_code == 200
        assert client.get("/setup").status_code == 404
    assert not real.state.live.running


def test_can_swap_only_on_the_setup_origin():
    origin = setup_origin(ServerConfig())
    assert origin == ORIGIN
    assert can_swap(ServerConfig(), origin)
    assert not can_swap(ServerConfig(public_origin="https://arm.example.com"), origin)
    assert not can_swap(ServerConfig(port=9000, public_origin="http://127.0.0.1:9000"), origin)
    assert not can_swap(ServerConfig(host="0.0.0.0", allow_public_bind=True), origin)


def test_prepare_is_shared_with_init(config, home):
    prepared = prepare(config)
    assert prepared.wrote_config and prepared.new_key == home / ".armature" / "master.key"
    again = prepare(config)
    assert not again.wrote_config and again.new_key is None and not again.has_admin


def _serve(monkeypatch, interactive=True):
    seen = {}
    monkeypatch.setattr("armature.server.cli._interactive", lambda: interactive)

    def fake_run(self):
        seen.update(app=self.config.app, host=self.config.host, port=self.config.port)

    monkeypatch.setattr("uvicorn.Server.run", fake_run)
    return seen


def test_run_with_no_admin_serves_setup_on_loopback(config, monkeypatch):
    seen = _serve(monkeypatch)
    result = CliRunner().invoke(cli_app, ["server", "run", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert isinstance(seen["app"], Swappable) and seen["host"] == "127.0.0.1" and seen["port"] == 8098
    assert "/setup#" not in result.stdout
    link = next(line.strip() for line in result.stderr.splitlines() if "/setup#" in line)
    assert link.startswith(f"{ORIGIN}/setup#") and len(link.split("#", 1)[1]) >= 40
    assert not config.exists()


def test_run_setup_stays_on_loopback_with_a_public_host(config, monkeypatch, tmp_path):
    config.parent.mkdir(parents=True)
    config.write_text("host: 0.0.0.0\nallow_public_bind: true\npublic_origin: https://arm.example.com\n"
                      f"database_url: sqlite:///{tmp_path / 'db.sqlite'}\n")
    seen = _serve(monkeypatch)
    result = CliRunner().invoke(cli_app, ["server", "run", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert seen["host"] == "127.0.0.1"


def test_run_with_codes_required_still_asks_for_create_admin(config, monkeypatch):
    monkeypatch.setenv("ARMATURE_SERVER_REQUIRE_TOTP", "true")
    seen = _serve(monkeypatch)
    result = CliRunner().invoke(cli_app, ["server", "run", "--config", str(config)])
    assert result.exit_code == 1 and "create-admin" in result.output
    assert not seen


def test_run_without_a_terminal_prints_no_link(config, monkeypatch):
    seen = _serve(monkeypatch, interactive=False)
    result = CliRunner().invoke(cli_app, ["server", "run", "--config", str(config)])
    assert result.exit_code == 1
    assert "in a terminal to set up" in result.output and "/setup#" not in result.output
    assert not seen


def test_two_admin_requests_at_once_make_one_admin(config):
    finish = Finish(swap=False)
    setup_database(client_for(config, finish))
    app = create_setup_app(config, origin=ORIGIN, token=TOKEN, finish=finish)

    async def both():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            return await asyncio.gather(*(client.post("/setup/api/admin", headers=headers(),
                                                      json={"username": name, "password": PASSWORD})
                                          for name in ("ada", "eve")))

    statuses = sorted(response.status_code for response in asyncio.run(both()))
    assert statuses[0] == 200 and statuses[1] in (404, 409)
    engine = make_engine(load_config(config).database_url)
    with engine.connect() as conn:
        assert len(conn.execute(select(users)).all()) == 1
    engine.dispose()
    assert len(finish.seen) == 1

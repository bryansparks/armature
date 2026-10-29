from urllib.parse import urlparse

import pytest
from sqlalchemy import select

from armature.server.config import ServerConfig, load_config
from armature.server.db.schema import users
from tests.server.app_support import (
    PASSWORD,
    Clock,
    add_user,
    login,
    make_client,
    mutation,
    sign_in_as,
)

NEW_PASSWORD = "a long new password"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(sqlite_engine, keyring, clock):
    return make_client(sqlite_engine, keyring, clock, require_totp=False)


def test_require_totp_is_off_by_default_and_env_turns_it_on(tmp_path):
    assert ServerConfig().require_totp is False
    missing = tmp_path / "none.yaml"
    assert load_config(missing, env={}).require_totp is False
    assert load_config(missing, env={"ARMATURE_SERVER_REQUIRE_TOTP": "true"}).require_totp is True


def test_session_reports_totp_off(client):
    assert client.get("/api/session").json()["auth"] == {"password": True, "oidc": False, "totp": False}


@pytest.mark.parametrize("confirmed", [True, False])
def test_password_alone_signs_in_and_any_code_is_ignored(sqlite_engine, keyring, clock, client, confirmed):
    user_id, _ = add_user(sqlite_engine, keyring, clock, "ana", confirmed=confirmed)
    response = login(client, "ana", None)
    assert response.status_code == 200, response.text
    assert client.get("/api/session").json()["user"]["id"] == user_id
    assert client.post("/api/logout", headers=mutation(response.json()["csrf"])).status_code == 200
    assert login(client, "ana", "000000").status_code == 200


def test_wrong_password_or_disabled_user_still_refused(sqlite_engine, keyring, clock, client):
    add_user(sqlite_engine, keyring, clock, "ana")
    add_user(sqlite_engine, keyring, clock, "off", disabled=True)
    response = login(client, "ana", None, password="wrong password here")
    assert response.status_code == 401
    assert response.json() == {"error": "Sign-in failed. Check the username and password."}
    assert login(client, "off", None).status_code == 401


def test_password_throttle_still_applies(sqlite_engine, keyring, clock, client):
    add_user(sqlite_engine, keyring, clock, "ana")
    codes = [login(client, "ana", None, password="wrong password here").status_code for _ in range(4)]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429


def test_step_up_route_needs_no_step_up(sqlite_engine, keyring, clock, client):
    admin_id, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, admin_id, step_up=False)
    response = client.post("/api/secrets", json={"name": "K", "value": "v"}, headers=mutation(csrf))
    assert response.status_code == 200, response.text


def test_step_up_route_still_needs_csrf(sqlite_engine, keyring, clock, client):
    admin_id, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    sign_in_as(client, sqlite_engine, keyring, clock, admin_id, step_up=False)
    response = client.post("/api/secrets", json={"name": "K", "value": "v"}, headers=mutation("forged"))
    assert response.status_code == 403


def test_invite_acceptance_needs_no_enrollment(sqlite_engine, keyring, clock, client):
    admin_id, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, admin_id, step_up=False)
    created = client.post("/api/users", json={"username": "bea"}, headers=mutation(csrf))
    assert created.status_code == 200, created.text
    user_id, token = created.json()["user"]["id"], urlparse(created.json()["invite_url"]).fragment

    guest = make_client(sqlite_engine, keyring, clock, require_totp=False)
    guest_csrf = guest.get("/api/session").json()["csrf"]
    shown = guest.post("/api/invite/inspect", json={"token": token}, headers=mutation(guest_csrf))
    assert shown.status_code == 200 and shown.json() == {"username": "bea", "otpauth_uri": None}
    done = guest.post("/api/invite/accept", json={"token": token, "password": NEW_PASSWORD},
                      headers=mutation(guest_csrf))
    assert done.status_code == 200, done.text
    with sqlite_engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.id == user_id)).one()
    assert row.totp_secret_enc is None and row.password_hash is not None

    again = make_client(sqlite_engine, keyring, clock, require_totp=False)
    again_csrf = again.get("/api/session").json()["csrf"]
    assert again.post("/api/invite/inspect", json={"token": token}, headers=mutation(again_csrf)).status_code == 401
    assert login(again, "bea", None, password=NEW_PASSWORD).status_code == 200

    listed = client.get("/api/users").json()
    bea = next(u for u in (listed["users"] if isinstance(listed, dict) else listed) if u["id"] == user_id)
    assert bea["has_password"] is True
    reset = client.post(f"/api/users/{user_id}/invite", json={}, headers=mutation(csrf))
    assert reset.status_code == 200
    assert login(make_client(sqlite_engine, keyring, clock, require_totp=False), "bea", None,
                 password=NEW_PASSWORD).status_code == 401


def test_totp_mode_still_refuses_password_alone(sqlite_engine, keyring, clock):
    add_user(sqlite_engine, keyring, clock, "ana")
    strict = make_client(sqlite_engine, keyring, clock, require_totp=True)
    assert login(strict, "ana", None, password=PASSWORD).status_code == 401

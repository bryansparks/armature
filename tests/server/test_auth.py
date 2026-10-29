import pytest
from sqlalchemy import func, select

from armature.server.auth import backoff_delay, ip_bucket
from armature.server.db.schema import audit, sessions
from tests.server.app_support import (
    ORIGIN,
    PASSWORD,
    Clock,
    add_user,
    login,
    make_client,
    mutation,
    sign_in_as,
)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(sqlite_engine, keyring, clock):
    return make_client(sqlite_engine, keyring, clock)


def test_signed_out_session_shape(client):
    body = client.get("/api/session").json()
    assert body["user"] is None and body["memberships"] == [] and body["sso"] is False
    assert body["auth"] == {"password": True, "oidc": False}
    assert isinstance(body["csrf"], str) and len(body["csrf"]) == 64


def test_login_sets_strict_secure_cookie_and_session(sqlite_engine, keyring, clock, client):
    user_id, secret = add_user(sqlite_engine, keyring, clock, "ana")
    response = login(client, "ana", clock.code(secret))
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Secure-armature=")
    for part in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert part in cookie
    body = client.get("/api/session").json()
    assert body["user"]["id"] == user_id and body["sso"] is False
    assert body["csrf"] == response.json()["csrf"]
    with sqlite_engine.connect() as conn:
        stored = conn.execute(select(sessions.c.token_hmac)).scalar_one()
    assert cookie.split(";")[0].split("=", 1)[1] != stored


def test_localhost_dev_origin_drops_secure(sqlite_engine, keyring, clock):
    dev = make_client(sqlite_engine, keyring, clock, public_origin="http://localhost:8098")
    _, secret = add_user(sqlite_engine, keyring, clock, "ana")
    csrf = dev.get("/api/session").json()["csrf"]
    response = dev.post("/api/login", json={"username": "ana", "password": PASSWORD, "totp": clock.code(secret)},
                        headers={"Origin": "http://localhost:8098", "X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.headers["set-cookie"].startswith("armature-local=")
    assert "Secure" not in response.headers["set-cookie"]
    assert "strict-transport-security" not in response.headers


def test_hsts_on_a_public_origin(client):
    assert client.get("/health").headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.parametrize("case", ["no_totp", "wrong_totp", "wrong_password", "unknown_user", "disabled"])
def test_login_failures_are_generic(sqlite_engine, keyring, clock, client, case):
    _, secret = add_user(sqlite_engine, keyring, clock, "ana", disabled=case == "disabled")
    username, password, code = "ana", PASSWORD, clock.code(secret)
    if case == "no_totp":
        code = None
    elif case == "wrong_totp":
        code = "000000" if code != "000000" else "111111"
    elif case == "wrong_password":
        password = "not the password at all"
    elif case == "unknown_user":
        username = "nobody"
    response = login(client, username, code, password=password)
    assert response.status_code == 401
    assert response.json() == {"error": "Sign-in failed. Check the username, password, and code."}


def test_login_needs_origin_and_pre_session_csrf(sqlite_engine, keyring, clock, client):
    _, secret = add_user(sqlite_engine, keyring, clock, "ana")
    body = {"username": "ana", "password": PASSWORD, "totp": clock.code(secret)}
    csrf = client.get("/api/session").json()["csrf"]
    assert client.post("/api/login", json=body, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/api/login", json=body,
                       headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf}).status_code == 403
    assert client.post("/api/login", json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": "x" * 64}).status_code == 403
    assert client.post("/api/login", json=body, headers=mutation(csrf)).status_code == 200


def test_mutations_need_origin_and_csrf(sqlite_engine, keyring, clock, client):
    admin_id, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, admin_id)
    assert client.post("/api/teams", json={"name": "a"}).status_code == 403
    assert client.post("/api/teams", json={"name": "a"}, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/api/teams", json={"name": "a"},
                       headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf}).status_code == 403
    assert client.post("/api/teams", json={"name": "a"},
                       headers={"Origin": ORIGIN, "X-CSRF-Token": csrf + "x"}).status_code == 403
    assert client.post("/api/teams", json={"name": "a"}, headers=mutation(csrf)).status_code == 200


def test_totp_replay_rejected(sqlite_engine, keyring, clock, client):
    _, secret = add_user(sqlite_engine, keyring, clock, "ana")
    code = clock.code(secret)
    first = login(client, "ana", code)
    assert first.status_code == 200
    assert client.post("/api/logout", headers=mutation(first.json()["csrf"])).status_code == 200
    assert login(client, "ana", code).status_code == 401
    clock.advance(seconds=30)
    assert login(client, "ana", clock.code(secret)).status_code == 200


def test_step_up_replay_rejected_and_window(sqlite_engine, keyring, clock, client):
    _, secret = add_user(sqlite_engine, keyring, clock, "ana", is_admin=True)
    csrf = login(client, "ana", clock.code(secret)).json()["csrf"]
    code = clock.code(secret)
    assert client.post("/api/step-up", json={"password": PASSWORD, "totp": code},
                       headers=mutation(csrf)).status_code == 401
    clock.advance(minutes=6)
    response = client.post("/api/secrets", json={"name": "K", "value": "v"}, headers=mutation(csrf))
    assert response.status_code == 403 and response.json() == {"error": "step_up_required"}
    fresh = clock.code(secret)
    assert client.post("/api/step-up", json={"password": PASSWORD, "totp": fresh},
                       headers=mutation(csrf)).status_code == 200
    assert client.post("/api/secrets", json={"name": "K", "value": "v"}, headers=mutation(csrf)).status_code == 200


@pytest.mark.parametrize("code", [None, "000000", "current"])
def test_unconfirmed_authenticator_cannot_sign_in(sqlite_engine, keyring, clock, client, code):
    _, secret = add_user(sqlite_engine, keyring, clock, "new", confirmed=False)
    response = login(client, "new", clock.code(secret) if code == "current" else code)
    assert response.status_code == 401
    assert response.json() == {"error": "Sign-in failed. Check the username, password, and code."}


def test_throttle_per_ip_then_per_user(sqlite_engine, keyring, clock):
    _, secret = add_user(sqlite_engine, keyring, clock, "ana")
    proxy = make_client(sqlite_engine, keyring, clock, peer=("127.0.0.1", 40000), trust_real_ip=True)
    from_a = {"X-Real-IP": "203.0.113.5"}
    for index in range(5):
        assert login(proxy, f"zed{index}", "123456", password="wrong password!!", headers=from_a).status_code == 401
    blocked = login(proxy, "ana", clock.code(secret), headers=from_a)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 60
    other_ip = {"X-Real-IP": "203.0.113.6"}
    assert login(proxy, "ana", clock.code(secret), headers=other_ip).status_code == 200
    for _ in range(3):
        clock.advance(seconds=1)
        login(proxy, "ana", "000000", password="wrong password!!", headers={"X-Real-IP": "198.51.100.1"})
    clock.advance(seconds=30)
    assert login(proxy, "ana", "000000", password="wrong password!!",
                 headers={"X-Real-IP": "198.51.100.2"}).status_code == 401
    assert login(proxy, "ana", clock.code(secret), headers={"X-Real-IP": "198.51.100.3"}).status_code == 429
    clock.advance(seconds=3)
    assert login(proxy, "ana", clock.code(secret), headers={"X-Real-IP": "198.51.100.3"}).status_code == 200


def test_x_real_ip_ignored_unless_trusted(sqlite_engine, keyring, clock):
    untrusted = make_client(sqlite_engine, keyring, clock, peer=("127.0.0.1", 40000))
    for index in range(5):
        login(untrusted, f"zed{index}", "123456", password="wrong password!!", headers={"X-Real-IP": f"203.0.113.{index}"})
    assert login(untrusted, "zed9", "123456", password="wrong password!!",
                 headers={"X-Real-IP": "203.0.113.99"}).status_code == 429


def test_x_real_ip_ignored_from_non_loopback_peer(sqlite_engine, keyring, clock):
    client = make_client(sqlite_engine, keyring, clock, trust_real_ip=True)
    for index in range(5):
        login(client, f"zed{index}", "123456", password="wrong password!!", headers={"X-Real-IP": f"203.0.113.{index}"})
    assert login(client, "zed", "123456", password="wrong password!!",
                 headers={"X-Real-IP": "203.0.113.99"}).status_code == 429


def test_ip_bucket_and_backoff():
    assert ip_bucket("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2::/64"
    assert ip_bucket("2001:db8:1:2::9") == ip_bucket("2001:db8:1:2:ffff::1")
    assert ip_bucket("::ffff:192.0.2.1") == "192.0.2.1"
    assert ip_bucket("192.0.2.1") == "192.0.2.1"
    assert [backoff_delay(n).total_seconds() for n in (0, 2, 3, 4, 30)] == [0, 0, 1, 2, 900]


def test_session_idle_expiry(sqlite_engine, keyring, clock, client):
    user_id, _ = add_user(sqlite_engine, keyring, clock, "ana")
    sign_in_as(client, sqlite_engine, keyring, clock, user_id)
    clock.advance(minutes=29)
    assert client.get("/api/teams").status_code == 200
    clock.advance(minutes=29)
    assert client.get("/api/teams").status_code == 200
    clock.advance(minutes=31)
    assert client.get("/api/teams").status_code == 401
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(sessions)).scalar_one() == 0


def test_session_absolute_expiry(sqlite_engine, keyring, clock, client):
    user_id, _ = add_user(sqlite_engine, keyring, clock, "ana")
    sign_in_as(client, sqlite_engine, keyring, clock, user_id)
    for _ in range(35):
        clock.advance(minutes=20)
        assert client.get("/api/teams").status_code == 200
    clock.advance(minutes=21)
    assert client.get("/api/teams").status_code == 401


def test_password_change_ends_other_sessions(sqlite_engine, keyring, clock):
    user_id, secret = add_user(sqlite_engine, keyring, clock, "ana")
    one = make_client(sqlite_engine, keyring, clock)
    two = make_client(sqlite_engine, keyring, clock)
    csrf = login(one, "ana", clock.code(secret)).json()["csrf"]
    sign_in_as(two, sqlite_engine, keyring, clock, user_id)
    assert one.post("/api/account/password", json={"current": "wrong password!!", "new": "a brand new password"},
                    headers=mutation(csrf)).status_code == 401
    assert one.post("/api/account/password", json={"current": PASSWORD, "new": "short"},
                    headers=mutation(csrf)).status_code == 400
    assert one.post("/api/account/password", json={"current": PASSWORD, "new": "a brand new password"},
                    headers=mutation(csrf)).status_code == 200
    assert one.get("/api/teams").status_code == 200
    assert two.get("/api/teams").status_code == 401
    clock.advance(seconds=30)
    assert login(one, "ana", clock.code(secret), password="a brand new password").status_code == 200


def test_last_admin_guard_and_sessions_end_on_role_change(sqlite_engine, keyring, clock):
    root_id, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    admin = make_client(sqlite_engine, keyring, clock)
    csrf = sign_in_as(admin, sqlite_engine, keyring, clock, root_id)
    response = admin.post(f"/api/users/{root_id}", json={"is_admin": False}, headers=mutation(csrf))
    assert response.status_code == 409
    assert admin.post(f"/api/users/{root_id}", json={"disabled": True}, headers=mutation(csrf)).status_code == 409
    other_id, _ = add_user(sqlite_engine, keyring, clock, "other", is_admin=True)
    victim = make_client(sqlite_engine, keyring, clock)
    sign_in_as(victim, sqlite_engine, keyring, clock, other_id)
    assert admin.post(f"/api/users/{other_id}", json={"is_admin": False}, headers=mutation(csrf)).status_code == 200
    assert victim.get("/api/teams").status_code == 401
    assert admin.post(f"/api/users/{root_id}", json={"is_admin": False}, headers=mutation(csrf)).status_code == 409


def test_disable_ends_sessions(sqlite_engine, keyring, clock):
    root_id, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    user_id, secret = add_user(sqlite_engine, keyring, clock, "ana")
    admin = make_client(sqlite_engine, keyring, clock)
    csrf = sign_in_as(admin, sqlite_engine, keyring, clock, root_id)
    user = make_client(sqlite_engine, keyring, clock)
    sign_in_as(user, sqlite_engine, keyring, clock, user_id)
    assert admin.post(f"/api/users/{user_id}", json={"disabled": True}, headers=mutation(csrf)).status_code == 200
    assert user.get("/api/teams").status_code == 401
    assert login(user, "ana", clock.code(secret)).status_code == 401


def test_every_login_and_mutation_is_audited(sqlite_engine, keyring, clock, client):
    _, secret = add_user(sqlite_engine, keyring, clock, "ana", is_admin=True)
    login(client, "ana", "000000")
    csrf = login(client, "ana", clock.code(secret)).json()["csrf"]
    client.post("/api/teams", json={"name": "mobile"}, headers=mutation(csrf))
    client.post("/api/logout", headers=mutation(csrf))
    with sqlite_engine.connect() as conn:
        rows = conn.execute(select(audit.c.action, audit.c.detail).order_by(audit.c.at)).all()
    actions = [r.action for r in rows]
    assert actions.count("login") == 2 and "team.create" in actions and "logout" in actions
    assert PASSWORD not in repr(rows)

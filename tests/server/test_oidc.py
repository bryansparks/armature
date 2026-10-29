"""OIDC sign-in against a fake issuer served through httpx.MockTransport. No network."""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey
from sqlalchemy import insert, select

from armature.server import secrets as secret_store
from armature.server.config import ServerConfig
from armature.server.db import new_id
from armature.server.db.schema import memberships, projects, sessions, teams, users
from tests.server.app_support import Clock, add_membership, add_user, make_client

ISSUER = "https://idp.test"
CLIENT_ID = "armature-client"
CLIENT_SECRET = "idp-client-secret"
OIDC = {
    "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret_ref": "OIDC_SECRET",
    "admin_groups": ["armature-admins"],
    "group_roles": {
        "mobile-devs": [{"scope_kind": "team", "scope": "mobile", "role": "contributor"}],
        "ios-leads": [{"scope_kind": "project", "scope": "mobile/ios", "role": "project_lead"}],
        "web-devs": [{"scope_kind": "team", "scope": "web", "role": "viewer"}],
    },
}


class FakeIssuer:
    def __init__(self):
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "alg": "RS256", "use": "sig"})
        self.codes: dict[str, dict] = {}
        self.issuer = ISSUER

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.issuer, "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
            })
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if path == "/token":
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, json={"error": "invalid_client"})
            form = parse_qs(request.content.decode())
            grant = self.codes.pop(form["code"][0], None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"][0].encode()).digest()).decode().rstrip("=")
            if challenge != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer",
                                             "id_token": grant["id_token"]})
        return httpx.Response(404)

    def sign(self, claims: dict, header: dict | None = None) -> str:
        return jwt.encode(header or {"alg": "RS256", "kid": "k1"}, claims, self.key)

    def claims(self, sub: str, nonce: str, groups=(), **extra) -> dict:
        now = int(time.time())
        return {"iss": ISSUER, "aud": CLIENT_ID, "sub": sub, "nonce": nonce, "iat": now, "exp": now + 300,
                "groups": list(groups), **extra}


@pytest.fixture
def idp():
    return FakeIssuer()


@pytest.fixture
def clock():
    from datetime import UTC, datetime
    return Clock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def setup(sqlite_engine, keyring, clock):
    with sqlite_engine.begin() as conn:
        secret_store.set_secret(conn, keyring, team_id=None, name="OIDC_SECRET", value=CLIENT_SECRET, actor_id=None)
        ids = {}
        for name in ("mobile", "web"):
            ids[name] = new_id()
            conn.execute(insert(teams).values(id=ids[name], name=name, created_at=clock()))
        ids["ios"] = new_id()
        conn.execute(insert(projects).values(id=ids["ios"], team_id=ids["mobile"], name="ios", created_at=clock()))
    return ids


@pytest.fixture
def client(sqlite_engine, keyring, clock, idp, setup):
    return make_client(sqlite_engine, keyring, clock, oidc=OIDC, oidc_transport=idp.transport())


def start(client):
    response = client.get("/auth/oidc/login")
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/authorize"
    params = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert params["code_challenge_method"] == "S256" and params["client_id"] == CLIENT_ID
    cookie = response.headers["set-cookie"]
    assert "SameSite=lax" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    return params


def issue(idp, params, id_token) -> str:
    code = new_id()
    idp.codes[code] = {"challenge": params["code_challenge"], "id_token": id_token}
    return code


def finish(client, code, state):
    response = client.get("/auth/oidc/callback", params={"code": code, "state": state})
    assert response.status_code == 200
    return "url=/login?error=sso" not in response.text


def sign_in(client, idp, sub="okta-1", groups=(), **extra) -> bool:
    params = start(client)
    code = issue(idp, params, idp.sign(idp.claims(sub, params["nonce"], groups, **extra)))
    return finish(client, code, params["state"])


def test_config_refuses_admin_through_group_roles():
    with pytest.raises(ValueError):
        ServerConfig(oidc={**OIDC, "group_roles": {"g": [{"scope_kind": "team", "scope": "t", "role": "admin"}]}})


def test_first_login_creates_user_with_group_roles(client, idp, setup, sqlite_engine):
    assert sign_in(client, idp, groups=["mobile-devs", "ios-leads", "unknown-group"], preferred_username="sam",
                   email="sam@corp.test", name="Sam")
    body = client.get("/api/session").json()
    assert body["user"]["username"] == "sam" and body["user"]["is_admin"] is False
    assert body["sso"] is True
    granted = {(m["scope_kind"], m["scope_id"], m["role"]) for m in body["memberships"]}
    assert granted == {("team", setup["mobile"], "contributor"), ("project", setup["ios"], "project_lead")}
    with sqlite_engine.connect() as conn:
        row = conn.execute(select(users)).one()
    assert (row.oidc_iss, row.oidc_sub, row.password_hash) == (ISSUER, "okta-1", None)
    assert client.get("/api/teams").status_code == 200


def test_bad_state_refused(client, idp):
    params = start(client)
    code = issue(idp, params, idp.sign(idp.claims("okta-1", params["nonce"])))
    assert not finish(client, code, "not-the-state")
    assert client.get("/api/session").json()["user"] is None


def test_missing_state_cookie_refused(sqlite_engine, keyring, clock, idp, setup, client):
    params = start(client)
    code = issue(idp, params, idp.sign(idp.claims("okta-1", params["nonce"])))
    other = make_client(sqlite_engine, keyring, clock, oidc=OIDC, oidc_transport=idp.transport())
    response = other.get("/auth/oidc/callback", params={"code": code, "state": params["state"]})
    assert "error=sso" in response.text


def test_replayed_code_refused(client, idp, sqlite_engine):
    params = start(client)
    state_cookie = client.cookies.get("__Secure-armature-oidc")
    code = issue(idp, params, idp.sign(idp.claims("okta-1", params["nonce"])))
    assert finish(client, code, params["state"])
    assert client.cookies.get("__Secure-armature-oidc") is None
    assert not finish(client, code, params["state"])
    client.cookies.set("__Secure-armature-oidc", state_cookie, domain="arm.test", path="/auth/oidc")
    assert not finish(client, code, params["state"])
    with sqlite_engine.connect() as conn:
        assert len(conn.execute(select(sessions)).all()) == 1


def test_wrong_nonce_refused(client, idp):
    params = start(client)
    code = issue(idp, params, idp.sign(idp.claims("okta-1", "some-other-nonce")))
    assert not finish(client, code, params["state"])


def test_foreign_issuer_refused(client, idp):
    params = start(client)
    claims = {**idp.claims("okta-1", params["nonce"]), "iss": "https://evil.test"}
    code = issue(idp, params, idp.sign(claims))
    assert not finish(client, code, params["state"])


def test_discovery_naming_another_issuer_refused(client, idp):
    idp.issuer = "https://evil.test"
    assert client.get("/auth/oidc/login").status_code == 502


def test_wrong_audience_and_expired_refused(client, idp):
    params = start(client)
    code = issue(idp, params, idp.sign({**idp.claims("okta-1", params["nonce"]), "aud": "another-client"}))
    assert not finish(client, code, params["state"])
    params = start(client)
    stale = {**idp.claims("okta-1", params["nonce"]), "exp": int(time.time()) - 3600}
    assert not finish(client, issue(idp, params, idp.sign(stale)), params["state"])


def test_none_and_hmac_algorithms_refused(client, idp):
    params = start(client)
    claims = idp.claims("okta-1", params["nonce"])
    head = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    assert not finish(client, issue(idp, params, f"{head}.{body}."), params["state"])
    params = start(client)
    claims = idp.claims("okta-1", params["nonce"])
    forged = jwt.encode({"alg": "HS256", "kid": "k1"}, claims, OctKey.generate_key(256))
    assert not finish(client, issue(idp, params, forged), params["state"])


def test_same_email_local_account_not_linked(client, idp, sqlite_engine, keyring, clock):
    local_id, _ = add_user(sqlite_engine, keyring, clock, "ana")
    with sqlite_engine.begin() as conn:
        conn.execute(users.update().where(users.c.id == local_id).values(email="ana@corp.test"))
    assert sign_in(client, idp, preferred_username="ana", email="ana@corp.test")
    me = client.get("/api/session").json()["user"]
    assert me["id"] != local_id and me["username"] is None
    with sqlite_engine.connect() as conn:
        local = conn.execute(select(users).where(users.c.id == local_id)).one()
    assert local.oidc_sub is None


def test_group_removal_drops_oidc_membership_keeps_manual(client, idp, setup, sqlite_engine, clock):
    assert sign_in(client, idp, groups=["mobile-devs", "ios-leads"])
    user_id = client.get("/api/session").json()["user"]["id"]
    add_membership(sqlite_engine, clock, user_id, "team", setup["web"], "team_lead")
    assert sign_in(client, idp, groups=["mobile-devs"])
    with sqlite_engine.connect() as conn:
        rows = {(r.scope_id, r.role, r.source) for r in conn.execute(
            select(memberships).where(memberships.c.user_id == user_id))}
    assert rows == {(setup["mobile"], "contributor", "oidc"), (setup["web"], "team_lead", "manual")}
    assert sign_in(client, idp, groups=["web-devs"])
    with sqlite_engine.connect() as conn:
        rows = {(r.scope_id, r.role, r.source) for r in conn.execute(
            select(memberships).where(memberships.c.user_id == user_id))}
    assert rows == {(setup["web"], "team_lead", "manual")}


def test_admin_only_through_admin_groups(client, idp, sqlite_engine, keyring, clock):
    add_user(sqlite_engine, keyring, clock, "local-admin", is_admin=True)
    assert sign_in(client, idp, groups=["mobile-devs"])
    assert client.get("/api/session").json()["user"]["is_admin"] is False
    assert sign_in(client, idp, groups=["armature-admins"])
    assert client.get("/api/session").json()["user"]["is_admin"] is True
    assert client.get("/api/audit").status_code == 200
    assert sign_in(client, idp, groups=[])
    assert client.get("/api/session").json()["user"]["is_admin"] is False
    assert client.get("/api/audit").status_code == 403


def test_disabled_oidc_user_refused(client, idp, sqlite_engine):
    assert sign_in(client, idp)
    with sqlite_engine.begin() as conn:
        conn.execute(users.update().values(disabled=True))
    client.cookies.clear()
    assert not sign_in(client, idp)


def start_fresh(client, return_to="#/secrets"):
    response = client.get("/auth/oidc/login", params={"step_up": "1", "return": return_to})
    params = {k: v[0] for k, v in parse_qs(urlparse(response.headers["location"]).query).items()}
    assert params["prompt"] == "login" and params["max_age"] == "0"
    return params


def test_normal_sso_sign_in_has_no_step_up(client, idp):
    from tests.server.app_support import mutation
    assert sign_in(client, idp, groups=["armature-admins"], auth_time=int(time.time()))
    csrf = client.get("/api/session").json()["csrf"]
    response = client.post("/api/secrets", json={"name": "K", "value": "v"}, headers=mutation(csrf))
    assert response.status_code == 403 and response.json() == {"error": "step_up_required"}
    assert client.post("/api/step-up", json={"password": "x", "totp": "1"}, headers=mutation(csrf)).status_code == 400


def test_fresh_sso_sign_in_steps_up_and_returns(client, idp):
    from tests.server.app_support import mutation
    assert sign_in(client, idp, groups=["armature-admins"])
    params = start_fresh(client)
    claims = idp.claims("okta-1", params["nonce"], ["armature-admins"], auth_time=int(time.time()) - 10)
    response = client.get("/auth/oidc/callback",
                          params={"code": issue(idp, params, idp.sign(claims)), "state": params["state"]})
    assert 'url=/#/secrets"' in response.text
    csrf = client.get("/api/session").json()["csrf"]
    assert client.post("/api/secrets", json={"name": "K", "value": "v"}, headers=mutation(csrf)).status_code == 200
    assert client.get("/api/secrets").text.count(CLIENT_SECRET) == 0


@pytest.mark.parametrize("auth_time", [None, -600])
def test_fresh_sso_needs_recent_auth_time(client, idp, auth_time):
    params = start_fresh(client)
    extra = {} if auth_time is None else {"auth_time": int(time.time()) + auth_time}
    claims = idp.claims("okta-1", params["nonce"], ["armature-admins"], **extra)
    assert not finish(client, issue(idp, params, idp.sign(claims)), params["state"])


def test_return_hash_is_checked(client, idp):
    params = start_fresh(client, return_to="https://evil.test/")
    claims = idp.claims("okta-1", params["nonce"], auth_time=int(time.time()))
    response = client.get("/auth/oidc/callback",
                          params={"code": issue(idp, params, idp.sign(claims)), "state": params["state"]})
    assert 'url=/"' in response.text and "evil" not in response.text


def test_username_race_retries_without_a_name(client, idp, sqlite_engine, keyring, clock, monkeypatch):
    add_user(sqlite_engine, keyring, clock, "sam")
    monkeypatch.setattr("armature.server.oidc._free_username", lambda conn, claims: "sam")
    assert sign_in(client, idp, preferred_username="sam")
    assert client.get("/api/session").json()["user"]["username"] is None


def test_last_admin_keeps_admin_when_the_group_goes(client, idp, sqlite_engine):
    from armature.server.db.schema import audit
    assert sign_in(client, idp, groups=["armature-admins"])
    assert sign_in(client, idp, groups=[])
    assert client.get("/api/session").json()["user"]["is_admin"] is True
    with sqlite_engine.connect() as conn:
        kept = conn.execute(select(audit.c.target_id).where(audit.c.action == "oidc.last_admin_kept")).scalars().all()
    assert kept == [client.get("/api/session").json()["user"]["id"]]

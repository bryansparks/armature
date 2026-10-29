"""Connections: write-only credentials, RBAC, Verify against mocked vendors, tokens, and webhooks that use them."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import insert, select, update

from armature.server import connections, ingest, vendors
from armature.server.crypto import SealError
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    github_installations,
    metadata,
    projects,
    teams,
    webhook_deliveries,
)
from armature.server.db.schema import connections as connections_table
from armature.server.vendors import VendorError, github
from tests.server.app_support import (
    ORIGIN,
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)

BOT_TOKEN = "xoxb-1111-2222-abcdefghijklmnop"
SIGNING = "slack-signing-secret-value"
HOOK_SECRET = "github-hook-secret-value"
FULL = {"contents": "write", "pull_requests": "write", "issues": "write", "checks": "write", "metadata": "read"}


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(private_key):
    return private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Fake name resolution: every host is public unless a test maps it elsewhere."""
    names: dict[str, list[str]] = {}
    monkeypatch.setattr(vendors, "resolve_host", lambda host: names.get(host, ["140.82.112.5"]))
    return names


@pytest.fixture
def world(sqlite_engine, keyring, clock):
    team, other, project = new_id(), new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="mobile", created_at=clock()))
        conn.execute(insert(teams).values(id=other, name="web", created_at=clock()))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", repo="Acme/App",
                                             created_at=clock()))
    users = {}
    for name, scope_kind, scope_id, role in (("lead", "team", team, "team_lead"), ("viewer", "team", team, "viewer"),
                                             ("plead", "project", project, "project_lead"),
                                             ("other", "team", other, "team_lead")):
        users[name] = add_user(sqlite_engine, keyring, clock, name)[0]
        add_membership(sqlite_engine, clock, users[name], scope_kind, scope_id, role)
    return SimpleNamespace(team=team, other=other, project=project, users=users)


class Vendor:
    """A fake of every vendor API the connections call, recording each request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.permissions = dict(FULL)
        self.status = 200
        self.minted = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="SECRET-BODY-TEXT",
                                  headers={"Location": "https://169.254.169.254/latest/meta-data/"})
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"id": 42, "slug": "armature-bot"})
        if path == "/users/armature-bot[bot]":
            return httpx.Response(200, json={"id": 9001, "login": "armature-bot[bot]"})
        if path == "/app/installations":
            return httpx.Response(200, json=[{"id": 7, "account": {"login": "Acme"}, "permissions": self.permissions}])
        if path == "/app/installations/7/access_tokens":
            self.minted += 1
            return httpx.Response(201, json={"token": f"ghs_minted{self.minted:032d}",
                                             "expires_at": "2026-10-23T13:00:00Z"})
        if path == "/api/auth.test":
            return httpx.Response(200, json={"ok": True, "team": "Acme", "team_id": "T0ACME", "user": "bot",
                                             "user_id": "U0BOT", "bot_id": "B0BOT"},
                                  headers={"X-OAuth-Scopes": "chat:write,channels:history"})
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "abc-123", "displayName": "Armature"})
        if path == "/graphql":
            return httpx.Response(200, json={"data": {"viewer": {"id": "u1", "name": "Armature"},
                                                      "organization": {"id": "o1", "name": "Acme", "urlKey": "acme"}}})
        return httpx.Response(404)


@pytest.fixture
def vendor():
    return Vendor()


@pytest.fixture
def app(sqlite_engine, keyring, clock, vendor):
    client = make_client(sqlite_engine, keyring, clock)
    client.app.state.vendor_transport = httpx.MockTransport(vendor)
    return client


@pytest.fixture
def signed_in(sqlite_engine, keyring, clock, vendor):
    """A client of its own per user, since signing in replaces the client's session cookie."""
    def make(user_id, step_up=True):
        client = make_client(sqlite_engine, keyring, clock)
        client.app.state.vendor_transport = httpx.MockTransport(vendor)
        client.headers.update(mutation(sign_in_as(client, sqlite_engine, keyring, clock, user_id, step_up=step_up)))
        return client
    return make


@pytest.fixture
def lead(app, sqlite_engine, keyring, clock, world):
    return mutation(sign_in_as(app, sqlite_engine, keyring, clock, world.users["lead"]))


def _add(app, headers, scope, body, *, project=False):
    kind = "projects" if project else "teams"
    response = app.post(f"/api/{kind}/{scope}/connections", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["connection"]


SLACK = {"kind": "slack", "name": "main", "bot_token": BOT_TOKEN, "signing_secret": SIGNING}


def test_members_read_and_only_a_stepped_up_lead_writes(app, signed_in, world, lead):
    made = _add(app, lead, world.team, SLACK)
    viewer = signed_in(world.users["viewer"])
    listed = viewer.get(f"/api/teams/{world.team}/connections")
    assert listed.status_code == 200 and [c["name"] for c in listed.json()["connections"]] == ["main"]
    assert viewer.post(f"/api/teams/{world.team}/connections", json={**SLACK, "name": "x"}).status_code == 403
    assert viewer.post(f"/api/connections/{made['id']}/verify").status_code == 403
    stale = signed_in(world.users["lead"], step_up=False)
    for path in (f"/api/connections/{made['id']}", f"/api/connections/{made['id']}/verify",
                 f"/api/connections/{made['id']}/delete"):
        refused = stale.post(path, json={})
        assert refused.status_code == 403 and refused.json()["error"] == "step_up_required", path
    outsider = signed_in(world.users["other"])
    assert outsider.get(f"/api/teams/{world.team}/connections").status_code == 404
    assert outsider.post(f"/api/connections/{made['id']}/delete").status_code == 404


def test_credentials_are_write_only_and_never_logged(app, sqlite_engine, world, lead):
    shown = _add(app, lead, world.team, SLACK)
    assert shown["credentials"] == {"bot_token": {"set": True, "last4": "mnop"}, "signing_secret": {"set": True}}
    assert shown["state"] == "unverified"
    again = app.post(f"/api/connections/{shown['id']}", json={"bot_token": "", "team_id": "T0ACME"}, headers=lead)
    assert again.status_code == 200 and again.json()["connection"]["credentials"]["signing_secret"]["set"]
    assert again.json()["connection"]["config"] == {"team_id": "T0ACME"}
    page = app.get(f"/api/teams/{world.team}/connections", headers=lead).text
    with sqlite_engine.connect() as conn:
        dumped = [str(row) for table in metadata.sorted_tables for row in conn.execute(select(table)).all()]
        logged = conn.execute(select(audit.c.detail).where(audit.c.action == "connection.set")).scalars().all()
    assert BOT_TOKEN not in page and SIGNING not in page and "v1:" not in page
    assert not [row for row in dumped if BOT_TOKEN in row or SIGNING in row]
    assert [d["credentials"] for d in logged] == [["bot_token", "signing_secret"], []]
    assert logged[1]["config"] == ["team_id"]


def test_sealed_credentials_are_bound_to_their_row(app, sqlite_engine, keyring, world, lead):
    first = _add(app, lead, world.team, SLACK)
    second = _add(app, lead, world.team, {**SLACK, "name": "other", "bot_token": "xoxb-9999-other-token"})
    with sqlite_engine.begin() as conn:
        sealed = conn.execute(select(connections_table.c.sealed)
                              .where(connections_table.c.id == first["id"])).scalar_one()
        conn.execute(update(connections_table).where(connections_table.c.id == second["id"]).values(sealed=sealed))
        row = connections.get(conn, second["id"])
    with pytest.raises(SealError):
        connections.credentials(keyring, row)
    shown = {c["id"]: c for c in app.get(f"/api/teams/{world.team}/connections", headers=lead).json()["connections"]}
    assert shown[second["id"]]["credentials"] == {"unreadable": True}


@pytest.mark.parametrize("body, problem", [
    ({"kind": "slack", "name": "s"}, "bot_token is required."),
    ({"kind": "jira", "name": "j", "api_token": "t0k", "email": "a@b.co", "site_url": "http://acme.atlassian.net"},
     "site_url must be an https URL"),
    ({"kind": "jira", "name": "j", "api_token": "t0k", "email": "a@b.co", "site_url": "https://169.254.169.254"},
     "site_url must be an https URL"),
    ({"kind": "github_app", "name": "g", "client_id": "Iv23liAbCdEf", "private_key_pem": "nope"},
     "The private key is not a readable PEM key."),
    ({"kind": "smtp", "name": "x"}, "kind must be one of"),
    ({"kind": "linear", "name": "bad name!", "api_key": "lin_api_x"}, "name is not valid."),
])
def test_bad_connections_are_refused(app, world, lead, body, problem):
    refused = app.post(f"/api/teams/{world.team}/connections", json=body, headers=lead)
    assert refused.status_code == 400 and refused.json()["error"].startswith(problem)


def _bearer(request):
    return request.headers["authorization"].removeprefix("Bearer ")


def test_github_verify_records_the_bot_and_installations_and_names_missing_permissions(
        app, sqlite_engine, world, lead, vendor, pem, private_key, clock):
    made = _add(app, lead, world.team, {"kind": "github_app", "name": "bot", "client_id": "Iv23liAbCdEf",
                                        "private_key_pem": pem, "webhook_secret": HOOK_SECRET})
    assert made["config"] == {"client_id": "Iv23liAbCdEf", "api_base": "https://api.github.com"}
    vendor.permissions = {**FULL, "checks": "read"}
    failed = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert failed["state"] == "error" and "on Acme lacks checks: write" in failed["last_error"]
    assert failed["installations"] == [{"owner": "acme", "installation_id": 7, "missing": ["checks: write"]}]
    jwt = _bearer(next(r for r in vendor.requests if r.url.path == "/app"))
    header, claims, signature = jwt.split(".")
    private_key.public_key().verify(base64.urlsafe_b64decode(signature + "=="), f"{header}.{claims}".encode(),
                                    padding.PKCS1v15(), hashes.SHA256())
    claims = json.loads(base64.urlsafe_b64decode(claims + "=="))
    assert claims["iss"] == "Iv23liAbCdEf" and claims["exp"] - claims["iat"] == 600
    assert claims["iat"] == int(clock().timestamp()) - 60
    assert "authorization" not in next(r for r in vendor.requests if r.url.path.startswith("/users/")).headers
    vendor.permissions = dict(FULL)
    ok = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert ok["state"] == "ok" and ok["verified_at"] and ok["last_error"] is None
    assert ok["identity"] == {"slug": "armature-bot", "bot_login": "armature-bot[bot]", "bot_user_id": 9001,
                              "app_id": 42}
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(github_installations.c.owner)).scalars().all() == ["acme"]
        states = conn.execute(select(audit.c.detail).where(audit.c.action == "connection.verify")
                              .order_by(audit.c.at)).scalars().all()
    assert [s["state"] for s in states] == ["error", "ok"]


@pytest.mark.parametrize("body, identity, call", [
    (SLACK, {"team": "Acme", "team_id": "T0ACME", "bot_user": "bot", "bot_user_id": "U0BOT", "bot_id": "B0BOT",
             "scopes": ["channels:history", "chat:write"]}, ("slack.com", "/api/auth.test")),
    ({"kind": "jira", "name": "jira", "api_token": "jira-token", "email": "bot@acme.com",
      "site_url": "acme.atlassian.net/"}, {"account_id": "abc-123", "display": "Armature"},
     ("acme.atlassian.net", "/rest/api/3/myself")),
    ({"kind": "linear", "name": "linear", "api_key": "lin_api_abcdefghijklmnopqrstuvwx"},
     {"user": "Armature", "user_id": "u1", "org": "Acme", "org_id": "o1", "org_key": "acme"},
     ("api.linear.app", "/graphql")),
])
def test_verify_each_kind(app, world, lead, vendor, body, identity, call):
    made = _add(app, lead, world.team, body)
    ok = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert ok["state"] == "ok", ok["last_error"]
    assert ok["identity"] == identity
    assert (vendor.requests[-1].headers["host"], vendor.requests[-1].url.path) == call


def test_verify_auth_headers(app, world, lead, vendor):
    _add(app, lead, world.team, SLACK)
    jira = _add(app, lead, world.team, {"kind": "jira", "name": "jira", "api_token": "jira-token",
                                        "email": "bot@acme.com", "site_url": "https://acme.atlassian.net"})
    linear = _add(app, lead, world.team, {"kind": "linear", "name": "linear", "api_key": "lin_api_key"})
    for made in app.get(f"/api/teams/{world.team}/connections", headers=lead).json()["connections"]:
        app.post(f"/api/connections/{made['id']}/verify", headers=lead)
    by_host = {r.headers["host"]: r.headers["authorization"] for r in vendor.requests}
    assert by_host["slack.com"] == f"Bearer {BOT_TOKEN}"
    assert by_host["acme.atlassian.net"] == "Basic " + base64.b64encode(b"bot@acme.com:jira-token").decode()
    assert by_host["api.linear.app"] == "lin_api_key"
    assert jira and linear


def test_slack_verify_names_a_missing_scope_and_a_wrong_workspace(app, world, lead, vendor):
    made = _add(app, lead, world.team, {**SLACK, "team_id": "T0OTHER"})
    failed = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert failed["state"] == "error" and "workspace T0ACME, not T0OTHER" in failed["last_error"]


def test_verify_errors_never_echo_the_vendor_body(app, world, lead, vendor):
    made = _add(app, lead, world.team, SLACK)
    vendor.status = 401
    failed = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert failed["state"] == "error" and failed["last_error"] == "Slack auth.test answered HTTP 401."


def test_a_credential_change_needs_a_new_verify(app, world, lead):
    made = _add(app, lead, world.team, SLACK)
    assert app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]["state"] == "ok"
    renamed = app.post(f"/api/connections/{made['id']}", json={"name": "renamed"}, headers=lead).json()
    assert renamed["connection"]["state"] == "ok"
    replaced = app.post(f"/api/connections/{made['id']}", json={"bot_token": "xoxb-new-token-value"}, headers=lead)
    assert replaced.json()["connection"]["state"] == "unverified"
    kind = app.post(f"/api/connections/{made['id']}", json={"kind": "jira"}, headers=lead)
    assert kind.status_code == 400


def test_mint_reuses_a_token_until_five_minutes_before_it_expires(vendor, pem, clock):
    transport = httpx.MockTransport(vendor)
    args = {"connection_id": new_id(), "creds": {"private_key_pem": pem}, "config": {"client_id": "Iv23liAbCdEf"},
            "installation_id": 7, "repo": "App", "permissions": {"contents": "read"}, "transport": transport}
    first = github.mint(now=clock(), **args)
    assert github.mint(now=clock() + timedelta(minutes=54), **args) == first and vendor.minted == 1
    asked = json.loads(next(r for r in vendor.requests if r.url.path.endswith("/access_tokens")).content)
    assert asked == {"repositories": ["App"], "permissions": {"contents": "read"}}
    assert github.mint(now=clock() + timedelta(minutes=56), **args) != first and vendor.minted == 2
    github.forget(args["connection_id"])
    github.mint(now=clock(), **args)
    assert vendor.minted == 3
    vendor.status = 403
    with pytest.raises(VendorError, match="answered HTTP 403"):
        github.mint(now=clock(), **{**args, "repo": "Other"})


def test_project_connections_and_resolution(app, sqlite_engine, signed_in, world, lead, pem):
    plead = signed_in(world.users["plead"])
    team_slack = _add(app, lead, world.team, SLACK)
    project_slack = _add(plead, {}, world.project, {**SLACK, "name": "project"}, project=True)
    assert project_slack["scope_kind"] == "project"
    assert plead.post(f"/api/connections/{team_slack['id']}", json={"name": "x"}).status_code == 403
    refused = plead.post(f"/api/projects/{world.project}/connections",
                         json={"kind": "github_app", "name": "g", "client_id": "Iv23liAbCdEf", "private_key_pem": pem})
    assert refused.status_code == 400 and "belongs to a team" in refused.json()["error"]
    bot = _add(app, lead, world.team, {"kind": "github_app", "name": "bot", "client_id": "Iv23liAbCdEf",
                                       "private_key_pem": pem})
    app.post(f"/api/connections/{bot['id']}/verify", headers=lead)
    with sqlite_engine.connect() as conn:
        def resolve(**kw):
            return connections.resolve(conn, team_id=world.team, **kw).id
        assert resolve(project_id=world.project, kind="slack") == project_slack["id"]
        assert resolve(project_id=None, kind="slack") == team_slack["id"]
        assert resolve(project_id=world.project, kind="github_app", owner="ACME") == bot["id"]
        with pytest.raises(connections.ConnError, match="No github_app"):
            resolve(project_id=world.project, kind="github_app", owner="someone-else")
    _add(app, lead, world.team, {**SLACK, "name": "second"})
    with sqlite_engine.connect() as conn:
        with pytest.raises(connections.ConnError, match="More than one"):
            connections.resolve(conn, team_id=world.team, project_id=None, kind="slack")
        assert connections.resolve(conn, team_id=world.team, project_id=None, kind="slack",
                                   name="second").name == "second"


def _hook(app, headers, team, **body):
    response = app.post(f"/api/teams/{team}/webhooks", json={"name": f"{body['source']}-hook", **body},
                        headers=headers)
    return response


def _sig(secret, raw):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_a_webhook_verifies_with_its_connections_secret(app, signed_in, world, lead, pem):
    bot = _add(app, lead, world.team, {"kind": "github_app", "name": "bot", "client_id": "Iv23liAbCdEf",
                                       "private_key_pem": pem, "webhook_secret": HOOK_SECRET})
    slack = _add(app, lead, world.team, SLACK)
    assert _hook(app, lead, world.team, source="slack", connection_id=bot["id"]).status_code == 400
    assert _hook(app, lead, world.team, source="generic", connection_id=bot["id"]).status_code == 400
    other = signed_in(world.users["other"])
    assert _hook(other, {}, world.other, source="github", connection_id=bot["id"]).status_code == 400
    hook = _hook(app, lead, world.team, source="github", connection_id=bot["id"]).json()["webhook"]
    assert hook["secret_name"] is None and hook["connection_name"] == "bot" and hook["secret_set"]
    raw = json.dumps({"zen": "hi"}).encode()
    signed = {"X-Hub-Signature-256": "sha256=" + _sig(HOOK_SECRET, raw), "X-GitHub-Event": "ping",
              "X-GitHub-Delivery": "d-1", "Content-Type": "application/json"}
    path = hook["url"].removeprefix(ORIGIN)
    assert app.post(path, content=raw, headers=signed).json() == {"status": "pong"}
    forged = {**signed, "X-Hub-Signature-256": "sha256=" + _sig("wrong", raw)}
    assert app.post(path, content=raw, headers=forged).status_code == 401
    refused = app.post(f"/api/webhooks/{hook['id']}/secret", json={"value": "x"}, headers=lead)
    assert refused.status_code == 409 and "connection" in refused.json()["error"]
    in_use = app.post(f"/api/connections/{bot['id']}/delete", headers=lead)
    assert in_use.status_code == 409 and "github-hook" in in_use.json()["error"]
    unlinked = app.post(f"/api/webhooks/{hook['id']}", json={"connection_id": None}, headers=lead)
    assert unlinked.json()["webhook"]["secret_name"] == "WEBHOOK_GITHUB_HOOK"
    assert app.post(f"/api/connections/{bot['id']}/delete", headers=lead).json() == {"ok": True}
    assert slack


def test_a_slack_webhook_is_pinned_to_its_connections_workspace(app, sqlite_engine, clock, world, lead):
    slack = _add(app, lead, world.team, {**SLACK, "team_id": "T0ACME", "app_id": "A0APP"})
    assert app.post(f"/api/connections/{slack['id']}/verify", headers=lead).json()["connection"]["state"] == "ok"
    hook = _hook(app, lead, world.team, source="slack", connection_id=slack["id"]).json()["webhook"]
    path = hook["url"].removeprefix(ORIGIN)

    def send(payload):
        raw = json.dumps(payload).encode()
        stamp = str(int(clock().timestamp()))
        headers = {"X-Slack-Request-Timestamp": stamp, "Content-Type": "application/json",
                   "X-Slack-Signature": "v0=" + _sig(SIGNING, b"v0:" + stamp.encode() + b":" + raw)}
        return app.post(path, content=raw, headers=headers)

    assert send({"type": "url_verification", "challenge": "c"}).text == "c"
    wrong = send({"type": "event_callback", "team_id": "T0EVIL", "api_app_id": "A0APP", "event_id": "E1",
                  "event": {"type": "app_mention"}})
    assert wrong.status_code == 403
    assert send({"type": "event_callback", "team_id": "T0ACME", "api_app_id": "A0OTHER", "event_id": "E2",
                 "event": {"type": "app_mention"}}).status_code == 403
    ok = send({"type": "event_callback", "team_id": "T0ACME", "api_app_id": "A0APP", "event_id": "E3",
               "event": {"type": "app_mention"}})
    assert ok.status_code == 200
    with sqlite_engine.connect() as conn:
        reasons = conn.execute(select(webhook_deliveries.c.status, webhook_deliveries.c.reason)
                               .order_by(webhook_deliveries.c.received_at, webhook_deliveries.c.id)).all()
    assert ("rejected", "team_id T0EVIL is not the connection's T0ACME") in reasons
    assert ("rejected", "api_app_id A0OTHER is not the connection's A0APP") in reasons


def test_vendor_token_shapes_are_redacted_in_run_text():
    text = ("push with ghs_abcdefghijklmnopqrstuvwx and gho_abcdefghijklmnopqrstuvwx, "
            "ghu_abcdefghijklmnopqrstuvwx, xoxb-1234-5678-abcdefgh, xoxp-1234-abcdefghij, "
            "lin_api_abcdefghijklmnopqrstuvwx")
    scrubbed = ingest.redact(text, ())
    for token in ("ghs_abcdef", "gho_abcdef", "ghu_abcdef", "xoxb-1234", "xoxp-1234", "lin_api_abcdef"):
        assert token not in scrubbed


def test_rejecting_a_pending_linked_webhook_keeps_the_connection(app, world, lead):
    slack = _add(app, lead, world.team, SLACK)
    hook = _hook(app, lead, world.team, source="slack", connection_id=slack["id"]).json()["webhook"]
    assert app.post(f"/api/webhooks/{hook['id']}/reject", headers=lead).json() == {"ok": True}
    shown = app.get(f"/api/teams/{world.team}/connections", headers=lead).json()["connections"]
    assert [c["credentials"]["signing_secret"]["set"] for c in shown] == [True]


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "100.64.0.1",
                                     "::1", "fe80::1", "fd00::1"])
def test_a_host_that_resolves_to_a_private_address_is_never_called(app, world, lead, vendor, dns, address):
    made = _add(app, lead, world.team, {"kind": "jira", "name": "jira", "api_token": "jira-token",
                                        "email": "bot@acme.com", "site_url": "https://acme.atlassian.net"})
    dns["acme.atlassian.net"] = [address]
    failed = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert failed["state"] == "error" and "resolves to a private address" in failed["last_error"]
    assert vendor.requests == []


def test_one_private_address_among_public_ones_is_refused(app, world, lead, vendor, dns):
    made = _add(app, lead, world.team, {"kind": "jira", "name": "jira", "api_token": "jira-token",
                                        "email": "bot@acme.com", "site_url": "acme.atlassian.net"})
    dns["acme.atlassian.net"] = ["104.192.141.1", "10.1.2.3"]
    failed = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert "resolves to a private address" in failed["last_error"] and vendor.requests == []


@pytest.mark.parametrize("body, dns_answer", [
    ({"kind": "github_app", "api_base": "https://169.254.169.254"}, None),
    ({"kind": "github_app", "api_base": "https://ghe.example.com"}, ["10.0.0.1"]),
    ({"kind": "jira", "site_url": "https://acme.atlassian.net"}, ["169.254.169.254"]),
])
def test_a_private_or_metadata_base_url_is_refused_when_saved(app, world, lead, dns, pem, body, dns_answer):
    if dns_answer:
        dns["ghe.example.com"] = dns["acme.atlassian.net"] = dns_answer
    extra = ({"client_id": "Iv23liAbCdEf", "private_key_pem": pem} if body["kind"] == "github_app"
             else {"api_token": "jira-token", "email": "bot@acme.com"})
    refused = app.post(f"/api/teams/{world.team}/connections", json={"name": "x", **extra, **body}, headers=lead)
    assert refused.status_code == 400 and "resolves to a private address" in refused.json()["error"]


def test_mint_refuses_a_metadata_api_base(vendor, pem, clock):
    with pytest.raises(VendorError, match="private address"):
        github.mint(connection_id=new_id(), creds={"private_key_pem": pem},
                    config={"client_id": "Iv23liAbCdEf", "api_base": "https://169.254.169.254"},
                    installation_id=7, repo="App", permissions={"contents": "read"}, now=clock(),
                    transport=httpx.MockTransport(vendor))
    assert vendor.requests == []


def test_an_unresolvable_host_and_plain_http_are_refused(monkeypatch):
    def fail(host):
        raise OSError("no such host")
    monkeypatch.setattr(vendors, "resolve_host", fail)
    with pytest.raises(VendorError, match="doesn't resolve"):
        vendors.public_address("https://nowhere.example", "Jira")
    with pytest.raises(VendorError, match="must use https"):
        vendors.public_address("http://acme.atlassian.net", "Jira")


def test_redirects_are_not_followed(app, world, lead, vendor):
    made = _add(app, lead, world.team, SLACK)
    vendor.status = 302
    failed = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert failed["last_error"] == "Slack auth.test answered HTTP 302." and len(vendor.requests) == 1


def test_a_slack_webhook_pins_the_team_verify_found_when_none_is_configured(app, sqlite_engine, clock, world,
                                                                            lead):
    slack = _add(app, lead, world.team, SLACK)
    assert app.post(f"/api/connections/{slack['id']}/verify", headers=lead).json()["connection"]["state"] == "ok"
    hook = _hook(app, lead, world.team, source="slack", connection_id=slack["id"]).json()["webhook"]
    path = hook["url"].removeprefix(ORIGIN)

    def send(team_id, event_id):
        raw = json.dumps({"type": "event_callback", "team_id": team_id, "event_id": event_id,
                          "event": {"type": "app_mention"}}).encode()
        stamp = str(int(clock().timestamp()))
        return app.post(path, content=raw, headers={
            "X-Slack-Request-Timestamp": stamp, "Content-Type": "application/json",
            "X-Slack-Signature": "v0=" + _sig(SIGNING, b"v0:" + stamp.encode() + b":" + raw)})

    assert send("T0EVIL", "E1").status_code == 403
    assert send("T0ACME", "E2").status_code == 200


def test_requests_go_to_the_checked_address_so_a_second_dns_answer_cant_redirect(app, world, lead, vendor,
                                                                                monkeypatch):
    made = _add(app, lead, world.team, {"kind": "jira", "name": "jira", "api_token": "jira-token",
                                        "email": "bot@acme.com", "site_url": "https://acme.atlassian.net"})
    answers = iter([["104.192.141.1"], ["169.254.169.254"]])
    asked = []
    monkeypatch.setattr(vendors, "resolve_host", lambda host: asked.append(host) or next(answers))
    ok = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert ok["state"] == "ok" and asked == ["acme.atlassian.net"]
    sent = vendor.requests[-1]
    assert sent.url.host == "104.192.141.1" and sent.headers["host"] == "acme.atlassian.net"
    assert sent.extensions["sni_hostname"] == "acme.atlassian.net"
    rebound = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert "resolves to a private address" in rebound["last_error"] and len(vendor.requests) == 1


@pytest.mark.parametrize("address, public", [
    ("64:ff9b::a9fe:a9fe", False), ("::ffff:10.0.0.1", False), ("2002:a9fe:a9fe::1", False),
    ("64:ff9b::8c52:7005", True), ("::ffff:140.82.112.5", True), ("2606:4700::1111", True),
])
def test_ipv4_wrapped_in_ipv6_is_judged_as_ipv4(monkeypatch, address, public):
    monkeypatch.setattr(vendors, "resolve_host", lambda host: [address])
    if public:
        assert vendors.public_address("https://api.example.com", "X") == address
    else:
        with pytest.raises(VendorError, match="private address"):
            vendors.public_address("https://api.example.com", "X")


def test_a_team_webhook_cant_use_a_project_connection(app, world, lead):
    project_slack = _add(app, lead, world.project, {**SLACK, "name": "project"}, project=True)
    refused = _hook(app, lead, world.team, source="slack", connection_id=project_slack["id"])
    assert refused.status_code == 400 and "project's connection" in refused.json()["error"]
    team_slack = _add(app, lead, world.team, SLACK)
    hook = _hook(app, lead, world.team, source="slack", connection_id=team_slack["id"]).json()["webhook"]
    relinked = app.post(f"/api/webhooks/{hook['id']}", json={"connection_id": project_slack["id"]}, headers=lead)
    assert relinked.status_code == 400


def test_an_unverified_slack_connection_admits_no_delivery(app, sqlite_engine, clock, world, lead):
    slack = _add(app, lead, world.team, {**SLACK, "team_id": "T0ACME"})
    hook = _hook(app, lead, world.team, source="slack", connection_id=slack["id"]).json()["webhook"]
    path = hook["url"].removeprefix(ORIGIN)

    def send(payload):
        raw = json.dumps(payload).encode()
        stamp = str(int(clock().timestamp()))
        return app.post(path, content=raw, headers={
            "X-Slack-Request-Timestamp": stamp, "Content-Type": "application/json",
            "X-Slack-Signature": "v0=" + _sig(SIGNING, b"v0:" + stamp.encode() + b":" + raw)})

    assert send({"type": "url_verification", "challenge": "c"}).text == "c"
    refused = send({"type": "event_callback", "team_id": "T0ACME", "event_id": "E1", "event": {"type": "x"}})
    assert refused.status_code == 403
    with sqlite_engine.connect() as conn:
        reasons = conn.execute(select(webhook_deliveries.c.reason)
                               .where(webhook_deliveries.c.status == "rejected")).scalars().all()
    assert reasons == ["connection has no verified workspace"]


@pytest.mark.parametrize("blank", ["", "   ", None, 123, ["x"]])
def test_only_nonblank_text_counts_as_a_credential(app, world, lead, blank):
    refused = app.post(f"/api/teams/{world.team}/connections",
                       json={"kind": "slack", "name": "s", "bot_token": blank}, headers=lead)
    assert refused.status_code == 400 and refused.json()["error"] == "bot_token is required."
    made = _add(app, lead, world.team, SLACK)
    assert app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]["state"] == "ok"
    kept = app.post(f"/api/connections/{made['id']}", json={"bot_token": blank, "signing_secret": blank},
                    headers=lead).json()["connection"]
    assert kept["state"] == "ok" and kept["credentials"]["bot_token"] == {"set": True, "last4": "mnop"}


def test_github_verify_says_when_it_stopped_at_the_page_limit(app, world, lead, pem, monkeypatch):
    monkeypatch.setattr(github, "PAGE", 1)
    monkeypatch.setattr(github, "MAX_PAGES", 2)
    made = _add(app, lead, world.team, {"kind": "github_app", "name": "bot", "client_id": "Iv23liAbCdEf",
                                        "private_key_pem": pem})
    failed = app.post(f"/api/connections/{made['id']}/verify", headers=lead).json()["connection"]
    assert failed["state"] == "error" and "more than 2 installations" in failed["last_error"]


def test_vendor_clients_ignore_proxy_settings_from_the_environment():
    from armature.server import vendors

    with vendors.client(None) as http:
        assert http.trust_env is False


GH_TOKEN = "gho_" + "b" * 36


@pytest.fixture
def gh(monkeypatch):
    """A fake gh CLI: each call records its host and gives the current token, or raises the set error."""
    fake = SimpleNamespace(hosts=[], token=GH_TOKEN, error=None)

    def run(host):
        fake.hosts.append(host)
        if fake.error:
            raise fake.error
        return fake.token
    monkeypatch.setattr(github, "_tokens", {})
    monkeypatch.setattr(github, "run_gh", run)
    return fake


@pytest.fixture
def admin(sqlite_engine, keyring, clock, signed_in, vendor):
    """A server admin's own client, the only one that may add a gh CLI connection."""
    return signed_in(add_user(sqlite_engine, keyring, clock, "root", is_admin=True)[0])


def _gh_user(vendor, scopes="repo, read:org"):
    def answer(request):
        if request.url.path in ("/user", "/api/v3/user"):
            vendor.requests.append(request)
            return httpx.Response(200, json={"login": "stephan", "id": 77},
                                  headers={} if scopes is None else {"X-OAuth-Scopes": scopes})
        return vendor(request)
    return answer


def test_a_gh_cli_connection_needs_no_key_and_verifies_as_the_gh_login(admin, sqlite_engine, keyring, world,
                                                                       vendor, gh):
    admin.app.state.vendor_transport = httpx.MockTransport(_gh_user(vendor))
    made = _add(admin, {}, world.team, {"kind": "github_app", "name": "cli", "auth": "gh_cli",
                                        "api_base": "https://collector.example.com", "webhook_secret": HOOK_SECRET})
    assert made["config"] == {"auth": "gh_cli", "api_base": "https://api.github.com"}
    assert made["reaches"] == ["Acme/App"]
    with sqlite_engine.connect() as conn:
        [detail] = conn.execute(select(audit.c.detail).where(audit.c.action == "connection.set")).scalars().all()
    assert detail["reaches"] == ["Acme/App"]
    ok = admin.post(f"/api/connections/{made['id']}/verify").json()["connection"]
    assert ok["state"] == "ok", ok["last_error"]
    assert ok["identity"] == {"auth": "gh_cli", "login": "stephan", "user_id": 77, "scopes": ["read:org", "repo"]}
    assert ok["installations"] == [] and gh.hosts == ["github.com"]
    assert _bearer(vendor.requests[-1]) == GH_TOKEN
    with sqlite_engine.connect() as conn:
        assert connections.resolve(conn, team_id=world.team, project_id=world.project, kind="github_app",
                                   owner="anyone").id == made["id"]
        assert connections.webhook_secret(keyring, connections.get(conn, made["id"])) == HOOK_SECRET


def test_a_gh_login_without_the_repo_scope_or_a_failing_gh_is_reported(admin, world, vendor, gh):
    admin.app.state.vendor_transport = httpx.MockTransport(_gh_user(vendor, scopes="read:org"))
    made = _add(admin, {}, world.team, {"kind": "github_app", "name": "cli", "auth": "gh_cli",
                                        "host": "ghe.acme.com"})
    assert made["config"]["api_base"] == "https://ghe.acme.com/api/v3"
    failed = admin.post(f"/api/connections/{made['id']}/verify").json()["connection"]
    assert failed["state"] == "error" and "lacks the repo scope" in failed["last_error"]
    assert gh.hosts == ["ghe.acme.com"] and vendor.requests[-1].headers["host"] == "ghe.acme.com"
    gh.error = VendorError("gh has no login for ghe.acme.com. Run gh auth login as the server's user.")
    failed = admin.post(f"/api/connections/{made['id']}/verify").json()["connection"]
    assert failed["state"] == "error" and failed["last_error"].startswith("gh has no login")


@pytest.mark.parametrize("body", [{"auth": "token"}, {"auth": "gh_cli", "host": "-x"}])
def test_bad_gh_cli_config_is_refused(admin, world, body):
    response = admin.post(f"/api/teams/{world.team}/connections", json={"kind": "github_app", "name": "cli", **body})
    assert response.status_code == 400


def test_only_a_server_admin_adds_or_changes_a_gh_cli_connection(app, admin, world, lead, pem):
    refused = app.post(f"/api/teams/{world.team}/connections",
                       json={"kind": "github_app", "name": "cli", "auth": "gh_cli"}, headers=lead)
    assert refused.status_code == 403 and "server admin" in refused.json()["error"]
    bot = _add(app, lead, world.team, {"kind": "github_app", "name": "bot", "client_id": "Iv23liAbCdEf",
                                       "private_key_pem": pem})
    assert app.post(f"/api/connections/{bot['id']}", json={"auth": "gh_cli"}, headers=lead).status_code == 403
    cli = _add(admin, {}, world.team, {"kind": "github_app", "name": "cli", "auth": "gh_cli"})
    for body in ({"name": "renamed"}, {"host": "ghe.acme.com"}, {"auth": None, "client_id": "Iv23liAbCdEf",
                                                                "private_key_pem": pem}):
        assert app.post(f"/api/connections/{cli['id']}", json=body, headers=lead).status_code == 403


def test_leaving_gh_cli_for_an_app_needs_a_private_key(admin, world, pem):
    cli = _add(admin, {}, world.team, {"kind": "github_app", "name": "cli", "auth": "gh_cli"})
    app_only = {"auth": None, "client_id": "Iv23liAbCdEf"}
    missing = admin.post(f"/api/connections/{cli['id']}", json=app_only)
    assert missing.status_code == 400 and "private_key_pem is required" in missing.json()["error"]
    switched = admin.post(f"/api/connections/{cli['id']}", json={**app_only, "private_key_pem": pem})
    assert switched.status_code == 200 and switched.json()["connection"]["config"]["client_id"] == "Iv23liAbCdEf"


def test_in_a_gh_cli_team_only_a_server_admin_sets_a_projects_repo(app, admin, world, lead):
    assert app.post(f"/api/projects/{world.project}", json={"repo": "Acme/Other"}, headers=lead).status_code == 200
    _add(admin, {}, world.team, {"kind": "github_app", "name": "cli", "auth": "gh_cli"})
    for path, body in ((f"/api/projects/{world.project}", {"repo": "Secret/Repo"}),
                       (f"/api/projects/{world.project}", {"connection_id": None}),
                       (f"/api/teams/{world.team}/projects", {"name": "new", "repo": "Secret/Repo"})):
        refused = app.post(path, json=body, headers=lead)
        assert refused.status_code == 403 and "server admin" in refused.json()["error"]
    assert admin.post(f"/api/projects/{world.project}", json={"repo": "Acme/App"}).status_code == 200
    assert app.post(f"/api/projects/{world.project}", json={"default_branch": "main"}, headers=lead).status_code == 200


def test_mint_gives_a_gh_cli_connection_the_gh_token_and_reuses_it_briefly(vendor, gh, clock):
    args = {"connection_id": new_id(), "creds": {}, "config": {"auth": "gh_cli"}, "installation_id": None,
            "repo": "App", "permissions": {"contents": "write"}, "transport": httpx.MockTransport(vendor)}
    assert github.mint(now=clock(), **args) == GH_TOKEN
    assert github.mint(now=clock() + timedelta(minutes=2), cache=False, **args) == GH_TOKEN
    assert gh.hosts == ["github.com"] and vendor.requests == []
    gh.token = "gho_" + "d" * 36
    assert github.mint(now=clock() + timedelta(minutes=4), **args) == gh.token and len(gh.hosts) == 2
    gh.token = "two words"
    with pytest.raises(VendorError, match="gave no token"):
        github.gh_token({"auth": "gh_cli"}, clock(), fresh=True)
    with pytest.raises(VendorError, match="no installation"):
        github.mint(now=clock(), **{**args, "config": {"client_id": "Iv23liAbCdEf"}})


def test_run_gh_asks_gh_for_the_hosts_token_and_never_echoes_its_output(tmp_path, monkeypatch):
    script = tmp_path / "gh"
    script.write_text('#!/bin/sh\nif [ "$4" = "github.com" ]; then echo "  gho_from_stub  "; exit 0; fi\n'
                      'echo "gho_leaked_secret" >&2; exit 1\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert github._run_gh("github.com") == "gho_from_stub"
    with pytest.raises(VendorError) as caught:
        github._run_gh("ghe.acme.com")
    assert "gho_leaked" not in str(caught.value) and caught.value.__cause__ is None
    monkeypatch.setenv("PATH", str(tmp_path / "none"))
    with pytest.raises(VendorError, match="not installed"):
        github._run_gh("github.com")

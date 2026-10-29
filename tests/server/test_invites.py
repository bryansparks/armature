from urllib.parse import urlparse

import pyotp
import pytest
from sqlalchemy import select

from armature.server.db.schema import invites, memberships, users
from tests.server.app_support import (
    ORIGIN,
    PASSWORD,
    Clock,
    add_membership,
    add_user,
    login,
    make_client,
    mutation,
    sign_in_as,
)
from tests.server.test_rbac import _side

NEW_PASSWORD = "a long new password"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def admin(sqlite_engine, keyring, clock):
    root_id, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    client = make_client(sqlite_engine, keyring, clock)
    return client, sign_in_as(client, sqlite_engine, keyring, clock, root_id)


def token_of(invite_url: str) -> str:
    parsed = urlparse(invite_url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{ORIGIN}/invite"
    assert not parsed.query
    return parsed.fragment


def invited(admin, **body) -> tuple[dict, str]:
    client, csrf = admin
    response = client.post("/api/users", json={"username": "bea", "email": "Bea@Corp.test", **body},
                           headers=mutation(csrf))
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"user", "invite_url"}
    return response.json()["user"], token_of(response.json()["invite_url"])


def visitor(sqlite_engine, keyring, clock):
    client = make_client(sqlite_engine, keyring, clock)
    return client, client.get("/api/session").json()["csrf"]


def inspect(client, csrf, token):
    return client.post("/api/invite/inspect", json={"token": token}, headers=mutation(csrf))


def accept(client, csrf, token, code, password=NEW_PASSWORD):
    return client.post("/api/invite/accept", json={"token": token, "password": password, "code": code},
                       headers=mutation(csrf))


def test_invite_flow_sets_password_and_authenticator(sqlite_engine, keyring, clock, admin):
    user, token = invited(admin)
    assert user["username"] == "bea" and user["totp_confirmed"] is False
    with sqlite_engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.id == user["id"])).one()
        stored = conn.execute(select(invites.c.token_hmac)).scalars().all()
    assert row.password_hash is None and row.totp_secret_enc is None
    assert token not in repr(stored) and len(stored) == 1
    assert login(make_client(sqlite_engine, keyring, clock), "bea", "000000").status_code == 401

    client, csrf = visitor(sqlite_engine, keyring, clock)
    shown = inspect(client, csrf, token)
    assert shown.status_code == 200 and set(shown.json()) == {"username", "otpauth_uri"}
    secret = pyotp.parse_uri(shown.json()["otpauth_uri"]).secret
    assert pyotp.parse_uri(inspect(client, csrf, token).json()["otpauth_uri"]).secret == secret
    assert accept(client, csrf, token, "000000").status_code == 401
    assert accept(client, csrf, token, clock.code(secret), password="short").status_code == 400
    done = accept(client, csrf, token, clock.code(secret))
    assert done.status_code == 200 and done.json()["user"]["username"] == "bea"
    assert client.get("/api/teams").status_code == 200
    assert accept(make_client(sqlite_engine, keyring, clock), csrf, token, clock.code(secret)).status_code in (401, 403)
    again, again_csrf = visitor(sqlite_engine, keyring, clock)
    assert inspect(again, again_csrf, token).status_code == 401
    clock.advance(seconds=30)
    assert login(again, "BEA", clock.code(secret), password=NEW_PASSWORD).status_code == 200


def test_invite_expires_and_reissue_replaces_it(sqlite_engine, keyring, clock, admin):
    user, first = invited(admin)
    client, csrf = admin
    clock.advance(hours=73)
    sign_in_as(client, sqlite_engine, keyring, clock, _admin_id(sqlite_engine))
    csrf = client.get("/api/session").json()["csrf"]
    guest, guest_csrf = visitor(sqlite_engine, keyring, clock)
    assert inspect(guest, guest_csrf, first).status_code == 401
    second = token_of(client.post(f"/api/users/{user['id']}/invite", json={}, headers=mutation(csrf))
                      .json()["invite_url"])
    third = token_of(client.post(f"/api/users/{user['id']}/invite", json={}, headers=mutation(csrf))
                     .json()["invite_url"])
    assert inspect(guest, guest_csrf, second).status_code == 401
    assert inspect(guest, guest_csrf, third).status_code == 200


def _admin_id(engine):
    with engine.connect() as conn:
        return conn.execute(select(users.c.id).where(users.c.username == "root")).scalar_one()


def test_admin_reset_sign_in(sqlite_engine, keyring, clock, admin):
    client, csrf = admin
    user_id, secret = add_user(sqlite_engine, keyring, clock, "ana")
    victim = make_client(sqlite_engine, keyring, clock)
    sign_in_as(victim, sqlite_engine, keyring, clock, user_id)
    token = token_of(client.post(f"/api/users/{user_id}/invite", json={}, headers=mutation(csrf)).json()["invite_url"])
    assert victim.get("/api/teams").status_code == 401
    assert login(make_client(sqlite_engine, keyring, clock), "ana", clock.code(secret)).status_code == 401
    with sqlite_engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.id == user_id)).one()
    assert row.password_hash is None and row.totp_secret_enc is None and row.totp_confirmed_at is None
    guest, guest_csrf = visitor(sqlite_engine, keyring, clock)
    new_secret = pyotp.parse_uri(inspect(guest, guest_csrf, token).json()["otpauth_uri"]).secret
    assert new_secret != secret
    clock.advance(seconds=30)
    assert accept(guest, guest_csrf, token, clock.code(new_secret)).status_code == 200
    assert client.post(f"/api/users/{_admin_id(sqlite_engine)}/invite", json={},
                       headers=mutation(csrf)).status_code == 400


def test_invite_needs_csrf_and_is_throttled(sqlite_engine, keyring, clock, admin):
    _, token = invited(admin)
    client, csrf = visitor(sqlite_engine, keyring, clock)
    assert client.post("/api/invite/inspect", json={"token": token}, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/api/invite/inspect", json={"token": token},
                       headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf}).status_code == 403
    for index in range(5):
        assert inspect(client, csrf, f"guess{index:02d}" + "x" * 30).status_code == 401
    blocked = inspect(client, csrf, token)
    assert blocked.status_code == 429 and blocked.json() == {"error": "Too many attempts. Wait and try again."}


def test_disabled_user_invite_refused(sqlite_engine, keyring, clock, admin):
    user, token = invited(admin)
    client, csrf = admin
    assert client.post(f"/api/users/{user['id']}", json={"disabled": True}, headers=mutation(csrf)).status_code == 200
    guest, guest_csrf = visitor(sqlite_engine, keyring, clock)
    assert inspect(guest, guest_csrf, token).status_code == 401


def test_team_lead_invites_into_own_team_only(sqlite_engine, keyring, clock):
    with sqlite_engine.begin() as conn:
        own, other = _side(conn, "own", clock), _side(conn, "other", clock)
    lead_id, _ = add_user(sqlite_engine, keyring, clock, "lead")
    add_membership(sqlite_engine, clock, lead_id, "team", own.team, "team_lead")
    client = make_client(sqlite_engine, keyring, clock)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, lead_id)

    def create(username, **body):
        return client.post("/api/users", json={"username": username, **body}, headers=mutation(csrf))

    made = create("cal", team_id=own.team, role="contributor")
    assert made.status_code == 200, made.text
    with sqlite_engine.connect() as conn:
        held = conn.execute(select(memberships.c.scope_id, memberships.c.role)
                            .where(memberships.c.user_id == made.json()["user"]["id"])).all()
    assert [(r.scope_id, r.role) for r in held] == [(own.team, "contributor")]
    assert create("dee", team_id=own.team, role="team_lead").status_code == 403
    assert create("eve", team_id=other.team, role="viewer").status_code == 404
    assert create("fay").status_code == 403
    assert create("gus", team_id=own.team, role="admin").status_code == 400

    pending_id = made.json()["user"]["id"]
    assert client.post(f"/api/users/{pending_id}/invite", json={}, headers=mutation(csrf)).status_code == 200
    member_id, _ = add_user(sqlite_engine, keyring, clock, "hal")
    add_membership(sqlite_engine, clock, member_id, "team", own.team, "viewer")
    assert client.post(f"/api/users/{member_id}/invite", json={}, headers=mutation(csrf)).status_code == 403
    stranger_id, _ = add_user(sqlite_engine, keyring, clock, "ivy", confirmed=False)
    assert client.post(f"/api/users/{stranger_id}/invite", json={}, headers=mutation(csrf)).status_code == 403


def test_lookup_exact_matches_only(sqlite_engine, keyring, clock, admin):
    invited(admin)
    with sqlite_engine.begin() as conn:
        own = _side(conn, "own", clock)
    lead_id, _ = add_user(sqlite_engine, keyring, clock, "lead")
    add_membership(sqlite_engine, clock, lead_id, "team", own.team, "team_lead")
    viewer_id, _ = add_user(sqlite_engine, keyring, clock, "vic")
    add_membership(sqlite_engine, clock, viewer_id, "project", own.project, "viewer")
    lead = make_client(sqlite_engine, keyring, clock)
    sign_in_as(lead, sqlite_engine, keyring, clock, lead_id)
    for query in ("bea", "BEA", "bea@corp.TEST"):
        found = lead.get("/api/users/lookup", params={"q": query}).json()["users"]
        assert [u["username"] for u in found] == ["bea"] and set(found[0]) == {"id", "username", "display_name"}
    for query in ("be", "b", "", "corp.test", "%"):
        assert lead.get("/api/users/lookup", params={"q": query}).json()["users"] == []
    viewer = make_client(sqlite_engine, keyring, clock)
    sign_in_as(viewer, sqlite_engine, keyring, clock, viewer_id)
    assert viewer.get("/api/users/lookup", params={"q": "bea"}).status_code == 403


def test_usernames_are_case_insensitive(sqlite_engine, keyring, clock, admin):
    client, csrf = admin
    assert client.post("/api/users", json={"username": "Zed"}, headers=mutation(csrf)).json()["user"]["username"] == "zed"
    assert client.post("/api/users", json={"username": "ZED"}, headers=mutation(csrf)).status_code == 409


def test_password_unchanged_by_failed_accept(sqlite_engine, keyring, clock, admin):
    _, token = invited(admin)
    client, csrf = visitor(sqlite_engine, keyring, clock)
    inspect(client, csrf, token)
    accept(client, csrf, token, "000000", password=PASSWORD)
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(users.c.password_hash).where(users.c.username == "bea")).scalar_one() is None


def _two_teams(engine, keyring, clock):
    with engine.begin() as conn:
        a, b = _side(conn, "a", clock), _side(conn, "b", clock)
    lead_id, _ = add_user(engine, keyring, clock, "lead-a")
    add_membership(engine, clock, lead_id, "team", a.team, "team_lead")
    client = make_client(engine, keyring, clock)
    return a, b, client, sign_in_as(client, engine, keyring, clock, lead_id)


def test_lead_cannot_take_over_another_teams_lead(sqlite_engine, keyring, clock):
    a, b, lead, csrf = _two_teams(sqlite_engine, keyring, clock)
    victim_id, _ = add_user(sqlite_engine, keyring, clock, "uma", confirmed=False)
    add_membership(sqlite_engine, clock, victim_id, "team", b.team, "team_lead")
    found = lead.get("/api/users/lookup", params={"q": "uma"}).json()["users"]
    assert [u["id"] for u in found] == [victim_id]
    added = lead.post("/api/memberships", json={"user_id": victim_id, "scope_kind": "team", "scope_id": a.team,
                                                "role": "viewer"}, headers=mutation(csrf))
    assert added.status_code == 200
    assert lead.post(f"/api/users/{victim_id}/invite", json={}, headers=mutation(csrf)).status_code == 403


def test_lead_cannot_reissue_for_a_lead_of_their_own_team(sqlite_engine, keyring, clock, admin):
    a, _, lead, csrf = _two_teams(sqlite_engine, keyring, clock)
    peer_id, _ = add_user(sqlite_engine, keyring, clock, "peer", confirmed=False)
    add_membership(sqlite_engine, clock, peer_id, "team", a.team, "team_lead")
    assert lead.post(f"/api/users/{peer_id}/invite", json={}, headers=mutation(csrf)).status_code == 403
    client, admin_csrf = admin
    assert client.post(f"/api/users/{peer_id}/invite", json={}, headers=mutation(admin_csrf)).status_code == 200


def test_reissue_makes_a_new_authenticator_secret(sqlite_engine, keyring, clock, admin):
    user, first = invited(admin)
    guest, guest_csrf = visitor(sqlite_engine, keyring, clock)
    before = pyotp.parse_uri(inspect(guest, guest_csrf, first).json()["otpauth_uri"]).secret
    client, csrf = admin
    second = token_of(client.post(f"/api/users/{user['id']}/invite", json={}, headers=mutation(csrf))
                      .json()["invite_url"])
    after = pyotp.parse_uri(inspect(guest, guest_csrf, second).json()["otpauth_uri"]).secret
    assert after != before


def test_only_admins_grant_or_touch_team_lead(sqlite_engine, keyring, clock):
    a, _, lead, csrf = _two_teams(sqlite_engine, keyring, clock)
    member_id, _ = add_user(sqlite_engine, keyring, clock, "mo")

    def grant(role, user_id=member_id):
        return lead.post("/api/memberships", json={"user_id": user_id, "scope_kind": "team", "scope_id": a.team,
                                                   "role": role}, headers=mutation(csrf))

    assert grant("team_lead").status_code == 403
    assert grant("project_lead").status_code == 200
    assert lead.post("/api/users", json={"username": "nu", "team_id": a.team, "role": "team_lead"},
                     headers=mutation(csrf)).status_code == 403
    peer_id, _ = add_user(sqlite_engine, keyring, clock, "peer")
    peer_membership = add_membership(sqlite_engine, clock, peer_id, "team", a.team, "team_lead")
    assert grant("viewer", peer_id).status_code == 403
    assert lead.post(f"/api/memberships/{peer_membership}/delete", json={}, headers=mutation(csrf)).status_code == 403


def test_lookup_is_throttled_per_caller(sqlite_engine, keyring, clock):
    _, _, lead, _ = _two_teams(sqlite_engine, keyring, clock)
    for _ in range(20):
        assert lead.get("/api/users/lookup", params={"q": "nobody"}).status_code == 200
    assert lead.get("/api/users/lookup", params={"q": "nobody"}).status_code == 429
    clock.advance(minutes=16)
    other = make_client(sqlite_engine, keyring, clock)
    lead_id = add_user(sqlite_engine, keyring, clock, "lead-b")[0]
    with sqlite_engine.connect() as conn:
        team_a = conn.execute(select(memberships.c.scope_id).where(memberships.c.role == "team_lead")).scalar()
    add_membership(sqlite_engine, clock, lead_id, "team", team_a, "team_lead")
    sign_in_as(other, sqlite_engine, keyring, clock, lead_id)
    assert other.get("/api/users/lookup", params={"q": "nobody"}).status_code == 200

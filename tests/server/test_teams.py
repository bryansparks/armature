"""Teams: an admin renames one from its page."""
from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from armature.server.db import new_id
from armature.server.db.schema import audit, teams
from tests.server.app_support import Clock, add_membership, add_user, make_client, mutation, sign_in_as


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(engine, keyring, clock):
    team_id, other_id = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team_id, name="Mobile", created_at=clock()))
        conn.execute(insert(teams).values(id=other_id, name="Web", created_at=clock()))
    admin_id, _ = add_user(engine, keyring, clock, "root", is_admin=True)
    lead_id, _ = add_user(engine, keyring, clock, "lead")
    add_membership(engine, clock, lead_id, "team", team_id, "team_lead")
    return {"team": team_id, "admin": admin_id, "lead": lead_id}


def _client(engine, keyring, clock, user_id):
    client = make_client(engine, keyring, clock, require_totp=False)
    return client, mutation(sign_in_as(client, engine, keyring, clock, user_id))


def _renames(engine, team_id):
    with engine.connect() as conn:
        return conn.execute(select(audit.c.detail).where(audit.c.action == "team.rename",
                                                         audit.c.team_id == team_id)).scalars().all()


def test_admin_renames_a_team(engine, keyring, clock, world):
    client, headers = _client(engine, keyring, clock, world["admin"])
    response = client.post(f"/api/teams/{world['team']}", headers=headers, json={"name": "  Mobile apps "})
    assert response.status_code == 200, response.text
    assert response.json()["team"]["name"] == "Mobile apps"
    with engine.connect() as conn:
        assert conn.execute(select(teams.c.name).where(teams.c.id == world["team"])).scalar_one() == "Mobile apps"
    assert _renames(engine, world["team"]) == [{"old_name": "Mobile", "name": "Mobile apps"}]


def test_same_name_is_not_audited(engine, keyring, clock, world):
    client, headers = _client(engine, keyring, clock, world["admin"])
    response = client.post(f"/api/teams/{world['team']}", headers=headers, json={"name": "Mobile"})
    assert response.status_code == 200 and response.json()["team"]["name"] == "Mobile"
    assert _renames(engine, world["team"]) == []


def test_team_lead_cannot_rename(engine, keyring, clock, world):
    client, headers = _client(engine, keyring, clock, world["lead"])
    assert client.post(f"/api/teams/{world['team']}", headers=headers, json={"name": "Phones"}).status_code == 403
    assert _renames(engine, world["team"]) == []


@pytest.mark.parametrize("name, status", [("Web", 409), ("", 400), ("   ", 400), ("bad/name", 400), ("x" * 101, 400)])
def test_rename_refuses_a_taken_or_bad_name(engine, keyring, clock, world, name, status):
    client, headers = _client(engine, keyring, clock, world["admin"])
    response = client.post(f"/api/teams/{world['team']}", headers=headers, json={"name": name})
    assert response.status_code == status, response.text
    if status == 409:
        assert response.json()["error"] == "A team with that name exists."
    with engine.connect() as conn:
        assert conn.execute(select(teams.c.name).where(teams.c.id == world["team"])).scalar_one() == "Mobile"
    assert _renames(engine, world["team"]) == []


def test_rename_of_an_unknown_team_is_not_found(engine, keyring, clock, world):
    client, headers = _client(engine, keyring, clock, world["admin"])
    assert client.post(f"/api/teams/{new_id()}", headers=headers, json={"name": "Phones"}).status_code == 404


def test_rename_needs_step_up(engine, keyring, clock, world):
    client = make_client(engine, keyring, clock)
    headers = mutation(sign_in_as(client, engine, keyring, clock, world["admin"], step_up=False))
    refused = client.post(f"/api/teams/{world['team']}", headers=headers, json={"name": "Phones"})
    assert refused.status_code == 403 and refused.json()["error"] == "step_up_required"
    assert _renames(engine, world["team"]) == []

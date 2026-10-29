"""Projects: the repository, its GitHub connection and default branch, and the list across teams."""
from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from armature.server.db import new_id
from armature.server.db.schema import audit, projects, teams
from armature.server.db.schema import connections as connections_table
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
    return {"team": team_id, "other": other_id, "admin": admin_id, "lead": lead_id,
            "github": _connection(engine, clock, team_id, "github_app", "main"),
            "slack": _connection(engine, clock, team_id, "slack", "chat"),
            "foreign": _connection(engine, clock, other_id, "github_app", "web")}


def _connection(engine, clock, team_id, kind, name):
    connection_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(connections_table).values(id=connection_id, team_id=team_id, scope=team_id, kind=kind,
                                                      name=name, state="unverified", created_at=clock(),
                                                      updated_at=clock()))
    return connection_id


def _client(engine, keyring, clock, user_id):
    client = make_client(engine, keyring, clock, require_totp=False)
    return client, mutation(sign_in_as(client, engine, keyring, clock, user_id))


@pytest.mark.parametrize("repo", ["https://github.com/acme/app.git", "git@github.com:acme/app.git", "acme/app",
                                  "github.com/acme/app", "https://github.com/acme/app/"])
def test_create_keeps_owner_and_name(engine, keyring, clock, world, repo):
    client, headers = _client(engine, keyring, clock, world["lead"])
    response = client.post(f"/api/teams/{world['team']}/projects", headers=headers,
                           json={"name": "app", "repo": repo, "connection_id": world["github"],
                                 "default_branch": "develop"})
    assert response.status_code == 200, response.text
    project = response.json()["project"]
    assert (project["repo"], project["connection_id"], project["default_branch"]) == \
        ("acme/app", world["github"], "develop")


@pytest.mark.parametrize("body, message", [
    ({"repo": "https://github.com/acme/app/tree/main"}, "owner/name"),
    ({"repo": "acme"}, "owner/name"),
    ({"repo": "https://gitlab.com/acme/app"}, "github.com"),
    ({"repo": "git@evil.test:acme/app.git"}, "github.com"),
    ({"repo": "ac.me/app"}, "owner/name"),
    ({"repo": f"{'a' * 40}/app"}, "owner/name"),
    ({"repo": "acme/.."}, "owner/name"),
    ({"repo": "acme/ap p"}, "owner/name"),
    ({"default_branch": "-x"}, "branch"),
    ({"default_branch": "a..b"}, "branch"),
])
def test_create_refuses_a_bad_repo_or_branch(engine, keyring, clock, world, body, message):
    client, headers = _client(engine, keyring, clock, world["lead"])
    response = client.post(f"/api/teams/{world['team']}/projects", headers=headers, json={"name": "app", **body})
    assert response.status_code == 400 and message in response.json()["error"]


@pytest.mark.parametrize("which", ["slack", "foreign"])
def test_connection_must_be_one_of_the_teams_github_connections(engine, keyring, clock, world, which):
    client, headers = _client(engine, keyring, clock, world["lead"])
    response = client.post(f"/api/teams/{world['team']}/projects", headers=headers,
                           json={"name": "app", "connection_id": world[which]})
    assert response.status_code == 400
    with engine.connect() as conn:
        assert conn.execute(select(projects)).all() == []


def test_update_changes_only_the_fields_sent(engine, keyring, clock, world):
    client, headers = _client(engine, keyring, clock, world["lead"])
    project_id = client.post(f"/api/teams/{world['team']}/projects", headers=headers,
                             json={"name": "app", "repo": "acme/app", "default_branch": "main"}).json()["project"]["id"]
    changed = client.post(f"/api/projects/{project_id}", headers=headers, json={"connection_id": world["github"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["project"]["repo"] == "acme/app" and changed.json()["project"]["default_branch"] == "main"
    cleared = client.post(f"/api/projects/{project_id}", headers=headers,
                          json={"repo": None, "connection_id": None, "default_branch": ""}).json()["project"]
    assert (cleared["repo"], cleared["connection_id"], cleared["default_branch"]) == (None, None, None)
    refused = client.post(f"/api/projects/{project_id}", headers=headers, json={"connection_id": world["foreign"]})
    assert refused.status_code == 400
    with engine.connect() as conn:
        actions = [r.action for r in conn.execute(select(audit).where(audit.c.target_id == project_id))]
    assert actions.count("project.update") == 2


def test_update_needs_a_project_lead(engine, keyring, clock, world):
    viewer_id, _ = add_user(engine, keyring, clock, "viewer")
    add_membership(engine, clock, viewer_id, "team", world["team"], "viewer")
    lead, lead_headers = _client(engine, keyring, clock, world["lead"])
    project_id = lead.post(f"/api/teams/{world['team']}/projects", headers=lead_headers,
                           json={"name": "app"}).json()["project"]["id"]
    client, headers = _client(engine, keyring, clock, viewer_id)
    assert client.post(f"/api/projects/{project_id}", headers=headers, json={"repo": "acme/app"}).status_code == 403


def test_list_across_teams_shows_only_what_the_user_sees(engine, keyring, clock, world):
    admin, admin_headers = _client(engine, keyring, clock, world["admin"])
    for team, name in ((world["team"], "ios"), (world["other"], "site")):
        assert admin.post(f"/api/teams/{team}/projects", headers=admin_headers, json={"name": name}).status_code == 200
    listed = admin.get("/api/projects").json()["projects"]
    assert [(p["team_name"], p["name"]) for p in listed] == [("Mobile", "ios"), ("Web", "site")]
    lead, _ = _client(engine, keyring, clock, world["lead"])
    assert [p["name"] for p in lead.get("/api/projects").json()["projects"]] == ["ios"]
    outsider_id, _ = add_user(engine, keyring, clock, "outsider")
    outsider, _ = _client(engine, keyring, clock, outsider_id)
    assert outsider.get("/api/projects").json()["projects"] == []


def test_webhook_routing_finds_the_project_by_its_repo(engine, keyring, clock, world):
    from armature.server.webhooks import project_for_repo
    client, headers = _client(engine, keyring, clock, world["lead"])
    project_id = client.post(f"/api/teams/{world['team']}/projects", headers=headers,
                             json={"name": "app", "repo": "https://github.com/Acme/App.git"}).json()["project"]["id"]
    with engine.connect() as conn:
        assert project_for_repo(conn, world["team"], "acme/app") == project_id


def test_repo_on_another_host_keeps_the_host_only_when_allowed():
    from armature.server.app.http import ApiError
    from armature.server.app.routes_api import repo_slug
    assert repo_slug("https://GitHub.com/acme/app") == "acme/app"
    assert repo_slug("https://ghe.corp/acme/app.git", host="ghe.corp") == "ghe.corp/acme/app"
    with pytest.raises(ApiError):
        repo_slug("https://ghe.corp/acme/app")


def test_a_connection_a_project_uses_cannot_be_deleted(engine, keyring, clock, world):
    client, headers = _client(engine, keyring, clock, world["lead"])
    assert client.post(f"/api/teams/{world['team']}/projects", headers=headers,
                       json={"name": "app", "connection_id": world["github"]}).status_code == 200
    refused = client.post(f"/api/connections/{world['github']}/delete", headers=headers, json={})
    assert refused.status_code == 409
    assert refused.json()["error"] == "Project app uses this connection. Pick another first."
    with engine.connect() as conn:
        assert conn.execute(select(connections_table.c.id).where(connections_table.c.id == world["github"])).first()
    assert client.post(f"/api/connections/{world['slack']}/delete", headers=headers, json={}).status_code == 200

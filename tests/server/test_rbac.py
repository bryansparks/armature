"""Every route against five roles, on the role's own team and on another team (IDOR)."""
from dataclasses import dataclass
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import insert

from armature.server import secrets as secret_store
from armature.server.db import new_id
from armature.server.db.schema import projects, runs, teams, workflows
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)

ROLES = ("admin", "team_lead", "project_lead", "contributor", "viewer")
SECRET_VALUES = ("own-team-secret-value", "other-team-secret-value", "instance-secret-value")
_names = count()


@dataclass
class Side:
    team: str
    project: str
    team_run: str
    project_run: str
    member: str


@dataclass
class World:
    own: Side
    other: Side
    users: dict[str, str]


def _side(conn, label, clock) -> Side:
    team_id, project_id, workflow_id = new_id(), new_id(), new_id()
    conn.execute(insert(teams).values(id=team_id, name=f"team-{label}", created_at=clock()))
    conn.execute(insert(projects).values(id=project_id, team_id=team_id, name=f"proj-{label}", created_at=clock()))
    conn.execute(insert(workflows).values(id=workflow_id, team_id=team_id, name=f"wf-{label}", current_version=1,
                                          created_at=clock(), updated_at=clock()))
    team_run, project_run = new_id(), new_id()
    for run_id, proj in ((team_run, None), (project_run, project_id)):
        conn.execute(insert(runs).values(id=run_id, team_id=team_id, project_id=proj, workflow_id=workflow_id,
                                         status="succeeded", started_at=clock(), cost_usd=Decimal("0.5")))
    return Side(team=team_id, project=project_id, team_run=team_run, project_run=project_run, member="")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(sqlite_engine, keyring, clock) -> World:
    with sqlite_engine.begin() as conn:
        own, other = _side(conn, "own", clock), _side(conn, "other", clock)
        secret_store.set_secret(conn, keyring, team_id=own.team, name="OWN", value=SECRET_VALUES[0], actor_id=None)
        secret_store.set_secret(conn, keyring, team_id=other.team, name="OTHER", value=SECRET_VALUES[1], actor_id=None)
        secret_store.set_secret(conn, keyring, team_id=None, name="INST", value=SECRET_VALUES[2], actor_id=None)
    user_ids = {role: add_user(sqlite_engine, keyring, clock, role.replace("_", "-"), is_admin=role == "admin")[0]
                for role in ROLES}
    add_membership(sqlite_engine, clock, user_ids["team_lead"], "team", own.team, "team_lead")
    for role in ("project_lead", "contributor", "viewer"):
        add_membership(sqlite_engine, clock, user_ids[role], "project", own.project, role)
    for side, label in ((own, "own"), (other, "other")):
        side.member = add_user(sqlite_engine, keyring, clock, f"member-{label}")[0]
        add_membership(sqlite_engine, clock, side.member, "project", side.project, "viewer")
    return World(own=own, other=other, users=user_ids)


def _fresh_membership(engine, keyring, clock, side: Side) -> str:
    user_id = add_user(engine, keyring, clock, f"fresh{next(_names)}")[0]
    return add_membership(engine, clock, user_id, "team", side.team, "viewer")


def _pending_member(engine, keyring, clock, side: Side) -> str:
    user_id = add_user(engine, keyring, clock, f"pending{next(_names)}", confirmed=False)[0]
    add_membership(engine, clock, user_id, "team", side.team, "viewer")
    return user_id


def _fresh_secret(engine, keyring, team_id) -> str:
    name = f"S{next(_names)}"
    with engine.begin() as conn:
        secret_store.set_secret(conn, keyring, team_id=team_id, name=name, value="throwaway", actor_id=None)
    return name


# (label, method, path builder, body builder, lowest role that may call it on its own team)
def _scoped_cases(engine, keyring, clock):
    return [
        ("list projects", "GET", lambda s: f"/api/teams/{s.team}/projects", None, "viewer"),
        ("create project", "POST", lambda s: f"/api/teams/{s.team}/projects",
         lambda s: {"name": f"p{next(_names)}"}, "team_lead"),
        ("get project", "GET", lambda s: f"/api/projects/{s.project}", None, "viewer"),
        ("list team secrets", "GET", lambda s: f"/api/teams/{s.team}/secrets", None, "team_lead"),
        ("set team secret", "POST", lambda s: f"/api/teams/{s.team}/secrets",
         lambda s: {"name": f"N{next(_names)}", "value": "v"}, "team_lead"),
        ("delete team secret", "POST",
         lambda s: f"/api/teams/{s.team}/secrets/{_fresh_secret(engine, keyring, s.team)}/delete", None, "team_lead"),
        ("grant on project", "POST", lambda s: "/api/memberships",
         lambda s: {"user_id": s.member, "scope_kind": "project", "scope_id": s.project, "role": "contributor"},
         "team_lead"),
        ("grant on team", "POST", lambda s: "/api/memberships",
         lambda s: {"user_id": s.member, "scope_kind": "team", "scope_id": s.team, "role": "viewer"}, "team_lead"),
        ("delete membership", "POST",
         lambda s: f"/api/memberships/{_fresh_membership(engine, keyring, clock, s)}/delete", None, "team_lead"),
        ("list team users", "GET", lambda s: f"/api/users?team={s.team}", None, "team_lead"),
        ("invite into team", "POST", lambda s: "/api/users",
         lambda s: {"username": f"inv{next(_names)}", "team_id": s.team, "role": "contributor"}, "team_lead"),
        ("reissue invite", "POST",
         lambda s: f"/api/users/{_pending_member(engine, keyring, clock, s)}/invite", None, "team_lead"),
    ]


def _instance_cases(world: World):
    return [
        ("create team", "POST", "/api/teams", lambda: {"name": f"t{next(_names)}"}, {"admin"}),
        ("create user", "POST", "/api/users", lambda: {"username": f"u{next(_names)}"}, {"admin"}),
        ("update user", "POST", f"/api/users/{world.own.member}", lambda: {"display_name": f"d{next(_names)}"},
         {"admin"}),
        ("list instance secrets", "GET", "/api/secrets", None, {"admin"}),
        ("set instance secret", "POST", "/api/secrets", lambda: {"name": f"I{next(_names)}", "value": "v"}, {"admin"}),
        ("list audit", "GET", "/api/audit", None, {"admin"}),
        ("list users", "GET", "/api/users", None, {"admin", "team_lead"}),
        ("look up a user", "GET", "/api/users/lookup?q=viewer", None, {"admin", "team_lead"}),
        ("reset sign-in", "POST", f"/api/users/{world.other.member}/invite", None, {"admin"}),
        ("list teams", "GET", "/api/teams", None, set(ROLES)),
        ("list runs", "GET", "/api/runs", None, set(ROLES)),
        ("session", "GET", "/api/session", None, set(ROLES)),
    ]


def _call(client, csrf, method, path, body):
    if method == "GET":
        return client.get(path)
    return client.post(path, json=body if body is not None else {}, headers=mutation(csrf))


def _rank(role):
    return ROLES[::-1].index(role)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("side_name", ["own", "other"])
def test_scoped_routes(sqlite_engine, keyring, clock, world, role, side_name):
    client = make_client(sqlite_engine, keyring, clock)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, world.users[role])
    side = getattr(world, side_name)
    mismatches = []
    for label, method, path, body, lowest in _scoped_cases(sqlite_engine, keyring, clock):
        allowed = role == "admin" or (side_name == "own" and _rank(role) >= _rank(lowest))
        response = _call(client, csrf, method, path(side), body(side) if body else None)
        ok = 200 <= response.status_code < 300
        if ok != allowed or (not ok and response.status_code not in (403, 404)):
            mismatches.append(f"{label}: {response.status_code} {response.text[:80]}")
        for value in SECRET_VALUES:
            assert value not in response.text, f"{label} returned a secret value"
    assert not mismatches, f"{role} on {side_name} team:\n" + "\n".join(mismatches)


@pytest.mark.parametrize("role", ROLES)
def test_instance_routes(sqlite_engine, keyring, clock, world, role):
    client = make_client(sqlite_engine, keyring, clock)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, world.users[role])
    mismatches = []
    for label, method, path, body, allowed_roles in _instance_cases(world):
        response = _call(client, csrf, method, path, body() if body else None)
        ok = 200 <= response.status_code < 300
        if ok != (role in allowed_roles) or (not ok and response.status_code != 403):
            mismatches.append(f"{label}: {response.status_code} {response.text[:80]}")
        for value in SECRET_VALUES:
            assert value not in response.text, f"{label} returned a secret value"
    assert not mismatches, f"{role}:\n" + "\n".join(mismatches)


@pytest.mark.parametrize("role", ROLES)
def test_lists_show_only_own_scope(sqlite_engine, keyring, clock, world, role):
    client = make_client(sqlite_engine, keyring, clock)
    sign_in_as(client, sqlite_engine, keyring, clock, world.users[role])
    team_ids = {t["id"] for t in client.get("/api/teams").json()["teams"]}
    run_ids = {r["id"] for r in client.get("/api/runs").json()["runs"]}
    other_runs = client.get(f"/api/runs?team={world.other.team}").json()["runs"]
    projects_seen = {p["id"] for p in client.get(f"/api/teams/{world.own.team}/projects").json()["projects"]}
    if role == "admin":
        assert team_ids == {world.own.team, world.other.team}
        assert run_ids == {world.own.team_run, world.own.project_run, world.other.team_run, world.other.project_run}
        assert len(other_runs) == 2
        return
    assert team_ids == {world.own.team}
    assert other_runs == []
    assert projects_seen == {world.own.project}
    if role == "team_lead":
        assert run_ids == {world.own.team_run, world.own.project_run}
        users = client.get("/api/users").json()["users"]
        assert world.other.member not in {u["id"] for u in users}
    else:
        assert run_ids == {world.own.project_run}


def test_run_shape_and_paging(sqlite_engine, keyring, clock, world):
    client = make_client(sqlite_engine, keyring, clock)
    sign_in_as(client, sqlite_engine, keyring, clock, world.users["admin"])
    first = client.get("/api/runs?limit=3").json()
    assert set(first["runs"][0]) == {"id", "team_id", "project_id", "workflow", "status", "triggered_by",
                                     "started_at", "finished_at", "cost_usd", "subject", "thread", "title",
                                     "verdict", "pr", "outcome", "fix_outcome", "risk", "trigger", "links",
                                     "location"}
    assert first["runs"][0]["workflow"].startswith("wf-") and first["runs"][0]["cost_usd"] == 0.5
    rest = client.get("/api/runs", params={"limit": 3, "before": first["next_before"]}).json()
    assert len(first["runs"]) == 3 and len(rest["runs"]) == 1 and rest["next_before"] is None
    assert not {r["id"] for r in first["runs"]} & {r["id"] for r in rest["runs"]}
    assert client.get("/api/runs?status=bogus").status_code == 400


def test_membership_body_cannot_widen_scope(sqlite_engine, keyring, clock, world):
    client = make_client(sqlite_engine, keyring, clock)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, world.users["team_lead"])
    for role in ("admin", "owner"):
        response = client.post("/api/memberships", json={"user_id": world.own.member, "scope_kind": "team",
                                                         "scope_id": world.own.team, "role": role},
                               headers=mutation(csrf))
        assert response.status_code == 400
    other_membership = _fresh_membership(sqlite_engine, keyring, clock, world.other)
    assert client.post(f"/api/memberships/{other_membership}/delete", json={},
                       headers=mutation(csrf)).status_code == 404


def test_team_viewer_cannot_lead(sqlite_engine, keyring, clock, world):
    viewer_id = add_user(sqlite_engine, keyring, clock, "team-viewer")[0]
    add_membership(sqlite_engine, clock, viewer_id, "team", world.own.team, "viewer")
    client = make_client(sqlite_engine, keyring, clock)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, viewer_id)
    assert client.get(f"/api/teams/{world.own.team}/projects").status_code == 200
    assert client.get(f"/api/projects/{world.own.project}").json()["project"]["role"] == "viewer"
    assert client.get(f"/api/teams/{world.own.team}/secrets").status_code == 403
    assert client.post(f"/api/teams/{world.own.team}/projects", json={"name": "x"},
                       headers=mutation(csrf)).status_code == 403
    assert {r["id"] for r in client.get("/api/runs").json()["runs"]} == {world.own.team_run, world.own.project_run}

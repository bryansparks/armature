"""Behavior that differs by backend, so these run on SQLite, Postgres, and MySQL through the db_matrix marker."""
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import insert, select

from armature.server import users as users_module
from armature.server.db import new_id
from armature.server.db.schema import runs, teams, users
from armature.server.users import LastAdmin, update_user
from tests.server.app_support import Clock, add_user, make_client, mutation, sign_in_as


@pytest.fixture
def clock():
    return Clock()


def test_two_admins_demoting_each_other_leave_one(engine, keyring, clock, monkeypatch):
    first, _ = add_user(engine, keyring, clock, "first", is_admin=True)
    second, _ = add_user(engine, keyring, clock, "second", is_admin=True)
    barrier = threading.Barrier(2, timeout=2)
    real_update = users_module.update

    # Both threads finish their reads before either writes, where the database lets them.
    def update_after_both_read(table):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real_update(table)

    monkeypatch.setattr(users_module, "update", update_after_both_read)
    outcomes = {}

    def demote(target, actor):
        try:
            with engine.begin() as conn:
                update_user(conn, target, is_admin=False, actor_id=actor)
            outcomes[target] = "ok"
        except LastAdmin:
            outcomes[target] = "refused"
        except Exception as exc:  # noqa: BLE001 - a deadlock victim also counts as refused
            outcomes[target] = f"error {exc.__class__.__name__}"

    threads = [threading.Thread(target=demote, args=(second, first)),
               threading.Thread(target=demote, args=(first, second))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    with engine.connect() as conn:
        admins = conn.execute(select(users.c.id).where(users.c.is_admin.is_(True))).scalars().all()
    assert len(admins) == 1, outcomes
    assert min(outcomes.values()) == "ok"


def test_run_paging_with_equal_microsecond_timestamps(engine, keyring, clock):
    admin_id, _ = add_user(engine, keyring, clock, "root", is_admin=True)
    team_id = new_id()
    at = datetime(2026, 10, 23, 12, 0, 0, 123456, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team_id, name="mobile", created_at=clock()))
        run_ids = {new_id() for _ in range(7)}
        for run_id in run_ids:
            conn.execute(insert(runs).values(id=run_id, team_id=team_id, status="succeeded", started_at=at,
                                             cost_usd=Decimal("0.1")))
        conn.execute(insert(runs).values(id=new_id(), team_id=team_id, status="failed",
                                         started_at=at.replace(microsecond=123455)))
    client = make_client(engine, keyring, clock)
    sign_in_as(client, engine, keyring, clock, admin_id)
    seen, before = [], None
    while True:
        params = {"limit": 3, "status": "succeeded"} | ({"before": before} if before else {})
        page = client.get("/api/runs", params=params).json()
        seen += [run["id"] for run in page["runs"]]
        assert all(run["started_at"].startswith("2026-10-23T12:00:00.123456") for run in page["runs"])
        before = page["next_before"]
        if before is None:
            break
    assert len(seen) == len(set(seen)) == 7 and set(seen) == run_ids


def test_names_are_case_sensitive_and_usernames_are_not(engine, keyring, clock):
    admin_id, _ = add_user(engine, keyring, clock, "root", is_admin=True)
    client = make_client(engine, keyring, clock)
    csrf = sign_in_as(client, engine, keyring, clock, admin_id)
    assert client.post("/api/teams", json={"name": "Mobile"}, headers=mutation(csrf)).status_code == 200
    assert client.post("/api/teams", json={"name": "mobile"}, headers=mutation(csrf)).status_code == 200
    assert client.post("/api/teams", json={"name": "mobile"}, headers=mutation(csrf)).status_code == 409
    team_id = next(t["id"] for t in client.get("/api/teams").json()["teams"] if t["name"] == "mobile")
    for name in ("App", "app"):
        response = client.post(f"/api/teams/{team_id}/projects", json={"name": name}, headers=mutation(csrf))
        assert response.status_code == 200
    for name in ("TOKEN", "token"):
        response = client.post(f"/api/teams/{team_id}/secrets", json={"name": name, "value": "v"},
                               headers=mutation(csrf))
        assert response.status_code == 200
    assert len(client.get(f"/api/teams/{team_id}/secrets").json()["secrets"]) == 2
    assert client.post("/api/users", json={"username": "Bea"}, headers=mutation(csrf)).status_code == 200
    assert client.post("/api/users", json={"username": "bea"}, headers=mutation(csrf)).status_code == 409
    assert [u["username"] for u in client.get("/api/users/lookup", params={"q": "BEA"}).json()["users"]] == ["bea"]


def test_group_grants_match_names_exactly(engine, keyring, clock):
    from armature.server.config import OIDCConfig
    from armature.server.oidc import granted_roles
    with engine.begin() as conn:
        exact, other = new_id(), new_id()
        conn.execute(insert(teams).values(id=exact, name="Mobile", created_at=clock()))
        conn.execute(insert(teams).values(id=other, name="mobile", created_at=clock()))
        cfg = OIDCConfig(issuer="i", client_id="c", client_secret_ref="s",
                         group_roles={"g": [{"scope_kind": "team", "scope": "Mobile", "role": "viewer"}]})
        assert granted_roles(conn, cfg, {"g"}) == {("team", exact): "viewer"}

from decimal import Decimal

import pytest
from sqlalchemy import insert

from armature.server.db import new_id
from armature.server.db.schema import INSTANCE_SCOPE, run_steps, runs, settings, teams
from tests.server.app_support import Clock, add_user, login, make_client, mutation


@pytest.fixture
def clock():
    return Clock()


def test_health_and_security_headers(sqlite_engine, keyring, clock):
    client = make_client(sqlite_engine, keyring, clock)
    response = client.get("/health")
    assert response.json() == {"ok": True}
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY" and response.headers["cache-control"] == "no-store"


def test_errors_are_json_and_api_needs_session(sqlite_engine, keyring, clock):
    client = make_client(sqlite_engine, keyring, clock)
    for path in ("/api/teams", "/api/runs", "/api/users", "/api/audit", "/api/secrets", "/api/users/lookup?q=ab"):
        response = client.get(path)
        assert response.status_code == 401 and response.json() == {"error": "Sign in first."}
    assert client.get("/api/nope").json() == {"error": "Not Found"}


def test_body_limit(sqlite_engine, keyring, clock):
    client = make_client(sqlite_engine, keyring, clock)
    response = client.post("/api/login", content=b"{" + b" " * 70000 + b"}",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413


@pytest.mark.parametrize("base", ["", "/armature"])
def test_ui_pages_and_assets(sqlite_engine, keyring, clock, base):
    client = make_client(sqlite_engine, keyring, clock, base_path=base)
    if base:
        assert client.get(base).headers["location"] == f"{base}/"
    assert client.get(f"{base}/").headers["location"] == f"{base}/login"
    page = client.get(f"{base}/login")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert f'<meta name="armature-base" content="{base}">' in page.text
    assert client.get(f"{base}/static/login.js").status_code == 200
    asset = client.get(f"{base}/static/style.css")
    assert asset.status_code == 200 and asset.headers["content-type"].startswith("text/css")
    assert client.get(f"{base}/static/views/runs.js").status_code == 200
    assert client.get(f"{base}/static/../config.py").status_code == 404
    assert client.get(f"{base}/static/index.html").status_code == 404

    _, secret = add_user(sqlite_engine, keyring, clock, "ana")
    csrf = client.get(f"{base}/api/session").json()["csrf"]
    signed = client.post(f"{base}/api/login", json={"username": "ana", "password": "correct horse battery",
                                                    "totp": clock.code(secret)}, headers=mutation(csrf))
    assert signed.status_code == 200
    assert f"Path={base or '/'}" in signed.headers["set-cookie"]
    index = client.get(f"{base}/")
    assert index.status_code == 200
    assert f'<meta name="armature-base" content="{base}">' in index.text
    assert 'src="static/app.js"' in index.text


def test_meta_base_is_escaped(sqlite_engine, keyring, clock, tmp_path):
    (tmp_path / "login.html").write_text('<head><meta name="armature-base" content=""></head>')
    client = make_client(sqlite_engine, keyring, clock, static_dir=tmp_path, base_path='/a"b')
    assert '<meta name="armature-base" content="/a&quot;b">' in client.get('/a"b/login').text


def test_login_session_and_runs_on_every_database(engine, keyring, clock):
    """The smoke that runs on Postgres and MySQL too, through the db_matrix marker."""
    user_id, secret = add_user(engine, keyring, clock, "ana", is_admin=True)
    team_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team_id, name="mobile", created_at=clock()))
        for index in range(3):
            clock.advance(seconds=1)
            conn.execute(insert(runs).values(id=new_id(), team_id=team_id, status="succeeded", started_at=clock(),
                                             cost_usd=Decimal("1.25")))
    client = make_client(engine, keyring, clock)
    response = login(client, "ana", clock.code(secret))
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    assert client.get("/api/session").json()["user"]["id"] == user_id
    first = client.get("/api/runs?limit=2").json()
    rest = client.get("/api/runs", params={"limit": 2, "before": first["next_before"]}).json()
    assert len(first["runs"]) == 2 and len(rest["runs"]) == 1 and first["runs"][0]["cost_usd"] == 1.25
    assert client.post("/api/teams", json={"name": "web"}, headers=mutation(csrf)).status_code == 200
    assert login(client, "ana", clock.code(secret)).status_code == 401
    assert client.post(f"/api/users/{user_id}", json={"is_admin": False}, headers=mutation(csrf)).status_code == 409
    assert client.post("/api/logout", headers=mutation(csrf)).status_code == 200
    assert client.get("/api/teams").status_code == 401


@pytest.mark.parametrize("show_cost", [True, False])
def test_runs_api_honors_show_cost(sqlite_engine, keyring, clock, show_cost):
    _user_id, secret = add_user(sqlite_engine, keyring, clock, "ana", is_admin=True)
    team_id, run_id = new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team_id, name="mobile", created_at=clock()))
        conn.execute(insert(runs).values(id=run_id, team_id=team_id, status="succeeded", started_at=clock(),
                                         cost_usd=Decimal("1.25")))
        conn.execute(insert(run_steps).values(run_id=run_id, step_key="review", step_id="review",
                                              status="succeeded", cost_usd=Decimal("0.5")))
        conn.execute(insert(settings).values(scope=INSTANCE_SCOPE, key="display.show_cost", value=show_cost,
                                             updated_at=clock()))
    client = make_client(sqlite_engine, keyring, clock)
    assert login(client, "ana", clock.code(secret)).status_code == 200
    listed = client.get("/api/runs").json()["runs"]
    detail = client.get(f"/api/runs/{run_id}").json()
    expected = (1.25, 0.5) if show_cost else (None, None)
    assert (listed[0]["cost_usd"], detail["steps"][0]["cost_usd"]) == expected
    assert detail["run"]["cost_usd"] == expected[0]


def _subject_runs(engine, clock):
    team_id = new_id()
    made = {}
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team_id, name="mobile", created_at=clock()))
        for name, subject, workflow, status in [
            ("dev_failed", "MEM-1", "development", "failed"), ("loose_old", None, "digest", "succeeded"),
            ("review_old", "acme/app#7", "code-review", "succeeded"), ("loose_new", None, "digest", "succeeded"),
            ("fix", "acme/app#7", "pr-fix", "failed"), ("dev_rerun", "MEM-1", "development", "running"),
            ("review_new", "acme/app#7", "code-review", "succeeded"),
        ]:
            clock.advance(seconds=1)
            made[name] = new_id()
            conn.execute(insert(runs).values(id=made[name], team_id=team_id, status=status, started_at=clock(),
                                             subject=subject, workflow_name=workflow))
    return made


def test_grouped_runs_keep_the_latest_per_subject(engine, keyring, clock):
    _user_id, secret = add_user(engine, keyring, clock, "ana", is_admin=True)
    made = _subject_runs(engine, clock)
    client = make_client(engine, keyring, clock)
    assert login(client, "ana", clock.code(secret)).status_code == 200

    def listed(**params):
        return [(r["id"], r.get("group_size")) for r in client.get("/api/runs", params=params).json()["runs"]]

    assert listed(group=True) == [(made["review_new"], 3), (made["dev_rerun"], 2), (made["loose_new"], 1),
                                  (made["loose_old"], 1)]
    assert listed(group=True, status="failed") == [(made["fix"], 1), (made["dev_failed"], 1)]
    assert listed(group=True, workflows="code-review") == [(made["review_new"], 2)]
    assert listed(group=True, subject="MEM-1") == [(made["dev_rerun"], 2)]
    newest_first = ["review_new", "dev_rerun", "fix", "loose_new", "review_old", "loose_old", "dev_failed"]
    assert listed() == [(made[name], None) for name in newest_first]

    seen, before = [], None
    while True:
        page = client.get("/api/runs", params={"group": True, "limit": 1} | ({"before": before} if before else {})).json()
        seen += [r["id"] for r in page["runs"]]
        before = page["next_before"]
        if before is None:
            break
    assert seen == [made["review_new"], made["dev_rerun"], made["loose_new"], made["loose_old"]]


def test_a_pull_requests_runs_group_with_the_issue_thread(engine, keyring, clock):
    _user_id, secret = add_user(engine, keyring, clock, "ana", is_admin=True)
    team_id = new_id()
    made = {}
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team_id, name="mobile", created_at=clock()))
        for name, subject, thread, workflow, title in [
            ("dev", "MEM-1", "MEM-1", "development", "Build it"),
            ("review", "acme/app#7", "MEM-1", "code-review", "[MEM-1] Build it"),
            ("other_pull", "acme/app#8", None, "code-review", "Unrelated"),
            ("fix", "acme/app#7", "MEM-1", "pr-fix", "Fix review comments"),
        ]:
            clock.advance(seconds=1)
            made[name] = new_id()
            conn.execute(insert(runs).values(id=made[name], team_id=team_id, status="succeeded", started_at=clock(),
                                             subject=subject, thread=thread, workflow_name=workflow,
                                             summary={"title": title}))
    client = make_client(engine, keyring, clock)
    assert login(client, "ana", clock.code(secret)).status_code == 200

    def listed(**params):
        return [(r["id"], r.get("group_size"), r["thread"], r["title"])
                for r in client.get("/api/runs", params=params).json()["runs"]]

    assert listed(group=True) == [(made["fix"], 3, "MEM-1", "Fix review comments"),
                                  (made["other_pull"], 1, None, "Unrelated")]
    assert [row[0] for row in listed(thread="MEM-1")] == [made["fix"], made["review"], made["dev"]]
    assert [row[0] for row in listed(thread="acme/app#8")] == [made["other_pull"]]
    assert listed(thread="acme/app#7") == []
    detail = client.get(f"/api/runs/{made['review']}").json()["run"]
    assert (detail["subject"], detail["thread"]) == ("acme/app#7", "MEM-1")

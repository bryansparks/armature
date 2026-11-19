"""Tracked repositories: the team lead's routes, resolving the GitHub ID, sync state seeding, and removal."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, select, update

from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    gh_check_runs,
    gh_conflict_samples,
    gh_pr_commits,
    gh_pr_files,
    gh_pr_links,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    gh_sync_state,
    people,
)
from armature.server.db.schema import connections as connection_rows
from tests.server.app_support import (
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)
from tests.server.tool_support import (  # noqa: F401
    Vendors,
    clock,
    dns,
    gh_cli,
    pem,
    world,
)

GH_REPO_ID = 4242


class RepoVendors(Vendors):
    """The tools' fake GitHub, plus the repository lookup a tracked repository needs."""

    def __init__(self):
        super().__init__()
        self.repos = {"/repos/acme/app": {"id": GH_REPO_ID, "full_name": "Acme/App"},
                      "/repos/acme/lib": {"id": 4343, "full_name": "Acme/Lib"}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        found = self.repos.get(request.url.path.lower())
        if found is not None and request.method == "GET":
            self.requests.append(request)
            return httpx.Response(200, json=found)
        return super().__call__(request)


@pytest.fixture
def vendor():
    return RepoVendors()


def _signed_in(engine, keyring, clock, vendor, user_id, step_up=True):
    client = make_client(engine, keyring, clock)
    client.app.state.vendor_transport = httpx.MockTransport(vendor)
    return SimpleNamespace(client=client, headers=mutation(sign_in_as(client, engine, keyring, clock, user_id,
                                                                      step_up=step_up)))


@pytest.fixture
def users(sqlite_engine, keyring, clock, world):
    found = {}
    for name, team, role in (("lead", world.team, "team_lead"), ("member", world.team, "contributor"),
                             ("outsider", world.other, "team_lead")):
        found[name] = add_user(sqlite_engine, keyring, clock, name)[0]
        add_membership(sqlite_engine, clock, found[name], "team", team, role)
    return found


@pytest.fixture
def lead(sqlite_engine, keyring, clock, vendor, users):
    return _signed_in(sqlite_engine, keyring, clock, vendor, users["lead"])


def _track(lead, world, **body):
    return lead.client.post(f"/api/teams/{world.team}/repos", json={"full_name": "acme/app", **body},
                            headers=lead.headers)


def _rows(engine, table, **where):
    with engine.connect() as conn:
        return conn.execute(select(table).where(*[table.c[k] == v for k, v in where.items()])).all()


def test_a_team_lead_tracks_a_repository_by_name(sqlite_engine, lead, world, clock, vendor):
    response = _track(lead, world, backfill_since="2025-05-01")
    assert response.status_code == 200, response.text
    repo = response.json()["repo"]
    assert (repo["gh_repo_id"], repo["full_name"], repo["project_id"], repo["project_name"]) == \
        (GH_REPO_ID, "acme/app", world.project, "app")
    assert (repo["connection_id"], repo["enabled"], repo["backfill_since"]) == (None, True, "2025-05-01")
    assert [s["kind"] for s in repo["sync"]] == ["backfill", "poll", "conflicts", "gap_fill"]
    with sqlite_engine.connect() as conn:
        states = conn.execute(select(gh_sync_state)).all()
        logged = conn.execute(select(audit).where(audit.c.action == "gh_repo.create")).one()
    assert {(s.kind, s.next_run_at, s.cursor, s.last_error) for s in states} == \
        {(kind, clock(), None, None) for kind in ("backfill", "poll", "conflicts", "gap_fill")}
    assert logged.detail == {"full_name": "acme/app", "gh_repo_id": GH_REPO_ID, "connection_id": None}
    [looked_up] = [r for r in vendor.requests if r.url.path.lower() == "/repos/acme/app"]
    assert looked_up.headers["authorization"].startswith("Bearer ghs_minted")

    listed = lead.client.get(f"/api/teams/{world.team}/repos").json()
    assert [r["full_name"] for r in listed["repos"]] == ["acme/app"]
    assert [c["id"] for c in listed["connections"]] == [world.github]
    again = _track(lead, world)
    assert again.status_code == 409 and again.json()["error"] == "acme/app is already tracked in this team."
    assert len([r for r in vendor.requests if r.url.path.lower() == "/repos/acme/app"]) == 1


def test_only_the_teams_lead_manages_its_repositories(sqlite_engine, keyring, clock, vendor, users, lead, world):
    repo_id = _track(lead, world).json()["repo"]["id"]
    member = _signed_in(sqlite_engine, keyring, clock, vendor, users["member"])
    outsider = _signed_in(sqlite_engine, keyring, clock, vendor, users["outsider"])
    path = f"/api/teams/{world.team}/repos"
    assert member.client.get(path).status_code == 403
    assert _track(member, world).status_code == 403
    assert member.client.post(f"{path}/{repo_id}", json={"enabled": False}, headers=member.headers).status_code == 403
    assert outsider.client.get(path).status_code == 404
    assert outsider.client.post(f"{path}/{repo_id}/delete", json={}, headers=outsider.headers).status_code == 404
    refused = member.client.post(f"{path}/{repo_id}/delete", json={}, headers=member.headers)
    assert refused.status_code == 403
    theirs = outsider.client.post(f"/api/teams/{world.other}/repos/{repo_id}", json={"enabled": False},
                                   headers=outsider.headers)
    assert theirs.status_code == 404 and theirs.json()["error"] == "No such tracked repository."
    assert make_client(sqlite_engine, keyring, clock).get(path).status_code == 401
    assert [r.enabled for r in _rows(sqlite_engine, gh_repos)] == [True]


def test_a_repository_github_cant_resolve_is_refused(sqlite_engine, lead, world, clock):
    missing = _track(lead, world, full_name="acme/nope")
    assert missing.status_code == 409
    assert missing.json()["error"] == \
        "Couldn't look up acme/nope through connection gh: GitHub GET /repos/acme/nope answered HTTP 404."
    uninstalled = _track(lead, world, full_name="elsewhere/app")
    assert uninstalled.status_code == 409 and "No github_app connection fits" in uninstalled.json()["error"]
    assert _track(lead, world, full_name="not a repo").status_code == 400
    assert _track(lead, world, backfill_since="May 1").status_code == 400
    assert _track(lead, world, connection_id=world.slack).json()["error"] == "No such GitHub connection in this team."
    with sqlite_engine.begin() as conn:
        conn.execute(update(connection_rows).where(connection_rows.c.id == world.github).values(state="error"))
    unverified = _track(lead, world, connection_id=world.github)
    assert unverified.status_code == 409
    assert unverified.json()["error"] == "Connection gh is not verified. Verify it first."
    assert _rows(sqlite_engine, gh_repos) == [] and _rows(sqlite_engine, gh_sync_state) == []


@pytest.mark.usefixtures("gh_cli")
def test_a_personal_token_beyond_the_teams_projects_needs_an_admin_who_pins_it(
        sqlite_engine, keyring, clock, vendor, lead, world):
    refused = _track(lead, world, full_name="Acme/Lib")
    assert refused.status_code == 403 and "needs an admin" in refused.json()["error"]
    assert _rows(sqlite_engine, gh_repos) == [] and vendor.requests == []
    admin = _signed_in(sqlite_engine, keyring, clock, vendor, add_user(sqlite_engine, keyring, clock, "root",
                                                                       is_admin=True)[0])
    response = _track(admin, world, full_name="Acme/Lib")
    assert response.status_code == 200, response.text
    repo = response.json()["repo"]
    assert (repo["gh_repo_id"], repo["project_id"], repo["connection_id"]) == (4343, None, world.github)
    reached = _track(lead, world)
    assert reached.status_code == 200 and reached.json()["repo"]["connection_id"] is None
    path = f"/api/teams/{world.team}/repos/{reached.json()['repo']['id']}"
    assert lead.client.post(path, json={"connection_id": world.github}, headers=lead.headers).status_code == 200
    pinned = f"/api/teams/{world.team}/repos/{repo['id']}"
    assert lead.client.post(pinned, json={"connection_id": None}, headers=lead.headers).status_code == 200
    again = lead.client.post(pinned, json={"connection_id": world.github}, headers=lead.headers)
    assert again.status_code == 403 and "needs an admin" in again.json()["error"]
    assert admin.client.post(pinned, json={"connection_id": world.github}, headers=admin.headers).status_code == 200


def test_changing_backfill_since_reopens_a_finished_backfill_where_it_stopped(sqlite_engine, lead, world, clock):
    repo_id = _track(lead, world).json()["repo"]["id"]
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind == "backfill")
                     .values(next_run_at=None, cursor='{"after": "75"}'))
    path = f"/api/teams/{world.team}/repos/{repo_id}"
    assert lead.client.post(path, json={"enabled": False}, headers=lead.headers).status_code == 200
    assert _rows(sqlite_engine, gh_sync_state, kind="backfill")[0].next_run_at is None
    clock.advance(minutes=5)
    assert lead.client.post(path, json={"backfill_since": "2024-01-01"}, headers=lead.headers).status_code == 200
    [state] = _rows(sqlite_engine, gh_sync_state, kind="backfill")
    assert (state.next_run_at, state.cursor) == (clock(), '{"after": "75"}')


def test_a_lead_updates_a_tracked_repository(sqlite_engine, lead, world):
    repo_id = _track(lead, world).json()["repo"]["id"]
    path = f"/api/teams/{world.team}/repos/{repo_id}"
    changed = lead.client.post(path, json={"enabled": False, "backfill_since": "2025-01-31",
                                            "connection_id": world.github}, headers=lead.headers)
    assert changed.status_code == 200, changed.text
    repo = changed.json()["repo"]
    assert (repo["enabled"], repo["backfill_since"], repo["connection_name"]) == (False, "2025-01-31", "gh")
    assert _rows(sqlite_engine, gh_repos)[0].backfill_since == date(2025, 1, 31)
    assert lead.client.post(path, json={"enabled": "no"}, headers=lead.headers).status_code == 400
    assert lead.client.post(path, json={"connection_id": world.slack}, headers=lead.headers).status_code == 400
    in_use = lead.client.post(f"/api/connections/{world.github}/delete", headers=lead.headers)
    assert in_use.status_code == 409 and "acme/app" in in_use.json()["error"]
    cleared = lead.client.post(path, json={"connection_id": None, "backfill_since": None}, headers=lead.headers)
    assert (cleared.json()["repo"]["connection_id"], cleared.json()["repo"]["backfill_since"]) == (None, None)
    with sqlite_engine.connect() as conn:
        assert [r.detail["changed"] for r in conn.execute(select(audit).where(audit.c.action == "gh_repo.update"))] \
            == [["backfill_since", "connection_id", "enabled"], ["backfill_since", "connection_id"]]


def _history(engine, team, repo_row_id, clock, number, person_id):
    pr = new_id()
    now = clock()
    with engine.begin() as conn:
        conn.execute(insert(gh_pull_requests).values(
            id=pr, team_id=team, repo_row_id=repo_row_id, gh_pr_id=number, number=number, author_person_id=person_id,
            author_gh_id=1, author_login="octo", state="open", created_at_gh=now, gh_updated_at=now, created_at=now,
            updated_at=now))
        conn.execute(insert(gh_pushes).values(id=new_id(), team_id=team, pr_row_id=pr, before_sha="a", after_sha="b",
                                              pushed_at=now, source="webhook", created_at=now))
        conn.execute(insert(gh_reviews).values(id=new_id(), team_id=team, pr_row_id=pr, gh_review_id=number,
                                               reviewer_gh_id=2, reviewer_login="rev", state="approved",
                                               created_at=now, updated_at=now))
        conn.execute(insert(gh_review_comments).values(id=new_id(), team_id=team, pr_row_id=pr, gh_comment_id=number,
                                                       author_gh_id=2, author_login="rev", created_at_gh=now,
                                                       updated_at_gh=now, created_at=now))
        conn.execute(insert(gh_check_runs).values(id=new_id(), team_id=team, pr_row_id=pr, gh_check_run_id=number,
                                                  repo_row_id=repo_row_id, name="build", head_sha="b",
                                                  status="completed", created_at=now))
        conn.execute(insert(gh_pr_commits).values(id=new_id(), team_id=team, repo_row_id=repo_row_id,
                                                  commit_sha=f"c{number}", pr_row_id=pr, created_at=now))
        conn.execute(insert(gh_conflict_samples).values(id=new_id(), team_id=team, pr_row_id=pr, sampled_at=now,
                                                        mergeable="conflicting"))
        conn.execute(insert(gh_pr_files).values(id=new_id(), team_id=team, repo_row_id=repo_row_id, pr_row_id=pr,
                                                path="src/a.py", path_key="k", created_at=now))
    return pr


def test_removing_a_repository_deletes_its_history(sqlite_engine, keyring, clock, vendor, users, lead, world):
    repo_id = _track(lead, world).json()["repo"]["id"]
    other_id = _track(lead, world, full_name="acme/lib").json()["repo"]["id"]
    person = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(people).values(id=person, team_id=world.team, display_name="octo", kind="human",
                                           created_at=clock(), updated_at=clock()))
    mine = _history(sqlite_engine, world.team, repo_id, clock, 1, person)
    kept = _history(sqlite_engine, world.team, other_id, clock, 2, person)
    with sqlite_engine.begin() as conn:
        for source, target in ((kept, mine), (mine, kept)):
            conn.execute(insert(gh_pr_links).values(id=new_id(), team_id=world.team, from_pr_row_id=source,
                                                    to_pr_row_id=target, kind="revert", confidence=1.0,
                                                    detector_version="1", created_at=clock()))
        conn.execute(insert(gh_check_runs).values(id=new_id(), team_id=world.team, pr_row_id=None, gh_check_run_id=99,
                                                  repo_row_id=repo_id, name="lint", head_sha="e",
                                                  status="completed", created_at=clock()))
    path = f"/api/teams/{world.team}/repos/{repo_id}"
    unstepped = _signed_in(sqlite_engine, keyring, clock, vendor, users["lead"], step_up=False)
    unstepped_delete = unstepped.client.post(f"{path}/delete", json={}, headers=unstepped.headers)
    assert unstepped_delete.json()["error"] == "step_up_required"
    assert lead.client.post(f"{path}/delete", json={}, headers=lead.headers).json() == {"ok": True}
    assert [r.gh_check_run_id for r in _rows(sqlite_engine, gh_check_runs)] == [2]
    for table in (gh_pushes, gh_reviews, gh_review_comments, gh_check_runs, gh_pr_commits, gh_conflict_samples,
                  gh_pr_files):
        assert [r.pr_row_id for r in _rows(sqlite_engine, table)] == [kept], table.name
    assert [r.id for r in _rows(sqlite_engine, gh_pull_requests)] == [kept]
    assert _rows(sqlite_engine, gh_pr_links) == []
    assert {r.repo_row_id for r in _rows(sqlite_engine, gh_sync_state)} == {other_id}
    assert [r.id for r in _rows(sqlite_engine, gh_repos)] == [other_id]
    assert len(_rows(sqlite_engine, people)) == 1
    with sqlite_engine.connect() as conn:
        logged = conn.execute(select(audit).where(audit.c.action == "gh_repo.delete")).one()
    assert logged.detail == {"full_name": "acme/app", "pull_requests": 1}
    assert lead.client.post(f"{path}/delete", json={}, headers=lead.headers).status_code == 404

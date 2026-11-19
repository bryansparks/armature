"""The GitHub sync service: backfill, poll, conflicts, and gap fill against a fake GitHub, its rate-limit manners,
the claim between servers, and the pushes it derives and classifies."""
from __future__ import annotations

import json
import re
import threading
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, select, update

from armature.server.db import new_id
from armature.server.db.schema import (
    findings,
    gh_check_runs,
    gh_conflict_samples,
    gh_pr_files,
    gh_pr_links,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    gh_sync_state,
    run_steps,
    runs,
    settings,
    tool_calls,
)
from armature.server.insights import attribution, collect, ghapi, repos, sync
from tests.server.tool_support import (  # noqa: F401
    BOT,
    Vendors,
    clock,
    dns,
    gh_cli,
    pem,
    start_run,
    vendor,
    world,
)

GH_REPO_ID = 4242
OCTO = {"__typename": "User", "login": "Octo", "databaseId": 101}
REV = {"__typename": "User", "login": "rev", "databaseId": 202}
ARMATURE_BOT = {"__typename": "Bot", "login": BOT.removesuffix("[bot]"), "databaseId": 9001}
BASE = "b" * 40
RESET = "2026-10-23T13:00:00Z"


def _sha(n: int) -> str:
    return f"{n:040x}"


def _commit(oid, at, *, parents=(), headline="work", checked=True):
    return {"oid": oid, "committedDate": at, "messageHeadline": headline,
            "parents": {"nodes": [{"oid": p} for p in parents]}, "author": {"email": "dev@acme.test", "user": None},
            "statusCheckRollup": {"state": "SUCCESS"} if checked else None}


def _node(number, *, created="2026-10-01T10:00:00Z", updated=None, state="OPEN", head=None, title=None, body=None,
          reviews=(), commits=(), forced=(), files=(), merged_at=None, merge=None, author=OCTO):
    head = head or (commits[-1]["oid"] if commits else _sha(number))
    return {"databaseId": 900000 + number, "number": number, "title": title or f"Change {number}", "body": body,
            "state": state, "isDraft": False, "createdAt": created, "updatedAt": updated or created,
            "closedAt": merged_at, "mergedAt": merged_at, "additions": 1, "deletions": 1, "changedFiles": 1,
            "baseRefName": "main", "headRefName": f"feature-{number}", "baseRefOid": BASE, "headRefOid": head,
            "headRepository": {"databaseId": GH_REPO_ID}, "mergeCommit": {"oid": merge} if merge else None,
            "author": author, "mergedBy": None, "reviews": {"nodes": list(reviews)},
            "commits": {"totalCount": len(commits), "nodes": [{"commit": c} for c in commits]},
            "timelineItems": {"nodes": list(forced)}, "files": {"nodes": [{"path": p} for p in files]}}


class FakeGitHub:
    """GitHub's GraphQL API over a list of pull request nodes, paged by offset cursors."""

    def __init__(self):
        self.prs: list[dict] = []
        self.objects: dict[str, dict] = {}
        self.remaining = 4999
        self.reset = RESET
        self.queue: list = []
        self.fail_after: set[str] = set()
        self.seen: list[dict] = []
        self.repo_id = GH_REPO_ID
        self.name = "Acme/App"

    def __call__(self, body):
        self.seen.append(body)
        if self.queue:
            return self.queue.pop(0)
        query, variables = body["query"], body["variables"]
        if variables.get("after") in self.fail_after:
            return httpx.Response(500)
        if f"{variables['owner']}/{variables['name']}".lower() != self.name.lower():
            return {"data": None, "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]}
        repo = {"databaseId": self.repo_id, "nameWithOwner": self.name}
        if "pullRequests(states: OPEN" in query:
            repo["pullRequests"] = self._page([{"databaseId": p["databaseId"], "mergeable": p.get("mergeable"),
                                                "mergeStateStatus": p.get("mergeStateStatus")}
                                               for p in self.prs if p["state"] == "OPEN"], variables)
        elif "pullRequests(" in query:
            key = "createdAt" if "CREATED_AT" in query else "updatedAt"
            repo["pullRequests"] = self._page(sorted(self.prs, key=lambda p: p[key], reverse=True), variables)
        elif "pullRequest(number" in query:
            repo["pullRequest"] = next((p for p in self.prs if p["number"] == variables["number"]), None)
        else:
            for name, oid in variables.items():
                if re.fullmatch(r"o\d+", name):
                    repo[f"c{name[1:]}"] = self.objects.get(oid)
        return {"data": {"rateLimit": {"cost": 1, "remaining": self.remaining, "resetAt": self.reset},
                         "repository": repo}}

    def _page(self, nodes, variables):
        start = int(variables["after"]) if variables.get("after") else 0
        chunk = nodes[start:start + variables["first"]]
        end = start + len(chunk)
        return {"pageInfo": {"hasNextPage": end < len(nodes), "endCursor": str(end)}, "nodes": chunk}

    def asked(self, marker):
        return [b["variables"] for b in self.seen if marker in b["query"]]


@pytest.fixture(autouse=True)
def unpaused():
    ghapi._paused.clear()
    yield
    ghapi._paused.clear()


@pytest.fixture
def github(vendor):
    fake = FakeGitHub()
    vendor.github_graphql = fake
    return fake


@pytest.fixture
def tracked(sqlite_engine, world, clock):
    row_id = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(gh_repos).values(id=row_id, team_id=world.team, gh_repo_id=GH_REPO_ID,
                                             full_name="acme/app", enabled=True, created_at=clock(),
                                             updated_at=clock()))
        conn.execute(insert(gh_sync_state), [{"id": new_id(), "team_id": world.team, "repo_row_id": row_id,
                                              "kind": kind, "next_run_at": clock(), "updated_at": clock()}
                                             for kind in sync.KINDS])
    return row_id


def _service(engine, keyring, clock, vendor, sleeps=None):
    return sync.SyncService(engine, keyring, clock=clock, transport=lambda: httpx.MockTransport(vendor),
                            sleep=sleeps.append if sleeps is not None else lambda _: None)


def _only(engine, kind, clock):
    """Make only ``kind`` due, so a tick runs just that."""
    with engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind != kind)
                     .values(next_run_at=clock() + timedelta(days=30)))
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind == kind).values(next_run_at=clock()))


def _state(engine, kind):
    with engine.connect() as conn:
        return conn.execute(select(gh_sync_state).where(gh_sync_state.c.kind == kind)).first()


def _rows(engine, table, *where):
    with engine.connect() as conn:
        return conn.execute(select(table).where(*where).order_by(table.c.id)).all()


def _numbers(engine):
    return sorted(r.number for r in _rows(engine, gh_pull_requests))


def _store_pr(engine, team, repo, number, *, updated, title="Stored", state="open"):
    row_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(gh_pull_requests).values(
            id=row_id, team_id=team, repo_row_id=repo, gh_pr_id=900000 + number, number=number, title=title,
            author_gh_id=101, author_login="Octo", state=state, created_at_gh=updated, gh_updated_at=updated,
            created_at=updated, updated_at=updated))
    return row_id


# backfill

def test_backfill_pages_from_its_cursor_resumes_after_a_failure_and_stops_at_the_floor(
        sqlite_engine, keyring, clock, vendor, github, tracked, monkeypatch):
    monkeypatch.setattr(sync, "PAGE", 2)
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_repos).values(backfill_since=date(2026, 9, 1)))
    github.prs = [_node(1, created="2026-08-15T10:00:00Z")] + [_node(n, created=f"2026-09-0{n}T10:00:00Z")
                                                               for n in range(2, 7)]
    github.fail_after = {"4"}
    _only(sqlite_engine, "backfill", clock)
    service = _service(sqlite_engine, keyring, clock, vendor)

    assert service.tick() == ["error: GitHub POST /graphql answered HTTP 500."]
    state = _state(sqlite_engine, "backfill")
    assert sync._cursor(state.cursor)["after"] == "4" and state.failures == 1
    assert _numbers(sqlite_engine) == [3, 4, 5, 6]

    github.fail_after = set()
    clock.advance(minutes=2)
    asked = len(github.asked("CREATED_AT"))
    assert service.tick() == ["ok"]
    assert [v["after"] for v in github.asked("CREATED_AT")[asked:]] == ["4"]
    state = _state(sqlite_engine, "backfill")
    assert (state.next_run_at, state.last_error, state.failures) == (None, None, 0)
    assert sync._cursor(state.cursor) == {"after": "4"}
    assert _numbers(sqlite_engine) == [2, 3, 4, 5, 6]
    clock.advance(days=1)
    assert service.tick() == []


def test_backfill_links_reverts_and_fixes_to_older_pull_requests_it_stores_later(
        sqlite_engine, keyring, clock, vendor, github, tracked, monkeypatch, world):
    monkeypatch.setattr(sync, "PAGE", 1)
    parser = _node(10, created="2026-09-02T10:00:00Z", state="MERGED", merged_at="2026-09-03T10:00:00Z",
                   merge=_sha(1010), files=["src/parser.py", "src/lexer.py", "README.md"], title="Add the parser")
    ancient = _node(9, created="2026-05-01T10:00:00Z", state="MERGED", merged_at="2026-05-02T10:00:00Z",
                    merge=_sha(1009), files=["src/parser.py", "src/lexer.py"], title="First parser")
    fix = _node(11, created="2026-09-10T10:00:00Z", state="MERGED", merged_at="2026-09-11T10:00:00Z",
                merge=_sha(1011), files=["src/parser.py", "src/lexer.py"], title="fix: parser crash on empty input")
    revert = _node(12, created="2026-09-12T10:00:00Z", state="MERGED", merged_at="2026-09-12T11:00:00Z",
                   merge=_sha(1012), title="Back out the parser", body="Reverts #10")
    github.prs = [ancient, parser, fix, revert]
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_repos).values(backfill_since=date(2026, 1, 1)))
    _only(sqlite_engine, "backfill", clock)
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == ["ok"]

    ids = {r.number: r.id for r in _rows(sqlite_engine, gh_pull_requests)}
    links = {(r.from_pr_row_id, r.to_pr_row_id, r.kind): r for r in _rows(sqlite_engine, gh_pr_links)}
    assert set(links) == {(ids[12], ids[10], "revert"), (ids[11], ids[10], "fix")}
    fixed = links[(ids[11], ids[10], "fix")]
    assert (fixed.confidence, fixed.detector_version, fixed.evidence["files"]) == (0.4, "1", 2)
    assert {r.path for r in _rows(sqlite_engine, gh_pr_files, gh_pr_files.c.pr_row_id == ids[10])} == {
        "src/parser.py", "src/lexer.py", "README.md"}


def test_a_low_rate_limit_stores_the_page_it_paid_for_then_pauses_every_kind_on_the_token(
        sqlite_engine, keyring, clock, vendor, github, tracked, monkeypatch):
    monkeypatch.setattr(sync, "PAGE", 1)
    github.prs = [_node(1), _node(2, created="2026-10-02T10:00:00Z")]
    github.remaining = 1200
    service = _service(sqlite_engine, keyring, clock, vendor)
    _only(sqlite_engine, "backfill", clock)
    assert service.tick() == ["paused until 2026-10-23T13:00:00+00:00"]
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind == "poll").values(next_run_at=clock()))
    assert service.tick() == ["paused until 2026-10-23T13:00:00+00:00"]
    assert _numbers(sqlite_engine) == [2]
    assert sync._cursor(_state(sqlite_engine, "backfill").cursor)["after"] == "1"
    assert len(github.asked("CREATED_AT")) == 1 and github.asked("UPDATED_AT") == []
    for kind in ("backfill", "poll"):
        state = _state(sqlite_engine, kind)
        assert state.next_run_at.isoformat() == "2026-10-23T13:00:00+00:00"
        assert (state.last_error, state.failures) == (None, None)


def test_a_run_that_finishes_on_a_low_limit_waits_for_the_reset(
        sqlite_engine, keyring, clock, vendor, github, tracked):
    github.remaining = 1000
    _only(sqlite_engine, "conflicts", clock)
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == ["ok"]
    assert _state(sqlite_engine, "conflicts").next_run_at.isoformat() == "2026-10-23T13:00:00+00:00"


def test_a_reset_time_in_the_past_still_pauses_a_minute(
        sqlite_engine, keyring, clock, vendor, github, tracked, monkeypatch):
    monkeypatch.setattr(sync, "PAGE", 1)
    github.prs = [_node(1), _node(2, created="2026-10-02T10:00:00Z")]
    github.remaining, github.reset = 900, "2026-10-23T11:00:00Z"
    _only(sqlite_engine, "backfill", clock)
    _service(sqlite_engine, keyring, clock, vendor).tick()
    assert _state(sqlite_engine, "backfill").next_run_at == clock() + timedelta(minutes=1)


def test_a_secondary_rate_limit_pauses_for_its_retry_after(sqlite_engine, keyring, clock, vendor, github, tracked):
    github.queue = [httpx.Response(403, headers={"Retry-After": "120"},
                                   json={"message": "You have exceeded a secondary rate limit."})]
    _only(sqlite_engine, "conflicts", clock)
    _service(sqlite_engine, keyring, clock, vendor).tick()
    assert _state(sqlite_engine, "conflicts").next_run_at == clock() + timedelta(seconds=120)


def test_a_502_retries_with_half_the_page_and_gives_up_after_three(
        sqlite_engine, keyring, clock, vendor, github, tracked):
    github.prs = [_node(1)]
    github.queue = [httpx.Response(502)]
    sleeps = []
    _only(sqlite_engine, "conflicts", clock)
    assert _service(sqlite_engine, keyring, clock, vendor, sleeps).tick() == ["ok"]
    assert [v["first"] for v in github.asked("states: OPEN")] == [100, 50]
    assert sleeps == [1.0]

    github.queue = [httpx.Response(502)] * 4
    sleeps.clear()
    clock.advance(minutes=10)
    assert _service(sqlite_engine, keyring, clock, vendor, sleeps).tick() == [
        "error: GitHub POST /graphql answered HTTP 502."]
    assert sleeps == [1.0, 2.0, 4.0]


# poll

def test_poll_refreshes_what_changed_since_its_watermark_and_moves_it(
        sqlite_engine, keyring, clock, vendor, github, tracked, monkeypatch):
    monkeypatch.setattr(sync, "PAGE", 1)
    github.prs = [_node(1, updated="2026-10-23T09:00:00Z"), _node(2, updated="2026-10-23T11:00:00Z"),
                  _node(3, updated="2026-10-23T11:30:00Z")]
    _only(sqlite_engine, "poll", clock)
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind == "poll")
                     .values(watermark=clock() - timedelta(hours=2)))
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == ["ok"]
    assert _numbers(sqlite_engine) == [2, 3]
    assert [v["after"] for v in github.asked("UPDATED_AT")] == [None, "1", "2"]
    state = _state(sqlite_engine, "poll")
    assert state.watermark.isoformat() == "2026-10-23T11:30:00+00:00"
    assert (state.cursor, state.next_run_at) == (None, clock() + timedelta(minutes=5))


def test_a_first_poll_reads_one_page_and_sets_the_watermark(
        sqlite_engine, keyring, clock, vendor, github, tracked, monkeypatch):
    monkeypatch.setattr(sync, "PAGE", 2)
    github.prs = [_node(n, updated=f"2026-10-23T0{n}:00:00Z") for n in range(1, 5)]
    _only(sqlite_engine, "poll", clock)
    _service(sqlite_engine, keyring, clock, vendor).tick()
    assert _numbers(sqlite_engine) == [3, 4]
    assert _state(sqlite_engine, "poll").watermark.isoformat() == "2026-10-23T04:00:00+00:00"


# conflicts

def test_conflicts_keep_unknown_as_unknown_and_store_only_changes(
        sqlite_engine, keyring, clock, vendor, github, tracked, world):
    pr = _store_pr(sqlite_engine, world.team, tracked, 1, updated=clock())
    node = _node(1)
    github.prs = [node, _node(2)]
    service = _service(sqlite_engine, keyring, clock, vendor)
    _only(sqlite_engine, "conflicts", clock)
    for mergeable, merge_state in (("UNKNOWN", "UNKNOWN"), ("UNKNOWN", "UNKNOWN"), ("MERGEABLE", "CLEAN"),
                                   ("CONFLICTING", "DIRTY")):
        node.update(mergeable=mergeable, mergeStateStatus=merge_state)
        assert service.tick() == ["ok"]
        clock.advance(minutes=10)
    samples = _rows(sqlite_engine, gh_conflict_samples)
    assert sorted((s.sampled_at, s.mergeable, s.merge_state) for s in samples) == [
        (clock() - timedelta(minutes=40), "unknown", "unknown"),
        (clock() - timedelta(minutes=20), "mergeable", "clean"),
        (clock() - timedelta(minutes=10), "conflicting", "dirty")]
    assert {s.pr_row_id for s in samples} == {pr}


# gap fill

def test_gap_fill_refreshes_only_pull_requests_that_differ(
        sqlite_engine, keyring, clock, vendor, github, tracked, world):
    old, same = clock() - timedelta(hours=5), clock() - timedelta(hours=3)
    _store_pr(sqlite_engine, world.team, tracked, 3, updated=old, title="Old title")
    _store_pr(sqlite_engine, world.team, tracked, 4, updated=same)
    github.prs = [_node(3, updated="2026-10-23T11:00:00Z", title="New title"),
                  _node(4, updated=same.isoformat()), _node(5, updated="2026-10-23T10:00:00Z")]
    vendor.pull_list = [{"id": 900003, "number": 3, "updated_at": "2026-10-23T11:00:00Z"},
                        {"id": 900005, "number": 5, "updated_at": "2026-10-23T10:00:00Z"},
                        {"id": 900004, "number": 4, "updated_at": same.isoformat()},
                        {"id": 900002, "number": 2, "updated_at": "2026-10-19T10:00:00Z"}]
    _only(sqlite_engine, "gap_fill", clock)
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == ["ok"]
    assert [v["number"] for v in github.asked("pullRequest(number")] == [3, 5]
    titles = {r.number: r.title for r in _rows(sqlite_engine, gh_pull_requests)}
    assert titles == {3: "New title", 4: "Stored", 5: "Change 5"}
    assert _state(sqlite_engine, "gap_fill").next_run_at == clock() + timedelta(hours=24)


# claims, settings, failures

def test_two_services_never_claim_one_state_twice(sqlite_engine, keyring, clock, vendor, tracked):
    first, second = _service(sqlite_engine, keyring, clock, vendor), _service(sqlite_engine, keyring, clock, vendor)
    state = _state(sqlite_engine, "poll")
    with sqlite_engine.begin() as conn:
        assert sync.claim(conn, state.id, state.next_run_at, clock() + sync.LEASE) is True
    with sqlite_engine.begin() as conn:
        assert sync.claim(conn, state.id, state.next_run_at, clock() + sync.LEASE) is False
    claimed = first.claim_due(clock())
    assert len(claimed) == 3 and state.id not in claimed
    assert second.claim_due(clock()) == []
    clock.advance(minutes=31)
    assert len(first.claim_due(clock(), limit=1)) == 1
    assert len(second.claim_due(clock())) == 3 and first.claim_due(clock()) == []


def test_sync_enabled_off_stops_the_team_and_only_that_team(sqlite_engine, keyring, clock, vendor, tracked, world):
    service = _service(sqlite_engine, keyring, clock, vendor)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(scope=world.team, team_id=world.team, key=sync.ENABLED_SETTING,
                                             value=False))
    assert service.claim_due(clock()) == []
    with sqlite_engine.begin() as conn:
        conn.execute(update(settings).values(value=True))
        conn.execute(insert(settings).values(scope="*", team_id=None, key=sync.ENABLED_SETTING, value="off"))
    assert len(service.claim_due(clock())) == 4


def test_the_backfill_floor_is_the_repository_date_else_the_setting_else_18_months(
        sqlite_engine, clock, tracked, world):
    with sqlite_engine.connect() as conn:
        repo = conn.execute(select(gh_repos)).first()
        assert sync._floor(conn, repo, clock()) == clock() - timedelta(days=548)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(scope=world.team, team_id=world.team, key=sync.SINCE_SETTING,
                                             value="2026-03-01"))
        repo = conn.execute(select(gh_repos)).first()
        assert sync._floor(conn, repo, clock()).isoformat() == "2026-03-01T00:00:00+00:00"
        conn.execute(update(gh_repos).values(backfill_since=date(2026, 6, 1)))
        repo = conn.execute(select(gh_repos)).first()
        assert sync._floor(conn, repo, clock()).isoformat() == "2026-06-01T00:00:00+00:00"


def test_a_failing_run_records_the_error_and_backs_off_up_to_30_minutes(
        sqlite_engine, keyring, clock, vendor, github, tracked):
    service = _service(sqlite_engine, keyring, clock, vendor)
    _only(sqlite_engine, "conflicts", clock)
    github.queue = [{"errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}]
    assert service.tick() == ["error: GitHub GraphQL answered with errors: NOT_FOUND."]
    state = _state(sqlite_engine, "conflicts")
    assert (state.failures, state.next_run_at) == (1, clock() + timedelta(minutes=2))
    assert state.last_error == "GitHub GraphQL answered with errors: NOT_FOUND."

    clock.advance(minutes=2)
    github.repo_id = 777
    service.tick()
    state = _state(sqlite_engine, "conflicts")
    assert (state.failures, state.next_run_at) == (2, clock() + timedelta(minutes=4))
    assert "now names a different repository" in state.last_error

    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.id == state.id).values(failures=9))
    clock.advance(minutes=4)
    service.tick()
    assert _state(sqlite_engine, "conflicts").next_run_at == clock() + timedelta(minutes=30)

    github.repo_id = GH_REPO_ID
    clock.advance(minutes=30)
    assert service.tick() == ["ok"]
    state = _state(sqlite_engine, "conflicts")
    assert (state.failures, state.last_error) == (0, None)


def test_run_now_runs_matching_due_states_and_leaves_leased_ones(
        sqlite_engine, keyring, clock, vendor, github, tracked, world):
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind == "backfill").values(next_run_at=None))
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind == "poll")
                     .values(next_run_at=clock() + timedelta(minutes=20)))
    found = sync.run_now(sqlite_engine, keyring, world.team, repo="Acme/App", clock=clock,
                         transport=httpx.MockTransport(vendor))
    assert found == [("acme/app", "backfill", "done"), ("acme/app", "conflicts", "ok"),
                     ("acme/app", "gap_fill", "error: GitHub GET /repos/acme/app/pulls answered HTTP 404."),
                     ("acme/app", "poll", sync.BUSY)]
    assert _state(sqlite_engine, "poll").next_run_at == clock() + timedelta(minutes=20)
    assert sync.run_now(sqlite_engine, keyring, world.other, clock=clock) == []


# pushes

def _call(engine, run, tool, target, detail, outcome="ok", created=None):
    with engine.begin() as conn:
        conn.execute(insert(tool_calls).values(id=new_id(), run_id=run.id, team_id=run.team, project_id=None,
                                               step_id="s", tool=tool, mode="live", outcome=outcome, target=target,
                                               detail=detail, created_at=created))


def test_sync_derives_pushes_classifies_each_and_credits_armature(
        sqlite_engine, keyring, clock, vendor, github, tracked, world):
    run = start_run(sqlite_engine, keyring, clock, world)
    run.team = world.team
    gone, base0, base1 = _sha(900), _sha(901), _sha(902)
    c1, c2, c3, c4, c5 = (_sha(n) for n in range(1, 6))
    target = "acme/app#7"
    _call(sqlite_engine, run, attribution.PUSH_TOOL, target, {"commit": c2}, outcome="approved", created=clock())
    _call(sqlite_engine, run, "github.post-review", target, {"posted": True, "review_id": 6001, "head": c3},
          created=clock())
    commits = [_commit(c1, "2026-10-20T10:00:00Z", headline="Add login"),
               _commit(c2, "2026-10-20T11:00:00Z"),
               _commit(c3, "2026-10-20T12:00:00Z", parents=(c2, BASE), headline="Merge branch 'main' into f"),
               _commit(c4, "2026-10-20T13:00:00Z", checked=False),
               _commit(c5, "2026-10-20T14:00:00Z")]
    review = {"databaseId": 5001, "state": "CHANGES_REQUESTED", "submittedAt": "2026-10-20T12:30:00Z",
              "body": "Please fix", "url": "https://github.com/r", "commit": {"oid": c3}, "author": REV,
              "comments": {"nodes": [{"databaseId": 7001, "path": "src/a.swift", "line": 3,
                                      "createdAt": "2026-10-20T12:30:00Z", "updatedAt": "2026-10-20T12:30:00Z",
                                      "url": "https://github.com/c", "replyTo": None, "commit": {"oid": c3},
                                      "author": REV}]}}
    ours = {"databaseId": 6001, "state": "COMMENTED", "submittedAt": "2026-10-20T12:40:00Z", "body": "Armature",
            "url": "https://github.com/a", "commit": {"oid": c3}, "author": ARMATURE_BOT, "comments": {"nodes": []}}
    forced = {"createdAt": "2026-10-20T10:05:00Z", "actor": OCTO, "beforeCommit": {"oid": gone},
              "afterCommit": {"oid": c1}}
    github.prs = [_node(7, created="2026-10-19T10:00:00Z", commits=commits, reviews=[review, ours],
                        forced=[forced])]
    github.objects = {
        gone: {"oid": gone, "history": {"nodes": [{"oid": gone, "messageHeadline": "Add login"},
                                                  {"oid": base0, "messageHeadline": "base"}]}},
        c1: {"oid": c1, "history": {"nodes": [{"oid": c1, "messageHeadline": "Add login"},
                                              {"oid": base1, "messageHeadline": "newer base"},
                                              {"oid": base0, "messageHeadline": "base"}]}},
        c5: {"oid": c5, "checkSuites": {"nodes": [{"checkRuns": {"nodes": [
            {"databaseId": 8001, "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS",
             "startedAt": "2026-10-20T14:01:00Z", "completedAt": "2026-10-20T14:09:00Z"}]}}]}}}
    vendor.compares[f"{c3}...{c5}"] = {"status": "ahead", "files": [{"filename": "src/a.swift"}]}
    _only(sqlite_engine, "backfill", clock)
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == ["ok"]

    found = {p.after_sha: p for p in _rows(sqlite_engine, gh_pushes)}
    assert {sha: (p.before_sha, p.forced, p.source, p.push_class, p.class_confidence)
            for sha, p in found.items()} == {
        c1: (gone, True, "sync", "rebase", 0.7),
        c2: (c1, False, "sync", "armature_fix", 1.0),
        c3: (c2, False, "sync", "merge_from_base", 0.8),
        c5: (c3, False, "sync", "fix_after_review", 0.7)}
    assert {p.classifier_version for p in found.values()} == {"1"}
    assert found[c2].actor_source == "armature" and found[c1].pusher_login == "Octo"
    assert found[c5].class_evidence == {"reviews": 2, "files": ["src/a.swift"], "matched": 1}
    reviews = {r.gh_review_id: r for r in _rows(sqlite_engine, gh_reviews)}
    assert (reviews[5001].actor_source, reviews[6001].actor_source) == ("github", "armature")
    assert reviews[6001].reviewer_login == BOT
    [check] = _rows(sqlite_engine, gh_check_runs)
    assert (check.name, check.head_sha, check.conclusion, check.pr_row_id) == (
        "build", c5, "success", _rows(sqlite_engine, gh_pull_requests)[0].id)
    [comment] = _rows(sqlite_engine, gh_review_comments)
    assert (comment.path, comment.gh_review_id, comment.body_length) == ("src/a.swift", 5001, None)


def test_a_review_sync_stores_gives_the_runner_run_that_posted_it_its_head_and_round(
        sqlite_engine, keyring, clock, vendor, github, tracked, world):
    head = _sha(3)
    run_id = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world.team, project_id=world.project, status="succeeded",
                                         started_at=clock(), subject="Acme/App#7"))
        conn.execute(insert(run_steps).values(run_id=run_id, step_key="post", step_id="post", status="succeeded",
                                              output={"posted": True, "review_id": 6001}))
    ours = {"databaseId": 6001, "state": "COMMENTED", "submittedAt": "2026-10-20T12:40:00Z", "body": "Armature",
            "url": "https://github.com/a", "commit": {"oid": head}, "author": ARMATURE_BOT, "comments": {"nodes": []}}
    github.prs = [_node(7, created="2026-10-19T10:00:00Z", commits=[_commit(head, "2026-10-20T12:00:00Z")],
                        reviews=[ours])]
    _only(sqlite_engine, "backfill", clock)
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == ["ok"]
    [run] = _rows(sqlite_engine, runs, runs.c.id == run_id)
    assert (run.head_sha, run.review_round, run.review_kind) == (head, 1, "full")


def test_a_failing_round_derivation_never_costs_the_synced_pull_request(
        sqlite_engine, keyring, clock, vendor, github, tracked, world, monkeypatch):
    from armature.server import rounds

    def fail(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(rounds, "derive", fail)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(runs).values(id=new_id(), team_id=world.team, project_id=world.project,
                                         status="succeeded", started_at=clock(), subject="Acme/App#7"))
    ours = {"databaseId": 6001, "state": "COMMENTED", "submittedAt": "2026-10-20T12:40:00Z", "body": "Armature",
            "url": "https://github.com/a", "commit": {"oid": _sha(3)}, "author": ARMATURE_BOT,
            "comments": {"nodes": []}}
    github.prs = [_node(7, created="2026-10-19T10:00:00Z", reviews=[ours])]
    _only(sqlite_engine, "backfill", clock)
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == ["ok"]
    assert [r.gh_review_id for r in _rows(sqlite_engine, gh_reviews)] == [6001]


def test_sync_never_duplicates_a_webhook_push_and_only_fills_what_it_lacks(
        sqlite_engine, keyring, clock, vendor, github, tracked, world):
    c1, c2, c3 = _sha(1), _sha(2), _sha(3)
    pr = _store_pr(sqlite_engine, world.team, tracked, 7, updated=clock() - timedelta(days=5))
    with sqlite_engine.begin() as conn:
        conn.execute(insert(gh_pushes).values(id=new_id(), team_id=world.team, pr_row_id=pr, before_sha="e" * 40,
                                              after_sha=c2, forced=None, pushed_at=clock(), source="webhook",
                                              created_at=clock()))
        conn.execute(insert(gh_pushes).values(id=new_id(), team_id=world.team, pr_row_id=pr, before_sha=c2,
                                              after_sha=c3, forced=True, pushed_at=clock(), source="webhook",
                                              created_at=clock()))
        conn.execute(insert(gh_review_comments).values(
            id=new_id(), team_id=world.team, pr_row_id=pr, gh_comment_id=7001, gh_review_id=5001, author_gh_id=202,
            author_login="rev", path="src/a.swift", created_at_gh=clock(), updated_at_gh=clock() - timedelta(days=1),
            body_length=42, created_at=clock()))
    review = {"databaseId": 5001, "state": "COMMENTED", "submittedAt": "2026-10-20T12:30:00Z", "body": "x",
              "url": "u", "commit": {"oid": c1}, "author": REV,
              "comments": {"nodes": [{"databaseId": 7001, "path": "src/a.swift", "line": 3,
                                      "createdAt": "2026-10-20T12:30:00Z", "updatedAt": "2026-10-23T11:00:00Z",
                                      "url": "u", "replyTo": None, "commit": {"oid": c1}, "author": REV}]}}
    github.prs = [_node(7, created="2026-10-19T10:00:00Z", updated="2026-10-23T11:00:00Z", reviews=[review],
                        commits=[_commit(c1, "2026-10-20T10:00:00Z"), _commit(c2, "2026-10-20T11:00:00Z"),
                                 _commit(c3, "2026-10-20T12:00:00Z")])]
    _only(sqlite_engine, "backfill", clock)
    _service(sqlite_engine, keyring, clock, vendor).tick()

    found = {p.after_sha: p for p in _rows(sqlite_engine, gh_pushes)}
    assert set(found) == {c2, c3}
    assert (found[c2].source, found[c2].before_sha, found[c2].forced) == ("webhook", "e" * 40, False)
    assert (found[c3].source, found[c3].forced) == ("webhook", True)
    [comment] = _rows(sqlite_engine, gh_review_comments)
    assert (comment.body_length, comment.updated_at_gh.isoformat()) == (42, "2026-10-23T11:00:00+00:00")


# review round 1

def _lease(engine, kind, clock):
    """Claim ``kind`` as a service would, and return its id and lease."""
    state = _state(engine, kind)
    lease = clock() + sync.LEASE
    with engine.begin() as conn:
        assert sync.claim(conn, state.id, state.next_run_at, lease)
    return state.id, lease


def test_a_renamed_repository_is_found_by_its_id_and_its_name_follows(
        sqlite_engine, keyring, clock, vendor, github, tracked):
    github.name = "Acme/Renamed"
    github.prs = [_node(1)]
    vendor.gets["/repositories/4242"] = {"id": GH_REPO_ID, "full_name": "Acme/Renamed"}
    _only(sqlite_engine, "conflicts", clock)
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == ["ok"]
    assert _rows(sqlite_engine, gh_repos)[0].full_name == "acme/renamed"
    assert [v["name"] for v in github.asked("states: OPEN")] == ["app", "renamed"]

    github.name = "Acme/Gone"
    vendor.gets["/repositories/4242"] = {"id": 999, "full_name": "Other/Thing"}
    clock.advance(minutes=10)
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == [
        "error: GitHub GraphQL answered with errors: NOT_FOUND."]
    assert _rows(sqlite_engine, gh_repos)[0].full_name == "acme/renamed"


@pytest.mark.usefixtures("gh_cli")
def test_sync_refuses_a_personal_token_on_a_repository_no_admin_pinned(
        sqlite_engine, keyring, clock, vendor, github, world):
    lib = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(gh_repos).values(id=lib, team_id=world.team, gh_repo_id=GH_REPO_ID, full_name="acme/lib",
                                             enabled=True, created_at=clock(), updated_at=clock()))
        conn.execute(insert(gh_sync_state).values(id=new_id(), team_id=world.team, repo_row_id=lib,
                                                  kind="conflicts", next_run_at=clock(), updated_at=clock()))
    github.name = "Acme/Lib"
    service = _service(sqlite_engine, keyring, clock, vendor)
    assert service.tick() == [f"error: {ghapi.PERSONAL}"]
    assert _state(sqlite_engine, "conflicts").last_error == ghapi.PERSONAL and github.seen == []
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_repos).values(connection_id=world.github))
    clock.advance(minutes=2)
    assert service.tick() == ["ok"]


def test_a_run_that_lost_its_lease_writes_nothing(sqlite_engine, keyring, clock, vendor, github, tracked):
    state_id, lease = _lease(sqlite_engine, "conflicts", clock)
    taken = clock() + timedelta(minutes=45)
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.id == state_id).values(next_run_at=taken))
    assert sync.run(sqlite_engine, keyring, state_id, lease, clock=clock,
                    transport=httpx.MockTransport(vendor)) == sync.LOST
    state = _state(sqlite_engine, "conflicts")
    assert (state.next_run_at, state.failures) == (taken, None)


def test_a_lease_holds_for_its_whole_length(sqlite_engine, keyring, clock, vendor, tracked):
    _only(sqlite_engine, "poll", clock)
    first, second = _service(sqlite_engine, keyring, clock, vendor), _service(sqlite_engine, keyring, clock, vendor)
    assert len(first.claim_due(clock())) == 1
    clock.advance(minutes=29)
    assert second.claim_due(clock()) == []
    clock.advance(minutes=2)
    assert len(second.claim_due(clock())) == 1


def test_conflicts_stop_at_their_budget_and_continue_next_tick(sqlite_engine, keyring, clock, vendor, github, tracked,
                                                               world, monkeypatch):
    monkeypatch.setattr(sync, "OPEN_PAGE", 1)
    for number in (1, 2):
        _store_pr(sqlite_engine, world.team, tracked, number, updated=clock())
    github.prs = [_node(1), _node(2)]
    state_id, lease = _lease(sqlite_engine, "conflicts", clock)
    assert sync.run(sqlite_engine, keyring, state_id, lease, clock=clock, transport=httpx.MockTransport(vendor),
                    budget_s=0) == "ok"
    assert len(github.asked("states: OPEN")) == 1 and len(_rows(sqlite_engine, gh_conflict_samples)) == 1
    assert _state(sqlite_engine, "conflicts").next_run_at == clock()


def test_stopping_the_service_ends_a_backfill_after_its_page(sqlite_engine, keyring, clock, vendor, github, tracked,
                                                             monkeypatch):
    monkeypatch.setattr(sync, "PAGE", 1)
    github.prs = [_node(1), _node(2, created="2026-10-02T10:00:00Z")]
    state_id, lease = _lease(sqlite_engine, "backfill", clock)
    stopping = threading.Event()
    stopping.set()
    assert sync.run(sqlite_engine, keyring, state_id, lease, clock=clock, transport=httpx.MockTransport(vendor),
                    stopping=stopping) == "ok"
    state = _state(sqlite_engine, "backfill")
    assert (_numbers(sqlite_engine), state.next_run_at, sync._cursor(state.cursor)["after"]) == ([2], clock(), "1")


def test_a_reopened_backfill_resumes_at_the_page_where_it_stopped(sqlite_engine, keyring, clock, vendor, github,
                                                                  tracked, monkeypatch):
    monkeypatch.setattr(sync, "PAGE", 1)
    github.prs = [_node(n, created=f"2026-09-0{n}T10:00:00Z") for n in range(1, 4)]
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_repos).values(backfill_since=date(2026, 9, 2)))
    _only(sqlite_engine, "backfill", clock)
    service = _service(sqlite_engine, keyring, clock, vendor)
    assert service.tick() == ["ok"] and _numbers(sqlite_engine) == [2, 3]
    with sqlite_engine.begin() as conn:
        repos.update_repo(conn, conn.execute(select(gh_repos)).first(), body={"backfill_since": "2026-08-01"},
                          actor_id=None, now=clock(), ip=None)
    asked = len(github.asked("CREATED_AT"))
    assert service.tick() == ["ok"] and _numbers(sqlite_engine) == [1, 2, 3]
    assert [v["after"] for v in github.asked("CREATED_AT")[asked:]] == ["2"]


def test_a_check_run_never_goes_back_from_completed(sqlite_engine, clock, tracked, world):
    def check(status, conclusion):
        return {"check_run": {"id": 8001, "name": "build", "head_sha": "a" * 40, "status": status,
                              "conclusion": conclusion, "pull_requests": []}}

    with sqlite_engine.begin() as conn:
        seen = collect._Seen(conn, world.team, conn.execute(select(gh_repos)).first(), clock(), frozenset())
        collect._check_run(seen, check("completed", "failure"))
        collect._check_run(seen, check("in_progress", None))
        assert conn.execute(select(gh_check_runs.c.status, gh_check_runs.c.conclusion)).one() == (
            "completed", "failure")
        collect._check_run(seen, check("completed", "success"))
        assert conn.execute(select(gh_check_runs.c.conclusion)).scalar() == "success"


def test_a_delivery_takes_over_a_push_row_sync_derived(sqlite_engine, clock, tracked, world):
    pr = _store_pr(sqlite_engine, world.team, tracked, 7, updated=clock())
    sender = {"id": 101, "login": "Octo", "type": "User"}
    with sqlite_engine.begin() as conn:
        seen = collect._Seen(conn, world.team, conn.execute(select(gh_repos)).first(), clock(), frozenset())
        collect._push_row(seen, pr, 7, "1" * 40, "2" * 40, user=None, at=clock() - timedelta(hours=1),
                          forced=False, source="sync")
        collect._push_row(seen, pr, 7, "1" * 40, "2" * 40, user=sender, at=clock(), forced=False)
    [push] = _rows(sqlite_engine, gh_pushes)
    assert (push.source, push.pusher_login, push.pusher_gh_id, push.pushed_at) == ("webhook", "Octo", 101, clock())
    assert push.pusher_person_id is not None


def test_pushes_committed_before_the_pull_request_opened_are_not_pushes_to_it():
    early, opened, later = _sha(1), _sha(2), _sha(3)
    node = _node(7, created="2026-10-20T12:00:00Z",
                 commits=[_commit(early, "2026-10-20T10:00:00Z"), _commit(opened, "2026-10-20T11:00:00Z"),
                          _commit(later, "2026-10-20T13:00:00Z")])
    assert [(p.before, p.after) for p in sync.derive_pushes(node)] == [(opened, later)]


def test_a_poll_refreshes_a_pull_request_updated_at_its_watermark(
        sqlite_engine, keyring, clock, vendor, github, tracked):
    github.prs = [_node(1, updated="2026-10-23T10:00:00Z"), _node(2, updated="2026-10-23T09:59:59Z")]
    _only(sqlite_engine, "poll", clock)
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind == "poll")
                     .values(watermark=clock() - timedelta(hours=2)))
    _service(sqlite_engine, keyring, clock, vendor).tick()
    assert _numbers(sqlite_engine) == [1]


def test_extras_queries_shrink_with_the_page_after_a_502(sqlite_engine, keyring, clock, vendor, github, tracked,
                                                         monkeypatch):
    monkeypatch.setattr(sync, "EXTRAS_CHUNK", 2)
    github.prs = [_node(n) for n in (1, 2, 3)]
    github.queue = [httpx.Response(502)]
    _only(sqlite_engine, "poll", clock)
    _service(sqlite_engine, keyring, clock, vendor).tick()
    extras = [b for b in github.seen if "object(oid" in b["query"]]
    assert [len([k for k in b["variables"] if re.fullmatch(r"o\d+", k)]) for b in extras] == [1, 1, 1]


def test_people_are_resolved_in_github_id_order():
    node = _node(7, author={"__typename": "User", "login": "zed", "databaseId": 300},
                 reviews=[{"databaseId": 1, "state": "COMMENTED", "author": REV, "comments": {"nodes": [
                     {"databaseId": 2, "author": OCTO}]}}])
    assert [u["id"] for u in sync._users(node)] == [101, 202, 300]


def test_sync_never_overwrites_a_newer_stored_pull_request(sqlite_engine, keyring, clock, vendor, github, tracked,
                                                           world):
    _store_pr(sqlite_engine, world.team, tracked, 1, updated=clock(), title="Newer")
    github.prs = [_node(1, updated="2026-10-01T10:00:00Z", title="Older")]
    _only(sqlite_engine, "backfill", clock)
    _service(sqlite_engine, keyring, clock, vendor).tick()
    [pr] = _rows(sqlite_engine, gh_pull_requests)
    assert (pr.title, pr.gh_updated_at) == ("Newer", clock())


def test_a_paused_poll_keeps_its_watermark_and_resumes_from_its_cursor(
        sqlite_engine, keyring, clock, vendor, github, tracked, monkeypatch):
    monkeypatch.setattr(sync, "PAGE", 1)
    github.prs = [_node(n, updated=f"2026-10-23T1{n}:00:00Z") for n in (1, 2)]
    watermark = clock() - timedelta(days=1)
    _only(sqlite_engine, "poll", clock)
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind == "poll").values(watermark=watermark))
    github.remaining = 1000
    service = _service(sqlite_engine, keyring, clock, vendor)
    assert service.tick() == ["paused until 2026-10-23T13:00:00+00:00"]
    state = _state(sqlite_engine, "poll")
    assert (state.watermark, sync._cursor(state.cursor)) == (watermark, {"after": "1",
                                                                         "newest": "2026-10-23T12:00:00+00:00"})
    github.remaining = 4999
    clock.advance(hours=1)
    asked = len(github.asked("UPDATED_AT"))
    assert service.tick() == ["ok"]
    assert [v["after"] for v in github.asked("UPDATED_AT")[asked:]] == ["1"]
    state = _state(sqlite_engine, "poll")
    assert (state.watermark.isoformat(), state.cursor) == ("2026-10-23T12:00:00+00:00", None)
    assert _numbers(sqlite_engine) == [1, 2]


def test_only_human_and_armature_reviews_make_a_fix_after_review(
        sqlite_engine, keyring, clock, vendor, github, tracked, world):
    run = start_run(sqlite_engine, keyring, clock, world)
    run.team = world.team
    c1, c2, c3 = _sha(1), _sha(2), _sha(3)
    target = "acme/app#7"
    _call(sqlite_engine, run, attribution.PUSH_TOOL, target, {"commit": c2}, outcome="refused", created=clock())
    _call(sqlite_engine, run, "github.post-review", target, {"posted": True, "head": c2}, created=clock())
    with sqlite_engine.begin() as conn:
        conn.execute(insert(findings).values(id=new_id(), run_id=run.id, finding_key="k", path="src/y.swift"))

    def review(review_id, author, head):
        return {"databaseId": review_id, "state": "COMMENTED", "submittedAt": "2026-10-20T10:30:00Z", "body": "b",
                "url": "u", "commit": {"oid": head}, "author": author, "comments": {"nodes": [
                    {"databaseId": review_id + 1, "path": "src/x.swift", "line": 1, "createdAt": "2026-10-20T10:30:00Z",
                     "updatedAt": "2026-10-20T10:30:00Z", "url": "u", "replyTo": None, "commit": {"oid": head},
                     "author": author}]}}

    bot = {"__typename": "Bot", "login": "copilot", "databaseId": 77}
    github.prs = [_node(7, created="2026-10-19T10:00:00Z", reviews=[review(5001, OCTO, c1), review(5003, bot, c1)],
                        commits=[_commit(c1, "2026-10-20T10:00:00Z"), _commit(c2, "2026-10-20T11:00:00Z"),
                                 _commit(c3, "2026-10-20T12:00:00Z")])]
    vendor.compares[f"{c1}...{c2}"] = {"files": [{"filename": "src/x.swift"}]}
    vendor.compares[f"{c2}...{c3}"] = {"files": [{"filename": "src/y.swift"}]}
    _only(sqlite_engine, "backfill", clock)
    _service(sqlite_engine, keyring, clock, vendor).tick()
    found = {p.after_sha: (p.push_class, p.class_evidence) for p in _rows(sqlite_engine, gh_pushes)}
    assert found == {c2: ("new_scope", {"reviewed": False}),
                     c3: ("fix_after_review", {"reviews": 1, "files": ["src/y.swift"], "matched": 1})}


def test_an_ambiguous_merge_asks_github_and_is_from_base(sqlite_engine, keyring, clock, vendor, github, tracked):
    def merged(number, other):
        c1, c2 = _sha(number * 10 + 1), _sha(number * 10 + 2)
        return _node(number, created="2026-10-19T10:00:00Z", commits=[
            _commit(c1, "2026-10-20T10:00:00Z"),
            _commit(c2, "2026-10-20T11:00:00Z", parents=(c1, other), headline="Merge pull request #3 from x/y")])

    ahead, diverged = "e" * 40, "f" * 40
    github.prs = [merged(1, ahead), merged(2, diverged)]
    vendor.compares[f"{ahead}...main"] = {"status": "ahead"}
    vendor.compares[f"{diverged}...main"] = {"status": "diverged"}
    _only(sqlite_engine, "backfill", clock)
    _service(sqlite_engine, keyring, clock, vendor).tick()
    found = {p.after_sha: (p.push_class, p.class_confidence) for p in _rows(sqlite_engine, gh_pushes)}
    assert found == {_sha(12): ("merge_from_base", 0.95), _sha(22): ("new_scope", 0.5)}


def _client(handler, clock, sleeps=None):
    connection = SimpleNamespace(id=new_id(), config={"api_base": "https://api.github.com"}, identity={})
    call = sync.ghapi.Call(run_id="", step_id="", connection=connection, creds={}, now=clock(), installation_id=7,
                           transport=httpx.MockTransport(handler))
    return ghapi.Client(call, "acme/app", clock=clock, sleep=sleeps.append if sleeps is not None else lambda _: None)


@pytest.fixture
def minted(monkeypatch):
    monkeypatch.setattr(ghapi.vendor, "mint", lambda **kw: "ghs_" + "t" * 36)


def _ok(first=None):
    return httpx.Response(200, json={"data": {"rateLimit": {"remaining": 4000, "resetAt": RESET}, "first": first}})


@pytest.mark.parametrize("remaining, paused", [(1500, False), (1499, True)])
def test_the_pause_starts_below_1500_points(minted, clock, remaining, paused):
    answer = httpx.Response(200, json={"data": {"rateLimit": {"remaining": remaining, "resetAt": RESET}}})
    with _client(lambda request: answer, clock) as gh:
        assert (gh.graphql("q", {}).pause_until is not None) is paused
        assert (gh.paused_until() is not None) is paused


@pytest.mark.parametrize("response, until", [
    (httpx.Response(403, headers={"Retry-After": "30"}), timedelta(seconds=30)),
    (httpx.Response(429, headers={"Retry-After": "30"}), timedelta(seconds=30)),
    (httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1792760400"}),
     timedelta(hours=1)),
    (httpx.Response(429, json={"message": "You have exceeded a secondary rate limit"}), timedelta(minutes=1)),
    (httpx.Response(200, headers={"X-RateLimit-Reset": "1792760400"},
                    json={"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}),
     timedelta(hours=1)),
])
def test_rate_limits_pause_for_as_long_as_github_says(minted, clock, response, until):
    sent = []
    with _client(lambda request: sent.append(request) or response, clock) as gh:
        with pytest.raises(ghapi.Paused) as paused:
            gh.graphql("q", {})
        assert paused.value.until == clock() + until
        with pytest.raises(ghapi.Paused):
            gh.graphql("q", {})
        assert len(sent) == 1


def test_a_plain_403_is_an_error_without_a_pause(minted, clock):
    with _client(lambda request: httpx.Response(403, json={"message": "Resource not accessible"}), clock) as gh:
        with pytest.raises(ghapi.GitHubError) as failed:
            gh.graphql("q", {})
        assert (failed.value.status, gh.paused_until()) == (403, None)


def test_a_503_retries_and_502s_halve_the_page_each_time(minted, clock):
    answers, firsts, sleeps = [httpx.Response(503)], [], []

    def handler(request):
        firsts.append(json.loads(request.content)["variables"].get("first"))
        return answers.pop(0) if answers else _ok()

    with _client(handler, clock, sleeps) as gh:
        gh.graphql("q", {"first": 100})
        assert (firsts, sleeps) == ([100, 50], [1.0])
        answers[:] = [httpx.Response(502)] * 2
        firsts.clear()
        gh.graphql("q", {"first": 100})
        assert firsts == [50, 25, 12]


def test_a_poll_moves_an_opened_fix_whose_pull_request_merged(sqlite_engine, keyring, clock, vendor, github, tracked,
                                                              world):
    from armature.server.db.schema import finding_candidates, fix_proposals, labels, runs
    from armature.server.insights import fixes, taxonomy

    proposal_id, merged = new_id(), "2026-10-23T09:00:00Z"
    branch = fixes.branch_of(proposal_id)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(fix_proposals).values(
            id=proposal_id, team_id=world.team, repo_row_id=tracked, cluster_key="v1|testing|acme/app|src",
            title="Fix: Tests in src", state="opened", branch=branch, base="main", created_at=clock(),
            updated_at=clock(), evidence={"taxonomy_version": taxonomy.VERSION, "label": "testing", "repo": "acme/app",
                                          "area": "src"}))
        for days in (3, 10):
            run_id, candidate = new_id(), new_id()
            at = clock() - timedelta(days=days)
            conn.execute(insert(runs).values(id=run_id, team_id=world.team, status="succeeded", started_at=at,
                                             subject="acme/app#5"))
            conn.execute(insert(finding_candidates).values(id=candidate, team_id=world.team, run_id=run_id,
                                                           step_key="first/testing", specialist="testing", ordinal=0,
                                                           path="src/a.kt", created_at=at))
            conn.execute(insert(labels).values(id=new_id(), team_id=world.team, subject_kind="candidate",
                                               subject_id=candidate, taxonomy_version=taxonomy.VERSION,
                                               label="testing", labeler="t", created_at=at))
    node = _node(31, updated="2026-10-23T10:00:00Z", state="MERGED", merged_at=merged)
    github.prs = [{**node, "headRefName": branch}]
    _only(sqlite_engine, "poll", clock)
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_sync_state).where(gh_sync_state.c.kind == "poll")
                     .values(watermark=clock() - timedelta(hours=2)))
    assert _service(sqlite_engine, keyring, clock, vendor).tick() == ["ok"]
    [row] = _rows(sqlite_engine, fix_proposals)
    assert (row.state, row.merged_at.isoformat(), row.flags_before) == ("merged", "2026-10-23T09:00:00+00:00", 2)

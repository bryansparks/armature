"""Collecting GitHub history from webhook deliveries into the gh_ tables, and the people behind it."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from armature.server import webhooks as hooks
from armature.server.db import new_id
from armature.server.db.schema import (
    connections as connection_rows,
)
from armature.server.db.schema import (
    gh_check_runs,
    gh_pr_commits,
    gh_pr_links,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    github_installations,
    people,
    person_identities,
    runs,
    tool_calls,
    webhooks,
)
from armature.server.insights import collect
from tests.server.test_webhooks import (  # noqa: F401
    _body,
    _create,
    _deliveries,
    _github,
    _hook,
    _path,
    _set_secret,
    app,
    clock,
    lead,
    world,
)

REPO_ID, OTHER_REPO, INSTALLATION = 4242, 5151, 7
HEAD, BASE, NEXT, LATER = "a" * 40, "b" * 40, "c" * 40, "d" * 40
OCTO = {"id": 101, "login": "Octo", "type": "User"}
REVIEWER = {"id": 202, "login": "rev", "type": "User"}


def _pr(number=12, *, title="Fix the login bug", head_ref="feature/MOB-12-login", updated="2026-10-23T10:00:00Z",
        user=None, body=None, merged=False, merged_by=None, merge_sha=None, head=HEAD, head_repo=REPO_ID):
    return {"id": 900000 + number, "number": number, "title": title, "body": body, "user": user or OCTO,
            "state": "closed" if merged else "open", "draft": False,
            "head": {"ref": head_ref, "sha": head, "repo": {"id": head_repo}},
            "base": {"ref": "main", "sha": BASE}, "merged": merged, "merge_commit_sha": merge_sha,
            "additions": 10, "deletions": 2, "changed_files": 3, "created_at": "2026-10-23T09:00:00Z",
            "updated_at": updated, "closed_at": updated if merged else None, "merged_at": updated if merged else None,
            "merged_by": merged_by}


def _event(action=None, repo=REPO_ID, sender=None, **fields):
    return {**({"action": action} if action else {}), "repository": {"id": repo, "full_name": "Acme/App"},
            "installation": {"id": INSTALLATION}, "sender": sender or OCTO, **fields}


def _send(lead, hook, event, payload, delivery):
    raw = _body(payload)
    return lead.client.post(_path(hook), content=raw, headers=_github(raw, event=event, delivery=delivery))


def _track(engine, team, clock, repo_id=REPO_ID, name="acme/app", enabled=True):
    row_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(gh_repos).values(id=row_id, team_id=team, gh_repo_id=repo_id, full_name=name,
                                             enabled=enabled, created_at=clock(), updated_at=clock()))
    return row_id


def _rows(engine, table, **where):
    with engine.connect() as conn:
        return conn.execute(select(table).where(*[table.c[k] == v for k, v in where.items()])
                            .order_by(table.c.id)).all()


def _one(engine, table, **where):
    [row] = _rows(engine, table, **where)
    return row


@pytest.fixture
def tracked(sqlite_engine, world, clock):
    return _track(sqlite_engine, world.team, clock)


@pytest.fixture
def hook(lead, world):
    return _hook(lead, world)


def _identities(engine, person_id):
    return sorted((r.kind, r.value, r.verified) for r in _rows(engine, person_identities, person_id=person_id))


# pull requests

def test_pull_request_events_upsert_one_row(sqlite_engine, lead, hook, tracked):
    opened = _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    assert opened.json() == {"status": "ignored", "jobs": 0}
    pr = _one(sqlite_engine, gh_pull_requests)
    assert (pr.repo_row_id, pr.gh_pr_id, pr.number, pr.state, pr.jira_key) == (tracked, 900012, 12, "open", "MOB-12")
    assert (pr.author_gh_id, pr.author_login, pr.head_sha, pr.base_sha, pr.additions) == (101, "Octo", HEAD, BASE, 10)
    assert pr.head_repo_gh_id == REPO_ID
    assert pr.merge_commit_sha is None and pr.merge_method is None
    author = _one(sqlite_engine, people)
    assert pr.author_person_id == author.id and (author.kind, author.display_name) == ("human", "Octo")
    assert _identities(sqlite_engine, author.id) == [("github_id", "101", True), ("github_login", "octo", True)]

    edited = _pr(title="Fix the login crash", updated="2026-10-23T11:00:00Z")
    _send(lead, hook, "pull_request", _event("edited", pull_request=edited), "d-2")
    merger = {"id": 303, "login": "boss", "type": "User"}
    merged = _pr(title="Fix the login crash", updated="2026-10-23T12:00:00Z", merged=True, merged_by=merger,
                 merge_sha=LATER)
    _send(lead, hook, "pull_request", _event("closed", pull_request=merged), "d-3")
    pr = _one(sqlite_engine, gh_pull_requests)
    assert (pr.title, pr.state, pr.merge_commit_sha, pr.merged_by_gh_id) == \
        ("Fix the login crash", "merged", LATER, 303)
    assert pr.merged_at_gh is not None and pr.closed_at_gh is not None
    commit = _one(sqlite_engine, gh_pr_commits)
    assert (commit.commit_sha, commit.pr_row_id, commit.repo_row_id) == (LATER, pr.id, tracked)
    assert {p.display_name for p in _rows(sqlite_engine, people)} == {"Octo", "boss"}


def test_a_late_older_event_does_not_roll_back_newer_data(sqlite_engine, lead, hook, tracked):
    newer = _pr(title="Newer", updated="2026-10-23T12:00:00Z")
    _send(lead, hook, "pull_request", _event("edited", pull_request=newer), "d-1")
    older = _pr(title="Older", updated="2026-10-23T11:00:00Z", head_ref="other-branch")
    _send(lead, hook, "pull_request", _event("edited", pull_request=older), "d-2")
    pr = _one(sqlite_engine, gh_pull_requests)
    assert (pr.title, pr.head_ref) == ("Newer", "feature/MOB-12-login")
    same = _pr(title="Same time", updated="2026-10-23T12:00:00Z")
    _send(lead, hook, "pull_request", _event("edited", pull_request=same), "d-3")
    assert _one(sqlite_engine, gh_pull_requests).title == "Same time"


def test_unhandled_actions_and_events_are_skipped(sqlite_engine, lead, hook, tracked):
    _send(lead, hook, "pull_request", _event("labeled", pull_request=_pr()), "d-1")
    _send(lead, hook, "issues", _event("opened", issue={"id": 1}), "d-2")
    _send(lead, hook, "check_run", _event("created", check_run={"id": 1}), "d-3")
    assert _rows(sqlite_engine, gh_pull_requests) == [] and _rows(sqlite_engine, gh_check_runs) == []


# pushes

def test_synchronize_records_a_push_and_a_push_event_marks_it_forced(sqlite_engine, lead, hook, tracked):
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    synced = _pr(updated="2026-10-23T11:00:00Z", head=NEXT)
    pusher = {"id": 404, "login": "pusher", "type": "User"}
    _send(lead, hook, "pull_request",
          _event("synchronize", sender=pusher, pull_request=synced, before=HEAD, after=NEXT), "d-2")
    push = _one(sqlite_engine, gh_pushes)
    assert (push.before_sha, push.after_sha, push.base_sha, push.source) == (HEAD, NEXT, BASE, "webhook")
    assert (push.forced, push.push_class, push.pusher_gh_id, push.pusher_login) == (None, None, 404, "pusher")
    pusher_identity = _one(sqlite_engine, person_identities, kind="github_id", value="404")
    assert pusher_identity.verified is True
    assert (push.pusher_person_id, push.actor_source) == (pusher_identity.person_id, "github")
    forced = _event(ref="refs/heads/feature/MOB-12-login", before=HEAD, after=NEXT, forced=True,
                    pusher={"name": "Octo", "email": "octo@example.test"})
    _send(lead, hook, "push", forced, "d-3")
    assert _one(sqlite_engine, gh_pushes).forced is True

    first = _event(ref="refs/heads/feature/MOB-12-login", before=NEXT, after=LATER, forced=False)
    _send(lead, hook, "push", first, "d-4")
    early = _one(sqlite_engine, gh_pushes, after_sha=LATER)
    assert (early.forced, early.base_sha, early.before_sha) == (False, None, NEXT)
    later = _pr(updated="2026-10-23T12:00:00Z", head=LATER)
    _send(lead, hook, "pull_request", _event("synchronize", pull_request=later, before=NEXT, after=LATER), "d-5")
    filled = _one(sqlite_engine, gh_pushes, after_sha=LATER)
    assert (filled.forced, filled.base_sha) == (False, BASE)


def test_a_push_to_a_branch_with_no_open_pull_request_records_nothing(sqlite_engine, lead, hook, tracked):
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    stranger = {"id": 909, "login": "stranger", "type": "User"}
    _send(lead, hook, "push", _event(ref="refs/heads/elsewhere", sender=stranger, before=HEAD, after=NEXT,
                                     forced=True, pusher={"name": "s", "email": "s@corp.test"}), "d-2")
    _send(lead, hook, "push", _event(ref="refs/tags/feature/MOB-12-login", before=HEAD, after=NEXT), "d-3")
    assert _rows(sqlite_engine, gh_pushes) == []
    assert [r.display_name for r in _rows(sqlite_engine, people)] == ["Octo"]
    assert _rows(sqlite_engine, person_identities, kind="email") == []


# reviews, comments, checks

def test_reviews_and_comments_keep_lengths_not_text(sqlite_engine, lead, hook, tracked):
    review = {"id": 7001, "user": REVIEWER, "state": "commented", "body": "Two nits", "commit_id": HEAD,
              "submitted_at": "2026-10-23T10:30:00Z", "html_url": "https://github.com/acme/app/pull/12#r1"}
    _send(lead, hook, "pull_request_review", _event("submitted", pull_request=_pr(), review=review), "d-1")
    comment = {"id": 8001, "pull_request_review_id": 7001, "user": REVIEWER, "path": "src/a.swift", "line": 4,
               "commit_id": HEAD, "body": "Use a guard here", "created_at": "2026-10-23T10:29:00Z",
               "updated_at": "2026-10-23T10:29:00Z", "html_url": "https://github.com/c/8001", "in_reply_to_id": None}
    _send(lead, hook, "pull_request_review_comment", _event("created", pull_request=_pr(), comment=comment), "d-2")
    stored = _one(sqlite_engine, gh_reviews)
    pr = _one(sqlite_engine, gh_pull_requests)
    assert (stored.pr_row_id, stored.state, stored.body_length, stored.comment_count, stored.head_sha) == \
        (pr.id, "commented", 8, None, HEAD)
    row = _one(sqlite_engine, gh_review_comments)
    assert (row.gh_review_id, row.path, row.line, row.body_length, row.deleted) == (7001, "src/a.swift", 4, 16, False)
    assert "body" not in gh_review_comments.c and "body" not in gh_reviews.c
    reviewer = _one(sqlite_engine, person_identities, kind="github_id", value="202")
    assert stored.reviewer_person_id == reviewer.person_id == row.author_person_id

    edited = {**comment, "body": "Use a guard", "updated_at": "2026-10-23T10:40:00Z"}
    _send(lead, hook, "pull_request_review_comment", _event("edited", pull_request=_pr(), comment=edited), "d-3")
    stale = {**comment, "body": "x", "updated_at": "2026-10-23T10:35:00Z"}
    _send(lead, hook, "pull_request_review_comment", _event("edited", pull_request=_pr(), comment=stale), "d-4")
    assert _one(sqlite_engine, gh_review_comments).body_length == 11
    _send(lead, hook, "pull_request_review_comment", _event("deleted", pull_request=_pr(), comment=edited), "d-5")
    assert _one(sqlite_engine, gh_review_comments).deleted is True

    dismissed = {**review, "state": "dismissed"}
    _send(lead, hook, "pull_request_review", _event("dismissed", pull_request=_pr(), review=dismissed), "d-6")
    _send(lead, hook, "pull_request_review", _event("edited", pull_request=_pr(), review=review), "d-7")
    assert _one(sqlite_engine, gh_reviews).state == "dismissed"


def test_a_review_event_never_overwrites_the_stored_pull_request(sqlite_engine, lead, hook, tracked):
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    partial = {k: v for k, v in _pr(title="From a review", updated="2026-10-23T13:00:00Z").items()
               if k not in ("additions", "deletions", "changed_files", "merged")}
    review = {"id": 7001, "user": REVIEWER, "state": "approved", "body": "", "commit_id": HEAD,
              "submitted_at": "2026-10-23T13:00:00Z"}
    _send(lead, hook, "pull_request_review", _event("submitted", pull_request=partial, review=review), "d-2")
    pr = _one(sqlite_engine, gh_pull_requests)
    assert (pr.title, pr.additions) == ("Fix the login bug", 10)
    assert _one(sqlite_engine, gh_reviews).state == "approved"


def test_a_completed_check_run_links_its_pull_request(sqlite_engine, lead, hook, tracked):
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    run = {"id": 6001, "name": "build", "head_sha": HEAD, "status": "completed", "conclusion": "failure",
           "started_at": "2026-10-23T10:00:00Z", "completed_at": "2026-10-23T10:05:00Z",
           "pull_requests": [{"id": 900012, "number": 12}]}
    _send(lead, hook, "check_run", _event("completed", check_run=run), "d-1b")
    alone = {**run, "id": 6002, "pull_requests": []}
    _send(lead, hook, "check_run", _event("completed", check_run=alone), "d-2")
    linked, unlinked = _rows(sqlite_engine, gh_check_runs, gh_check_run_id=6001) + \
        _rows(sqlite_engine, gh_check_runs, gh_check_run_id=6002)
    pr = _one(sqlite_engine, gh_pull_requests)
    assert (linked.pr_row_id, linked.repo_row_id, linked.conclusion, linked.name) == \
        (pr.id, tracked, "failure", "build")
    assert unlinked.pr_row_id is None and unlinked.completed_at is not None
    _send(lead, hook, "check_run", _event("completed", check_run={**run, "conclusion": "success"}), "d-3")
    assert _one(sqlite_engine, gh_check_runs, gh_check_run_id=6001).conclusion == "success"


# what is skipped

def test_untracked_and_disabled_repositories_are_skipped(sqlite_engine, lead, hook, world, clock):
    _track(sqlite_engine, world.team, clock, enabled=False)
    _track(sqlite_engine, world.other, clock, repo_id=OTHER_REPO, name="acme/other")
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    _send(lead, hook, "pull_request", _event("opened", repo=OTHER_REPO, pull_request=_pr(13)), "d-2")
    _send(lead, hook, "pull_request", _event("opened", repo=9999, pull_request=_pr(14)), "d-3")
    assert _rows(sqlite_engine, gh_pull_requests) == [] and _rows(sqlite_engine, people) == []


def test_a_held_delivery_is_collected_only_once_approved(sqlite_engine, lead, world, tracked):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    assert _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1").json() == \
        {"status": "held"}
    assert _rows(sqlite_engine, gh_pull_requests) == []
    [held] = [d for d in _deliveries(sqlite_engine, hook["id"]) if d.status == "held"]
    approved = lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{held.id}/approve", json={},
                                headers=lead.headers)
    assert approved.json()["replayed"] is True
    assert _one(sqlite_engine, gh_pull_requests).number == 12


def test_a_sender_that_is_not_pinned_is_not_collected(sqlite_engine, lead, world, tracked):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    with sqlite_engine.begin() as conn:
        conn.execute(webhooks.update().where(webhooks.c.id == hook["id"])
                     .values(approval="approved", pinned_senders=["github:installation:99"]))
    assert _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1").json() == \
        {"status": "held"}
    assert _rows(sqlite_engine, gh_pull_requests) == []


def test_a_test_delivery_is_never_collected(sqlite_engine, lead, hook, world, clock):
    repo = _track(sqlite_engine, world.team, clock, enabled=False)
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_repos).where(gh_repos.c.id == repo).values(enabled=True))
    [delivery] = _deliveries(sqlite_engine, hook["id"])
    tested = lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{delivery.id}/test", json={},
                              headers=lead.headers)
    assert tested.status_code == 200
    assert _rows(sqlite_engine, gh_pull_requests) == []


def test_an_installation_that_is_not_the_connections_is_skipped(sqlite_engine, world, clock, tracked, caplog):
    connection = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(connection_rows).values(id=connection, team_id=world.team, kind="github_app", name="gh",
                                                    config={}, identity={}, state="ok", created_at=clock(),
                                                    updated_at=clock()))
        conn.execute(insert(github_installations).values(id=new_id(), connection_id=connection, owner="acme",
                                                         installation_id=INSTALLATION, permissions={}))
    linked = SimpleNamespace(id="hook-1", team_id=world.team, connection_id=connection)
    foreign = {**_event("opened", pull_request=_pr()), "installation": {"id": 8}}
    collect.collect(sqlite_engine, linked, "pull_request", foreign, clock())
    missing = {k: v for k, v in _event("opened", pull_request=_pr()).items() if k != "installation"}
    collect.collect(sqlite_engine, linked, "pull_request", missing, clock())
    assert _rows(sqlite_engine, gh_pull_requests) == []
    assert "its installation is not the connection's" in caplog.text
    collect.collect(sqlite_engine, linked, "pull_request", _event("opened", pull_request=_pr()), clock())
    assert _one(sqlite_engine, gh_pull_requests).number == 12


def test_an_oversized_payload_is_collected_from_memory(sqlite_engine, lead, hook, tracked, monkeypatch):
    monkeypatch.setattr(hooks, "PAYLOAD_MAX", 10)
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    [delivery] = _deliveries(sqlite_engine, hook["id"])
    assert delivery.payload is None
    assert _one(sqlite_engine, gh_pull_requests).number == 12


def test_a_collector_failure_rolls_back_and_does_not_change_the_response(sqlite_engine, lead, hook, tracked,
                                                                         monkeypatch, caplog):
    handler = collect.HANDLERS["pull_request"]

    def broken(seen, payload):
        handler(seen, payload)
        raise RuntimeError("disk full near octo@corp.test")

    monkeypatch.setitem(collect.HANDLERS, "pull_request", broken)
    response = _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    assert response.status_code == 200 and response.json() == {"status": "ignored", "jobs": 0}
    assert f"collecting a GitHub pull_request delivery to webhook {hook['id']} failed: RuntimeError" in caplog.text
    assert "octo@corp.test" not in caplog.text and "Traceback" not in caplog.text
    assert _rows(sqlite_engine, gh_pull_requests) == []
    assert _rows(sqlite_engine, people) == [] and _rows(sqlite_engine, person_identities) == []


# links, Jira keys, people

def test_reverts_link_to_the_pull_request_they_revert(sqlite_engine, lead, hook, tracked):
    merged = _pr(updated="2026-10-23T12:00:00Z", merged=True, merge_sha=LATER)
    _send(lead, hook, "pull_request", _event("closed", pull_request=merged), "d-1")
    explicit = _pr(13, title="Back out the login fix", body="Reverts acme/app#12\n\nIt broke sign-in.")
    _send(lead, hook, "pull_request", _event("opened", pull_request=explicit), "d-2")
    titled = _pr(14, title='Revert "Fix the login bug"', body="Undo it.")
    _send(lead, hook, "pull_request", _event("opened", pull_request=titled), "d-3")
    short = _pr(15, title="Undo", body="Reverts #12")
    _send(lead, hook, "pull_request", _event("opened", pull_request=short), "d-4")
    unknown = _pr(16, title="Undo again", body="Reverts acme/app#999")
    _send(lead, hook, "pull_request", _event("opened", pull_request=unknown), "d-5")
    target = _one(sqlite_engine, gh_pull_requests, number=12).id
    numbers = {r.id: r.number for r in _rows(sqlite_engine, gh_pull_requests)}
    links = sorted((numbers[r.from_pr_row_id], r.confidence, json.dumps(r.evidence))
                   for r in _rows(sqlite_engine, gh_pr_links, to_pr_row_id=target, kind="revert"))
    assert links == [(13, 1.0, '{"body": "Reverts acme/app#12"}'),
                     (14, 0.6, '{"title": "Revert \\"Fix the login bug\\""}'),
                     (15, 1.0, '{"body": "Reverts #12"}')]
    assert len(_rows(sqlite_engine, gh_pr_links)) == 3


def test_jira_key_comes_from_the_branch_first():
    assert collect.jira_key("feature/MOB-12-login", "ABC-1 Fix") == "MOB-12"
    assert collect.jira_key("fix-login", "ABC-1: fix the login") == "ABC-1"
    assert collect.jira_key("mob-12", "lowercase mob-12") is None
    assert collect.jira_key(None, None) is None
    assert collect.jira_key("main", "fix CVE-2024-3094") is None
    assert collect.jira_key("MOB-12-login", "x") == "MOB-12"


def test_bots_are_marked_as_bots_and_a_gh_cli_login_is_not(sqlite_engine, lead, hook, world, clock, tracked):
    with sqlite_engine.begin() as conn:
        for name, identity in (("app", {"bot_login": "acme-reviewer"}), ("cli", {"login": "Stephan"})):
            conn.execute(insert(connection_rows).values(id=new_id(), team_id=world.team, kind="github_app",
                                                        name=name, config={}, identity=identity, state="ok",
                                                        created_at=clock(), updated_at=clock()))
    authors = [{"id": 11, "login": "renovate", "type": "Bot"}, {"id": 12, "login": "dependabot[bot]", "type": "User"},
               {"id": 13, "login": "stephan", "type": "User"}, {"id": 14, "login": "human", "type": "User"},
               {"id": 15, "login": "acme-reviewer", "type": "User"}]
    for n, author in enumerate(authors, start=20):
        _send(lead, hook, "pull_request", _event("opened", pull_request=_pr(n, user=author)), f"d-{n}")
    kinds = {r.display_name: r.kind for r in _rows(sqlite_engine, people)}
    assert kinds == {"renovate": "bot", "dependabot[bot]": "bot", "stephan": "human", "human": "human",
                     "acme-reviewer": "bot"}


def test_a_login_change_keeps_the_old_login(sqlite_engine, lead, hook, tracked):
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    renamed = _pr(13, user={**OCTO, "login": "Octo2"})
    _send(lead, hook, "pull_request", _event("opened", pull_request=renamed), "d-2")
    person = _one(sqlite_engine, people)
    assert _identities(sqlite_engine, person.id) == [("github_id", "101", True), ("github_login", "octo", True),
                                                     ("github_login", "octo2", True)]
    assert {r.author_person_id for r in _rows(sqlite_engine, gh_pull_requests)} == {person.id}


def test_one_person_across_every_kind_of_event(sqlite_engine, lead, hook, tracked):
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr(user=REVIEWER)), "d-1")
    review = {"id": 7001, "user": OCTO, "state": "approved", "body": "", "commit_id": HEAD,
              "submitted_at": "2026-10-23T10:30:00Z"}
    _send(lead, hook, "pull_request_review", _event("submitted", pull_request=_pr(user=REVIEWER), review=review),
          "d-2")
    comment = {"id": 8001, "user": OCTO, "path": "a", "commit_id": HEAD, "body": "x",
               "created_at": "2026-10-23T10:29:00Z", "updated_at": "2026-10-23T10:29:00Z"}
    _send(lead, hook, "pull_request_review_comment",
          _event("created", pull_request=_pr(user=REVIEWER), comment=comment), "d-3")
    push = _event(ref="refs/heads/feature/MOB-12-login", before=HEAD, after=NEXT, forced=False,
                  pusher={"name": "Octo", "email": "101+Octo@users.noreply.github.com"},
                  commits=[{"author": {"email": "octo@corp.test"}, "committer": {"email": "noreply@github.com"}},
                           {"author": {"email": "303+boss@users.noreply.github.com"}}])
    _send(lead, hook, "push", push, "d-4")
    _send(lead, hook, "push", {**push, "pusher": {"name": "Octo", "email": "octo@corp.test"}, "after": LATER}, "d-5")
    octo = _one(sqlite_engine, person_identities, kind="github_id", value="101").person_id
    assert len(_rows(sqlite_engine, people)) == 3
    assert _identities(sqlite_engine, octo) == [("email", "101+octo@users.noreply.github.com", True),
                                                ("email", "octo@corp.test", False), ("github_id", "101", True),
                                                ("github_login", "octo", True)]
    boss = _one(sqlite_engine, person_identities, kind="github_id", value="303").person_id
    assert ("email", "303+boss@users.noreply.github.com", True) in _identities(sqlite_engine, boss)
    assert _one(sqlite_engine, gh_reviews).reviewer_person_id == octo
    assert _one(sqlite_engine, gh_review_comments).author_person_id == octo


# lead fixes: Armature's own reviews and pushes, gh CLI webhooks, forks

CLI = {"id": 13, "login": "Stephan", "type": "User"}


def _cli_connection(engine, team, clock, config=None):
    connection = new_id()
    with engine.begin() as conn:
        conn.execute(insert(connection_rows).values(id=connection, team_id=team, kind="github_app", name="cli",
                                                    config=config or {"auth": "gh_cli"},
                                                    identity={"auth": "gh_cli", "login": "Stephan"}, state="ok",
                                                    created_at=clock(), updated_at=clock()))
    return connection


def _tool_call(engine, team, clock, tool, detail, *, target="acme/app#12", outcome="ok", at=None):
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=team, status="succeeded", started_at=clock()))
        conn.execute(insert(tool_calls).values(id=new_id(), run_id=run_id, team_id=team, step_id="post", tool=tool,
                                               mode="live", outcome=outcome, target=target, detail=detail,
                                               created_at=at or clock()))


def _review_event(review_id, user, head=HEAD, submitted="2026-10-23T12:05:00Z"):
    review = {"id": review_id, "user": user, "state": "commented", "body": "", "commit_id": head,
              "submitted_at": submitted}
    return _event("submitted", pull_request=_pr(), review=review)


def _armature(engine):
    return _one(engine, person_identities, kind="armature", value="armature").person_id


def test_armatures_reviews_are_credited_to_armature_not_the_login(sqlite_engine, lead, hook, world, clock, tracked):
    _cli_connection(sqlite_engine, world.team, clock)
    _tool_call(sqlite_engine, world.team, clock, "github.post-review",
               {"head": HEAD, "number": 12, "posted": True, "approved": False})
    _tool_call(sqlite_engine, world.team, clock, "review.record-post",
               {"head": NEXT, "number": 12, "posted": True, "approved": True, "review_id": 7004})
    _send(lead, hook, "pull_request_review", _review_event(7001, CLI), "d-1")
    _send(lead, hook, "pull_request_review", _review_event(7002, CLI, head=NEXT), "d-2")
    _send(lead, hook, "pull_request_review", _review_event(7003, REVIEWER), "d-3")
    _send(lead, hook, "pull_request_review", _review_event(7004, CLI, head=LATER, submitted="2026-10-25T00:00:00Z"),
          "d-4")
    _send(lead, hook, "pull_request_review", _review_event(7005, CLI, submitted="2026-10-23T12:30:00Z"), "d-5")
    armature = _armature(sqlite_engine)
    stephan = _one(sqlite_engine, person_identities, kind="github_id", value="13").person_id
    reviews = {r.gh_review_id: (r.actor_source, r.reviewer_person_id) for r in _rows(sqlite_engine, gh_reviews)}
    assert reviews[7001] == ("armature", armature)
    assert reviews[7002] == ("github", stephan)
    assert reviews[7003][0] == "github" and reviews[7003][1] != armature
    assert reviews[7004] == ("armature", armature)
    assert reviews[7005] == ("github", stephan)
    assert _one(sqlite_engine, people, id=armature).kind == "bot"
    assert _one(sqlite_engine, people, id=stephan).kind == "human"
    assert len(_rows(sqlite_engine, person_identities, kind="armature")) == 1


def test_a_review_delivery_gives_the_runner_run_that_posted_it_its_head_and_round(sqlite_engine, lead, hook, world,
                                                                                    clock, tracked):
    from armature.server.db.schema import run_steps
    _cli_connection(sqlite_engine, world.team, clock)
    run_id = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world.team, project_id=world.project, status="succeeded",
                                         started_at=clock(), subject="Acme/App#12"))
        conn.execute(insert(run_steps).values(run_id=run_id, step_key="post", step_id="post", status="succeeded",
                                              output={"posted": True, "review_id": 7001}))
    _send(lead, hook, "pull_request_review", _review_event(7001, CLI), "d-1")
    run = _one(sqlite_engine, runs, id=run_id)
    assert (run.head_sha, run.review_round, run.review_kind) == (HEAD, 1, "full")


def test_a_review_call_recorded_after_its_webhook_claims_the_review(sqlite_engine, lead, hook, world, clock,
                                                                     tracked):
    from armature.server.insights import attribution
    _cli_connection(sqlite_engine, world.team, clock)
    _send(lead, hook, "pull_request_review", _review_event(7001, CLI), "d-1")
    assert _one(sqlite_engine, gh_reviews).actor_source == "github"
    detail = {"head": HEAD, "number": 12, "posted": True, "approved": False}
    attribution.claim_reviews(sqlite_engine, world.team, "acme/app#12", {**detail, "posted": False}, clock())
    attribution.claim_reviews(sqlite_engine, world.team, "acme/app#13", detail, clock())
    assert _one(sqlite_engine, gh_reviews).actor_source == "github"
    attribution.claim_reviews(sqlite_engine, world.team, "acme/app#12", detail,
                              _one(sqlite_engine, gh_reviews).submitted_at)
    row = _one(sqlite_engine, gh_reviews)
    assert (row.actor_source, row.reviewer_person_id) == ("armature", _armature(sqlite_engine))


def test_armatures_pushes_are_credited_by_commit(sqlite_engine, lead, hook, world, clock, tracked):
    _tool_call(sqlite_engine, world.team, clock, "github.push", {"branch": "feature/MOB-12-login", "commit": NEXT},
               outcome="approved")
    _tool_call(sqlite_engine, world.team, clock, "github.push", {"branch": "feature/MOB-12-login", "commit": LATER},
               outcome="refused")
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    _send(lead, hook, "pull_request", _event("synchronize", pull_request=_pr(updated="2026-10-23T11:00:00Z"),
                                             before=HEAD, after=NEXT), "d-2")
    _send(lead, hook, "push", _event(ref="refs/heads/feature/MOB-12-login", before=NEXT, after=LATER, forced=False),
          "d-3")
    ours, theirs = _one(sqlite_engine, gh_pushes, after_sha=NEXT), _one(sqlite_engine, gh_pushes, after_sha=LATER)
    assert (ours.actor_source, ours.pusher_person_id, ours.pusher_login) == ("armature", _armature(sqlite_engine),
                                                                             "Octo")
    octo = _one(sqlite_engine, person_identities, kind="github_id", value="101").person_id
    assert (theirs.actor_source, theirs.pusher_person_id) == ("github", octo)


def test_a_gh_cli_webhook_collects_without_an_installation(sqlite_engine, lead, world, clock, tracked):
    connection = _cli_connection(sqlite_engine, world.team, clock)
    linked = SimpleNamespace(id="hook-1", team_id=world.team, connection_id=connection)
    bare = {k: v for k, v in _event("opened", pull_request=_pr()).items() if k != "installation"}
    collect.collect(sqlite_engine, linked, "pull_request", {**bare, "repository": {"id": OTHER_REPO}}, clock())
    assert _rows(sqlite_engine, gh_pull_requests) == []
    elsewhere = SimpleNamespace(id="hook-2", team_id=world.other, connection_id=connection)
    collect.collect(sqlite_engine, elsewhere, "pull_request", bare, clock())
    assert _rows(sqlite_engine, gh_pull_requests) == []
    collect.collect(sqlite_engine, linked, "pull_request", bare, clock())
    assert _one(sqlite_engine, gh_pull_requests).number == 12


def test_a_push_never_lands_on_a_forks_pull_request(sqlite_engine, lead, hook, tracked):
    fork = _pr(13, head_repo=777)
    _send(lead, hook, "pull_request", _event("opened", pull_request=fork), "d-1")
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-2")
    _send(lead, hook, "push", _event(ref="refs/heads/feature/MOB-12-login", before=HEAD, after=NEXT, forced=True),
          "d-3")
    [push] = _rows(sqlite_engine, gh_pushes)
    numbers = {r.id: (r.number, r.head_repo_gh_id) for r in _rows(sqlite_engine, gh_pull_requests)}
    assert numbers[push.pr_row_id] == (12, REPO_ID)
    assert sorted(numbers.values()) == [(12, REPO_ID), (13, 777)]


# review round 1

def test_a_closed_unmerged_or_draft_pull_request_keeps_no_merge_commit(sqlite_engine, lead, hook, tracked):
    closed = {**_pr(updated="2026-10-23T11:00:00Z"), "state": "closed", "closed_at": "2026-10-23T11:00:00Z",
              "merge_commit_sha": LATER}
    _send(lead, hook, "pull_request", _event("closed", pull_request=closed), "d-1")
    draft = {**_pr(13), "draft": True}
    _send(lead, hook, "pull_request", _event("converted_to_draft", pull_request=draft), "d-2")
    rows = {r.number: r for r in _rows(sqlite_engine, gh_pull_requests)}
    assert (rows[12].state, rows[12].draft, rows[12].merge_commit_sha, rows[12].merged_at_gh) == \
        ("closed", False, None, None)
    assert rows[12].closed_at_gh is not None
    assert (rows[13].state, rows[13].draft, rows[13].merge_commit_sha) == ("open", True, None)
    assert _rows(sqlite_engine, gh_pr_commits) == []


def test_a_human_becomes_a_bot_once_a_connection_names_its_login(sqlite_engine, lead, hook, world, clock, tracked):
    reviewer = {"id": 15, "login": "acme-reviewer", "type": "User"}
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr(user=reviewer)), "d-1")
    assert _one(sqlite_engine, people).kind == "human"
    with sqlite_engine.begin() as conn:
        conn.execute(insert(connection_rows).values(id=new_id(), team_id=world.team, kind="github_app", name="app",
                                                    config={}, identity={"bot_login": "acme-reviewer"}, state="ok",
                                                    created_at=clock(), updated_at=clock()))
    _send(lead, hook, "pull_request", _event("edited", pull_request=_pr(user=reviewer, updated="2026-10-23T11:00:00Z")),
          "d-2")
    assert _one(sqlite_engine, people).kind == "bot"


def test_a_reused_login_stays_with_its_first_owner(sqlite_engine, lead, hook, tracked):
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr(user={**OCTO, "login": "octo"})), "d-1")
    newcomer = {"id": 505, "login": "Octo", "type": "User"}
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr(13, user=newcomer)), "d-2")
    first = _one(sqlite_engine, person_identities, kind="github_id", value="101").person_id
    second = _one(sqlite_engine, person_identities, kind="github_id", value="505").person_id
    assert first != second
    assert _one(sqlite_engine, person_identities, kind="github_login", value="octo").person_id == first
    assert _identities(sqlite_engine, second) == [("github_id", "505", True)]


def test_last_seen_moves_at_most_hourly(sqlite_engine, lead, hook, tracked, monkeypatch):
    from datetime import timedelta
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    first = _one(sqlite_engine, person_identities, kind="github_id", value="101").last_seen
    server = lead.client.app.state.server
    start = server.now()
    monkeypatch.setattr(server, "now", lambda: start + timedelta(minutes=30))
    _send(lead, hook, "pull_request", _event("edited", pull_request=_pr(updated="2026-10-23T11:00:00Z")), "d-2")
    assert _one(sqlite_engine, person_identities, kind="github_id", value="101").last_seen == first
    monkeypatch.setattr(server, "now", lambda: start + timedelta(minutes=61))
    _send(lead, hook, "pull_request", _event("edited", pull_request=_pr(updated="2026-10-23T12:00:00Z")), "d-3")
    assert _one(sqlite_engine, person_identities, kind="github_id", value="101").last_seen == \
        start + timedelta(minutes=61)


def test_no_revert_link_to_an_unmerged_namesake_or_to_itself(sqlite_engine, lead, hook, tracked):
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr(20, title="Feature X")), "d-1")
    reverting = _pr(21, title='Revert "Feature X"')
    _send(lead, hook, "pull_request", _event("opened", pull_request=reverting), "d-2")
    itself = _pr(22, title="Undo", body="Reverts #22")
    _send(lead, hook, "pull_request", _event("opened", pull_request=itself), "d-3")
    assert len(_rows(sqlite_engine, gh_pull_requests)) == 3
    assert _rows(sqlite_engine, gh_pr_links) == []


def test_only_github_webhooks_queue_collection(sqlite_engine, lead, hook, world, tracked, monkeypatch):
    from armature.server.app import routes_webhooks
    queued = []
    monkeypatch.setattr(routes_webhooks.collect, "collect", lambda *args: queued.append(args[2]))
    generic = _create(lead, world, "generic", generic_auth="bearer")
    _set_secret(lead, generic)
    with sqlite_engine.begin() as conn:
        conn.execute(webhooks.update().where(webhooks.c.id == generic["id"]).values(approval="approved"))
    raw = _body(_event("opened", pull_request=_pr()))
    accepted = lead.client.post(_path(generic), content=raw,
                                headers={"Authorization": "Bearer s3cret-value", "X-GitHub-Event": "pull_request"})
    assert accepted.status_code == 200 and queued == []
    _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1")
    assert queued == ["pull_request"]


def test_a_replayed_delivery_is_collected(sqlite_engine, lead, world, tracked):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    with sqlite_engine.begin() as conn:
        conn.execute(webhooks.update().where(webhooks.c.id == hook["id"])
                     .values(approval="approved", pinned_senders=["github:installation:99"]))
    assert _send(lead, hook, "pull_request", _event("opened", pull_request=_pr()), "d-1").json() == \
        {"status": "held"}
    with sqlite_engine.begin() as conn:
        conn.execute(webhooks.update().where(webhooks.c.id == hook["id"])
                     .values(pinned_senders=["github:installation:99", f"github:installation:{INSTALLATION}"]))
    [held] = [d for d in _deliveries(sqlite_engine, hook["id"]) if d.status == "held"]
    replayed = lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{held.id}/replay", json={},
                                headers=lead.headers)
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert _one(sqlite_engine, gh_pull_requests).number == 12

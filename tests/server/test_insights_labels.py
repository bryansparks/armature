"""Labeling review comments and pull request change types: fetching comment text without keeping it, the rate-limit
pause, Jira issue types, the shared daily cap, and Jira bug fixes in the defect links."""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, insert, select, update

from armature.server import llm
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    connections,
    finding_candidates,
    gh_pr_commits,
    gh_pr_links,
    gh_pull_requests,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    labels,
    metadata,
    people,
    providers,
    runs,
    settings,
)
from armature.server.insights import (
    attribution,
    change_types,
    comment_labels,
    defects,
    ghapi,
    jobs,
)
from tests.server.tool_support import (  # noqa: F401
    Vendors,
    clock,
    dns,
    pem,
    vendor,
    world,
)

GH_REPO_ID = 4242
RESET = "2026-10-23T13:00:00Z"
SECRET = "SECRET-COMMENT-TEXT"


class Threads:
    """GitHub's GraphQL review threads, from the comments stored per pull request number."""

    def __init__(self):
        self.bodies: dict[int, list[tuple[int, str]]] = {}
        self.remaining = 4999
        self.seen: list[dict] = []
        self.missing = False
        self.gone: set[int] = set()

    def __call__(self, body):
        self.seen.append(body["variables"])
        if self.missing:
            return {"data": {"repository": None},
                    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]}
        repo = {}
        for name, number in body["variables"].items():
            if number in self.gone:
                repo[f"p{name[1:]}"] = None
            elif re.fullmatch(r"n\d+", name):
                nodes = [{"databaseId": i, "body": text} for i, text in self.bodies.get(number, [])]
                repo[f"p{name[1:]}"] = {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [
                    {"comments": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}]}}
        return {"data": {"rateLimit": {"remaining": self.remaining, "resetAt": RESET}, "repository": repo}}

    def numbers(self):
        return [[v for k, v in sorted(seen.items()) if re.fullmatch(r"n\d+", k)] for seen in self.seen]


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        items = json.loads(kwargs["messages"][-1]["content"])["items"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"labels": [self.answer(item) for item in items]})))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20), _hidden_params={"response_cost": 0.01})

    def items(self, call=-1):
        return json.loads(self.calls[call]["messages"][-1]["content"])["items"]


def _comment_answer(item):
    kind = "nit" if "nit" in item["text"] else "substantive"
    return {"id": item["id"], "kind": kind, "category": "naming" if kind == "nit" else "correctness"}


def _change_answer(item):
    return {"id": item["id"], "type": "code-maintenance" if "refactor" in item["title"].lower() else "enhancement"}


@pytest.fixture(autouse=True)
def unpaused():
    ghapi._paused.clear()
    yield
    ghapi._paused.clear()


@pytest.fixture
def threads(vendor):
    fake = Threads()
    vendor.github_graphql = fake
    return fake


@pytest.fixture
def jira_types():
    return {}


@pytest.fixture
def transport(vendor, jira_types):
    """The fake vendors, with a Jira issue type per key: a name, None for a 404, or an int status to answer."""
    asked: list[str] = []

    def handle(request):
        found = re.fullmatch(r"/rest/api/3/issue/([A-Z0-9-]+)", request.url.path)
        if found is None:
            return vendor(request)
        asked.append(found.group(1))
        answer = jira_types.get(found.group(1))
        if isinstance(answer, int):
            return httpx.Response(answer)
        if answer is None:
            return httpx.Response(404)
        return httpx.Response(200, json={"fields": {"issuetype": {"name": answer}}})

    mock = httpx.MockTransport(handle)
    mock.jira_asked = asked
    return mock


@pytest.fixture
def repo(sqlite_engine, world, clock):
    row_id = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(gh_repos).values(id=row_id, team_id=world.team, gh_repo_id=GH_REPO_ID,
                                             full_name="acme/app", project_id=world.project, enabled=True,
                                             created_at=clock(), updated_at=clock()))
    return row_id


@pytest.fixture
def model(sqlite_engine, world, clock, monkeypatch):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(providers).values(id=new_id(), team_id=world.team, kind="anthropic", config={},
                                              aliases={"fast": "claude-haiku-5-5"}, created_at=clock(),
                                              updated_at=clock()))
    fake = FakeModel(lambda item: _comment_answer(item) if "text" in item else _change_answer(item))
    monkeypatch.setattr(llm, "litellm_completion", fake)
    return fake


class History:
    def __init__(self, engine, world, repo, clock):
        self.engine, self.world, self.repo, self.clock = engine, world, repo, clock
        self.next_comment = 1000
        with engine.begin() as conn:
            self.alice = self._person(conn, "Alice", "human")
            self.ci_bot = self._person(conn, "CI", "bot")
            self.armature = attribution.armature_person(conn, world.team, clock())

    def _person(self, conn, name, kind):
        row_id = new_id()
        conn.execute(insert(people).values(id=row_id, team_id=self.world.team, display_name=name, kind=kind,
                                           created_at=self.clock(), updated_at=self.clock()))
        return row_id

    def pr(self, number, *, title="Change", head_ref=None, jira_key=None, merged_days_ago=1, state="merged",
           files=()):
        row_id, at = new_id(), self.clock() - timedelta(days=merged_days_ago)
        with self.engine.begin() as conn:
            conn.execute(insert(gh_pull_requests).values(
                id=row_id, team_id=self.world.team, repo_row_id=self.repo, gh_pr_id=900000 + number, number=number,
                title=title, author_gh_id=101, author_login="octo", state=state, head_ref=head_ref or f"b-{number}",
                jira_key=jira_key, created_at_gh=at - timedelta(days=1), gh_updated_at=at,
                merged_at_gh=at if state == "merged" else None, created_at=at, updated_at=at))
            conn.execute(insert(gh_pr_commits).values(id=new_id(), team_id=self.world.team, repo_row_id=self.repo,
                                                      commit_sha=f"{number:040x}", pr_row_id=row_id,
                                                      created_at=at))
            defects.add_files(conn, self.world.team, self.repo, row_id, list(files), at)
        return row_id

    def comment(self, pr_id, *, minutes_ago, person=None, login="alice", review=None, deleted=False):
        self.next_comment += 1
        row_id, at = new_id(), self.clock() - timedelta(minutes=minutes_ago)
        with self.engine.begin() as conn:
            conn.execute(insert(gh_review_comments).values(
                id=row_id, team_id=self.world.team, pr_row_id=pr_id, gh_comment_id=self.next_comment,
                gh_review_id=review, author_person_id=person, author_gh_id=7, author_login=login, path="src/a.py",
                line=3, created_at_gh=at, updated_at_gh=at, deleted=deleted, body_length=None, created_at=at))
        return row_id, self.next_comment

    def review(self, pr_id, gh_review_id, actor_source):
        with self.engine.begin() as conn:
            conn.execute(insert(gh_reviews).values(
                id=new_id(), team_id=self.world.team, pr_row_id=pr_id, gh_review_id=gh_review_id,
                reviewer_gh_id=9001, reviewer_login="armature-bot[bot]", actor_source=actor_source,
                state="commented", submitted_at=self.clock(), created_at=self.clock(), updated_at=self.clock()))


@pytest.fixture
def history(sqlite_engine, world, repo, clock):
    return History(sqlite_engine, world, repo, clock)


def _labels(engine, kind=None):
    with engine.connect() as conn:
        query = select(labels)
        if kind:
            query = query.where(labels.c.subject_kind == kind)
        return conn.execute(query).all()


def _by_subject(engine, kind):
    found: dict[str, dict[str, str]] = {}
    for r in _labels(engine, kind):
        found.setdefault(r.subject_id, {})[r.taxonomy_version] = r.label
    return found


def _no_column_holds(engine, text):
    with engine.connect() as conn:
        for table in metadata.sorted_tables:
            for row in conn.execute(select(table)):
                for value in row:
                    assert text not in json.dumps(value, default=str), table.name


def _comments(history, threads, count, *, text=lambda i: f"{SECRET} {i}"):
    """``count`` comments by Alice, one per pull request, newest on the lowest number."""
    made = []
    for i in range(count):
        pr = history.pr(i + 1)
        row_id, gh_id = history.comment(pr, minutes_ago=i + 1, person=history.alice)
        threads.bodies[i + 1] = [(gh_id, text(i))]
        made.append(row_id)
    return made


# comments

def test_comment_text_comes_in_batches_and_only_two_labels_are_kept(sqlite_engine, keyring, clock, world, history,
                                                                    threads, model, transport):
    wanted = _comments(history, threads, 7, text=lambda i: f"{SECRET} {'nit' if i % 2 else 'race'} {i}")
    pr = history.pr(50)
    skipped = [history.comment(pr, minutes_ago=30, person=history.ci_bot)[0],
               history.comment(pr, minutes_ago=30, login="dependabot[bot]")[0],
               history.comment(pr, minutes_ago=30, person=history.armature)[0],
               history.comment(pr, minutes_ago=30, person=history.alice, review=77)[0],
               history.comment(pr, minutes_ago=30, person=history.alice, deleted=True)[0]]
    history.review(pr, 77, attribution.ARMATURE)
    unmatched, unmatched_gh = history.comment(pr, minutes_ago=30, login="carol")
    threads.bodies[50] = [(unmatched_gh, f"{SECRET} race unmatched")]

    totals = comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(),
                                  transport=transport, clock=clock)

    assert threads.numbers() == [[1, 2, 3, 4, 5], [6, 7, 50]]
    assert (totals.calls, totals.labeled, totals.attempted) == (1, 8, 8)
    assert sorted(item["text"].split()[-1] for item in model.items()) == sorted([*map(str, range(7)), "unmatched"])
    found = _by_subject(sqlite_engine, "review_comment")
    assert set(found) == {*wanted, unmatched} and not set(found) & set(skipped)
    assert found[wanted[0]] == {"comment-v1": "substantive", "v1": "correctness"}
    assert found[wanted[1]] == {"comment-v1": "nit", "v1": "other"}
    assert {r.labeler for r in _labels(sqlite_engine)} == {"fast:anthropic/claude-haiku-5-5"}
    _no_column_holds(sqlite_engine, SECRET)
    with sqlite_engine.connect() as conn:
        [entry] = conn.execute(select(audit).where(audit.c.action == "insights.label")).all()
    assert entry.target_id == "comment-v1" and entry.detail["calls"] == 1
    assert {k: entry.detail[k] for k in ("labeled", "labeled_by_model", "labeled_by_jira", "unavailable")} == {
        "labeled": 8, "labeled_by_model": 8, "labeled_by_jira": 0, "unavailable": 0}

    again = comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(),
                                 transport=transport, clock=clock)
    assert (again.calls, again.attempted, len(threads.seen), len(model.calls)) == (0, 0, 2, 1)


def test_rest_reads_what_the_threads_miss_and_a_404_marks_the_comment_unavailable(
        sqlite_engine, keyring, clock, world, vendor, history, threads, model, transport):
    pr = history.pr(1)
    kept, kept_gh = history.comment(pr, minutes_ago=1, person=history.alice)
    gone, gone_gh = history.comment(pr, minutes_ago=2, person=history.alice)
    vendor.gets[f"/repos/acme/app/pulls/comments/{kept_gh}"] = {"id": kept_gh, "body": f"{SECRET} nit spacing"}

    totals = comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=10, now=clock(),
                                  transport=transport, clock=clock)

    assert (totals.labeled, totals.unavailable, totals.calls) == (2, 1, 1)
    assert _by_subject(sqlite_engine, "review_comment") == {kept: {"comment-v1": "nit", "v1": "other"},
                                                          gone: {"comment-v1": "unavailable"}}
    rest = [r.url.path for r in vendor.requests if "/pulls/comments/" in r.url.path]
    assert rest == [f"/repos/acme/app/pulls/comments/{kept_gh}", f"/repos/acme/app/pulls/comments/{gone_gh}"]
    comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=10, now=clock(), transport=transport,
                         clock=clock)
    assert len([r for r in vendor.requests if "/pulls/comments/" in r.url.path]) == 2
    _no_column_holds(sqlite_engine, SECRET)


@pytest.mark.parametrize("failure", ["repository", "pull request"])
def test_a_repository_or_pull_request_github_cant_resolve_marks_nothing(sqlite_engine, keyring, clock, world, vendor,
                                                                        history, threads, model, transport, failure):
    pr = history.pr(1)
    history.comment(pr, minutes_ago=1, person=history.alice)
    if failure == "repository":
        threads.missing = True
    else:
        threads.gone.add(1)

    totals = comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=10, now=clock(),
                                  transport=transport, clock=clock)

    assert (totals.labeled, totals.attempted, _labels(sqlite_engine)) == (0, 0, [])
    rest = [r for r in vendor.requests if "/pulls/comments/" in r.url.path]
    assert len(rest) == (0 if failure == "repository" else 1)


def test_a_disabled_repository_still_has_its_comments_labeled(sqlite_engine, keyring, clock, world, repo, history,
                                                              threads, model, transport):
    [wanted] = _comments(history, threads, 1)
    with sqlite_engine.begin() as conn:
        conn.execute(update(gh_repos).where(gh_repos.c.id == repo).values(enabled=False))
    comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=10, now=clock(), transport=transport,
                         clock=clock)
    assert set(_by_subject(sqlite_engine, "review_comment")) == {wanted}


def test_a_low_rate_limit_keeps_the_batch_it_paid_for_and_leaves_the_rest(sqlite_engine, keyring, clock, world,
                                                                          history, threads, model, transport):
    wanted = _comments(history, threads, 7)
    threads.remaining = 100

    first = comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(),
                                 transport=transport, clock=clock)

    assert threads.numbers() == [[1, 2, 3, 4, 5]]
    assert (first.labeled, first.attempted) == (5, 5)
    assert set(_by_subject(sqlite_engine, "review_comment")) == set(wanted[:5])

    again = comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(),
                                 transport=transport, clock=clock)
    assert (again.attempted, len(threads.seen)) == (0, 1)

    clock.advance(hours=2)
    threads.remaining = 4999
    later = comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(),
                                 transport=transport, clock=clock)
    assert later.labeled == 2 and threads.numbers()[-1] == [6, 7]


def test_a_paused_token_fetches_nothing(sqlite_engine, keyring, clock, world, history, threads, model, transport):
    _comments(history, threads, 2)
    ghapi._paused[(world.github, 7, ghapi.GRAPHQL)] = clock() + timedelta(minutes=30)
    totals = comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(),
                                  transport=transport, clock=clock)
    assert (totals.attempted, totals.calls, threads.seen, _labels(sqlite_engine)) == (0, 0, [], [])


def test_the_limit_bounds_what_is_fetched(sqlite_engine, keyring, clock, world, history, threads, model, transport):
    wanted = _comments(history, threads, 4)
    totals = comment_labels.label(sqlite_engine, keyring, world.team, "fast", limit=3, now=clock(),
                                  transport=transport, clock=clock)
    assert threads.numbers() == [[1, 2, 3]] and totals.attempted == 3
    assert set(_by_subject(sqlite_engine, "review_comment")) == set(wanted[:3])


# change types

@pytest.mark.parametrize("issue_type, change", [
    ("Bug", "bug-fix"), ("Defect", "bug-fix"), ("Story", "new-feature"), ("New Feature", "new-feature"),
    ("Improvement", "enhancement"), ("Enhancement", "enhancement"), ("Task", None), ("Sub-task", None),
    ("Chore", None), ("Tech Debt", "code-maintenance"), ("Maintenance", "code-maintenance"),
    ("Build", "platform-maintenance"), ("CI", "platform-maintenance"), ("Release", "platform-maintenance"),
    ("Platform Work", "platform-maintenance"), ("Dependencies", "platform-maintenance"),
    ("Dependency Update", "platform-maintenance"), ("Epic", None), ("Spike", None), ("Builder", None),
])
def test_jira_issue_types_map_to_change_types(issue_type, change):
    assert change_types.from_jira(issue_type) == change


def test_change_types_come_from_jira_once_per_key_and_else_from_the_model(sqlite_engine, keyring, clock, world,
                                                                          history, model, transport, jira_types):
    jira_types.update({"MOB-1": "Bug", "MOB-2": "Task", "MOB-3": "Release"})
    bug = history.pr(1, jira_key="MOB-1")
    same_key = history.pr(2, jira_key="MOB-1")
    task = history.pr(3, title="Refactor the cart", jira_key="MOB-2")
    release = history.pr(4, jira_key="MOB-3")
    missing = history.pr(5, title="Add sharing", jira_key="MOB-404")
    plain = history.pr(6, title="Add a filter", head_ref="feature/filter",
                       files=[f"src/f{i:02d}.py" for i in range(40)])
    history.pr(7, state="open")

    totals = change_types.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(),
                                transport=transport)

    assert sorted(transport.jira_asked) == ["MOB-1", "MOB-2", "MOB-3", "MOB-404"]
    rows = {r.subject_id: (r.label, r.labeler) for r in _labels(sqlite_engine, "pull_request")}
    assert rows == {bug: ("bug-fix", "jira:Bug"), same_key: ("bug-fix", "jira:Bug"),
                    release: ("platform-maintenance", "jira:Release"),
                    task: ("code-maintenance", "fast:anthropic/claude-haiku-5-5"),
                    missing: ("enhancement", "fast:anthropic/claude-haiku-5-5"),
                    plain: ("enhancement", "fast:anthropic/claude-haiku-5-5")}
    assert {r.taxonomy_version for r in _labels(sqlite_engine, "pull_request")} == {"change-v1"}
    assert (totals.labeled, totals.by_jira, totals.attempted, totals.calls) == (6, 3, 6, 1)
    with sqlite_engine.connect() as conn:
        [entry] = conn.execute(select(audit).where(audit.c.action == "insights.label")).all()
    assert entry.target_id == "change-v1" and (entry.detail["labeled_by_model"], entry.detail["labeled_by_jira"],
                                               entry.detail["unavailable"]) == (3, 3, 0)
    [filtered] = [item for item in model.items() if item["title"] == "Add a filter"]
    assert filtered["branch"] == "feature/filter" and filtered["files"] == [f"src/f{i:02d}.py" for i in range(30)]

    again = change_types.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(),
                               transport=transport)
    assert (again.attempted, len(model.calls), len(transport.jira_asked)) == (0, 1, 4)


@pytest.mark.parametrize("broken", ["missing", "unverified", "failing"])
def test_a_missing_or_failing_jira_connection_falls_back_to_the_model(sqlite_engine, keyring, clock, world, history,
                                                                      model, transport, jira_types, broken):
    jira_types.update({"MOB-1": 500 if broken == "failing" else "Bug", "MOB-2": "Bug"})
    with sqlite_engine.begin() as conn:
        if broken == "missing":
            conn.execute(delete(connections).where(connections.c.id == world.jira))
        elif broken == "unverified":
            conn.execute(update(connections).where(connections.c.id == world.jira).values(state="error"))
    first = history.pr(1, title="Refactor auth", jira_key="MOB-1", merged_days_ago=1)
    second = history.pr(2, title="Add login", jira_key="MOB-2", merged_days_ago=2)

    change_types.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(), transport=transport)

    assert transport.jira_asked == (["MOB-1"] if broken == "failing" else [])
    rows = {r.subject_id: (r.label, r.labeler) for r in _labels(sqlite_engine, "pull_request")}
    assert rows == {first: ("code-maintenance", "fast:anthropic/claude-haiku-5-5"),
                    second: ("enhancement", "fast:anthropic/claude-haiku-5-5")}


def test_a_jira_bug_fix_links_to_what_it_fixes_at_higher_confidence(sqlite_engine, keyring, clock, world, history,
                                                                    model, transport, jira_types):
    files = ["src/a.py", "src/b.py"]
    earlier = history.pr(1, merged_days_ago=10, files=files)
    titled_earlier = history.pr(2, merged_days_ago=10, files=["src/c.py", "src/d.py"])
    fix = history.pr(3, title="Handle an empty cart", jira_key="MOB-1", merged_days_ago=1, files=files)
    titled = history.pr(4, title="fix: crash on launch", jira_key="MOB-2", merged_days_ago=1,
                        files=["src/c.py", "src/d.py"])
    jira_types.update({"MOB-1": "Bug", "MOB-2": "Bug"})
    with sqlite_engine.begin() as conn:
        assert defects.link_fixes(conn, world.team, [fix, titled], clock()) == 1

    change_types.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(), transport=transport)

    with sqlite_engine.connect() as conn:
        links = {(r.from_pr_row_id, r.to_pr_row_id): r.confidence for r in conn.execute(select(gh_pr_links))}
    assert links == {(fix, earlier): defects.JIRA_CONFIDENCE, (titled, titled_earlier): defects.JIRA_CONFIDENCE}
    with sqlite_engine.begin() as conn:
        assert defects.link_fixes(conn, world.team, [fix, titled], clock()) == 0


def test_a_bug_fix_from_the_model_never_raises_a_link(sqlite_engine, keyring, clock, world, history, model,
                                                     transport):
    model.answer = lambda item: {"id": item["id"], "type": "bug-fix" if item["title"] != "Change" else "enhancement"}
    history.pr(1, merged_days_ago=10, files=["src/a.py", "src/b.py"])
    history.pr(2, merged_days_ago=10, files=["src/c.py", "src/d.py"])
    plain = history.pr(3, title="Handle an empty cart", merged_days_ago=1, files=["src/a.py", "src/b.py"])
    titled = history.pr(4, title="fix: crash on launch", merged_days_ago=1, files=["src/c.py", "src/d.py"])
    with sqlite_engine.begin() as conn:
        assert defects.link_fixes(conn, world.team, [plain, titled], clock()) == 1

    change_types.label(sqlite_engine, keyring, world.team, "fast", limit=100, now=clock(), transport=transport)
    with sqlite_engine.begin() as conn:
        assert defects.link_fixes(conn, world.team, [plain, titled], clock()) == 0
        links = {r.from_pr_row_id: r.confidence for r in conn.execute(select(gh_pr_links))}

    labeled = {r.subject_id: r.label for r in _labels(sqlite_engine, "pull_request")}
    assert labeled[plain] == labeled[titled] == "bug-fix"
    assert links == {titled: defects.TITLE_CONFIDENCE}


# the daily job

def _candidate(engine, world, clock, claim):
    with engine.begin() as conn:
        run_id = new_id()
        conn.execute(insert(runs).values(id=run_id, team_id=world.team, project_id=world.project, status="succeeded",
                                         started_at=clock()))
        conn.execute(insert(finding_candidates).values(
            id=new_id(), team_id=world.team, project_id=world.project, run_id=run_id, step_key="s",
            specialist="correctness", ordinal=0, finding_key=new_id(), path="a.py", line=1, claim=claim,
            created_at=clock()))


def test_the_three_passes_share_the_daily_cap(sqlite_engine, keyring, clock, world, history, threads, model,
                                              transport, jira_types):
    model.answer = lambda item: (_comment_answer(item) if "text" in item else _change_answer(item)
                                 if "title" in item else {"id": item["id"], "category": "correctness",
                                                          "confidence": 0.9})
    _candidate(sqlite_engine, world, clock, "a null deref")
    comments = _comments(history, threads, 3)
    jira_types["MOB-9"] = "Bug"
    history.pr(60, jira_key="MOB-9", merged_days_ago=0)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key=jobs.CAP_SETTING, value=3))

    done = jobs.label_team(sqlite_engine, keyring, world.team, clock(), transport)

    assert {name: t.attempted for name, t in done.items()} == {"findings": 1, "comments": 2}
    assert len(_labels(sqlite_engine, "candidate")) == 1
    assert set(_by_subject(sqlite_engine, "review_comment")) == set(comments[:2])
    assert _labels(sqlite_engine, "pull_request") == []

    done = jobs.label_team(sqlite_engine, keyring, world.team, clock(), transport)
    assert {name: t.attempted for name, t in done.items()} == {"comments": 1, "changes": 2}
    assert len(_labels(sqlite_engine, "pull_request")) == 2


def test_a_team_without_a_model_labels_only_by_jira_and_logs_once(sqlite_engine, keyring, clock, world, history,
                                                                   threads, transport, jira_types, caplog):
    _comments(history, threads, 2)
    jira_types.update({"MOB-1": "Story", "MOB-2": "Task"})
    story = history.pr(70, jira_key="MOB-1")
    history.pr(71, jira_key="MOB-2")

    with caplog.at_level(logging.WARNING, logger="armature.server.insights"):
        done = jobs.label_team(sqlite_engine, keyring, world.team, clock(), transport)

    assert list(done) == ["changes"] and threads.seen == []
    assert {r.subject_id: r.label for r in _labels(sqlite_engine)} == {story: "new-feature"}
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING
                and r.name.startswith("armature.server.insights")]) == 1


def test_a_failing_model_stops_comments_and_leaves_jira_change_types(sqlite_engine, keyring, clock, world, history,
                                                                     threads, model, transport, jira_types):
    def answer(item):
        if "text" in item:
            raise RuntimeError("model down")
        return _change_answer(item)

    model.answer = answer
    _comments(history, threads, 2)
    jira_types["MOB-1"] = "Bug"
    bug = history.pr(80, jira_key="MOB-1", merged_days_ago=0)

    done = jobs.label_team(sqlite_engine, keyring, world.team, clock(), transport)

    assert list(done) == ["comments", "changes"]
    assert (done["comments"].calls, done["changes"].calls, len(model.calls)) == (0, 0, 1)
    assert {r.subject_id: r.label for r in _labels(sqlite_engine)} == {bug: "bug-fix"}


def test_another_teams_comments_and_pull_requests_stay_unlabeled(sqlite_engine, keyring, clock, world, history,
                                                                 threads, model, transport):
    mine = _comments(history, threads, 1)
    other_repo, other_pr, other_comment = new_id(), new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(gh_repos).values(id=other_repo, team_id=world.other, gh_repo_id=5151,
                                             full_name="acme/web", enabled=True, created_at=clock(),
                                             updated_at=clock()))
        conn.execute(insert(gh_pull_requests).values(
            id=other_pr, team_id=world.other, repo_row_id=other_repo, gh_pr_id=777, number=1, title="Theirs",
            author_gh_id=5, author_login="dana", state="merged", created_at_gh=clock(), gh_updated_at=clock(),
            merged_at_gh=clock(), created_at=clock(), updated_at=clock()))
        conn.execute(insert(gh_review_comments).values(
            id=other_comment, team_id=world.other, pr_row_id=other_pr, gh_comment_id=31337, author_gh_id=5,
            author_login="dana", created_at_gh=clock(), updated_at_gh=clock(), created_at=clock()))

    with sqlite_engine.connect() as conn:
        assert [r.id for r in comment_labels.unlabeled(conn, world.team, 100)] == mine
        assert other_pr not in {r.id for r in change_types.unlabeled(conn, world.team, 100)}
    jobs.label_team(sqlite_engine, keyring, world.team, clock(), transport)

    subjects = {r.subject_id for r in _labels(sqlite_engine)}
    assert set(mine) <= subjects and not subjects & {other_pr, other_comment}
    assert {r.team_id for r in _labels(sqlite_engine)} == {world.team}
    assert all(v.get("name") != "web" for v in threads.seen)

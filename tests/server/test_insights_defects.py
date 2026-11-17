"""Escaped-defect candidates: which merged fixes link to which earlier pull requests, and how many."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from armature.server.db import new_id
from armature.server.db.schema import gh_pr_commits, gh_pr_links, gh_pull_requests, gh_repos, teams
from armature.server.insights import defects

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
FILES = ["src/a.py", "src/b.py"]


@pytest.fixture
def repo(sqlite_engine):
    team, repo_id = new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="mobile", created_at=T0))
        conn.execute(insert(gh_repos).values(id=repo_id, team_id=team, gh_repo_id=1, full_name="acme/app",
                                             enabled=True, created_at=T0, updated_at=T0))
    return team, repo_id


def _merged(engine, repo, number, *, title="Change", files=FILES, days_before=10, landed=True):
    team, repo_id = repo
    row_id, merged = new_id(), T0 - timedelta(days=days_before)
    with engine.begin() as conn:
        conn.execute(insert(gh_pull_requests).values(
            id=row_id, team_id=team, repo_row_id=repo_id, gh_pr_id=number, number=number, title=title,
            author_gh_id=1, author_login="octo", state="merged", created_at_gh=merged, merged_at_gh=merged,
            gh_updated_at=merged, created_at=T0, updated_at=T0))
        if landed:
            conn.execute(insert(gh_pr_commits).values(id=new_id(), team_id=team, repo_row_id=repo_id,
                                                      commit_sha=f"{number:040x}", pr_row_id=row_id, created_at=T0))
        defects.add_files(conn, team, repo_id, row_id, files, T0)
    return row_id


def _fix(engine, repo, files=FILES, title="fix: crash on launch"):
    return _merged(engine, repo, 999, title=title, files=files, days_before=0)


def _links(engine, repo, fix):
    with engine.begin() as conn:
        made = defects.link_fixes(conn, repo[0], [fix], T0)
        rows = conn.execute(select(gh_pr_links.c.to_pr_row_id).where(gh_pr_links.c.from_pr_row_id == fix,
                                                                     gh_pr_links.c.kind == "fix")).scalars().all()
    return made, set(rows)


@pytest.mark.parametrize("title, fix", [
    ("fix: crash on launch", True), ("Fix the login crash", True), ("Bugfix: null token", True),
    ("bug fix for sync", True), ("fixes #12", True), ("Fixed the parser", True),
    ("fix typo in README", False), ("fix(ci): pin the runner", False), ("Fix lint warnings", False),
    ("fixture cleanup", False), ("Add a fix button", False), (None, False),
])
def test_which_titles_are_fixes(title, fix):
    assert defects.is_fix(title) is fix


def test_a_fix_links_to_the_last_toucher_of_each_shared_file_and_ranks_by_overlap(sqlite_engine, repo):
    older = _merged(sqlite_engine, repo, 1, days_before=30, files=["src/a.py", "src/b.py", "src/c.py"])
    newer = _merged(sqlite_engine, repo, 2, days_before=5, files=["src/a.py", "src/b.py"])
    third = _merged(sqlite_engine, repo, 3, days_before=20, files=["src/c.py", "src/d.py"])
    fix = _fix(sqlite_engine, repo, files=["src/a.py", "src/b.py", "src/c.py", "src/d.py"])
    assert _links(sqlite_engine, repo, fix) == (2, {newer, third})
    assert older not in _links(sqlite_engine, repo, fix)[1]


def test_links_stop_at_three_and_a_rerun_adds_none(sqlite_engine, repo):
    paths = [f"src/{n}.py" for n in range(10)]
    for number in range(5):
        _merged(sqlite_engine, repo, number + 1, days_before=number + 1, files=paths[number * 2:number * 2 + 2])
    fix = _fix(sqlite_engine, repo, files=paths)
    made, linked = _links(sqlite_engine, repo, fix)
    assert (made, len(linked)) == (3, 3)
    _merged(sqlite_engine, repo, 50, days_before=0.5, files=paths[:2])
    assert _links(sqlite_engine, repo, fix) == (0, linked)


def test_only_landed_merges_inside_90_days_count(sqlite_engine, repo):
    _merged(sqlite_engine, repo, 1, days_before=91)
    _merged(sqlite_engine, repo, 2, days_before=3, landed=False)
    inside = _merged(sqlite_engine, repo, 3, days_before=89)
    assert _links(sqlite_engine, repo, _fix(sqlite_engine, repo)) == (1, {inside})


def test_one_shared_file_or_only_lock_and_generated_files_link_nothing(sqlite_engine, repo):
    noise = ["package-lock.json", "ios/Podfile.lock", "App.xcodeproj/project.pbxproj", "CHANGELOG.md",
             "src/generated/api.swift", "web/app.min.js", "Package.resolved", "yarn.lock", "gradle.lockfile"]
    _merged(sqlite_engine, repo, 1, files=["src/a.py", *noise])
    assert _links(sqlite_engine, repo, _fix(sqlite_engine, repo, files=["src/a.py", *noise])) == (0, set())
    assert [p for p in noise if defects.counts(p)] == []


def test_a_typo_fix_links_nothing(sqlite_engine, repo):
    _merged(sqlite_engine, repo, 1)
    assert _links(sqlite_engine, repo, _fix(sqlite_engine, repo, title="fix typo in docs")) == (0, set())


def test_each_linked_pull_request_shares_two_files_with_the_fix(sqlite_engine, repo):
    _merged(sqlite_engine, repo, 1, files=["src/a.py"])
    _merged(sqlite_engine, repo, 2, files=["src/b.py"])
    assert _links(sqlite_engine, repo, _fix(sqlite_engine, repo)) == (0, set())

"""Insights numbers and API: each measure on hand-computed data, previous windows, role gates, team scoping, attention,
people corrections, and the labeling and clustering jobs."""
from __future__ import annotations

import hashlib
import itertools
import json
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from sqlalchemy import insert, select

from armature.server import llm
from armature.server.app import routes_insights
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    candidate_matches,
    cluster_members,
    clusters,
    connections,
    finding_candidates,
    findings,
    gh_check_runs,
    gh_conflict_samples,
    gh_pr_links,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    labels,
    llm_calls,
    people,
    person_identities,
    providers,
    runs,
    settings,
    tool_calls,
)
from armature.server.insights import (
    attribution,
    jobs,
    labeling,
    metrics,
    people_admin,
    taxonomy,
)
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)
from tests.server.live_support import add_team

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
GH_IDS = itertools.count(1000)


def at(month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


class Builder:
    """Rows for one team, inserted as they are described."""

    def __init__(self, engine, team):
        self.engine = engine
        self.team, self.project = team

    def _add(self, table, **values):
        row_id = values.pop("id", None) or new_id()
        with self.engine.begin() as conn:
            conn.execute(insert(table).values(id=row_id, team_id=self.team, **values))
        return row_id

    def repo(self, full_name):
        return self._add(gh_repos, gh_repo_id=next(GH_IDS), full_name=full_name, enabled=True, created_at=NOW,
                         updated_at=NOW)

    def person(self, name, kind="human", user_id=None):
        person_id = self._add(people, display_name=name, kind=kind, user_id=user_id, created_at=NOW, updated_at=NOW)
        gh = next(GH_IDS)
        self._add(person_identities, person_id=person_id, kind="github_id", value=str(gh), verified=True,
                  first_seen=NOW, last_seen=NOW)
        return SimpleNamespace(id=person_id, gh=gh, login=name.lower())

    def armature(self):
        with self.engine.begin() as conn:
            person_id = attribution.armature_person(conn, self.team, NOW)
        return SimpleNamespace(id=person_id, gh=9001, login="armature-bot[bot]")

    def pr(self, repo, number, author, created, merged=None, *, state=None, head=None, additions=None,
           deletions=None, draft=False, title=None, closed=None):
        return self._add(gh_pull_requests, repo_row_id=repo, gh_pr_id=next(GH_IDS), number=number,
                         title=title or f"PR {number}", author_person_id=author.id, author_gh_id=author.gh,
                         author_login=author.login, state=state or ("merged" if merged else "open"), draft=draft,
                         head_sha=head, additions=additions, deletions=deletions, created_at_gh=created,
                         merged_at_gh=merged, closed_at_gh=closed or merged, gh_updated_at=merged or created,
                         created_at=NOW,
                         updated_at=NOW)

    def review(self, pr, who, state, head, submitted, actor_source="github"):
        gh_review_id = next(GH_IDS)
        self._add(gh_reviews, pr_row_id=pr, gh_review_id=gh_review_id, reviewer_person_id=who.id,
                  reviewer_gh_id=who.gh, reviewer_login=who.login, actor_source=actor_source, state=state,
                  head_sha=head, submitted_at=submitted, url=f"https://github.com/r/{gh_review_id}", created_at=NOW,
                  updated_at=NOW)
        return gh_review_id

    def comment(self, pr, who, path, created, review_id=None, reply_to=None, head=None):
        return self._add(gh_review_comments, pr_row_id=pr, gh_comment_id=next(GH_IDS), gh_review_id=review_id,
                         author_person_id=who.id, author_gh_id=who.gh, author_login=who.login, path=path, line=3,
                         head_sha=head, created_at_gh=created, updated_at_gh=created, in_reply_to=reply_to,
                         url="https://github.com/c", created_at=NOW)

    def push(self, pr, after, pushed, kind=None, pusher=None):
        self._add(gh_pushes, pr_row_id=pr, before_sha="b" + after, after_sha=after, pushed_at=pushed,
                  source="sync", push_class=kind, pusher_person_id=pusher.id if pusher else None, created_at=NOW)

    def check(self, repo, pr, head, status, conclusion, completed):
        self._add(gh_check_runs, pr_row_id=pr, gh_check_run_id=next(GH_IDS), repo_row_id=repo, name="ci",
                  head_sha=head, status=status, conclusion=conclusion, completed_at=completed, created_at=NOW)

    def sample(self, pr, sampled, mergeable):
        self._add(gh_conflict_samples, pr_row_id=pr, sampled_at=sampled, mergeable=mergeable)

    def link(self, source, target, kind, confidence, evidence=None):
        self._add(gh_pr_links, from_pr_row_id=source, to_pr_row_id=target, kind=kind, confidence=confidence,
                  evidence=evidence, detector_version="1", created_at=NOW)

    def label(self, kind, subject_id, label, version=taxonomy.VERSION):
        self._add(labels, subject_kind=kind, subject_id=subject_id, taxonomy_version=version, label=label,
                  labeler="test", created_at=NOW)

    def run(self, target, head, started):
        run_id = self._add(runs, project_id=self.project, status="succeeded", started_at=started, head_sha=head)
        self._add(tool_calls, run_id=run_id, project_id=self.project, step_id="decisions", tool="review.decisions",
                  mode="live", outcome="ok", target=target, created_at=started)
        return run_id

    def posted(self, target, head, when):
        run_id = self._add(runs, project_id=self.project, status="succeeded", started_at=when)
        self._add(tool_calls, run_id=run_id, project_id=self.project, step_id="post", tool="github.post-review",
                  mode="live", outcome="ok", target=target, detail={"posted": True, "head": head}, created_at=when)

    def flag(self, run_id, specialist, path, created, decision=None, category=None, ordinal=0):
        candidate = self._add(finding_candidates, project_id=self.project, run_id=run_id,
                              step_key=f"review/{specialist}", specialist=specialist, ordinal=ordinal, path=path,
                              line=7, claim=f"{specialist} claim on {path}", created_at=created)
        finding_id = None
        if decision is not None:
            finding_id = new_id()
            with self.engine.begin() as conn:
                conn.execute(insert(findings).values(id=finding_id, run_id=run_id, finding_key=new_id()[:12],
                                                     path=path, decision=decision, created_at=created))
        self._add(candidate_matches, candidate_id=candidate, finding_id=finding_id, match_ordinal=0,
                  method="key" if decision else "none", matcher_version="1", created_at=created)
        if category:
            self.label("candidate", candidate, category)
        return candidate

    def spend(self, run_id, step_key, model, cost, created, cached=False):
        self._add(llm_calls, run_id=run_id, seq=next(GH_IDS), step_key=step_key, model_id=model, cost_usd=cost,
                  cached=cached, created_at=created)


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def world(sqlite_engine):
    """Two repos, five people, a bot, Armature, and pull requests whose every number below is worked by hand."""
    team = add_team(sqlite_engine, lambda: NOW, "mobile")
    other = add_team(sqlite_engine, lambda: NOW, "web")
    b = Builder(sqlite_engine, team)
    app, lib = b.repo("acme/app"), b.repo("acme/lib")
    alice, bob, carol, dave, erin = (b.person(n) for n in ("Alice", "Bob", "Carol", "Dave", "Erin"))
    bot = b.person("dependabot[bot]", kind="bot")
    arm = b.armature()

    # P1: 2 days to merge, first human review after 12 hours, rounds on h1a and h1b.
    p1 = b.pr(app, 1, alice, at(11, 1), at(11, 3), head="h1b", additions=40, deletions=10)
    b.review(p1, alice, "commented", "h1d", at(11, 1, 23))
    first = b.review(p1, bob, "commented", "h1a", at(11, 2, 0))
    b.review(p1, bot, "approved", "h1c", at(11, 2, 1))
    b.review(p1, bob, "approved", "h1b", at(11, 2, 12))
    root = b.comment(p1, bob, "app/src/Main.kt", at(11, 1, 23, 30), review_id=first, head="h1a")
    reply = b.comment(p1, bob, "app/src/Main.kt", at(11, 1, 23, 45), review_id=first, reply_to=1, head="h1a")
    b.label(metrics.COMMENT_KIND, reply, "nit", labeling.COMMENT_VERSION)
    b.label(metrics.COMMENT_KIND, reply, "conventions")
    b.comment(p1, alice, "app/src/Main.kt", at(11, 2, 1), reply_to=1, head="h1a")
    # Phase 3c files two labels on each comment: substantive or not, and its category.
    b.label(metrics.COMMENT_KIND, root, metrics.SUBSTANTIVE, labeling.COMMENT_VERSION)
    b.label(metrics.COMMENT_KIND, root, "concurrency")
    b.label(metrics.CHANGE_KIND, p1, "enhancement")
    b.push(p1, "h1a", at(11, 1, 13), "rebase")
    b.push(p1, "h1b", at(11, 2, 3), "fix_after_review")
    # Samples are stored on change. Unknown at 16:00 is GitHub recomputing, so the conflict runs 13:00 to 19:00.
    for hour, state in ((13, "conflicting"), (16, "unknown"), (17, "conflicting"), (19, "mergeable")):
        b.sample(p1, at(11, 1, hour), state)
    b.check(app, p1, "h1b", "completed", "success", at(11, 2, 14))
    b.check(app, p1, "h1b", "completed", "failure", at(11, 2, 13))
    b.check(app, p1, "h1b", "in_progress", None, None)
    b.check(app, p1, "h1a", "completed", "failure", at(11, 1, 20))

    # P2: 1 day, first review after 6 hours, rounds on h2a, h2b, and Armature's posted h2c.
    p2 = b.pr(app, 2, bob, at(11, 5), at(11, 6), head="h2c", additions=5, deletions=3)
    b.review(p2, alice, "changes_requested", "h2a", at(11, 5, 18))
    b.review(p2, alice, "approved", "h2b", at(11, 6, 0))
    b.posted("acme/app#2", "h2c", at(11, 5, 20))
    b.push(p2, "h2b", at(11, 5, 20))
    b.check(app, p2, "h2c", "completed", "success", at(11, 6, 1))

    # P3: 4 days, never reviewed.
    b.pr(app, 3, carol, at(10, 20), at(10, 24), additions=200, deletions=100)
    # P4 in lib: 1 day, Armature reviewed h4z first, Erin approved h4a after 2 hours.
    p4 = b.pr(lib, 1, dave, at(11, 8), at(11, 9), head="h4a", additions=25, deletions=5)
    b.review(p4, arm, "commented", "h4z", at(11, 8, 13), actor_source="armature")
    b.review(p4, erin, "approved", "h4a", at(11, 8, 14))
    b.check(lib, p4, "h4a", "completed", "timed_out", at(11, 8, 15))
    # P10 reverts P1 and P11 fixes P2, each merged in the window with no reviews.
    p10 = b.pr(app, 10, dave, at(11, 10), at(11, 10, 18), additions=600, deletions=0)
    b.label(metrics.CHANGE_KIND, p10, "platform-maintenance")
    p11 = b.pr(app, 11, erin, at(11, 11), at(11, 12))
    b.link(p10, p1, "revert", 1.0)
    b.link(p11, p2, "fix", 0.4)
    # Carol waits on reviews: two open more than 2 days, one newer, one draft.
    b.pr(app, 6, carol, at(11, 9), head="h6")
    b.pr(app, 7, carol, at(11, 10), head="h7")
    b.pr(app, 8, carol, at(11, 12), head="h8")
    b.pr(app, 13, carol, NOW - metrics.WAITING_TOO_LONG, head="h13")
    b.pr(app, 9, carol, at(11, 1), head="h9", draft=True)
    # The previous window: P5 in app with one round, P12 in lib with one round.
    p5 = b.pr(app, 5, alice, at(9, 20), at(9, 22), head="h5a", additions=1, deletions=1)
    b.review(p5, erin, "approved", "h5a", at(9, 21))
    p12 = b.pr(lib, 12, dave, at(9, 24), at(9, 25), head="h12")
    b.review(p12, erin, "approved", "h12", at(9, 24, 18))

    run_a = b.run("acme/app#1", "h1a", at(11, 1, 20))
    c1 = b.flag(run_a, "correctness", "app/src/Main.kt", at(11, 1, 20), "block", "concurrency")
    c2 = b.flag(run_a, "security", "app/src/Net.kt", at(11, 1, 20), None, "security-privacy")
    run_b = b.run("acme/app#1", "h1b", at(11, 2, 5))
    b.flag(run_b, "correctness", "app/src/Main.kt", at(11, 2, 5), "suggest", "concurrency")
    run_c = b.run("acme/app#2", "h2a", at(11, 5, 17))
    b.flag(run_c, "performance", "app/feature/List.kt", at(11, 5, 17), "drop", "performance")
    b.flag(run_c, "security", "app/src/Net.kt", at(11, 5, 17), None, "security-privacy", ordinal=1)
    run_d = b.run("acme/lib#1", "h4a", at(11, 8, 13))
    b.flag(run_d, "correctness", "lib/core/Parse.kt", at(11, 8, 13), "block", "correctness")
    run_e = b.run("acme/app#5", "h5a", at(9, 20, 13))
    b.flag(run_e, "correctness", "app/src/Main.kt", at(9, 20, 13), "block", "concurrency")

    b.spend(run_a, "review/correctness", "claude-opus", 1.0, at(11, 1, 20))
    b.spend(run_a, "review/security#1", "claude-sonnet", 0.5, at(11, 1, 20))
    b.spend(run_c, "first/performance", "claude-sonnet", 0.25, at(11, 5, 17))
    b.spend(run_c, "first/performance", "claude-sonnet", 9.0, at(11, 5, 17), cached=True)
    b.spend(run_e, "review/correctness", "claude-opus", 2.0, at(9, 20, 13))

    # Another team's rows, which must never show up.
    o = Builder(sqlite_engine, other)
    o_repo = o.repo("acme/app")
    mallory = o.person("Mallory")
    o_pr = o.pr(o_repo, 1, mallory, at(11, 1), at(11, 2), head="x1", additions=1, deletions=1)
    o.review(o_pr, o.person("Oscar"), "approved", "x1", at(11, 1, 13))
    o_run = o.run("acme/app#1", "x1", at(11, 1, 14))
    o.flag(o_run, "security", "app/src/Net.kt", at(11, 1, 14), None, "security-privacy")
    o.spend(o_run, "review/security", "claude-opus", 100.0, at(11, 1, 14))
    return SimpleNamespace(team=team[0], project=team[1], other=other[0], app=app, lib=lib, alice=alice, bob=bob,
                           carol=carol, dave=dave, erin=erin, bot=bot, arm=arm, p1=p1, p2=p2, p4=p4, p10=p10,
                           c1=c1, c2=c2, o_repo=o_repo, o_pr=o_pr, mallory=mallory, b=b)


def _team(engine, world, days=30, repo=None, since_days=120):
    with engine.connect() as conn:
        return metrics.Team(conn, world.team, NOW - timedelta(days=since_days), NOW, repo_id=repo)


W30 = metrics.window(NOW, 30)


def test_each_overview_measure_matches_its_definition(sqlite_engine, world):
    team = _team(sqlite_engine, world)
    assert metrics.merged_count(team, W30) == 6
    assert metrics.time_to_merge_days(team, W30) == pytest.approx(1.0)
    assert metrics.time_to_merge_days(team, W30, world.lib) == pytest.approx(1.0)
    assert metrics.time_to_first_review_hours(team, W30) == pytest.approx(6.0)
    # lib's only review before Erin's is Armature's, an hour in, which isn't a first human review.
    assert metrics.time_to_first_review_hours(team, W30, world.lib) == pytest.approx(2.0)
    assert {(pr.repo.full_name, pr.row.number): pr.rounds for pr in team.merged(W30)} == {
        ("acme/app", 1): 2, ("acme/app", 2): 3, ("acme/app", 3): 0, ("acme/lib", 1): 2, ("acme/app", 10): 0,
        ("acme/app", 11): 0}
    assert metrics.review_rounds(team, W30) == pytest.approx(7 / 6)
    assert metrics.review_rounds(team, W30, world.app) == pytest.approx(1.0)
    assert metrics.review_rounds(team, W30, world.lib) == pytest.approx(2.0)
    assert metrics.conflict_hours(team, W30) == pytest.approx(6.0)
    assert metrics.ci_failure_rate(team, W30, world.app) == pytest.approx(1 / 3)
    assert metrics.ci_failure_rate(team, W30) == pytest.approx(0.5)


def test_previous_window_values(sqlite_engine, world):
    team = _team(sqlite_engine, world)
    prev = W30.previous()
    assert (metrics.merged_count(team, prev), metrics.time_to_merge_days(team, prev)) == (2, pytest.approx(1.5))
    assert metrics.review_rounds(team, prev, world.app) == 1.0
    assert metrics.review_rounds(team, prev, world.lib) == 1.0
    assert metrics.conflict_hours(team, prev) == 0


def test_the_median_pull_requests_stages_and_push_kinds(sqlite_engine, world):
    team = _team(sqlite_engine, world, repo=world.app)
    stages = metrics.stages(team, W30, world.app)
    # app's merged times: P10 0.25, P2 1, P11 1, P1 2, P3 4 days. The median is the third, P11, with no reviews.
    assert stages["pr"]["number"] == 11
    assert (stages["waiting_first_review"], stages["in_review"], stages["approved_to_merged"]) == (24.0, 0.0, 0.0)
    # P1: bob's first review (not Alice's own or the bot's), his approval, two conflict spans, and the last check.
    assert metrics.pr_stages(team.prs[world.p1]) == {
        "waiting_first_review": 12.0, "in_review": 12.0, "blocked_by_conflicts": 6.0, "waiting_on_ci": 2.0,
        "approved_to_merged": 24.0}
    assert metrics.push_kinds(team, W30, world.app) == [
        {"kind": "fix_after_review", "count": 1, "share": 0.333}, {"kind": "rebase", "count": 1, "share": 0.333},
        {"kind": None, "count": 1, "share": 0.333}]


def test_hotspots_rank_by_the_sum_of_three_ranks(sqlite_engine, world):
    team = _team(sqlite_engine, world)
    rows = metrics.hotspots(team, W30)
    assert [(r["path"], r["findings"], r["churn"], r["conflict_prs"]) for r in rows] == [
        ("app/src/Main.kt", 3, 1, 0), ("app/src/Net.kt", 2, 2, 0), ("app/feature/List.kt", 1, 1, 0),
        ("lib/core/Parse.kt", 1, 1, 0)]
    assert [r["path"] for r in metrics.hotspots(team, W30, world.app)] == [
        "app/src/Main.kt", "app/src/Net.kt", "app/feature/List.kt"]


def test_people_measures_in_four_areas_with_team_medians(sqlite_engine, world):
    team = _team(sqlite_engine, world)
    rows, median, hot = metrics.people_rows(team, W30)
    assert hot == {(world.app, "app/src/Main.kt"), (world.app, "app/src/Net.kt"), (world.app, "app/feature/List.kt"),
                   (world.lib, "lib/core/Parse.kt")}
    by_name = {r["name"]: r for r in rows}
    assert sorted(by_name) == ["Alice", "Bob", "Carol", "Dave", "Erin"]
    assert {n: (r["shipping"]["merged"], r["shipping"]["size_weighted"]) for n, r in by_name.items()} == {
        "Alice": (1, 3), "Bob": (1, 1), "Carol": (1, 5), "Dave": (2, 11), "Erin": (1, 0)}
    assert by_name["Dave"]["shipping"]["by_type"] == {"unclassified": 1, "platform-maintenance": 1}
    assert {n: tuple(r["quality"].values()) for n, r in by_name.items()} == {
        "Alice": (2.0, 1.0, 1), "Bob": (3.0, 0.0, 0), "Carol": (0.0, None, 0), "Dave": (1.0, 0.5, 0),
        "Erin": (0.0, None, 0)}
    assert by_name["Bob"]["review"] == {"reviews_given": 2, "lines_reviewed": 50, "first_review_hours": 12.0,
                                        "substantive_comments": 1, "review_hours": 0.55}
    assert by_name["Alice"]["review"] == {"reviews_given": 2, "lines_reviewed": 8, "first_review_hours": 6.0,
                                          "substantive_comments": 0, "review_hours": 0.02}
    assert by_name["Erin"]["review"] == {"reviews_given": 1, "lines_reviewed": 30, "first_review_hours": 2.0,
                                         "substantive_comments": 0, "review_hours": 0.03}
    assert {n: tuple(r["unblocking"].values()) for n, r in by_name.items()} == {
        "Alice": (1, 0), "Bob": (1, 0), "Carol": (0, 0), "Dave": (1, 1), "Erin": (0, 0)}
    assert median["quality"] == {"rounds_per_pr": 1.0, "kept_per_round": 0.5, "bugs_traced": 0}
    assert median["review"]["reviews_given"] == 1 and median["shipping"]["merged"] == 1
    assert metrics.armature_reviews(team, W30) == 1


def test_findings_categories_specialists_and_dropped_flags_that_became_bugs(sqlite_engine, world):
    team = _team(sqlite_engine, world, since_days=150)
    view = metrics.findings_view(team, W30)
    assert view["categories"] == [
        {"category": "concurrency", "count": 2, "previous": 1},
        {"category": "security-privacy", "count": 2, "previous": 0},
        {"category": "correctness", "count": 1, "previous": 0},
        {"category": "performance", "count": 1, "previous": 0}]
    assert view["specialists"] == [
        {"specialist": "security", "raised": 2, "kept_share": 0.0, "dropped_share": 1.0, "dropped_then_bug": 2},
        {"specialist": "performance", "raised": 1, "kept_share": 0.0, "dropped_share": 1.0, "dropped_then_bug": 1},
        {"specialist": "correctness", "raised": 3, "kept_share": 1.0, "dropped_share": 0.0, "dropped_then_bug": 0}]
    assert [(i["flag"]["specialist"], i["pr"]["number"], i["bug_pr"]["number"], i["kind"])
            for i in view["dropped_became_bugs"]] == [
        ("security", 2, 11, "fix"), ("performance", 2, 11, "fix"), ("security", 1, 10, "revert")]
    weekly = metrics.findings_view(team, W30, category="concurrency")["weekly"]
    assert len(weekly["weeks"]) == 12 and sum(weekly["repos"][world.app]) == 3
    by_person = metrics.findings_view(team, W30, person_id=world.bob.id)
    assert by_person["categories"] == [{"category": "performance", "count": 1, "previous": 0},
                                       {"category": "security-privacy", "count": 1, "previous": 0}]
    assert metrics.findings_view(team, W30, repo_id=world.lib)["categories"] == [
        {"category": "correctness", "count": 1, "previous": 0}]


def test_model_spend_leaves_out_cached_calls_and_other_teams(sqlite_engine, world):
    with sqlite_engine.connect() as conn:
        view = metrics.models_view(conn, world.team, W30)
        only = metrics.models_view(conn, world.team, W30, "security")
    assert view["spend_usd"] == {"value": 1.75, "previous": 2.0}
    assert view["cost_per_reviewed_pr_usd"] == {"value": 0.875, "previous": 2.0}
    assert view["comparison_spend_usd"] == {"value": 0.0, "previous": 0.0}
    assert [(s["name"], s["spend_usd"]) for s in view["by_specialist"]] == [
        ("correctness", 1.0), ("security", 0.5), ("performance", 0.25)]
    assert [(s["name"], s["spend_usd"]) for s in view["by_model"]] == [("claude-opus", 1.0), ("claude-sonnet", 0.75)]
    assert (view["comparisons"], view["recommendations"]) == ([], [])
    assert only["spend_usd"]["value"] == 0.5


def test_attention_items_in_order(sqlite_engine, world):
    team = _team(sqlite_engine, world)
    items = metrics.attention(team, W30)
    assert [i["kind"] for i in items] == ["dropped_flag_bug", "dropped_flag_bug", "rounds_rising",
                                          "waiting_on_review", "specialist_dropped_bugs"]
    assert (items[0]["pr"]["number"], items[0]["bug_pr"]["number"], items[0]["flag"]["specialist"]) == (2, 11,
                                                                                                         "performance")
    assert (items[1]["pr"]["number"], items[1]["link"], items[1]["flag"]["category"]) == (1, "revert",
                                                                                          "security-privacy")
    assert (items[2]["repo"], items[2]["value"], items[2]["previous"], items[2]["top_category"]) == (
        "acme/lib", 2.0, 1.0, "correctness")
    assert (items[3]["person"], [p["number"] for p in items[3]["prs"]]) == ("Carol", [6, 7])
    assert items[4] == {"kind": "specialist_dropped_bugs", "specialist": "security", "count": 2}


def test_attention_returns_at_most_six(sqlite_engine):
    team_ids = add_team(sqlite_engine, lambda: NOW, "busy")
    b = Builder(sqlite_engine, team_ids)
    repo = b.repo("acme/busy")
    for i in range(7):
        person = b.person(f"Waiter {i}")
        b.pr(repo, 100 + 2 * i, person, at(11, 1))
        b.pr(repo, 101 + 2 * i, person, at(11, 2))
    with sqlite_engine.connect() as conn:
        items = metrics.attention(metrics.Team(conn, team_ids[0], NOW - timedelta(days=90), NOW), W30)
    assert len(items) == 6 and {i["kind"] for i in items} == {"waiting_on_review"}


def _files(engine, world, pr, *paths):
    table = metrics.file_table()
    with engine.begin() as conn:
        conn.execute(insert(table), [{"id": new_id(), "team_id": world.team, "repo_row_id": world.app,
                                      "pr_row_id": pr, "path": path,
                                      "path_key": hashlib.sha256(path.encode()).hexdigest(), "created_at": NOW}
                                     for path in paths])


@pytest.mark.skipif(metrics.file_table() is None, reason="this build stores no pull request file lists")
def test_a_conflict_hotspot_needs_a_file_list(sqlite_engine, world):
    _files(sqlite_engine, world, world.p1, "app/build.gradle.kts", "app/src/Main.kt")
    _files(sqlite_engine, world, world.p2, "app/src/Main.kt")
    team = _team(sqlite_engine, world)
    assert team.prs[world.p2].paths() == {"app/src/Main.kt"} and team.prs[world.p4].files is None
    rows = {r["path"]: r for r in metrics.hotspots(team, W30, limit=100)}
    assert (rows["app/build.gradle.kts"]["conflict_prs"], rows["app/build.gradle.kts"]["conflict_hours"]) == (1, 6.0)
    items = [i for i in metrics.attention(team, W30) if i["kind"] == "conflict_file"]
    assert items == []  # Two files share P1's 6 hours, so neither is double the next.
    team.prs[world.p1].files = {"app/build.gradle.kts"}
    [item] = [i for i in metrics.attention(team, W30) if i["kind"] == "conflict_file"]
    assert (item["path"], item["hours"], item["next_hours"]) == ("app/build.gradle.kts", 6.0, 0.0)


# The API


@pytest.fixture
def users(sqlite_engine, keyring, clock, world):
    found = {}
    for name, team, role in (("lead", world.team, "team_lead"), ("member", world.team, "contributor"),
                             ("outsider", world.other, "team_lead")):
        found[name] = add_user(sqlite_engine, keyring, clock, name)[0]
        add_membership(sqlite_engine, clock, found[name], "team", team, role)
    with sqlite_engine.begin() as conn:
        conn.execute(people.update().where(people.c.id == world.bob.id).values(user_id=found["member"]))
    return found


def _client(engine, keyring, clock, user_id, step_up=True):
    client = make_client(engine, keyring, clock)
    headers = mutation(sign_in_as(client, engine, keyring, clock, user_id, step_up=step_up))
    return SimpleNamespace(client=client, headers=headers)


@pytest.fixture
def lead(sqlite_engine, keyring, clock, users):
    return _client(sqlite_engine, keyring, clock, users["lead"])


@pytest.fixture
def member(sqlite_engine, keyring, clock, users):
    return _client(sqlite_engine, keyring, clock, users["member"])


def _get(who, world, route, /, **params):
    return who.client.get(f"/api/teams/{world.team}/insights{route}", params=params)


def test_the_overview_route_answers_with_metrics_attention_and_people_for_leads(lead, member, world):
    response = _get(lead, world, "/overview", days="30")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"]["days"] == 30 and len(body["dates"]) == 30
    assert [(m["id"], m["value"], m["previous"], m["unit"]) for m in body["metrics"]] == [
        ("merged", 6, 2, "count"), ("time_to_merge", 1.0, 1.5, "days"), ("review_rounds", 1.17, 1.0, "rounds"),
        ("conflict_hours", 6.0, 0, "hours")]
    merged = body["metrics"][0]["series"]
    assert set(merged) == {world.app, world.lib} and len(merged[world.app]) == 30
    # Each point counts the 7 days before it: on Nov 13, P2 (Nov 6 at noon), P10, and P11 in app.
    assert merged[world.app][-1] == 3 and merged[world.lib][-1] == 1
    assert len(body["attention"]) == 5
    assert {p["name"] for p in body["people"]} == {"Alice", "Bob", "Carol", "Dave", "Erin"}
    alice = next(p for p in body["people"] if p["name"] == "Alice")
    assert (alice["merged"], alice["rounds"], alice["reviews"], alice["first_review_hours"]) == (1, 2.0, 2, 6.0)
    assert "people" not in _get(member, world, "/overview").json()
    one = _get(lead, world, "/overview", repo=world.lib).json()
    assert set(one["metrics"][0]["series"]) == {world.lib} and one["metrics"][0]["value"] == 1


def test_people_routes_are_for_leads_and_each_person_sees_their_own_page(lead, member, world):
    assert _get(member, world, "/people").status_code == 403
    page = _get(lead, world, "/people", view="review").json()
    assert page["view"] == "review" and page["armature_reviews"] == 1
    assert {r["name"] for r in page["rows"]} == {"Alice", "Bob", "Carol", "Dave", "Erin"}
    assert page["team_median"]["reviews_given"] == 1
    own = _get(member, world, f"/people/{world.bob.id}")
    assert own.status_code == 200, own.text
    assert own.json()["person"]["name"] == "Bob" and own.json()["quality"]["rounds_per_pr"] == 3.0
    trend = own.json()["trend"]
    assert len(trend["weeks"]) == 13 and trend["person"]["shipping"][-2:] == [0, 1]
    assert _get(member, world, f"/people/{world.alice.id}").status_code == 403
    assert _get(member, world, f"/people/{new_id()}").status_code == 403
    assert _get(lead, world, f"/people/{world.alice.id}").status_code == 200
    assert _get(lead, world, f"/people/{world.bot.id}").status_code == 404
    assert _get(lead, world, f"/people/{world.arm.id}").status_code == 404
    assert _get(member, world, "/findings", person=world.alice.id).status_code == 403
    assert _get(lead, world, "/findings", person=world.alice.id).status_code == 200


def test_another_teams_rows_never_appear(lead, world, sqlite_engine, keyring, clock, users):
    outsider = _client(sqlite_engine, keyring, clock, users["outsider"])
    assert _get(outsider, world, "/overview").status_code in (403, 404)
    text = json.dumps([_get(lead, world, path).json() for path in
                       ("/overview", "/people", "/findings", "/models", f"/repos/{world.app}", "/repos")])
    assert "Mallory" not in text and "Oscar" not in text and world.o_repo not in text and world.o_pr not in text
    assert _get(lead, world, "/models").json()["spend_usd"]["value"] == 1.75
    assert _get(lead, world, f"/prs/{world.o_pr}").status_code == 404
    assert _get(lead, world, f"/repos/{world.o_repo}").status_code == 404
    assert _get(lead, world, "/overview", repo=world.o_repo).status_code == 400
    assert _get(lead, world, f"/people/{world.mallory.id}").status_code == 404


def test_input_validation(lead, world):
    for params in ({"days": "14"}, {"days": "abc"}, {"days": "-7"}):
        response = _get(lead, world, "/overview", **params)
        assert response.status_code == 400 and response.json()["error"] == "days must be 7, 30, or 90."
    assert _get(lead, world, "/overview", days="7").status_code == 200
    assert _get(lead, world, "/people", view="score").status_code == 400
    assert _get(lead, world, "/findings", category="nope").status_code == 400
    assert _get(lead, world, "/findings", person=new_id()).status_code == 404
    assert _get(lead, world, "/fixes", status="lost").status_code == 400
    assert _get(lead, world, "/fixes", status="waiting").json() == {"waiting": [], "in_progress": [], "done": [],
                                                                    "declined": []}
    assert _get(lead, world, "/models", days="90").status_code == 200


def test_the_repo_routes(lead, world):
    assert [r["full_name"] for r in _get(lead, world, "/repos").json()["repos"]] == ["acme/app", "acme/lib"]
    body = _get(lead, world, f"/repos/{world.app}").json()
    assert body["repo"]["name"] == "app" and body["time_to_merge"] == {"unit": "days", "value": 1.0,
                                                                         "previous": 2.0}
    tiles = {t["id"]: (t["value"], t["previous"]) for t in body["tiles"]}
    assert tiles == {"time_to_first_review": (9.0, 24.0), "review_rounds": (1.0, 1.0),
                     "conflict_hours": (6.0, 0), "ci_failure_rate": (0.333, None)}
    assert [h["path"] for h in body["hotspots"]] == ["app/src/Main.kt", "app/src/Net.kt", "app/feature/List.kt"]
    assert body["stages"]["pr"]["number"] == 11


def test_the_pull_request_timeline(lead, world):
    body = _get(lead, world, f"/prs/{world.p1}").json()
    assert body["pr"]["number"] == 1 and body["pr"]["rounds"] == 2 and body["pr"]["change_type"] == "enhancement"
    assert [(p["after_sha"], p["kind"]) for p in body["pushes"]] == [("h1a", "rebase"), ("h1b", "fix_after_review")]
    assert [c["hours"] for c in body["conflicts"]] == [6.0]
    heads = {r["head_sha"]: r for r in body["rounds"]}
    assert {f["specialist"]: f["status"] for f in heads["h1a"]["flags"]} == {"correctness": "kept",
                                                                             "security": "dropped"}
    assert heads["h1a"]["flags"][0]["url"] == "https://github.com/acme/app/blob/h1a/app/src/Main.kt#L7"
    assert [f["category"] for f in heads["h1b"]["flags"]] == ["concurrency"]
    assert len(heads["h1a"]["comments"]) == 3
    assert body["merge"]["at"] == at(11, 3).isoformat()
    assert [(link["kind"], link["direction"], link["pr"]["number"]) for link in body["links"]] == [
        ("revert", "fixed_by", 10)]


# People corrections


def _person_rows(engine, person_id):
    with engine.connect() as conn:
        return {f"{c.table.name}.{c.name}": len(conn.execute(select(c.table.c.id).where(c == person_id)).all())
                for c in people_admin.person_columns()}


def test_every_column_that_points_at_a_person_is_known():
    assert {f"{c.table.name}.{c.name}" for c in people_admin.person_columns()} == {
        "gh_pull_requests.author_person_id", "gh_pushes.pusher_person_id", "gh_reviews.reviewer_person_id",
        "gh_review_comments.author_person_id"}


def test_merging_moves_identities_and_rows_and_is_audited(lead, member, world, sqlite_engine):
    world.b.push(world.p4, "h4e", at(11, 8, 15), pusher=world.erin)
    world.b.comment(world.p4, world.erin, "lib/core/Parse.kt", at(11, 8, 14))
    path = f"/api/teams/{world.team}/insights/people/{world.erin.id}/merge"
    assert member.client.post(path, json={"into": world.dave.id}, headers=member.headers).status_code == 403
    assert lead.client.post(path, json={"into": world.erin.id}, headers=lead.headers).status_code == 400
    assert lead.client.post(path, json={"into": world.arm.id}, headers=lead.headers).status_code == 409
    before = _person_rows(sqlite_engine, world.mallory.id)
    assert lead.client.post(path, json={"into": world.mallory.id}, headers=lead.headers).status_code == 404
    assert _person_rows(sqlite_engine, world.erin.id) == {
        "gh_pull_requests.author_person_id": 1, "gh_pushes.pusher_person_id": 1, "gh_reviews.reviewer_person_id": 3,
        "gh_review_comments.author_person_id": 1}
    assert _person_rows(sqlite_engine, world.mallory.id) == before
    response = lead.client.post(path, json={"into": world.dave.id}, headers=lead.headers)
    assert response.status_code == 200, response.text
    assert response.json()["person"]["id"] == world.dave.id
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(people).where(people.c.id == world.erin.id)).first() is None
        assert {r.person_id for r in conn.execute(select(person_identities).where(
            person_identities.c.value.in_([str(world.erin.gh), str(world.dave.gh)])))} == {world.dave.id}
        assert {r.reviewer_person_id for r in conn.execute(select(gh_reviews).where(
            gh_reviews.c.reviewer_gh_id == world.erin.gh))} == {world.dave.id}
        assert conn.execute(select(gh_pull_requests.c.author_person_id).where(
            gh_pull_requests.c.number == 11, gh_pull_requests.c.team_id == world.team)).scalar() == world.dave.id
        logged = conn.execute(select(audit).where(audit.c.action == "insights.person.merge")).one()
    assert (logged.target_id, logged.detail["into"], logged.detail["identities"]) == (world.erin.id, world.dave.id, 1)
    assert logged.detail["rows"] == {
        "gh_pull_requests.author_person_id": 1, "gh_pushes.pusher_person_id": 1, "gh_reviews.reviewer_person_id": 3,
        "gh_review_comments.author_person_id": 1}
    assert set(_person_rows(sqlite_engine, world.erin.id).values()) == {0}


def test_deleting_a_person_needs_a_step_up_and_clears_their_rows(lead, member, world, sqlite_engine, keyring, clock,
                                                                users):
    path = f"/api/teams/{world.team}/insights/people/{world.bob.id}/delete"
    assert member.client.post(path, headers=member.headers).status_code == 403
    outside = f"/api/teams/{world.team}/insights/people/{world.mallory.id}/delete"
    assert lead.client.post(outside, headers=lead.headers).status_code == 404
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(people).where(people.c.id == world.mallory.id)).first() is not None
    stale = _client(sqlite_engine, keyring, clock, users["lead"], step_up=False)
    refused = stale.client.post(path, headers=stale.headers)
    assert refused.status_code == 403 and refused.json()["error"] == "step_up_required"
    assert lead.client.post(f"/api/teams/{world.team}/insights/people/{world.arm.id}/delete",
                            headers=lead.headers).status_code == 409
    assert lead.client.post(path, headers=lead.headers).status_code == 200
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(people).where(people.c.id == world.bob.id)).first() is None
        assert conn.execute(select(person_identities).where(person_identities.c.person_id == world.bob.id)).all() == []
        assert {r.reviewer_person_id for r in conn.execute(select(gh_reviews).where(
            gh_reviews.c.reviewer_gh_id == world.bob.gh))} == {None}
        logged = conn.execute(select(audit).where(audit.c.action == "insights.person.delete")).one()
    assert logged.target_id == world.bob.id and logged.detail["identities"] == 1
    assert lead.client.post(path, headers=lead.headers).status_code == 404


# Jobs


class FakeModel:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        items = json.loads(kwargs["messages"][-1]["content"])["items"]
        content = {"labels": [{"id": i["id"], "category": "testing", "confidence": 0.8} for i in items]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                               _hidden_params={"response_cost": 0.01})


@pytest.fixture
def labeled_team(sqlite_engine):
    team = add_team(sqlite_engine, lambda: NOW, "jobs")
    with sqlite_engine.begin() as conn:
        conn.execute(insert(providers).values(id=new_id(), team_id=team[0], kind="anthropic", config={},
                                              aliases={"fast": "claude-haiku-5-5"}, created_at=NOW, updated_at=NOW))
        conn.execute(insert(settings).values(team_id=team[0], key=jobs.CAP_SETTING, value=3))
    b = Builder(sqlite_engine, team)
    run_id = b.run("acme/app#1", "h1", NOW - timedelta(days=1))
    for i in range(5):
        b.flag(run_id, "correctness", f"app/src/F{i}.kt", NOW - timedelta(days=1), ordinal=i)
    return team[0]


def _count(engine, table, **where):
    with engine.connect() as conn:
        return len(conn.execute(select(table).where(*[table.c[k] == v for k, v in where.items()])).all())


def test_the_labeling_job_claims_one_slot_a_day_and_stops_at_the_cap(sqlite_engine, keyring, labeled_team,
                                                                    monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(llm, "litellm_completion", model)
    clock = Clock(NOW)
    service = jobs.Jobs(sqlite_engine, keyring, clock=clock)
    other = jobs.Jobs(sqlite_engine, keyring, clock=clock)
    service.tick()
    other.tick()
    assert _count(sqlite_engine, labels, team_id=labeled_team) == 3
    assert _count(sqlite_engine, audit, action="insights.label", team_id=labeled_team) == 1
    with sqlite_engine.connect() as conn:
        slot = conn.execute(select(settings.c.value).where(settings.c.scope == labeled_team,
                                                           settings.c.key == jobs.LABEL_SLOT)).scalar()
    assert slot == {"next_run_at": datetime(2026, 11, 14, tzinfo=UTC).isoformat()}
    clock.advance(hours=11, minutes=59)
    service.tick()
    assert _count(sqlite_engine, labels, team_id=labeled_team) == 3
    clock.advance(minutes=1)
    service.tick()
    assert _count(sqlite_engine, labels, team_id=labeled_team) == 5
    assert len(model.calls) == 2


def test_a_claim_is_a_compare_and_set(sqlite_engine, labeled_team):
    assert jobs.claim(sqlite_engine, labeled_team, "insights.jobs.test", NOW, NOW + timedelta(days=1))
    assert not jobs.claim(sqlite_engine, labeled_team, "insights.jobs.test", NOW, NOW + timedelta(days=1))
    later = NOW + timedelta(days=1)
    assert jobs.claim(sqlite_engine, labeled_team, "insights.jobs.test", later, later + timedelta(days=1))
    assert not jobs.claim(sqlite_engine, labeled_team, "insights.jobs.test", later, later + timedelta(days=1))


def test_clustering_keeps_groups_of_three_or_more(sqlite_engine, labeled_team):
    team = (labeled_team, None)
    b = Builder(sqlite_engine, team)
    b.project = None
    run_id = b.run("acme/android-tree#4", "h4", NOW - timedelta(days=3))
    old = b.run("acme/android-tree#2", "h2", NOW - timedelta(days=40))
    for i, path in enumerate(("app/feature/A.kt", "app/feature/x/B.kt", "app/feature/C.kt")):
        b.flag(run_id, "concurrency", path, NOW - timedelta(days=3 - i), category="concurrency", ordinal=i)
    for i, path in enumerate(("lib/core/A.kt", "lib/core/B.kt")):
        b.flag(run_id, "simplicity", path, NOW - timedelta(days=2), category="simplicity", ordinal=10 + i)
    b.flag(old, "concurrency", "app/feature/Old.kt", NOW - timedelta(days=40), category="concurrency")
    assert jobs.cluster(sqlite_engine, labeled_team, NOW) == {"clusters": 1, "removed": 0, "labeled": 5}
    with sqlite_engine.connect() as conn:
        [row] = conn.execute(select(clusters).where(clusters.c.team_id == labeled_team)).all()
        members = conn.execute(select(cluster_members).where(cluster_members.c.cluster_id == row.id)).all()
    assert (row.label, row.scope, row.size) == ("concurrency", {"repo": "acme/android-tree", "area": "app/feature"}, 3)
    assert row.summary == "Concurrency in android-tree app/feature, flagged 3 times in 4 weeks"
    assert len(members) == 3 and {m.team_id for m in members} == {labeled_team}
    b.flag(run_id, "concurrency", "app/feature/D.kt", NOW - timedelta(days=1), category="concurrency", ordinal=20)
    assert jobs.cluster(sqlite_engine, labeled_team, NOW)["clusters"] == 1
    with sqlite_engine.connect() as conn:
        [again] = conn.execute(select(clusters).where(clusters.c.team_id == labeled_team)).all()
    assert (again.id, again.size) == (row.id, 4)
    assert _count(sqlite_engine, cluster_members, cluster_id=row.id) == 4
    # Later, only the two newest flags are within 28 days, so the cluster and its members go.
    shrunk = NOW + jobs.CLUSTER_SPAN - timedelta(days=2) + timedelta(hours=1)
    assert jobs.cluster(sqlite_engine, labeled_team, shrunk) == {"clusters": 0, "removed": 1, "labeled": 2}
    assert _count(sqlite_engine, clusters, team_id=labeled_team) == 0
    assert _count(sqlite_engine, cluster_members, team_id=labeled_team) == 0
    assert jobs.cluster(sqlite_engine, labeled_team, NOW)["clusters"] == 1
    assert jobs.cluster(sqlite_engine, labeled_team, NOW + timedelta(days=60)) == {"clusters": 0, "removed": 1,
                                                                                    "labeled": 0}
    assert _count(sqlite_engine, clusters, team_id=labeled_team) == 0


@pytest.fixture(autouse=True)
def fresh_answers():
    routes_insights.answers.clear()
    yield
    routes_insights.answers.clear()


# Review round 1


def _sample(hour, state, day=1):
    return SimpleNamespace(sampled_at=at(11, day, hour), mergeable=state)


def test_conflict_spans_come_from_samples_stored_on_change():
    # Unknown is GitHub recomputing, so it doesn't end a conflict. Only a mergeable sample does.
    assert metrics.intervals([_sample(13, "conflicting"), _sample(16, "unknown"), _sample(19, "mergeable")],
                             at(11, 2)) == [(at(11, 1, 13), at(11, 1, 19))]
    assert metrics.intervals([_sample(13, "conflicting"), _sample(16, "unknown"), _sample(17, "conflicting"),
                              _sample(19, "mergeable"), _sample(20, "unknown"), _sample(21, "conflicting"),
                              _sample(22, "mergeable")], at(11, 2)) == [(at(11, 1, 13), at(11, 1, 19)),
                                                                        (at(11, 1, 21), at(11, 1, 22))]
    assert metrics.intervals([_sample(13, "unknown"), _sample(14, "mergeable")], at(11, 2)) == []
    # The last sample lasts until the close, or now while open, and a repeat sample doesn't split a span.
    assert metrics.intervals([_sample(13, "mergeable"), _sample(14, "conflicting")], at(11, 1, 20)) == [
        (at(11, 1, 14), at(11, 1, 20))]
    assert metrics.intervals([_sample(13, "conflicting"), _sample(14, "conflicting"), _sample(15, "mergeable")],
                             at(11, 2)) == [(at(11, 1, 13), at(11, 1, 15))]
    assert metrics.intervals([_sample(13, "conflicting"), _sample(18, "mergeable")], at(11, 1, 15)) == [
        (at(11, 1, 13), at(11, 1, 15))]


def test_an_open_conflict_runs_until_now_and_a_closed_one_until_its_close(sqlite_engine):
    ids = add_team(sqlite_engine, lambda: NOW, "spans")
    b = Builder(sqlite_engine, ids)
    repo, who = b.repo("acme/spans"), b.person("Pat")
    still_open = b.pr(repo, 1, who, at(11, 1))
    b.sample(still_open, NOW - timedelta(hours=10), "conflicting")
    closed = b.pr(repo, 2, who, at(11, 1), state="closed", closed=at(11, 5, 16))
    b.sample(closed, at(11, 5, 12), "conflicting")
    with sqlite_engine.connect() as conn:
        team = metrics.Team(conn, ids[0], NOW - timedelta(days=60), NOW)
    assert team.prs[still_open].conflict_hours(W30) == pytest.approx(10.0)
    assert team.prs[closed].conflict_hours(W30) == pytest.approx(4.0)
    assert metrics.conflict_hours(team, metrics.Window(W30.start, NOW - timedelta(hours=4))) == pytest.approx(10.0)


def test_github_links_use_the_connections_web_address(sqlite_engine):
    assert metrics.web_base("https://api.github.com") == "https://github.com"
    assert metrics.web_base("https://ghe.acme.test/api/v3/") == "https://ghe.acme.test"
    assert metrics.web_base(None) == metrics.web_base("not a url") == "https://github.com"
    ids = add_team(sqlite_engine, lambda: NOW, "enterprise")
    connection = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(connections).values(id=connection, team_id=ids[0], kind="github_app", name="ghe",
                                                config={"api_base": "https://ghe.acme.test/api/v3"},
                                                state="ok", created_at=NOW, updated_at=NOW))
    b = Builder(sqlite_engine, ids)
    repo = b.repo("acme/app")
    pr = b.pr(repo, 7, b.person("Pat"), at(11, 1), at(11, 2), head="abc")
    with sqlite_engine.connect() as conn:
        found = metrics.pr_detail(conn, ids[0], pr, NOW)
    assert found["pr"]["url"] == "https://ghe.acme.test/acme/app/pull/7"


def test_job_slots_never_show_in_the_settings_list(sqlite_engine, keyring, clock, labeled_team):
    assert jobs.claim(sqlite_engine, labeled_team, jobs.LABEL_SLOT, NOW, NOW + timedelta(days=1))
    admin = _client(sqlite_engine, keyring, clock, add_user(sqlite_engine, keyring, clock, "root", is_admin=True)[0])
    listed = admin.client.get("/api/settings").json()["settings"]
    assert jobs.CAP_SETTING in {s["key"] for s in listed}
    assert not {s["key"] for s in listed} & set(jobs.SLOTS)


def test_a_claim_loses_to_a_newer_stamp_and_keeps_the_winners_next_run(sqlite_engine, labeled_team):
    slot = "_insights.jobs.test"
    assert jobs.claim(sqlite_engine, labeled_team, slot, NOW, NOW + timedelta(days=1))
    with sqlite_engine.connect() as conn:
        stale = conn.execute(select(settings.c.updated_at).where(settings.c.scope == labeled_team,
                                                                 settings.c.key == slot)).scalar()
    later = NOW + timedelta(days=1)
    assert jobs.claim(sqlite_engine, labeled_team, slot, later, later + timedelta(days=1))
    with sqlite_engine.begin() as conn:
        assert not jobs.take(conn, labeled_team, slot, stale, later + timedelta(days=7), later)
    with sqlite_engine.connect() as conn:
        kept = conn.execute(select(settings.c.value).where(settings.c.scope == labeled_team,
                                                           settings.c.key == slot)).scalar()
    assert kept == {"next_run_at": (later + timedelta(days=1)).isoformat()}


def _rounds_team(engine, name, now_rounds):
    ids = add_team(engine, lambda: NOW, name)
    b = Builder(engine, ids)
    repo, author, reviewer = b.repo(f"acme/{name}"), b.person("Author"), b.person("Reviewer")
    for i, (when, count) in enumerate([(at(9, 20), 4), *((at(11, 1), n) for n in now_rounds)]):
        pr = b.pr(repo, i + 1, author, when, when + timedelta(days=1))
        for head in range(count):
            b.review(pr, reviewer, "commented", f"h{i}-{head}", when + timedelta(hours=head + 1))
    with engine.connect() as conn:
        return metrics.Team(conn, ids[0], NOW - timedelta(days=90), NOW)


def test_review_rounds_must_rise_more_than_a_quarter(sqlite_engine):
    exactly = _rounds_team(sqlite_engine, "even", [5])
    assert metrics.review_rounds(exactly, W30) == 5.0 and metrics.review_rounds(exactly, W30.previous()) == 4.0
    assert [i for i in metrics.attention(exactly, W30) if i["kind"] == "rounds_rising"] == []
    above = _rounds_team(sqlite_engine, "above", [5, 6])
    [item] = [i for i in metrics.attention(above, W30) if i["kind"] == "rounds_rising"]
    assert (item["value"], item["previous"]) == (5.5, 4.0)


@pytest.mark.skipif(metrics.file_table() is None, reason="this build stores no pull request file lists")
@pytest.mark.parametrize(("minutes", "listed"), [(120, False), (126, True)])
def test_a_conflict_file_must_lead_by_more_than_double(sqlite_engine, minutes, listed):
    ids = add_team(sqlite_engine, lambda: NOW, f"files{minutes}")
    b = Builder(sqlite_engine, ids)
    repo, who = b.repo("acme/files"), b.person("Pat")
    world = SimpleNamespace(team=ids[0], app=repo)
    for number, path, length in ((1, "A.kt", minutes), (2, "B.kt", 60)):
        pr = b.pr(repo, number, who, at(11, 1), at(11, 3))
        b.sample(pr, at(11, 1, 13), "conflicting")
        b.sample(pr, at(11, 1, 13) + timedelta(minutes=length), "mergeable")
        _files(sqlite_engine, world, pr, path)
    with sqlite_engine.connect() as conn:
        team = metrics.Team(conn, ids[0], NOW - timedelta(days=90), NOW)
    found = [i["path"] for i in metrics.attention(team, W30) if i["kind"] == "conflict_file"]
    assert found == (["A.kt"] if listed else [])


@pytest.mark.skipif(metrics.file_table() is None, reason="this build stores no pull request file lists")
def test_a_dropped_flag_turns_into_a_bug_only_through_a_later_fix_of_its_file(sqlite_engine, world):
    early = world.b.pr(world.app, 20, world.dave, at(10, 30), at(11, 12))
    world.b.link(early, world.p1, "fix", 1.0)
    team = _team(sqlite_engine, world)
    c2 = next(c for c in team.candidates if c.row.id == world.c2)
    assert [link.from_pr_row_id for link in metrics._became_bug(team, c2)] == [world.p10]
    _files(sqlite_engine, world, world.p10, "app/src/Other.kt")
    team = _team(sqlite_engine, world)
    c2 = next(c for c in team.candidates if c.row.id == world.c2)
    assert metrics._became_bug(team, c2) == []
    _files(sqlite_engine, world, world.p10, "app/src/Net.kt")
    team = _team(sqlite_engine, world)
    c2 = next(c for c in team.candidates if c.row.id == world.c2)
    assert [link.from_pr_row_id for link in metrics._became_bug(team, c2)] == [world.p10]


def test_bugs_traced_and_the_review_hour_cap(sqlite_engine, world):
    for number, confidence, evidence in ((21, 0.6, None), (22, 0.59, {"confirmed": True}), (23, 0.59, None)):
        fixer = world.b.pr(world.app, number, world.dave, at(11, 11), at(11, 12))
        world.b.link(fixer, world.p1, "fix", confidence, evidence)
    long_review = world.b.review(world.p4, world.carol, "commented", "h4a", at(11, 8, 20))
    world.b.comment(world.p4, world.carol, "lib/core/Parse.kt", at(11, 8, 14), review_id=long_review)
    team = _team(sqlite_engine, world)
    # The revert at 1.0 and the fixes at 0.6 and at 0.59 confirmed. Not the one at 0.59 alone.
    assert metrics.person_measures(team, W30, world.alice.id, set())["quality"]["bugs_traced"] == 3
    assert metrics.person_measures(team, W30, world.carol.id, set())["review"]["review_hours"] == 3.0


def test_identical_requests_share_one_answer_until_people_change(lead, world, monkeypatch):
    calls = []
    real = metrics.overview
    monkeypatch.setattr(metrics, "overview", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = _get(lead, world, "/overview").json()
    assert _get(lead, world, "/overview").json() == first and len(calls) == 1
    assert _get(lead, world, "/overview", days="7").status_code == 200 and len(calls) == 2
    path = f"/api/teams/{world.team}/insights/people/{world.erin.id}/merge"
    assert lead.client.post(path, json={"into": world.dave.id}, headers=lead.headers).status_code == 200
    _get(lead, world, "/overview")
    assert len(calls) == 3


def test_the_answer_cache_is_bounded_and_expires():
    now = [0.0]
    answers = routes_insights.Answers(ttl=10, size=2, clock=lambda: now[0])
    made = []
    for key in ("a", "b", "c"):
        answers.get(("t", key), lambda key=key: made.append(key) or key)
    assert answers.get(("t", "a"), lambda: made.append("a") or "a") == "a" and made == ["a", "b", "c", "a"]
    assert answers.get(("t", "c"), lambda: made.append("c")) == "c" and len(made) == 4
    now[0] = 11
    answers.get(("t", "c"), lambda: made.append("c") or "c")
    assert made[-1] == "c" and len(made) == 5


# Timing


def _busy_team(engine, prs=500):
    """About a month at 500 pull requests a month, spread over 90 days, with reviews, comments, checks, conflicts,
    and five flags each."""
    ids = add_team(engine, lambda: NOW, "busy-team")
    b = Builder(engine, ids)
    repos = [b.repo("acme/one"), b.repo("acme/two")]
    folks = [b.person(f"Dev {i}") for i in range(20)]
    rows = defaultdict(list)
    span = timedelta(days=88) / prs
    for i in range(prs):
        pr_id, repo, author = new_id(), repos[i % 2], folks[i % 20]
        created = NOW - timedelta(days=89) + i * span
        merged = None if i % 10 == 0 else created + timedelta(hours=6 * (i % 5 + 1))
        head = f"h{i}"
        rows[gh_pull_requests].append({
            "id": pr_id, "team_id": ids[0], "repo_row_id": repo, "gh_pr_id": next(GH_IDS), "number": i + 1, "title": f"PR {i}",
            "author_person_id": author.id, "author_gh_id": author.gh, "author_login": author.login,
            "state": "merged" if merged else "open", "draft": False, "head_sha": head, "additions": i % 300, "deletions": i % 40,
            "created_at_gh": created, "merged_at_gh": merged, "closed_at_gh": merged, "gh_updated_at": merged or created,
            "created_at": NOW, "updated_at": NOW})
        for n, (state, sha) in enumerate((("commented", f"{head}a"), ("approved", head))):
            who = folks[(i + n + 1) % 20]
            review_id = next(GH_IDS)
            rows[gh_reviews].append({
                "id": new_id(), "team_id": ids[0], "pr_row_id": pr_id, "gh_review_id": review_id, "reviewer_person_id": who.id,
                "reviewer_gh_id": who.gh, "reviewer_login": who.login, "actor_source": "github", "state": state, "head_sha": sha,
                "submitted_at": created + timedelta(hours=2 * n + 2), "created_at": NOW, "updated_at": NOW})
            rows[gh_review_comments].append({
                "id": new_id(), "team_id": ids[0], "pr_row_id": pr_id, "gh_comment_id": next(GH_IDS), "gh_review_id": review_id,
                "author_person_id": who.id, "author_gh_id": who.gh, "author_login": who.login,
                "path": f"src/m{i % 40}/F{i % 7}.kt", "line": 3, "head_sha": sha, "created_at_gh": created + timedelta(hours=1),
                "updated_at_gh": created, "created_at": NOW})
            rows[gh_check_runs].append({
                "id": new_id(), "team_id": ids[0], "pr_row_id": pr_id, "gh_check_run_id": next(GH_IDS), "repo_row_id": repo,
                "name": f"ci{n}", "head_sha": head, "status": "completed", "conclusion": "failure" if i % 7 == 0 else "success",
                "completed_at": created + timedelta(hours=5), "created_at": NOW})
        if i % 5 == 0:
            for hours, state in ((1, "conflicting"), (3, "mergeable")):
                rows[gh_conflict_samples].append({"id": new_id(), "team_id": ids[0], "pr_row_id": pr_id,
                                                      "sampled_at": created + timedelta(hours=hours), "mergeable": state})
        run_id = new_id()
        rows[runs].append({"id": run_id, "team_id": ids[0], "project_id": ids[1], "status": "succeeded", "started_at": created,
                               "head_sha": head})
        rows[tool_calls].append({"id": new_id(), "team_id": ids[0], "run_id": run_id, "project_id": ids[1],
                                     "step_id": "decisions", "tool": "review.decisions", "mode": "live", "outcome": "ok",
                                     "target": f"acme/{'one' if i % 2 == 0 else 'two'}#{i + 1}", "created_at": created})
        for k in range(5):
            candidate = new_id()
            rows[finding_candidates].append({
                "id": candidate, "team_id": ids[0], "project_id": ids[1], "run_id": run_id, "step_key": f"review/s{k}",
                "specialist": f"s{k}", "ordinal": k, "path": f"src/m{i % 40}/F{k}.kt", "line": k, "claim": "claim",
                "created_at": created + timedelta(minutes=30)})
            rows[candidate_matches].append({"id": new_id(), "team_id": ids[0], "candidate_id": candidate, "finding_id": None,
                                                "match_ordinal": 0, "method": "none", "matcher_version": "1",
                                                "created_at": created})
            rows[labels].append({"id": new_id(), "team_id": ids[0], "subject_kind": "candidate", "subject_id": candidate,
                                     "taxonomy_version": taxonomy.VERSION, "label": "correctness", "labeler": "t",
                                     "created_at": NOW})
    with engine.begin() as conn:
        for table in (gh_pull_requests, gh_reviews, gh_review_comments, gh_check_runs, gh_conflict_samples, runs,
                      tool_calls, finding_candidates, candidate_matches, labels):
            conn.execute(insert(table), rows[table])
    return ids[0], folks


def test_the_heavy_screens_answer_quickly_at_ninety_days(sqlite_engine):
    team_id, folks = _busy_team(sqlite_engine)
    timings = {}
    with sqlite_engine.connect() as conn:
        repo = conn.execute(select(gh_repos.c.id).where(gh_repos.c.team_id == team_id,
                                                        gh_repos.c.full_name == "acme/one")).scalar()
        for name, compute in (
                ("overview", lambda: metrics.overview(conn, team_id, NOW, 90, None, with_people=True)),
                ("people", lambda: metrics.people_view(conn, team_id, NOW, 90, None, "review")),
                ("person", lambda: metrics.person_view(conn, team_id, NOW, 90, folks[3].id)),
                ("prs", lambda: metrics.pr_list(conn, team_id, NOW, 90, repo_id=repo, stage="in_review",
                                                push_class=None, category="correctness", person_id=None,
                                                with_people=True)),
                ("file", lambda: metrics.file_view(conn, team_id, NOW, 90, repo, "src/m0/F0.kt", with_people=True)),
                ("specialist", lambda: metrics.specialist_view(conn, team_id, NOW, 90, "s1", with_people=True)),
                ("pr", lambda: metrics.pr_detail(conn, team_id, metrics.pr_id_by_number(conn, team_id, repo, 1),
                                                 NOW))):
            # CPU time, so other work on the machine can't fail it but a slower measure still does.
            started = time.process_time()
            answer = compute()
            timings[name] = time.process_time() - started
            assert answer
    assert all(seconds < 2.0 for seconds in timings.values()), timings


# Review round 1, part 2


def test_flags_load_with_their_pull_request_whatever_the_screens_span(sqlite_engine, world):
    old = world.b.pr(world.app, 30, world.alice, at(6, 1), at(11, 10), head="h30")
    world.b.review(old, world.bob, "approved", "h30", at(11, 10, 6))
    run_id = world.b.run("acme/app#30", "h30", at(6, 2))
    world.b.flag(run_id, "correctness", "app/src/Old.kt", at(6, 2), "block", "correctness")
    w7 = metrics.window(NOW, 7)
    with sqlite_engine.connect() as conn:
        team = metrics.Team(conn, world.team, metrics._since(w7), NOW, children=metrics.FOR_PEOPLE)
    assert [c.row.path for c in team.prs[old].candidates] == ["app/src/Old.kt"]
    assert metrics.person_measures(team, w7, world.alice.id, set())["quality"] == {
        "rounds_per_pr": 1.0, "kept_per_round": 1.0, "bugs_traced": 1}


def test_a_reviewers_first_review_counts_only_in_its_own_window(sqlite_engine, world):
    p5 = next(pr for pr in _team(sqlite_engine, world).prs.values() if pr.row.number == 5)
    world.b.review(p5.id, world.erin, "commented", "h5a", at(11, 1))
    world.b.review(world.p2, world.erin, "commented", "h2b", at(11, 5, 22))
    team = _team(sqlite_engine, world)
    erin = metrics.person_measures(team, W30, world.erin.id, set())["review"]
    # Erin's first reviews: P5 on Sep 21 (outside), P4 after 2 hours, P2 after 10 hours.
    assert (erin["reviews_given"], erin["first_review_hours"]) == (3, 6.0)
    previous = metrics.person_measures(team, W30.previous(), world.erin.id, set())["review"]
    assert previous["first_review_hours"] == pytest.approx(15.0)


def test_stages_never_add_up_past_the_time_to_merge(sqlite_engine, world):
    p11 = next(pr for pr in _team(sqlite_engine, world).prs.values() if pr.row.number == 11)
    world.b.review(p11.id, world.alice, "commented", "h11", at(11, 13))
    team = _team(sqlite_engine, world)
    stages = metrics.pr_stages(team.prs[p11.id])
    assert stages == {"waiting_first_review": 24.0, "in_review": 0.0, "blocked_by_conflicts": 0,
                      "waiting_on_ci": 0.0, "approved_to_merged": 0.0}
    for pr in team.merged(W30):
        found = metrics.pr_stages(pr)
        spent = found["waiting_first_review"] + found["in_review"] + found["approved_to_merged"]
        assert spent <= metrics._hours(pr.row.merged_at_gh - pr.row.created_at_gh) + 0.01


def test_time_to_first_review_counts_by_when_the_first_review_came(sqlite_engine, world):
    waited = world.b.pr(world.app, 31, world.carol, at(9, 1), head="h31")
    world.b.review(waited, world.dave, "commented", "h31", at(11, 1))
    team = _team(sqlite_engine, world, since_days=150)
    # P1 12, P2 6, P4 2, and P31, opened in September and first reviewed on November 1 after 61 days.
    assert metrics.time_to_first_review_hours(team, W30) == pytest.approx(9.0)
    # The previous window has P5 (24) and P12 (6) only. P31 was still waiting then, so it doesn't count.
    assert metrics.time_to_first_review_hours(team, W30.previous()) == pytest.approx(15.0)


def test_substantive_comments_wait_for_every_label_and_skip_own_pull_requests(sqlite_engine, world):
    team = _team(sqlite_engine, world)
    assert metrics.person_measures(team, W30, world.alice.id, set())["review"]["substantive_comments"] == 0
    world.b.comment(world.p4, world.bob, "lib/core/Parse.kt", at(11, 8, 16))
    team = _team(sqlite_engine, world)
    assert metrics.person_measures(team, W30, world.bob.id, set())["review"]["substantive_comments"] is None


def test_one_bug_item_per_pull_request_and_at_most_two(sqlite_engine, world, monkeypatch):
    again = world.b.pr(world.app, 24, world.dave, at(11, 11), at(11, 12, 6))
    world.b.link(again, world.p1, "fix", 0.9)
    p3 = next(pr for pr in _team(sqlite_engine, world).prs.values() if pr.row.number == 3)
    run_id = world.b.run("acme/app#3", "h3", at(10, 21))
    world.b.flag(run_id, "security", "app/src/Old.kt", at(10, 21), None, "security-privacy")
    third = world.b.pr(world.app, 25, world.dave, at(11, 12), at(11, 12, 18))
    world.b.link(third, p3.id, "fix", 0.9)
    items = metrics.attention(_team(sqlite_engine, world), W30)
    bugs = [i for i in items if i["kind"] == "dropped_flag_bug"]
    # Newest fix first: P25 into P3, P11 into P2. P1's two links (P24 and P10) would make one item, and the cap
    # leaves it out.
    assert [b["pr"]["number"] for b in bugs] == [3, 2]
    monkeypatch.setattr(metrics, "DROPPED_FLAG_ITEMS", 10)
    every = metrics.attention(_team(sqlite_engine, world), W30)
    assert [i["pr"]["number"] for i in every if i["kind"] == "dropped_flag_bug"] == [3, 2, 1]
    assert len(items) <= metrics.ATTENTION_MAX


def test_attention_names_no_one_below_team_lead(lead, member, world):
    seen = _get(member, world, "/overview").json()["attention"]
    waiting = next(i for i in seen if i["kind"] == "waiting_on_review")
    assert waiting["count"] == 2 and "person" not in waiting and "person_id" not in waiting
    text = json.dumps(seen)
    for person in (world.alice, world.bob, world.carol, world.dave, world.erin):
        assert person.id not in text
    for name in ("Alice", "Bob", "Carol", "Dave", "Erin"):
        assert name not in text
    named = next(i for i in _get(lead, world, "/overview").json()["attention"] if i["kind"] == "waiting_on_review")
    assert (named["person"], named["person_id"]) == ("Carol", world.carol.id)


def test_a_merge_into_a_bot_or_out_of_armature_is_refused(lead, world, sqlite_engine):
    base = f"/api/teams/{world.team}/insights/people"
    assert lead.client.post(f"{base}/{world.erin.id}/merge", json={"into": world.bot.id},
                            headers=lead.headers).status_code == 409
    assert lead.client.post(f"{base}/{world.arm.id}/merge", json={"into": world.dave.id},
                            headers=lead.headers).status_code == 409
    with sqlite_engine.connect() as conn:
        assert len(conn.execute(select(people).where(people.c.id.in_([world.erin.id, world.arm.id]))).all()) == 2


# 3a.2: drill-down


def test_a_pull_request_by_repo_and_number(lead, world):
    by_number = _get(lead, world, f"/repos/{world.app}/prs/1")
    assert by_number.status_code == 200 and by_number.json() == _get(lead, world, f"/prs/{world.p1}").json()
    assert _get(lead, world, f"/repos/{world.lib}/prs/1").json()["pr"]["id"] == world.p4
    assert _get(lead, world, f"/repos/{world.app}/prs/999").status_code == 404
    assert _get(lead, world, f"/repos/{world.o_repo}/prs/1").status_code == 404
    assert _get(lead, world, f"/repos/{world.app}/prs/one").status_code == 400


def _login(engine, world, person, login, seen):
    with engine.begin() as conn:
        conn.execute(insert(person_identities).values(id=new_id(), team_id=world.team, person_id=person.id,
                                                      kind="github_login", value=login, verified=True,
                                                      first_seen=seen, last_seen=seen))


def test_a_person_by_login_follows_the_newest_owner_and_the_same_gate(lead, member, world, sqlite_engine):
    _login(sqlite_engine, world, world.alice, "alice", NOW)
    _login(sqlite_engine, world, world.bob, "bob", NOW)
    _login(sqlite_engine, world, world.dave, "dave-old", at(1, 1))
    found = _get(lead, world, "/people/by-login/Bob")
    assert found.status_code == 200 and found.json()["person"]["id"] == world.bob.id
    assert _get(lead, world, "/people/by-login/dave-old").json()["person"]["id"] == world.dave.id
    assert _get(lead, world, "/people/by-login/nobody").status_code == 404
    assert _get(member, world, "/people/by-login/bob").json()["person"]["name"] == "Bob"
    # The lead's cached answer for Alice never reaches the contributor.
    assert _get(member, world, "/people/by-login/alice").status_code == 403
    assert _get(member, world, "/people/by-login/nobody").status_code == 403


def test_a_files_detail_links_to_github_and_counts_by_week(lead, member, world, sqlite_engine):
    with sqlite_engine.begin() as conn:
        conn.execute(gh_pull_requests.update().where(gh_pull_requests.c.id == world.p2).values(base_ref="main"))
    body = _get(lead, world, f"/repos/{world.app}/files", path="app/src/Main.kt").json()
    assert body["url"] == "https://github.com/acme/app/blob/main/app/src/Main.kt"
    assert body["findings"] == {"value": 3, "previous": 1}
    assert len(body["weekly"]["weeks"]) == 12 and sum(body["weekly"]["findings"]) == 4
    assert [(p["number"], p["author"]) for p in body["prs"]] == [(1, "Alice"), (5, "Alice")]
    assert "author" not in _get(member, world, f"/repos/{world.app}/files", path="app/src/Main.kt").json()["prs"][0]
    assert _get(lead, world, f"/repos/{world.app}/files").status_code == 400
    assert _get(lead, world, f"/repos/{world.o_repo}/files", path="a.kt").status_code == 404


def test_the_pull_request_list_for_each_drill_down(lead, member, world):
    def numbers(**params):
        return [p["number"] for p in _get(lead, world, "/prs", repo=world.app, **params).json()["prs"]]

    rows = _get(lead, world, "/prs", repo=world.app, stage="waiting_first_review").json()["prs"]
    assert [(p["number"], p["stage_hours"]) for p in rows] == [(3, 96.0), (11, 24.0), (1, 12.0), (2, 6.0),
                                                              (10, 6.0)]
    assert numbers(stage="blocked")[0] == 1
    assert numbers(push_class="rebase") == [1] and numbers(push_class="unclassified") == [2]
    assert numbers(category="performance") == [2]
    assert numbers(person=world.bob.id) == [2]
    recent = _get(lead, world, "/prs", repo=world.app).json()["prs"]
    stamps = [p["merged_at"] or p["created_at"] for p in recent]
    assert stamps == sorted(stamps, reverse=True)
    assert _get(member, world, "/prs", person=world.bob.id).status_code == 403
    assert "author" not in _get(member, world, "/prs", stage="in_review").json()["prs"][0]
    assert _get(lead, world, "/prs", stage="lunch").status_code == 400
    assert _get(lead, world, "/prs", category="nope").status_code == 400


def test_a_specialists_last_dropped_flags(lead, member, world):
    body = _get(lead, world, "/findings/specialists/security").json()
    assert (body["raised"], body["dropped"]) == (2, 2)
    assert [(f["pr"]["number"], f["path"], f["line"]) for f in body["flags"]] == [(2, "app/src/Net.kt", 7),
                                                                                  (1, "app/src/Net.kt", 7)]
    assert body["flags"][0]["url"] == "https://github.com/acme/app/blob/h2a/app/src/Net.kt#L7"
    assert "author" not in _get(member, world, "/findings/specialists/security").json()["flags"][0]["pr"]
    assert _get(lead, world, "/findings/specialists/correctness").json()["flags"] == []


def test_findings_name_each_repos_top_category_and_repos_their_tracking_time(lead, world):
    assert _get(lead, world, "/findings").json()["top_by_repo"] == [
        {"repo_id": world.app, "repo": "acme/app", "category": "concurrency", "count": 2},
        {"repo_id": world.lib, "repo": "acme/lib", "category": "correctness", "count": 1}]
    assert {r["tracked_at"] for r in _get(lead, world, "/repos").json()["repos"]} == {NOW.isoformat()}


def test_a_merge_needs_a_step_up(world, sqlite_engine, keyring, clock, users):
    stale = _client(sqlite_engine, keyring, clock, users["lead"], step_up=False)
    refused = stale.client.post(f"/api/teams/{world.team}/insights/people/{world.erin.id}/merge",
                                json={"into": world.dave.id}, headers=stale.headers)
    assert refused.status_code == 403 and refused.json()["error"] == "step_up_required"


@pytest.mark.parametrize("path", ["../../evil/repo/x.kt", "a/%2e%2e/%2E./b.kt", "dir/./file?x=1#frag name.kt",
                                  "/leading//double.kt"])
def test_a_line_link_stays_inside_its_repository(path):
    url = metrics.line_url("https://github.com", "acme/app", "abc", path, 3)
    assert url.startswith("https://github.com/acme/app/blob/abc/") and url.endswith("#L3")
    tail = url[len("https://github.com/acme/app/blob/abc/"):-len("#L3")]
    assert not {"..", ".", ""} & {unquote(part) for part in tail.split("/")}
    assert "?" not in tail and "#" not in tail and " " not in tail


def test_line_links_encode_like_encode_uri_component():
    assert metrics.line_url("https://github.com", "acme/app", "abc", "dir/./file?x=1#frag name.kt", None) == \
        "https://github.com/acme/app/blob/abc/dir/file%3Fx%3D1%23frag%20name.kt"


def test_substantive_comments_read_only_the_comment_labels(sqlite_engine, world):
    # Bob's two comments carry a category label too. Counting those would read "concurrency" as not substantive.
    team = _team(sqlite_engine, world)
    assert metrics.person_measures(team, W30, world.bob.id, set())["review"]["substantive_comments"] == 1
    late = world.b.comment(world.p4, world.bob, "lib/core/Parse.kt", at(11, 8, 16))
    world.b.label(metrics.COMMENT_KIND, late, "correctness")
    team = _team(sqlite_engine, world)
    assert metrics.person_measures(team, W30, world.bob.id, set())["review"]["substantive_comments"] is None
    # A comment whose text couldn't be read is labeled for good, and never counts as substantive.
    world.b.label(metrics.COMMENT_KIND, late, "unavailable", labeling.COMMENT_VERSION)
    team = _team(sqlite_engine, world)
    assert metrics.person_measures(team, W30, world.bob.id, set())["review"]["substantive_comments"] == 1


# 3a.3: logins


def test_every_person_reference_carries_the_newest_login(lead, member, world, sqlite_engine):
    _login(sqlite_engine, world, world.bob, "bob-old", at(9, 1))
    _login(sqlite_engine, world, world.bob, "bob-new", NOW)
    _login(sqlite_engine, world, world.alice, "alice", NOW)
    _login(sqlite_engine, world, world.carol, "carol", NOW)
    people_rows = {r["id"]: r["login"] for r in _get(lead, world, "/people").json()["rows"]}
    assert (people_rows[world.bob.id], people_rows[world.alice.id], people_rows[world.dave.id]) == (
        "bob-new", "alice", None)
    overview = _get(lead, world, "/overview").json()
    assert {r["id"]: r["login"] for r in overview["people"]}[world.bob.id] == "bob-new"
    assert next(i for i in overview["attention"] if i["kind"] == "waiting_on_review")["login"] == "carol"
    assert _get(lead, world, f"/people/{world.bob.id}").json()["person"]["login"] == "bob-new"
    detail = _get(lead, world, f"/prs/{world.p1}").json()
    assert detail["pr"]["author_login"] == "alice"
    assert {r["reviewer_login"] for round_ in detail["rounds"] for r in round_["reviews"]
            if r["reviewer_id"] == world.bob.id} == {"bob-new"}
    assert _get(lead, world, "/prs", person=world.bob.id).json()["prs"][0]["author_login"] == "bob-new"
    files = _get(lead, world, f"/repos/{world.app}/files", path="app/src/Main.kt").json()
    assert files["prs"][0]["author_login"] == "alice"
    hidden = _get(member, world, "/prs", stage="in_review").json()["prs"][0]
    assert "author_login" not in hidden and "author" not in hidden
    assert "login" not in json.dumps(_get(member, world, "/overview").json()["attention"])


# 3a.4: Armature's comments under a person's login


def test_armatures_review_comments_never_count_for_the_person_it_posts_as(sqlite_engine, world):
    # Armature posts through Erin's gh CLI login: its review is Armature's, its comments carry Erin's identity.
    posted = world.b.review(world.p2, world.arm, "commented", "h2b", at(11, 5, 19), actor_source="armature")
    for minute in (0, 5):
        world.b.comment(world.p2, world.erin, "app/src/Net.kt", at(11, 5, 18, minute), review_id=posted)
    own = world.b.review(world.p2, world.erin, "commented", "h2b", at(11, 5, 21))
    mine = world.b.comment(world.p2, world.erin, "app/src/Net.kt", at(11, 5, 20), review_id=own)
    world.b.label(metrics.COMMENT_KIND, mine, metrics.SUBSTANTIVE, labeling.COMMENT_VERSION)
    team = _team(sqlite_engine, world)
    assert [c.id for _, c in team.comments_by[world.erin.id]] == [mine]
    review = metrics.person_measures(team, W30, world.erin.id, set())["review"]
    # Erin's own comment is labeled, so Armature's unlabeled ones can't hold the count at null.
    assert review["substantive_comments"] == 1
    # Her review of P2 took the hour from her comment to submitting it, plus 0.03 for P4.
    assert review["review_hours"] == 1.03
    net = next(r for r in metrics.hotspots(team, W30) if r["path"] == "app/src/Net.kt")
    assert net["findings"] == 3


# Comparison runs stay out of every measure.

def _comparison(engine, b, target="acme/app#1", when=None, paths=("app/feature/A.kt", "app/feature/B.kt",
                                                                    "app/feature/C.kt")):
    """A comparison run of a review of ``target``: arm-mode review calls, three labeled flags, and its spend."""
    from armature.server.db.schema import eval_arms, eval_experiments
    when = when or at(11, 3)
    primary = b._add(runs, project_id=b.project, status="succeeded", started_at=when)
    experiment = b._add(eval_experiments, specialist="concurrency", variable="effort", baseline={},
                        variant={"effort": "high"}, sample_rate=1.0, daily_budget_usd=25, max_concurrent_arms=2,
                        status="running", min_prs=30, created_at=when)
    arm_id = new_id()
    run_id = b._add(runs, project_id=b.project, status="succeeded", started_at=when, trigger_ref={"arm_id": arm_id})
    b._add(eval_arms, id=arm_id, experiment_id=experiment, primary_run_id=primary, arm_run_id=run_id,
           head_sha="a" * 40, label="variant", status="done", created_at=when)
    for tool, detail in (("review.decisions", None), ("github.post-review", {"posted": True, "head": "h1b"})):
        b._add(tool_calls, run_id=run_id, project_id=b.project, step_id=tool, tool=tool, mode="arm", outcome="ok",
               target=target, detail=detail, created_at=when)
    flags = [b._add(finding_candidates, project_id=b.project, run_id=run_id, step_key="first/concurrency",
                    specialist="concurrency", ordinal=i, path=path, line=7, claim="lock leaks", arm_id=arm_id,
                    created_at=when) for i, path in enumerate(paths)]
    for flag in flags:
        b.label("candidate", flag, "concurrency")
    b.spend(run_id, "first/concurrency", "claude-opus", 3.0, when)
    return SimpleNamespace(run=run_id, experiment=experiment, flags=flags)


def test_comparison_runs_count_in_no_findings_or_people_measure(sqlite_engine, world):
    before = metrics.findings_view(_team(sqlite_engine, world, since_days=150), W30)
    by_person = metrics.findings_view(_team(sqlite_engine, world, since_days=150), W30, person_id=world.alice.id)
    found = _comparison(sqlite_engine, world.b)
    team = _team(sqlite_engine, world, since_days=150)
    assert metrics.findings_view(team, W30) == before
    assert metrics.findings_view(team, W30, person_id=world.alice.id) == by_person
    assert not {c.row.id for c in team.candidates} & set(found.flags)


def test_a_comparison_runs_calls_never_name_the_pull_request_it_reviewed(sqlite_engine, world):
    found = _comparison(sqlite_engine, world.b)
    with sqlite_engine.connect() as conn:
        assert metrics.run_targets(conn, world.team, [found.run]) == {}


def test_comparison_spend_is_apart_from_review_spend(sqlite_engine, world):
    from armature.server.db.schema import eval_judgments
    found = _comparison(sqlite_engine, world.b)
    world.b._add(eval_judgments, candidate_id=found.flags[0], experiment_id=found.experiment, valid=True, judge="t",
                 cost_usd=0.5, created_at=at(11, 3))
    with sqlite_engine.connect() as conn:
        view = metrics.models_view(conn, world.team, W30)
        other = metrics.models_view(conn, world.team, W30, "security")
    assert view["spend_usd"] == {"value": 1.75, "previous": 2.0}
    assert view["cost_per_reviewed_pr_usd"] == {"value": 0.875, "previous": 2.0}
    assert view["comparison_spend_usd"] == {"value": 3.5, "previous": 0.0}
    assert [s["name"] for s in view["by_specialist"]] == ["correctness", "security", "performance"]
    assert other["comparison_spend_usd"]["value"] == 0.0
    [row] = view["comparisons"]
    assert row["result"] == {"kind": "early", "words": "Too early to tell"} and row["prs"] == 0


def test_comparison_flags_are_never_labeled_or_clustered(sqlite_engine, labeled_team):
    b = Builder(sqlite_engine, (labeled_team, None))
    b.project = None
    found = _comparison(sqlite_engine, b, target="acme/android-tree#4", when=NOW - timedelta(days=2))
    assert jobs.cluster(sqlite_engine, labeled_team, NOW) == {"clusters": 0, "removed": 0, "labeled": 0}
    with sqlite_engine.begin() as conn:
        conn.execute(labels.delete().where(labels.c.subject_id.in_(found.flags)))
        listed = {subject_id for _, subject_id, _ in labeling._unlabeled(conn, labeled_team, 100)}
    assert not listed & set(found.flags) and len(listed) == 5


def test_a_comparison_runs_decisions_never_reach_the_leads_last_decision(sqlite_engine, world):
    from armature.server.tools import Call
    from armature.server.tools.context import _decided
    found = _comparison(sqlite_engine, world.b)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(findings).values(id=new_id(), run_id=found.run, project_id=world.project,
                                             finding_key="0123456789ab", path="app/feature/A.kt", decision="drop",
                                             created_at=at(11, 3)))
    call = Call(run_id=new_id(), step_id="context", connection=None, creds={}, now=NOW, project_id=world.project,
                team_id=world.team, engine=sqlite_engine)
    assert _decided(call, "acme/app", 1, ["0123456789ab"]) == {}

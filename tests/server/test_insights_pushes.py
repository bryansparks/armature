"""Classifying a push by what it was, from the push and its pull request's context alone."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from armature.server.insights.pushes import (
    ARMATURE_FIX,
    FIX_AFTER_REVIEW,
    FORCE_REWRITE,
    MERGE_FROM_BASE,
    NEW_SCOPE,
    REBASE,
    Changed,
    Commit,
    Context,
    OnBase,
    Push,
    Review,
    Verdict,
    classify,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
OLD, NEW, BASE, OTHER = "1" * 40, "2" * 40, "3" * 40, "4" * 40


def _push(forced=False, armature=False, at=T0):
    return Push(OLD, NEW, forced, at, armature)


def test_armature_fix_wins_over_everything_else():
    ctx = Context(base_ref="main", armature_commits=frozenset({NEW}),
                  commits={NEW: Commit((OLD, BASE), "Merge branch 'main' into feature")})
    found = classify(_push(forced=True), ctx)
    assert found == Verdict(ARMATURE_FIX, 1.0, {"commit": NEW, "from": "github.push call"})
    assert classify(_push(armature=True), Context(base_ref="main")).push_class == ARMATURE_FIX


def test_a_merge_with_a_known_base_parent_or_a_merge_headline_is_from_base():
    by_parent = Context(base_ref="main", base_commits=frozenset({BASE}), commits={NEW: Commit((OLD, BASE), "x")})
    assert classify(_push(), by_parent) == Verdict(MERGE_FROM_BASE, 0.8, {"parent_on_base": BASE,
                                                                          "from": "graphql"})
    for headline in ("Merge branch 'main' into feature", "Merge remote-tracking branch 'origin/main' into f"):
        found = classify(_push(), Context(base_ref="main", commits={NEW: Commit((OLD, OTHER), headline)}))
        assert (found.push_class, found.confidence) == (MERGE_FROM_BASE, 0.8), headline


def test_an_ambiguous_merge_asks_whether_the_other_parent_is_on_base():
    ctx = Context(base_ref="main", commits={NEW: Commit((OLD, OTHER), "Merge branch 'release' into feature")})
    assert classify(_push(), ctx) == OnBase(OTHER)
    ctx.on_base[OTHER] = True
    assert classify(_push(), ctx) == Verdict(MERGE_FROM_BASE, 0.95, {"parent_on_base": OTHER, "from": "compare"})
    ctx.on_base[OTHER] = False
    assert classify(_push(), ctx).push_class == NEW_SCOPE


def test_a_single_parent_commit_is_never_a_merge():
    ctx = Context(base_ref="main", base_commits=frozenset({OLD}), commits={NEW: Commit((OLD,), "Merge branch 'main'")})
    assert classify(_push(), ctx).push_class == NEW_SCOPE


def _history(*commits):
    return tuple(commits)


def test_a_force_push_that_keeps_headlines_and_count_is_a_rebase():
    shared = ("9" * 40, "base work")
    old = _history(("a" * 40, "Add login"), ("b" * 40, "Add form"), shared)
    # The rebase put the same two commits on top of a newer base commit.
    new = _history(("c" * 40, "Add login"), ("d" * 40, "Add form"), ("e" * 40, "newer base"), shared)
    found = classify(_push(forced=True), Context(base_ref="main", histories={OLD: old, NEW: new}))
    assert (found.push_class, found.confidence) == (REBASE, 0.7)
    assert found.evidence["commits"] == 2 and "no clone" in found.evidence["patch_ids"]


def test_a_force_push_that_changes_headlines_or_count_is_a_rewrite():
    shared = ("9" * 40, "base")
    old = _history(("a" * 40, "Add login"), ("b" * 40, "Add form"), shared)
    squashed = _history(("c" * 40, "Add login"), shared)
    reworded = _history(("c" * 40, "Add sign-in"), ("d" * 40, "Add form"), shared)
    for new in (squashed, reworded):
        found = classify(_push(forced=True), Context(base_ref="main", histories={OLD: old, NEW: new}))
        assert (found.push_class, found.confidence) == (FORCE_REWRITE, 0.6)


def test_histories_that_never_meet_use_the_pull_requests_commit_count():
    old = _history(("a" * 40, "Add login"), ("b" * 40, "old base"))
    new = _history(("c" * 40, "Add login"), ("d" * 40, "new base"))
    counted = Context(base_ref="main", histories={OLD: old, NEW: new}, commit_counts={NEW: 1})
    assert classify(_push(forced=True), counted).push_class == REBASE
    uncounted = Context(base_ref="main", histories={OLD: old, NEW: new})
    assert classify(_push(forced=True), uncounted).evidence["histories_met"] is False


def test_a_force_push_whose_old_head_is_gone_is_a_rewrite():
    found = classify(_push(forced=True), Context(base_ref="main", histories={OLD: None, NEW: ()}))
    assert (found.push_class, found.evidence["old_head"]) == (FORCE_REWRITE, "gone")


def test_a_push_after_a_review_that_touches_flagged_files_is_a_fix():
    review = Review(T0 - timedelta(hours=1), OLD, frozenset({"src/a.swift", "src/b.swift"}))
    ctx = Context(base_ref="main", reviews=(review,))
    assert classify(_push(), ctx) == Changed(OLD, NEW)
    ctx.changed[(OLD, NEW)] = frozenset({"src/a.swift", "README.md"})
    assert classify(_push(), ctx) == Verdict(FIX_AFTER_REVIEW, 0.7, {"reviews": 1, "files": ["src/a.swift"],
                                                                     "matched": 1})
    ctx.changed[(OLD, NEW)] = frozenset({"README.md"})
    assert (classify(_push(), ctx).push_class, classify(_push(), ctx).confidence) == (NEW_SCOPE, 0.5)


def test_a_push_with_no_review_before_it_is_new_scope_without_a_compare():
    later = Review(T0 + timedelta(hours=1), "f" * 40, frozenset({"src/a.swift"}))
    before_previous = Review(T0 - timedelta(days=2), "e" * 40, frozenset({"src/a.swift"}))
    ctx = Context(base_ref="main", reviews=(later, before_previous), heads_at={OLD: T0 - timedelta(days=1)})
    assert classify(_push(), ctx) == Verdict(NEW_SCOPE, 0.5, {"reviewed": False})


def test_a_review_that_flagged_no_files_needs_no_compare():
    ctx = Context(base_ref="main", reviews=(Review(T0 - timedelta(minutes=5), OLD, frozenset()),))
    assert classify(_push(), ctx) == Verdict(NEW_SCOPE, 0.5, {"reviews": 1, "flagged_files": 0})


def test_unknown_changed_files_fall_back_to_new_scope():
    ctx = Context(base_ref="main", reviews=(Review(None, OLD, frozenset({"a"})),), changed={(OLD, NEW): None})
    assert classify(_push(), ctx).evidence == {"reviews": 1, "changed_files": "unknown"}


def test_an_amend_then_force_push_over_an_unchanged_base_is_a_rewrite_not_a_rebase():
    base = ("9" * 40, "base work")
    login = ("b" * 40, "Add login")
    old = _history(("a" * 40, "Add form"), login, base)
    amended = _history(("c" * 40, "Add form"), login, base)
    found = classify(_push(forced=True), Context(base_ref="main", histories={OLD: old, NEW: amended}))
    assert (found.push_class, found.evidence["commits_before"], found.evidence["commits_after"]) == (
        FORCE_REWRITE, 1, 1)
    full = _history(("c" * 40, "Add form"), ("d" * 40, "Add login"), base)
    assert classify(_push(forced=True), Context(base_ref="main", histories={OLD: old, NEW: full})).push_class \
        == FORCE_REWRITE
    counted = Context(base_ref="main", histories={OLD: old[:2], NEW: full[:2]}, commit_counts={NEW: 2})
    assert classify(_push(forced=True), counted).push_class == FORCE_REWRITE

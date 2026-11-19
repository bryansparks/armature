"""Classify a push to a pull request's head by what it was, with a confidence and the evidence for it.

``classify`` is a pure function of the push and its pull request's context. When the answer turns on a fact only a
compare call gives, it returns the question instead of guessing, and the sync service asks GitHub and classifies
again with the answer in the context. The rules apply in order, and the first that matches decides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from itertools import takewhile
from typing import Any

CLASSIFIER_VERSION = "1"
ARMATURE_FIX = "armature_fix"
MERGE_FROM_BASE = "merge_from_base"
REBASE = "rebase"
FORCE_REWRITE = "force_rewrite"
FIX_AFTER_REVIEW = "fix_after_review"
NEW_SCOPE = "new_scope"
NO_PATCH_IDS = "patch IDs not compared, because the server has no clone"
EVIDENCE_FILES = 10


@dataclass(frozen=True)
class Push:
    before: str
    after: str
    forced: bool | None
    pushed_at: datetime | None
    armature: bool = False


@dataclass(frozen=True)
class Commit:
    parents: tuple[str, ...]
    headline: str


@dataclass(frozen=True)
class Review:
    """A human review or a review Armature posted, with the files its comments or findings named."""
    at: datetime | None
    head: str | None
    paths: frozenset[str]


@dataclass
class Context:
    base_ref: str | None
    # Commits known to be on the base branch: the pull request's base and each push's recorded base.
    base_commits: frozenset[str] = frozenset()
    # Commits approved github.push calls on this pull request name.
    armature_commits: frozenset[str] = frozenset()
    commits: dict[str, Commit] = field(default_factory=dict)
    # Each force-pushed head's history, newest first, as (oid, headline). None when GitHub no longer has the commit.
    histories: dict[str, tuple[tuple[str, str], ...] | None] = field(default_factory=dict)
    # How many commits the pull request had at a head, where GitHub said.
    commit_counts: dict[str, int] = field(default_factory=dict)
    reviews: tuple[Review, ...] = ()
    # When each of the pull request's heads was pushed.
    heads_at: dict[str, datetime] = field(default_factory=dict)
    # Answers to OnBase questions: whether the commit is on the base branch, None when GitHub couldn't say.
    on_base: dict[str, bool | None] = field(default_factory=dict)
    # Answers to Changed questions: the files changed from before to after, None when GitHub couldn't say.
    changed: dict[tuple[str, str], frozenset[str] | None] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    push_class: str
    confidence: float
    evidence: dict[str, Any]


@dataclass(frozen=True)
class OnBase:
    """Whether ``sha`` is on the base branch."""
    sha: str


@dataclass(frozen=True)
class Changed:
    """Which files changed from ``before`` to ``after``."""
    before: str
    after: str


def _merge_headline(headline: str, base: str | None) -> bool:
    if not base:
        return False
    name = re.escape(base)
    return re.match(rf"^Merge (?:branch '{name}'|remote-tracking branch '(?:[^']*/)?{name}')", headline) is not None


def _merge(push: Push, ctx: Context) -> Verdict | OnBase | None:
    commit = ctx.commits.get(push.after)
    if commit is None or len(commit.parents) != 2:
        return None
    hit = next((p for p in commit.parents if p in ctx.base_commits), None)
    if hit is not None:
        return Verdict(MERGE_FROM_BASE, 0.8, {"parent_on_base": hit, "from": "graphql"})
    if _merge_headline(commit.headline, ctx.base_ref):
        return Verdict(MERGE_FROM_BASE, 0.8, {"headline": commit.headline[:200], "from": "graphql"})
    if not ctx.base_ref:
        return None
    for parent in [p for p in commit.parents if p != push.before] or list(commit.parents):
        if parent not in ctx.on_base:
            return OnBase(parent)
        if ctx.on_base[parent]:
            return Verdict(MERGE_FROM_BASE, 0.95, {"parent_on_base": parent, "from": "compare"})
    return None


def _headlines(history: tuple[tuple[str, str], ...], stop: set[str]) -> list[str]:
    return [headline for _, headline in takewhile(lambda c: c[0] not in stop, history)]


def _forced(push: Push, ctx: Context) -> Verdict:
    old, new = ctx.histories.get(push.before), ctx.histories.get(push.after)
    if old is None or new is None:
        gone = "gone" if push.before in ctx.histories and old is None else "not fetched"
        return Verdict(FORCE_REWRITE, 0.6, {"forced": True, "old_head": gone, "patch_ids": NO_PATCH_IDS})
    shared = {oid for oid, _ in old} & {oid for oid, _ in new}
    if shared:
        before, after = _headlines(old, shared), _headlines(new, shared)
        # A rebase onto a newer base leaves the base's new commits between the pull request's and the shared
        # ancestor. Without them the base didn't move, and the same headlines mean an amend or a reset.
        if before and after[:len(before)] == before and len(after) > len(before):
            return Verdict(REBASE, 0.7, {"forced": True, "commits": len(before), "headlines_match": True,
                                         "base_moved": True, "patch_ids": NO_PATCH_IDS})
        return Verdict(FORCE_REWRITE, 0.6, {"forced": True, "commits_before": len(before),
                                            "commits_after": len(after), "patch_ids": NO_PATCH_IDS})
    count = ctx.commit_counts.get(push.after)
    # The commits below the pull request's differ, because the histories share none, so the base moved.
    if count and min(len(old), len(new)) > count and [h for _, h in old[:count]] == [h for _, h in new[:count]]:
        return Verdict(REBASE, 0.7, {"forced": True, "commits": count, "headlines_match": True,
                                     "count_from": "pull request", "base_moved": True, "patch_ids": NO_PATCH_IDS})
    return Verdict(FORCE_REWRITE, 0.6, {"forced": True, "histories_met": False, "patch_ids": NO_PATCH_IDS})


def _after_review(push: Push, ctx: Context) -> Verdict | Changed:
    previous = ctx.heads_at.get(push.before)
    reviews = [r for r in ctx.reviews if (r.head is not None and r.head == push.before) or (
        r.at is not None and push.pushed_at is not None and r.at <= push.pushed_at
        and (previous is None or r.at > previous))]
    if not reviews:
        return Verdict(NEW_SCOPE, 0.5, {"reviewed": False})
    flagged = frozenset().union(*(r.paths for r in reviews))
    if not flagged:
        return Verdict(NEW_SCOPE, 0.5, {"reviews": len(reviews), "flagged_files": 0})
    key = (push.before, push.after)
    if key not in ctx.changed:
        return Changed(push.before, push.after)
    changed = ctx.changed[key]
    if changed is None:
        return Verdict(NEW_SCOPE, 0.5, {"reviews": len(reviews), "changed_files": "unknown"})
    hit = sorted(changed & flagged)
    if hit:
        return Verdict(FIX_AFTER_REVIEW, 0.7, {"reviews": len(reviews), "files": hit[:EVIDENCE_FILES],
                                               "matched": len(hit)})
    return Verdict(NEW_SCOPE, 0.5, {"reviews": len(reviews), "flagged_files": len(flagged), "matched": 0})


def classify(push: Push, ctx: Context) -> Verdict | OnBase | Changed:
    if push.armature or push.after in ctx.armature_commits:
        return Verdict(ARMATURE_FIX, 1.0, {"commit": push.after, "from": "github.push call"})
    merged = _merge(push, ctx)
    if merged is not None:
        return merged
    if push.forced:
        return _forced(push, ctx)
    return _after_review(push, ctx)

"""code-review: a pull request on a review branch gets S14b's specialist panel, team lead, posted review, and mark.

Replaces ops' review branch of ``/gh/pr``, ``bin/pr-review``'s queue, and specs/code-review.yml. The bodies are
S14b's ``code-review`` and ``review-pass`` starters, and the roles its seeded review roles.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from armature.server import starters
from armature.server.migrate_ops import MigrateError, Rows, Rule, Webhook

# ops' teams.json review_branches.
REVIEW_BRANCHES = {"fs-eng/mobile-ios": "master", "fs-eng/android-tree": "dev", "fs-eng/mobile-api-nodejs": "master"}
ACTIONS = ["opened", "reopened", "synchronize", "ready_for_review"]
# ops' pr-authors.json. The push gate reads it as github.push_authors, team:<slug> entries included. A repository
# ops left out gets no list, so nothing is pushed there.
AUTHORS = {"fs-eng/mobile-ios": ["team:ios-temple", "lynns", "smootar", "talmagejsharp-fs"],
           "fs-eng/android-tree": ["team:android-temple", "moretanjames"]}
WEBHOOK = Webhook("github", "github")
NEXT = ("ops reviewed only the authors in pr-authors.json. That list now gates pushes (github.push_authors), but no "
        "rule condition checks team membership, so every author is reviewed.")
INPUTS = {"repo": "$.repository.clone_url", "slug": "$.repository.full_name", "number": "$.pull_request.number",
          "ref": "$.pull_request.head.sha", "branch": "$.pull_request.head.ref"}
KEY = ["repository.full_name", "pull_request.number"]
# ops cancelled a queued or running review when its pull request closed unmerged, and queued nothing.
CLOSED = {"events": ["pull_request"],
          "conditions": [{"path": "action", "op": "eq", "value": "closed"},
                         {"path": "pull_request.merged", "op": "eq", "value": False}],
          "project_from": "repository.full_name", "key": KEY, "cancel_only": True}


def config(branch: str) -> dict[str, Any]:
    return {
        "events": ["pull_request"],
        "conditions": [{"path": "action", "op": "in", "value": ACTIONS},
                       {"path": "pull_request.draft", "op": "eq", "value": False},
                       {"path": "pull_request.base.ref", "op": "eq", "value": branch},
                       {"path": "sender.type", "op": "ne", "value": "Bot"}],
        "project_from": "repository.full_name",
        "key": KEY,
        "concurrency": "supersede",
        "cancel_running": True,
        "inputs": INPUTS,
    }


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    repo = getattr(project, "repo", None) or ""
    branch = REVIEW_BRANCHES.get(repo) or getattr(project, "default_branch", None)
    if not branch:
        raise MigrateError(f"No review branch for {repo or 'this project'}. Set the project's default branch first.")
    return Rows(name="code-review", kind=starters.KINDS["code-review"], body=starters.body("code-review"),
                callees=(("review-pass", starters.KINDS["review-pass"], starters.body("review-pass")),),
                review_roles=True, rules=(Rule(WEBHOOK, config(branch)), Rule(WEBHOOK, CLOSED, "closed")),
                settings={"github.push_authors": AUTHORS[repo], "github.push_exclude": []} if repo in AUTHORS else {},
                notes=(NEXT,))

"""The built-in tools the broker runs on a step's behalf, each against one connection's vendor API.

A tool takes the step's checked ``with:`` values and returns a JSON result. A
refusal the step should see as data is part of that result. A refusal that is
the caller's fault raises ``ToolError``, and a vendor failure ``VendorError``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import Engine


class ToolError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Call:
    """One broker call: who asked, and the connection it runs as."""
    run_id: str
    step_id: str
    connection: Any
    creds: dict[str, str]
    now: datetime
    run_url: str = ""
    installation_id: int | None = None
    project_id: str | None = None
    policy: dict[str, Any] = field(default_factory=dict)
    transport: httpx.BaseTransport | None = None
    team_id: str | None = None
    # Only the review tools read or write the database, each in its own transaction.
    engine: Engine | None = None

    @property
    def config(self) -> dict[str, Any]:
        return self.connection.config or {}

    @property
    def identity(self) -> dict[str, Any]:
        return self.connection.identity or {}


@dataclass(frozen=True)
class Tool:
    kind: str
    run: Callable[[Call, dict[str, Any]], dict[str, Any]] | None


def registry() -> dict[str, Tool]:
    from armature.server.tools import github, jira, linear, marks, review, slack
    return {
        "github.post-review": Tool("github_app", review.post),
        "github.check-run": Tool("github_app", github.check_run),
        "github.pr-comment": Tool("github_app", github.pr_comment),
        "github.open-pr": Tool("github_app", github.open_pr),
        "github.permission": Tool("github_app", github.permission),
        "github.pr-diff": Tool("github_app", github.pr_diff),
        # Checkout only earns a read credential for git, through the broker's git-credential route.
        "github.checkout": Tool("github_app", None),
        "github.push": Tool("github_app", github.push_gate),
        "slack.post": Tool("slack", slack.post),
        "slack.update": Tool("slack", slack.update),
        "jira.comment": Tool("jira", jira.comment),
        "jira.create": Tool("jira", jira.create),
        "jira.get-issue": Tool("jira", jira.get_issue),
        "jira.transition": Tool("jira", jira.transition),
        "linear.comment": Tool("linear", linear.comment),
        "linear.transition": Tool("linear", linear.transition),
        "review.risk": Tool("github_app", marks.risk),
        "review.marked": Tool("github_app", marks.marked),
        "review.mark": Tool("github_app", marks.mark),
        "review.pushable": Tool("github_app", marks.pushable),
        "review.decisions": Tool("github_app", marks.decisions),
    }

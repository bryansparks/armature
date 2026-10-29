"""development: a Jira issue assigned to me, in a project's component, is approved, read, implemented, pushed to a
new branch, rated, held for approval when high risk, opened as a pull request, and reviewed.

Replaces ops' bin/jira-watch poll and specs/development.yml. One copy per project, each polling its own components.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from armature.server import pollers, starters
from armature.server.migrate_ops import MigrateError, Poll, Rows
from armature.server.migrate_ops.code_review import REVIEW_BRANCHES
from armature.server.migrate_ops.ported import DIR

# ops' development.json components map, inverted to each platform's repository.
COMPONENTS = {"fs-eng/android-tree": ("Android",), "fs-eng/mobile-ios": ("iOS", "Mobile iOS"),
              "fs-eng/mobile-api-nodejs": ("Mobile API",)}
MISSION = (DIR / "mission/development.md").read_text().strip()
WORK = (DIR / "prompt/jira_work.md").read_text()
# The ops prompt's parts that name an ops script or step this body doesn't have: the brief, Slack questions, and the
# script that committed for the session.
DROPPED = (WORK[WORK.index("The specialists already read"):WORK.index("Treat the issue, its comments")],)
REPLACED = {
    "The\ndeveloper it is assigned to approved this run in Slack":
        "The\ndeveloper it is assigned to approved this run",
    "You are\nalready in the worktree, and a script commits, pushes, and opens the draft pull\nrequest after you "
    "finish. Never run git commands that write, and never touch\nJira.":
        "You are\nalready in the worktree. When the outcome is implemented, commit the change in one\ncommit. Never "
        "push, never run any other git command that writes, and never touch\nJira.",
}
NEXT = "The Jira poll starts disabled and in shadow. Enable it, then turn shadow off after a few polls match ops' log."
BRANCH = "armature/{{ issue }}"
OUTCOMES = ["implemented", "already_fixed", "invalid", "blocked"]
SCHEMA = {"type": "object", "required": ["outcome", "summary", "verification", "pr_title", "pr_body"],
          "properties": {"outcome": {"enum": OUTCOMES}, "summary": {"type": "string"},
                         "verification": {"type": "string"}, "pr_title": {"type": "string"},
                         "pr_body": {"type": "string"}, "follow_ups": {"type": "array", "items": {"type": "string"}}}}


def prompt() -> str:
    text = WORK
    for part in DROPPED:
        text = text.replace(part, "")
    for old, new in REPLACED.items():
        if old not in text:
            raise ValueError(f"jira_work.md changed: {old[:40]!r} is gone")
        text = text.replace(old, new)
    return (text.strip() + "\n\nThe issue is {{ issue }}. The summary, description, and comments context files "
            "hold it as Jira returned it, fenced as data.")


def _block(text: str, spaces: int) -> str:
    return "\n".join((" " * spaces + line) if line else "" for line in text.splitlines())


def publish_stages(branch: str) -> str:
    """development-review's stages for a pushed ``branch``, copied rather than called, since its gate pauses only in
    the job's own workflow. They follow a ``push`` step and an ``implement`` step with pr_title and pr_body."""
    return f"""\
  # development-review's stages, here rather than called, because its gate pauses only in the job's own workflow.
  - id: rate
    skip_if: "{{{{ not push.pushed }}}}"
    agent:
      model: review
      ref: "{branch}"
      prompt: |
        Rate the risk of merging branch {branch} of {{{{ slug }}}} into {{{{ ref }}}}, checked out here, with the
        risk-rubric skill.
      skills: [risk-rubric]
    output_mode: guided_json
    output_schema:
      type: object
      required: [level, reasons]
      properties:
        level: {{enum: [low, medium, high]}}
        reasons: {{type: array, items: {{type: string}}}}
    depends_on: [push]

  - id: risk
    skip_if: "{{{{ not push.pushed }}}}"
    adapter: review/risk
    with:
      repo: "{{{{ slug }}}}"
      branch: "{branch}"
      base: "{{{{ ref }}}}"
      level: "{{{{ rate.level }}}}"
      reasons: "{{{{ rate.reasons }}}}"
      expect_head: "{{{{ rate.commit }}}}"
    depends_on: [rate]

  # Anything but low or medium risk, a missing level included, needs a person's approval before the pull request.
  - id: approval
    gate: human
    condition: "{{{{ push.pushed and risk.level not in ['low', 'medium'] }}}}"
    present: |
      Branch {branch} of {{{{ slug }}}} is high risk. Approve opening its pull request into {{{{ ref }}}}?
    approval:
      min_role: project_lead
    depends_on: [risk]

  - id: open
    skip_if: "{{{{ not push.pushed or (risk.level not in ['low', 'medium'] and not approval.approved) }}}}"
    adapter: github/open-pr
    with:
      repo: "{{{{ slug }}}}"
      head: "{branch}"
      base: "{{{{ ref }}}}"
      title: "{{{{ implement.pr_title }}}}"
      body: "{{{{ implement.pr_body }}}}"
    depends_on: [approval]

  - id: review
    skip_if: "{{{{ not open.number }}}}"
    call: {{workflow: review-pass}}
    with:
      repo: "{{{{ repo }}}}"
      slug: "{{{{ slug }}}}"
      number: "{{{{ open.number }}}}"
      ref: "{{{{ risk.head }}}}"
    depends_on: [open]
"""


def body() -> str:
    return f"""\
name: development
version: "1.0"
description: "Implement a Jira issue newly assigned to me, push it to a new branch, and open its pull request through development-review."
mission: {json.dumps(MISSION)}
contracts:
  inputs:
    - {{name: issue, type: string, required: true}}
    - {{name: repo, type: string, required: true}}
    - {{name: slug, type: string, required: true}}
    - {{name: ref, type: string, required: true}}
  timeout_hours: 6
stages:
  # ops asked the developer in Slack before it started any work.
  - id: approve
    gate: human
    present: |
      Start work on {{{{ issue }}}} in {{{{ slug }}}}?
    approval:
      min_role: project_lead
    depends_on: []

  - id: read
    skip_if: "{{{{ not approve.approved }}}}"
    adapter: jira/get-issue
    with:
      issue: "{{{{ issue }}}}"
    depends_on: [approve]

  # TODO: ops had four specialists consult and a tech lead brief the session. The consults wait for review roles
  # without the pull request preamble.
  - id: implement
    skip_if: "{{{{ not read.found }}}}"
    agent:
      model: develop
      access: write
      prompt: |
{_block(prompt(), 8)}
      context_files:
        summary: "{{{{ read.summary }}}}"
        description: "{{{{ read.description }}}}"
        comments: "{{{{ read.comments }}}}"
    output_mode: guided_json
    output_schema: {json.dumps(SCHEMA)}
    depends_on: [read]

  - id: push
    skip_if: "{{{{ implement.outcome != 'implemented' or not implement.commit }}}}"
    adapter: github/push
    with:
      repo: "{{{{ slug }}}}"
      branch: "{BRANCH}"
      commit: "{{{{ implement.commit }}}}"
      worktree: "{{{{ implement.worktree }}}}"
    depends_on: [implement]

{publish_stages(BRANCH)}
  # TODO: ops posted to the issue's Slack thread when it started and when it finished, and the session could ask
  # questions there. S6B conversations don't reach a development run yet.
"""


def jql(repo: str) -> str:
    names = ", ".join(json.dumps(c) for c in COMPONENTS[repo])
    return pollers.ORDER_BY.sub("", pollers.DEFAULT_JQL) + f" AND component in ({names}) ORDER BY created ASC"


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    repo = getattr(project, "repo", None) or ""
    if repo not in COMPONENTS:
        raise MigrateError(f"development runs only on the FamilySearch repositories, not {repo or 'this project'}.")
    base = REVIEW_BRANCHES[repo]
    inputs = {"issue": "$.key", "repo": f"https://github.com/{repo}.git", "slug": repo,
              "ref": base}
    config = {"name": f"Jira issues assigned to me for {repo}", "connection_id": "@jira", "jql": jql(repo),
              "inputs": inputs, "project_id": project.id, "shadow": True, "seed_first": True}
    callees = (("review-pass", starters.KINDS["review-pass"], starters.body("review-pass")),)
    return Rows(name="development", kind="work", body=body(), callees=callees, review_roles=True,
                polls=(Poll("jira_poll", config),),
                notes=(NEXT,))

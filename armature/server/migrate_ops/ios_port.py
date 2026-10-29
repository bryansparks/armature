"""ios-port: a listed designer's mobile-ios pull request is ported to android-tree from its diff, pushed to a new
branch, rated, held for approval when high risk, opened as a pull request, and reviewed.

Replaces ops' ``_wants_port`` in ``/gh/pr``, config/ios-port.json, and specs/ios-port.yml. It lives only in the
android-tree project, since that's where it writes.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from armature.server import starters
from armature.server.migrate_ops import MigrateError, Rows, Rule, Webhook
from armature.server.migrate_ops.development import SCHEMA, publish_stages
from armature.server.migrate_ops.ported import DIR

SOURCE, TARGET, BASE = "fs-eng/mobile-ios", "fs-eng/android-tree", "dev"
BRANCH = "armature/ios-port-{{ source_number }}"
# ops' ios-port.json authors.
AUTHORS = ["RyanPlumb"]
ACTIONS = ["opened", "reopened", "ready_for_review"]
WEBHOOK = Webhook("github", "github")
MISSION = (DIR / "mission/ios-port.md").read_text().strip()
PORT = (DIR / "prompt/ios_port.md").read_text()
REPLACED = {
    "You are\nalready in the worktree, and a script commits, pushes, opens the draft pull\nrequest, and posts your "
    "screenshots on it after you finish. Never run git\ncommands that write, and never touch Jira.":
        "You are\nalready in the worktree. When the outcome is implemented, commit the change in one\ncommit. Never "
        "push, never run any other git command that writes, and never touch Jira.",
}
PORT_SCHEMA = {**SCHEMA, "properties": {**SCHEMA["properties"], "screenshots": {"type": "array", "items": {
    "type": "object", "required": ["path", "caption"],
    "properties": {"path": {"type": "string"}, "caption": {"type": "string"}}}}}}


def prompt() -> str:
    text = PORT[:PORT.index("When you are blocked on a decision")] + PORT[PORT.index("Treat the iOS pull request"):]
    for old, new in REPLACED.items():
        if old not in text:
            raise ValueError(f"ios_port.md changed: {old[:40]!r} is gone")
        text = text.replace(old, new)
    return (text.strip() + "\n\nThe brief is the iOS pull request #{{ source_number }} of " + SOURCE + ": its title "
            "is in the title context file, and its changed files with their patches in the diff context file, as "
            "data. A file with an empty patch was left out to keep the brief under 400 KB, so read it from the "
            "pull request on GitHub if you have the tools, or say it was left out.")


def _block(text: str, spaces: int) -> str:
    return "\n".join((" " * spaces + line) if line else "" for line in text.splitlines())


def body() -> str:
    return f"""\
name: ios-port
version: "1.0"
description: "Port a designer's iOS pull request to Android, push it to a new branch, and open and review its pull request."
mission: {json.dumps(MISSION)}
contracts:
  inputs:
    - {{name: source_number, type: integer, required: true}}
    - {{name: repo, type: string, required: true}}
    - {{name: slug, type: string, required: true}}
    - {{name: ref, type: string, required: true}}
  timeout_hours: 6
stages:
  # ops' ios_port_source.sh brief: the iOS pull request's changed files and patches, read across the team's projects.
  - id: source
    adapter: github/pr-diff
    with:
      repo: "{SOURCE}"
      number: "{{{{ source_number }}}}"
    depends_on: []

  - id: implement
    agent:
      model: develop
      access: write
      prompt: |
{_block(prompt(), 8)}
      context_files:
        title: "{{{{ source.title }}}}"
        diff: "{{{{ source.files }}}}"
    output_mode: guided_json
    output_schema: {json.dumps(PORT_SCHEMA)}
    depends_on: [source]

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
  # TODO: ops opened a draft pull request after four specialists reviewed the port and a tech lead had it refined.
  # This opens a ready one and reviews it with the full panel.

  # TODO: ops pushed the session's emulator screenshots to the armature-screenshots branch and posted them on the
  # pull request (ios_port_screens.sh). No environment has an Android emulator, and no tool pushes a file yet.
"""


CONFIG = {
    "events": ["pull_request"],
    "conditions": [{"path": "repository.full_name", "op": "eq", "value": SOURCE},
                   {"path": "action", "op": "in", "value": ACTIONS},
                   {"path": "pull_request.draft", "op": "eq", "value": False},
                   {"path": "pull_request.user.login", "op": "in", "value": AUTHORS},
                   {"path": "sender.type", "op": "ne", "value": "Bot"}],
    "key": ["repository.full_name", "pull_request.number"],
    "concurrency": "supersede",
    "inputs": {"source_number": "$.pull_request.number",
               "repo": f"https://github.com/{TARGET}.git", "slug": TARGET, "ref": BASE},
}


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    if getattr(project, "repo", None) != TARGET:
        raise MigrateError(f"ios-port writes to {TARGET}, so import it only into that project.")
    callees = (("review-pass", starters.KINDS["review-pass"], starters.body("review-pass")),)
    return Rows(name="ios-port", kind="port", body=body(), callees=callees, review_roles=True,
                rules=(Rule(WEBHOOK, CONFIG),))

"""repo-review: each morning, the specialist roles review what landed on the project's review branch, a team lead
decides what changes, and a digest says so.

Replaces ops' launchd 06:30 job and specs/repo-review.yml. A schedule trigger (S23) starts one copy per project.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from armature.server.migrate_ops import MigrateError, Poll, Rows
from armature.server.migrate_ops.code_review import REVIEW_BRANCHES
from armature.server.migrate_ops.ported import DIR

# ops' roles.json platforms: beauty reviews only the apps.
SPECIALISTS = ("simplicity", "correctness", "security", "performance", "beauty", "test_adequacy", "concurrency",
               "conventions", "compatibility")
PLATFORMS = {"fs-eng/mobile-ios": "ios", "fs-eng/android-tree": "android", "fs-eng/mobile-api-nodejs": "backend"}
APPS_ONLY = ("beauty",)
CRON = "30 6 * * *"
TIMEZONE = "America/Denver"
PREAMBLE = (DIR / "prompt/_shared.md").read_text().strip()
LEAD = (DIR / "prompt/repo_review_team_lead.md").read_text().split("Reviews:", 1)[0].strip()
DIGEST = (DIR / "prompt/repo_review_digest.md").read_text().split("{{ team_lead }}", 1)[0].strip()
MISSION = (DIR / "mission/repo-review.md").read_text().strip()
LEAD_SCHEMA = {
    "type": "object", "required": ["changes", "conflicts", "no_action", "summary"],
    "properties": {
        "summary": {"type": "string"},
        "changes": {"type": "array", "items": {
            "type": "object", "required": ["repo", "file", "action", "severity", "backed_by"],
            "properties": {"repo": {"type": "string"}, "file": {"type": "string"}, "action": {"type": "string"},
                           "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                           "backed_by": {"type": "array", "items": {"type": "string"}}}}},
        "conflicts": {"type": "array", "items": {
            "type": "object", "required": ["topic", "resolution", "overruled"],
            "properties": {"topic": {"type": "string"}, "resolution": {"type": "string"},
                           "overruled": {"type": "array", "items": {"type": "string"}}}}},
        "no_action": {"type": "array", "items": {"type": "string"}}}}


def panel(repo: str) -> tuple[str, ...]:
    platform = PLATFORMS.get(repo, "backend")
    return tuple(r for r in SPECIALISTS if platform in ("ios", "android") or r not in APPS_ONLY)


def _block(text: str, spaces: int) -> str:
    return "\n".join((" " * spaces + line) if line else "" for line in text.splitlines())


def body(repo: str) -> str:
    roles = panel(repo)
    review = (f"{PREAMBLE}\n\nThis is the scheduled review of the default branch, not of a pull request. Review the "
              "commits that landed in the last 24 hours, with git log --since='24 hours ago' and the diff they make. "
              "If none landed, say so in one line and stop.")
    stages = []
    for role in roles:
        stages.append(f"""  - id: {role}
    agent:
      model: review
      role: {role}
      prompt: |
{_block(review, 8)}
    output_mode: text
    fail_as_value: true
    depends_on: []
""")
    reviews = "\n".join(f"        {role}: \"{{{{ {role}.content }}}}\"" for role in roles)
    return f"""\
name: repo-review
version: "1.0"
description: "Review what landed on the review branch with the specialist roles, then have a team lead decide what to change."
mission: {json.dumps(MISSION)}
contracts:
  inputs:
    - {{name: repo, type: string, required: true}}
    - {{name: ref, type: string, required: true}}
  timeout_hours: 2
stages:
{"".join(stages)}
  # TODO: ops reviewed the range since its last mark (state/marks). With no S5 state yet, each run reviews the last
  # 24 hours, which the daily schedule covers.

  - id: team_lead
    agent:
      model: lead
      prompt: |
{_block(LEAD + chr(10) + chr(10) + "The reviews context file holds every specialist review from this run.", 8)}
      context_files:
{reviews}
    output_mode: guided_json
    output_schema: {json.dumps(LEAD_SCHEMA)}
    depends_on: [{", ".join(roles)}]

  - id: digest
    agent:
      model: fast
      prompt: |
{_block(DIGEST + chr(10) + chr(10) + "The team lead's decisions are in the team_lead context file.", 8)}
      context_files:
        team_lead: "{{{{ team_lead }}}}"
    output_mode: text
    depends_on: [team_lead]

  # TODO: ops attached digest.md as the run's report with digest_report.sh. S9 reports aren't built, so the digest
  # is this step's output only.
"""


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    repo = getattr(project, "repo", None) or ""
    branch = REVIEW_BRANCHES.get(repo) or getattr(project, "default_branch", None)
    if repo not in PLATFORMS or not branch:
        raise MigrateError(f"repo-review runs only on the FamilySearch repositories, not {repo or 'this project'}.")
    inputs = {"repo": f"https://github.com/{repo}.git", "ref": branch}
    config = {"name": f"Daily review of {repo}", "cron": CRON, "timezone": TIMEZONE, "inputs": inputs,
              "project_id": project.id}
    return Rows(name="repo-review", kind="other", body=body(repo), review_roles=True,
                polls=(Poll("schedule", config),),
                notes=("The schedule starts disabled. Enable it on the Triggers page after a dry run.",))

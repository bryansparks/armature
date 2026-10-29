"""pr-merged: a merged pull request moves its Jira issue to QA, or resolves a Defect as Fixed.

Replaces ops' merge branch of ``/gh/pr``, ``render.sh`` with ``templates/pr_merged_comment.txt``, and
``jira_transition.sh`` with the ``JIRA_TRANSITION_*`` values from ``config/ops.env.example``. Each project gets
its own copy, and the ``jira.transition_qa`` setting stands in for ops' ``JIRA_TRANSITION_QA_<PLATFORM>``.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from armature.server.defs import TEMPLATE_MARKER
from armature.server.migrate_ops import MigrateError, Rows, Rule, Webhook

QA_SETTING = "jira.transition_qa"
QA_DEFAULT = "In Test"
QA_MAX = 100

BODY = """\
name: pr-merged
version: "1.0"
description: "On a merged PR, move its Jira issue to QA Review, or resolve it as Fixed if it is a Defect."
contracts:
  inputs:
    - {name: repo, type: string, default: ""}
    - {name: number, type: string, default: ""}
    - {name: title, type: string, default: ""}
    - {name: body, type: string, default: ""}
    - {name: head_ref, type: string, default: ""}
    - {name: base_ref, type: string, default: ""}
    - {name: merged_by, type: string, default: ""}
    - {name: html_url, type: string, default: ""}
  timeout_hours: 0.2
stages:
  - id: transition
    adapter: jira/transition
    with:
      key_from: ["{{ head_ref }}", "{{ title }}", "{{ body }}"]
      to: "In Test"
      from: In Progress
      defect_to: Resolve Issue
      defect_resolution: Fixed
      projects: [MEMTEMDIS]
      comment: |-
        Merged {{ repo }}#{{ number }} by {{ merged_by }}.

        {{ title }}
        {{ html_url }}

        Moved automatically on merge.
"""

WEBHOOK = Webhook("github", "github")
NEXT = ("Add the github webhook URL to each repository's GitHub webhooks, or link it to the team's GitHub App "
        "connection.")

# A plain close is the same action with merged false and must not move the issue.
CONFIG = {
    "events": ["pull_request"],
    "project_from": "repository.full_name",
    "conditions": [{"path": "action", "op": "eq", "value": "closed"},
                   {"path": "pull_request.merged", "op": "eq", "value": True}],
    "inputs": {"repo": "$.repository.full_name", "number": "$.pull_request.number",
               "title": "$.pull_request.title", "body": "$.pull_request.body",
               "head_ref": "$.pull_request.head.ref", "base_ref": "$.pull_request.base.ref",
               "merged_by": "$.pull_request.merged_by.login", "html_url": "$.pull_request.html_url"},
}


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    qa = setting(QA_SETTING)
    if qa is not None and (not isinstance(qa, str) or not 0 < len(qa) <= QA_MAX or TEMPLATE_MARKER.search(qa)):
        raise MigrateError(f"The {QA_SETTING} setting must be a Jira status or transition name of at most "
                           f"{QA_MAX} characters.")
    body = BODY.replace(f'      to: "{QA_DEFAULT}"\n', f"      to: {json.dumps(qa)}\n") if qa else BODY
    return Rows(name="pr-merged", kind="merge", body=body, rules=(Rule(WEBHOOK, CONFIG),),
                notes=(NEXT,))

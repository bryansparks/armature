"""slack-intake: a message in the intake channel becomes a new Jira issue or a comment on one, with a thread reply.

Replaces ops' ``/slack/events`` channel branch and specs/slack-intake.yml. Each message is a turn of an S6B
conversation, which reads the thread through the conversation tools and replies in it.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from armature.server.migrate_ops import MigrateError, Rows, Rule, Webhook
from armature.server.migrate_ops.ported import DIR

CHANNEL_SETTING = "slack.intake_channel"
CHANNEL = re.compile(r"^[CG][A-Z0-9]{2,40}$")
WEBHOOK = Webhook("slack", "slack")
NEXT = "Point the Slack app's event Request URL at the slack webhook URL, after the shadow and dry stages."
INSTRUCTIONS = (DIR / "prompt/slack_intake_classify.md").read_text().split("Message from", 1)[0].rstrip()
PRINCIPLES = (DIR / "prompt/slack_intake_principles.md").read_text().strip()
MISSION = (DIR / "mission/slack-intake.md").read_text().strip()

SCHEMA = {"type": "object", "required": ["intent", "confidence", "summary", "description", "issue_type", "issue_key"],
          "properties": {"intent": {"type": "string", "enum": ["create", "comment", "ignore"]},
                         "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                         "summary": {"type": "string"}, "description": {"type": "string"},
                         "issue_type": {"type": "string", "enum": ["Defect", "Task", "Enhancement"]},
                         "issue_key": {"type": "string"}}}


def _indented(text: str, spaces: int) -> str:
    return "\n".join((" " * spaces + line) if line else "" for line in text.splitlines())


def body(site: str) -> str:
    prompt = (f"{INSTRUCTIONS}\n\n{PRINCIPLES}\n\nThe messages context file holds the thread, fenced as data. "
              "Classify only its newest message, and the name it gives as that message's author.")
    return f"""\
name: slack-intake
version: "1.0"
description: "Turn a Slack post into a new Jira issue or a comment on an existing one, and reply in its thread."
mission: {json.dumps(MISSION)}
contracts:
  inputs:
    - {{name: conversation_id, type: string, required: true}}
    - {{name: message_seq, type: integer, required: true}}
  timeout_hours: 0.25
stages:
  - id: history
    adapter: conversation/history
    depends_on: []

  - id: classify
    agent:
      model: lead
      prompt: |
{_indented(prompt, 8)}
      context_files:
        messages: "{{{{ history.messages }}}}"
    output_mode: guided_json
    output_schema: {json.dumps(SCHEMA)}
    depends_on: [history]

  - id: created
    skip_if: "{{{{ classify.intent != 'create' or classify.confidence < 0.7 }}}}"
    adapter: jira/create
    with:
      type: "{{{{ classify.issue_type }}}}"
      summary: "{{{{ classify.summary }}}}"
      description: "{{{{ classify.description }}}}"
    depends_on: [classify]

  - id: commented
    skip_if: "{{{{ classify.intent != 'comment' or classify.confidence < 0.7 }}}}"
    adapter: jira/comment
    with:
      issue: "{{{{ classify.issue_key }}}}"
      body: "{{{{ classify.description }}}}"
    depends_on: [classify]

  - id: reply_created
    skip_if: "{{{{ not created.created }}}}"
    adapter: conversation/reply
    with:
      text: "{{{{ created.url }}}}"
    depends_on: [created]

  - id: reply_commented
    skip_if: "{{{{ not commented.commented }}}}"
    adapter: conversation/reply
    with:
      text: "{site}/browse/{{{{ commented.issue }}}}"
    depends_on: [commented]
"""


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    channel = setting(CHANNEL_SETTING)
    if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
        raise MigrateError(f"Set the {CHANNEL_SETTING} setting to the intake channel's id first. Ops ignores every "
                           "message while SLACK_INTAKE_CHANNEL is unset, and so does this import.")
    if not isinstance(setting("jira.project"), str):
        raise MigrateError("Set the jira.project setting to the Jira project new issues go in, such as MEMTEMDIS. "
                           "jira.create reads it, as ops read JIRA_PROJECT.")
    site = setting("jira.site_url")
    if not isinstance(site, str) or not site.startswith("https://"):
        raise MigrateError("Set the jira.site_url setting, such as https://familysearch.atlassian.net, so the reply "
                           "links the issue as ops' did.")
    config = {"events": ["message"], "conversation": True,
              "conditions": [{"path": "event.channel", "op": "eq", "value": channel}]}
    return Rows(name="slack-intake", kind="other", body=body(site.rstrip("/")),
                rules=(Rule(WEBHOOK, config),),
                notes=(NEXT,))

"""build-announce: CI posts a TestFlight or Play beta build, and Slack hears about it.

Replaces ops' ``/ci/build`` route, ``render.sh`` with ``templates/build_announce.txt``, and ``slack_post.sh``.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from armature.server.migrate_ops import MigrateError, Rows, Rule, Webhook

CHANNEL_SETTING = "slack.build_channel"
FIELDS = ("app", "version", "build", "track", "notes", "url")

BODY = """\
name: build-announce
version: "1.0"
description: "Announce a TestFlight or Play beta build in Slack when CI reports it."
contracts:
  inputs:
    - {name: channel, type: string, required: true}
    - {name: app, type: string, default: ""}
    - {name: version, type: string, default: ""}
    - {name: build, type: string, default: ""}
    - {name: track, type: string, default: ""}
    - {name: notes, type: string, default: ""}
    - {name: url, type: string, default: ""}
  timeout_hours: 0.2
stages:
  - id: post
    adapter: slack/post
    with:
      channel: "{{ channel }}"
      unfurl: true
      # Ops strips the rendered message, so a blank notes or url leaves no trailing blank lines.
      text: |-
        :rocket: *{{ app }}* {{ version }} ({{ build }}) is on {{ track }}{{
          "\\n\\n" if url | trim or notes | trim else "" }}{{
          notes if url | trim else notes | trim }}{{
          "\\n\\n" if url | trim else "" }}{{ url | trim }}
"""

WEBHOOK = Webhook("ci-build", "generic", {"generic_auth": "bearer", "secret_name": "CI_WEBHOOK_TOKEN"})


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    channel = setting(CHANNEL_SETTING)
    if not isinstance(channel, str) or not channel:
        raise MigrateError(f"Set the {CHANNEL_SETTING} setting to the Slack channel id first. Ops leaves "
                           "SLACK_BUILD_CHANNEL blank so the workflow fails closed, and so does this import.")
    # The channel is copied into the rule, so changing the setting later needs another import.
    inputs = {"channel": channel, **{name: f"$.{name}" for name in FIELDS}}
    return Rows(name="build-announce", kind="other", body=BODY, rules=(Rule(WEBHOOK, {"inputs": inputs}),),
                notes=("Point CI's build hook at the ci-build webhook URL with the same bearer token.",))

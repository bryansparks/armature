"""localization: translate the strings a pull request added or changed into every language its app supports, and
commit the result, by default without pushing it.

Replaces ops' bin/localization and specs/localization.yml. Like ops, it runs by hand, from the workflow's Run button.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from armature.server.migrate_ops import MigrateError, Rows
from armature.server.migrate_ops.ported import DIR

APPS = ("fs-eng/mobile-ios", "fs-eng/android-tree")
MODES = ["draft", "preview_branch"]
NEXT = ("Run it from the workflow's page with the pull request's head branch as ref and its base branch as base. The "
        "translate model alias picks the model per project.")
TRANSLATE = (DIR / "prompt/localization_translate.md").read_text()
REPLACED = {
    "The files to edit are listed below, one section per file, each with the keys\nto write in it. Edit those files "
    "and nothing else.":
        "Find the user-facing strings this pull request added, changed, or deleted, by\ncomparing its head with "
        "{{ base }}, then edit every language file this app keeps\nfor them, and nothing else.",
    "Every key below appears in exactly one of written or skipped.":
        "Every key you found appears in exactly one of written or skipped. Remove a\ndeleted key from every language "
        "file. When you are done, commit the result in one\ncommit, and never push.",
}
SCHEMA = {"type": "object", "required": ["written", "skipped"],
          "properties": {"written": {"type": "array", "items": {"type": "object", "required": ["file", "key"],
                                                                "properties": {"file": {"type": "string"},
                                                                               "key": {"type": "string"}}}},
                         "skipped": {"type": "array", "items": {"type": "object",
                                                                "required": ["file", "key", "reason"],
                                                                "properties": {"file": {"type": "string"},
                                                                               "key": {"type": "string"},
                                                                               "reason": {"type": "string"}}}}}}


def prompt() -> str:
    text = TRANSLATE
    for old, new in REPLACED.items():
        if old not in text:
            raise ValueError(f"localization_translate.md changed: {old[:40]!r} is gone")
        text = text.replace(old, new)
    return text.strip()


def _block(text: str, spaces: int) -> str:
    return "\n".join((" " * spaces + line) if line else "" for line in text.splitlines())


def body() -> str:
    return f"""\
name: localization
version: "1.0"
description: "Translate the strings a pull request added or changed into every language its app supports, and commit the result."
contracts:
  inputs:
    - {{name: repo, type: string, required: true}}
    - {{name: slug, type: string, required: true}}
    - {{name: number, type: integer, required: true}}
    - {{name: ref, type: string, required: true}}
    - {{name: base, type: string, required: true}}
    - {{name: mode, type: string, default: draft, enum: {json.dumps(MODES)}}}
  timeout_hours: 3
stages:
  # TODO: ops found the strings with localization_strings.py (detect), ran one session per language, removed deleted
  # keys (prune), and checked placeholders and escapes (check), all as FamilySearch pack scripts. No FamilySearch pack
  # exists on the server yet, so one session does all of it.
  - id: translate
    agent:
      model: translate
      access: write
      prompt: |
{_block(prompt(), 8)}
    output_mode: guided_json
    output_schema: {json.dumps(SCHEMA)}
    depends_on: []

  # draft, ops' default, keeps the commit in the run. TODO: ops saved it as a patch file, which needs S9 reports.
  - id: preview
    skip_if: "{{{{ mode != 'preview_branch' or not translate.commit }}}}"
    adapter: github/push
    with:
      repo: "{{{{ slug }}}}"
      branch: "armature/l10n/pr-{{{{ number }}}}"
      commit: "{{{{ translate.commit }}}}"
      worktree: "{{{{ translate.worktree }}}}"
    depends_on: [translate]
"""


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    if getattr(project, "repo", None) not in APPS:
        raise MigrateError(f"localization translates only the apps: {', '.join(APPS)}.")
    return Rows(name="localization", kind="other", body=body(),
                notes=(NEXT,))

"""`armature emit` without the rest of Armature: `python -m armature.events`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from armature.events.emitter import ARTIFACT_TYPES, artifact_problem, get_emitter
from armature.events.summary import link_url


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="armature emit",
        description="Send an event to ARMATURE_EVENTS. Does nothing when it is unset.",
    )
    parser.add_argument("kind", nargs="?", help="Event kind, such as step_start or finding")
    parser.add_argument("--step", default=os.environ.get("ARMATURE_STEP_ID") or None,
                        help="Step id the event belongs to (default: ARMATURE_STEP_ID)")
    parser.add_argument("--json", dest="json_payload", help="Payload as a JSON object")
    parser.add_argument("--field", dest="fields", action="append", default=[],
                        help="Payload field as key=value, repeatable")
    parser.add_argument("--name", help="artifact: the artifact's name, such as review.md")
    parser.add_argument("--type", dest="media_type", default="text/markdown", choices=ARTIFACT_TYPES,
                        help="artifact: the content's media type (default: text/markdown)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", help="artifact: read the content from this file")
    source.add_argument("--text", help="artifact: the content itself")
    parser.add_argument("--link", dest="links", action="append", default=[],
                        help="summary: a link as label=url, https or a same-origin path, repeatable")
    parser.add_argument("--stream-claude", action="store_true",
                        help="Read Claude Code stream-json on stdin, emit its events with each --field, and copy stdin to stdout")
    return parser


def _payload(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.json_payload:
        try:
            payload = json.loads(args.json_payload)
        except ValueError as exc:
            parser.error(f"--json is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            parser.error("--json must be a JSON object")
    for item in args.fields:
        key, sep, value = item.partition("=")
        if not sep or not key:
            parser.error(f"--field must be key=value, got {item!r}")
        try:
            payload[key] = json.loads(value)
        except ValueError:
            payload[key] = value
    return payload


def _summary(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict[str, Any]:
    """title, verdict, and outcome as given, every other --field under fields, and each --link."""
    payload: dict[str, Any] = {}
    if args.json_payload:
        payload = _payload(parser, argparse.Namespace(json_payload=args.json_payload, fields=[]))
    fields = dict(payload.get("fields") or {})
    for item in args.fields:
        key, sep, value = item.partition("=")
        if not sep or not key:
            parser.error(f"--field must be key=value, got {item!r}")
        if key in ("title", "verdict", "outcome"):
            payload[key] = value
        else:
            fields[key] = value
    if fields:
        payload["fields"] = fields
    links = list(payload.get("links") or [])
    for item in args.links:
        label, sep, url = item.partition("=")
        if not sep or not label or link_url(url) is None:
            parser.error(f"--link must be label=url with an https URL or a same-origin path, got {item!r}")
        links.append({"label": label, "url": url})
    if links:
        payload["links"] = links
    return payload


def _artifact(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.json_payload or args.fields:
        parser.error("an artifact takes --name with --file or --text, not --json or --field")
    if not args.name or (args.file is None and args.text is None):
        parser.error("an artifact needs --name and one of --file or --text")
    if args.file is not None:
        try:
            with open(args.file, encoding="utf-8") as handle:
                content = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            parser.error(f"could not read --file: {exc}")
    else:
        content = args.text
    problem = artifact_problem(args.name, args.media_type, content)
    if problem:
        parser.error(problem)
    emitter = get_emitter()
    emitter.artifact(args.name, content, args.media_type, args.step)
    emitter.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.stream_claude:
        from armature.events.claude_stream import pipe
        fields = _payload(parser, args)
        emitter = get_emitter()
        pipe(emitter, args.step, sys.stdin.buffer, sys.stdout.buffer, fields)
        emitter.close()
        return 0
    if not args.kind:
        parser.error("KIND is required unless --stream-claude is set")
    if args.kind == "artifact":
        return _artifact(parser, args)
    if args.name or args.file is not None or args.text is not None:
        parser.error("--name, --file, and --text go with the artifact kind")
    if args.links and args.kind != "summary":
        parser.error("--link goes with the summary kind")
    payload = _summary(parser, args) if args.kind == "summary" else _payload(parser, args)
    emitter = get_emitter()
    emitter.emit(args.kind, args.step, **payload)
    emitter.close()
    return 0

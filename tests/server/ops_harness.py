"""The recorded-trigger harness: an ops fixture delivery through the imported rules and workflow, in dry mode.

A fixture under tests/fixtures/ops/<workflow>/ holds a webhook delivery, the
vendor state it meets, and the ``ops`` block capture.py recorded from ops.
``replay`` runs the delivery through the team's webhook rules, then each
matched workflow under the real engine. Each built-in tool step runs the real
tool in process against fake vendors and returns its real result, so later
steps branch as a live run would, and the harness sees the writes that run
would make. A conversation tool records its reply. An agent stage answers with
the fixture's canned output and records what it was given. Nothing leaves the
process.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from sqlalchemy import select

from armature.nodes.agent import AgentNode
from armature.nodes.agent import _render as render_context
from armature.nodes.gate import GATE_DIR_ENV, GateSuspended, HumanGateNode
from armature.nodes.script import ScriptNode
from armature.runner.process import WORKFLOW_FILE, write_bundle
from armature.runtime.engine import Harness
from armature.server import broker, calls, defs, sanitize, vendors, webhooks
from armature.server.db.schema import webhooks as webhooks_table
from armature.server.dryrun import stored_body
from armature.server.tools import Call, registry
from armature.spec.loader import load_spec
from tests.server.tool_support import Vendors

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ops"
SITE = "https://familysearch.atlassian.net"
SLACK_TS = "1700000000.000100"
CONVERSATION = {"conversation_id": "conversation-1", "message_seq": 1}
# ops' JIRA_PROJECT, which the jira.project setting carries on the platform.
JIRA_PROJECT = "MEMTEMDIS"
# ops' slack_post.sh sent no unfurl flags, so Slack previewed links and media.
OPS_UNFURL = {"unfurl_links": True, "unfurl_media": True}
SLACK_KEYS = ("channel", "text", "thread_ts", "unfurl_links", "unfurl_media")
CONNECTIONS = {
    "slack": ({"bot_token": "xoxb-harness"}, {}),
    "jira": ({"api_token": "jira-harness"}, {"site_url": SITE, "email": "bot@familysearch.test"}),
}


@dataclass
class Replay:
    runs: list[tuple[str, str | None]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    paused: str | None = None
    replies: list[str] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None


def fixtures(workflow: str) -> list[Path]:
    return sorted((FIXTURES / workflow).glob("*.json"))


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _decisions(engine, team_id: str, fixture: dict[str, Any]) -> list[dict[str, Any]]:
    source, headers, payload = fixture["source"], fixture["headers"], fixture["payload"]
    event = webhooks.event_of(source, headers, payload)
    with engine.connect() as conn:
        hooks = conn.execute(select(webhooks_table).where(webhooks_table.c.team_id == team_id,
                                                          webhooks_table.c.source == source)).all()
        return [d for hook in hooks for d in webhooks.evaluate(conn, team_id, source,
                                                               webhooks.rules_of(conn, hook.id), event, payload)
                if d["matched"]]


def _workflow(engine, workflow_id: str) -> tuple[Any, str, dict[str, str]]:
    """The workflow row, its body, and its call bundle, as the server hands them to a job."""
    with engine.connect() as conn:
        row = defs.get_row(conn, defs.WORKFLOW, workflow_id)
        body = defs.body_of(conn, defs.WORKFLOW, row.id, row.current_version)
        return row, body, calls.bundle(conn, row.scope, body, row.name, None, stored_body)


def _run(body: str, bundle: dict[str, str], inputs: dict[str, Any], tmp: Path, monkeypatch, vendor: Vendors,
         agents: dict[str, Any], out: Replay, pause: bool = False) -> None:
    """Run the body. With ``pause`` a human gate suspends the run as on a server, instead of answering."""
    tmp.mkdir(parents=True)
    original = ScriptNode.execute

    async def script(node, context):
        cmd = node._adapter.cmd or ""
        if node._adapter.pack_dir is None or not cmd.startswith("armature tool "):
            return await original(node, context)
        call = {"step": node._step_id, "tool": cmd.split()[2], "params": json.loads(node._env["ARMATURE_WITH"])}
        out.calls.append(call)
        out.result = _tool(vendor, call, out)
        return out.result

    async def agent(node, context):
        stage = node._stage
        out.sessions.append({"stage": stage.id, "role": stage.agent.role, "model": stage.agent.model,
                             "access": stage.agent.access, "prompt": stage.agent.prompt,
                             "context_files": {name: render_context(text, context)
                                               for name, text in stage.agent.context_files.items()}})
        if stage.id not in agents:
            raise AssertionError(f"no canned output for agent stage {stage.id}")
        # A list answers the stage's runs in order, as when called workflows run the same stage again.
        found = agents[stage.id]
        return copy.deepcopy(found.pop(0) if isinstance(found, list) else found)

    for name in ("ARMATURE_TOOLS_URL", "ARMATURE_EVENTS", "ARMATURE_EVENTS_TOKEN", "ARMATURE_SERVER",
                 "ARMATURE_STUB_OUTPUTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp / "home"))
    monkeypatch.setattr(ScriptNode, "execute", script)
    monkeypatch.setattr(AgentNode, "execute", agent)

    async def gate(node, context):
        out.gates.append(node._stage.id)
        return {"approved": agents.get(f"gate:{node._stage.id}", True), "feedback": None}

    if pause:
        monkeypatch.setenv(GATE_DIR_ENV, str(tmp / "gate"))
    else:
        monkeypatch.delenv(GATE_DIR_ENV, raising=False)
        monkeypatch.setattr(HumanGateNode, "execute", gate)
    write_bundle(tmp, stored_body(body), bundle)
    path = tmp / WORKFLOW_FILE
    harness = Harness(load_spec(path, vars=inputs), session_dir=tmp / "session", traces_db=tmp / "traces.db",
                      use_cache=False)
    try:
        asyncio.run(harness.run(inputs))
    except GateSuspended as paused:
        out.paused = paused.stage.id


def _effects(vendor: Vendors, transitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = {str(t["id"]): t["name"] for t in transitions}
    out = []
    for request in vendor.writes():
        path, body = request.url.path, vendor.body(request)
        found = re.fullmatch(r"/rest/api/3/issue/([A-Z][A-Z0-9]+-\d+)/(transitions|comment)", path)
        if path == "/rest/api/3/issue":
            fields = body["fields"]
            out.append({"vendor": "jira", "op": "create", "project": fields["project"]["key"],
                        "type": fields["issuetype"]["name"], "summary": fields["summary"],
                        "paragraphs": [_lines(p["content"]) for p in (fields.get("description") or {}).get("content", [])]})
        elif path == "/api/chat.postMessage":
            out.append({"vendor": "slack", "op": "post", **{k: body[k] for k in SLACK_KEYS if k in body}})
        elif found and found.group(2) == "transitions":
            made = {"vendor": "jira", "op": "transition", "issue": found.group(1),
                    "transition": names[body["transition"]["id"]]}
            resolution = ((body.get("fields") or {}).get("resolution") or {}).get("name")
            out.append({**made, **({"resolution": resolution} if resolution else {})})
        elif found:
            out.append({"vendor": "jira", "op": "comment", "issue": found.group(1),
                        "paragraphs": [_lines(p["content"]) for p in body["body"]["content"]]})
        else:
            out.append({"vendor": "other", "method": request.method, "path": path, "body": body})
    return out


def _lines(nodes: list[dict[str, Any]]) -> list[str]:
    lines = [""]
    for node in nodes:
        if node["type"] == "hardBreak":
            lines.append("")
        else:
            lines[-1] += node["text"]
    return lines


def _tool(vendor: Vendors, call: dict[str, Any], out: Replay) -> dict[str, Any]:
    params = broker._checked(call["tool"], call["params"])
    if call["tool"] == "conversation.history":
        return {"messages": "", "proposal": None, "state": {}}
    if call["tool"] in ("conversation.reply", "conversation.ask"):
        out.replies.append(params["text"])
        return {"posted": True}
    tool = registry()[call["tool"]]
    creds, config = CONNECTIONS[tool.kind]
    if call["tool"] == "slack.post":
        vendor.slack = {"ok": True, "channel": params["channel"], "ts": SLACK_TS}
    ctx = Call(run_id="dry-run", step_id=call["step"], connection=SimpleNamespace(config=config, identity={}),
               creds=creds, now=datetime.now(UTC), transport=httpx.MockTransport(vendor),
               policy={"project": JIRA_PROJECT} if call["tool"] == "jira.create" else {})
    return tool.run(ctx, params)


def run_stored(engine, workflow_id: str, inputs: dict[str, Any], tmp: Path, monkeypatch,
               agents: dict[str, Any] | None = None, pause: bool = False) -> Replay:
    """Run one stored workflow as a trigger with no delivery would, such as a schedule or a poll."""
    monkeypatch.setattr(vendors, "resolve_host", lambda host: ["140.82.112.5"])
    vendor = Vendors()
    out = Replay()
    row, body, bundle = _workflow(engine, workflow_id)
    out.runs.append((row.name, row.project_id))
    _run(body, bundle, inputs, tmp / "run-0", monkeypatch, vendor, agents or {}, out, pause)
    out.effects = _effects(vendor, [])
    return out


def replay(engine, team_id: str, fixture: dict[str, Any], tmp: Path, monkeypatch,
           agents: dict[str, Any] | None = None) -> Replay:
    """Run the fixture's delivery through the team's rules and each workflow they match, and the tools it calls."""
    monkeypatch.setattr(vendors, "resolve_host", lambda host: ["140.82.112.5"])
    jira = fixture.get("jira") or {}
    vendor = Vendors()
    vendor.jira_issue, vendor.jira_transitions = jira.get("issue"), jira.get("transitions") or []
    out = Replay()
    for index, decision in enumerate(_decisions(engine, team_id, fixture)):
        row, body, bundle = _workflow(engine, decision["workflow_id"])
        out.runs.append((row.name, decision["project_id"]))
        inputs = CONVERSATION if decision.get("conversation") else decision["inputs"]
        _run(body, bundle, inputs, tmp / f"run-{index}", monkeypatch, vendor, agents or {}, out)
    out.effects = _effects(vendor, vendor.jira_transitions)
    return out


def _expected(effect: dict[str, Any]) -> dict[str, Any]:
    if effect["vendor"] == "slack":
        return {**OPS_UNFURL, **effect, "text": sanitize.slack(effect["text"])}
    if effect["op"] in ("comment", "create"):
        made = {k: v for k, v in effect.items() if k != "body"}
        return {**made, "paragraphs": [p.split("\n") for p in re.split(r"\n\s*\n", effect["body"])]}
    return effect


def expected_effects(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Ops' effects as the platform sends them: Slack text escaped, since the server never posts raw mrkdwn, and a
    comment as Jira holds it, in paragraphs of lines."""
    return [_expected(e) for e in fixture["ops"]["effects"]]


def same_result(ops: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
    """Whether two tool results agree, reason wording aside: ops names its env vars where the tool names params."""
    if ops is None or new is None:
        return ops is new
    drop = ("reason", "detail")
    return ({k: v for k, v in ops.items() if k not in drop} == {k: v for k, v in new.items() if k not in drop}
            and ("reason" in ops) == ("reason" in new))

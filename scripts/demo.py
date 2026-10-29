"""Seed and serve a local Armature demo with replayed sample runs. Used by scripts/demo.sh.

    python scripts/demo.py seed  <data dir>   # config, key, schema, sample data, replayed runs
    python scripts/demo.py serve <data dir>   # the server in the foreground, offline

Runs are replayed through the server's own ingest (LiveHub), so the UI draws them as it draws real runs. Nothing
here calls an LLM, AWS, or a vendor: serve swaps vendor checks and the AWS loop for offline stand-ins.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import insert, select, update

from armature.events import location
from armature.events.sign import sign_engine
from armature.nodes.gate import gate_digest
from armature.server import audit, connections, defs, display, environments, gates, rbac, risk, runners, webhooks
from armature.server.config import ServerConfig, load_config, write_config
from armature.server.crypto import keyring_from_source, write_key_file
from armature.server.db import make_engine, migrate, new_id, utcnow
from armature.server.db.schema import aws_connections, jobs, projects, teams, triggers
from armature.server.db.schema import connections as connection_rows
from armature.server.ingest import LiveHub
from armature.server.users import create_admin

PORT = 8197
ORIGIN = f"http://127.0.0.1:{PORT}"
USERNAME = "demo"
PASSWORD = "demo-armature"
TEAM = "FamilySearch"
TEAM_SLUG = "familysearch"
PROJECTS = (("mobile-ios", "ios", "fs-eng/mobile-ios"), ("android-tree", "android", "fs-eng/android-tree"),
            ("mobile-api-nodejs", "node", "fs-eng/mobile-api-nodejs"))
SPECIALISTS = ("simplicity", "correctness", "security", "performance", "beauty", "test_adequacy", "concurrency",
               "conventions", "compatibility")
LIVE_FILE = "live-run.json"


def note(text: str) -> None:
    print(f"  {text}", flush=True)


def skip(text: str) -> None:
    print(f"  skipped: {text}", flush=True)


def iso(value: datetime) -> str:
    return value.isoformat()


# Config, key, and schema.

def write_setup(data: Path) -> ServerConfig:
    data.mkdir(parents=True, exist_ok=True)
    path = data / "server.yaml"
    cfg = ServerConfig(database_url=f"sqlite:///{data / 'server.db'}", public_origin=ORIGIN, host="127.0.0.1",
                       port=PORT, require_totp=False, master_key=f"file:{data / 'master.key'}",
                       events_socket="", packs_dir=str(data / "packs"),
                       lost_after_minutes=24 * 60)
    write_config(cfg, path)
    write_key_file(data / "master.key")
    migrate(cfg.database_url)
    return load_config(path)


# Instance data.

def seed_people(conn, keyring, now: datetime) -> tuple[str, dict[str, str], str]:
    admin = create_admin(conn, keyring, username=USERNAME, password=PASSWORD, require_totp=False)
    team_id = new_id()
    conn.execute(insert(teams).values(id=team_id, name=TEAM, slug=TEAM_SLUG, created_at=now))
    audit.record(conn, action="team.create", actor_id=admin.user_id, team_id=team_id, target_kind="team",
                 target_id=team_id, detail={"name": TEAM})
    project_ids = {}
    for name, platform, repo in PROJECTS:
        project_ids[name] = new_id()
        conn.execute(insert(projects).values(id=project_ids[name], team_id=team_id, name=name, platform=platform,
                                             repo=repo, default_branch="main", created_at=now))
        audit.record(conn, action="project.create", actor_id=admin.user_id, team_id=team_id, target_kind="project",
                     target_id=project_ids[name], detail={"name": name, "repo": repo})
    note(f"admin {USERNAME}, team {TEAM}, projects {', '.join(repo for _, _, repo in PROJECTS)}")
    return admin.user_id, project_ids, team_id


def _dummy_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


IDENTITIES = {
    "github_app": {"slug": "armature-familysearch", "bot_login": "armature-familysearch[bot]", "bot_user_id": 4242,
                   "app_id": 912345},
    "slack": {"team": "FamilySearch", "team_id": "T0DEMO0001", "bot_user": "armature", "bot_user_id": "U0DEMO0001",
              "bot_id": "B0DEMO0001", "scopes": ["chat:write", "reactions:write", "channels:history"]},
    "jira": {"account_id": "5f0demo0000000000000001", "display": "Armature Bot"},
}


def seed_connections(conn, keyring, user_id: str, team_id: str, now: datetime) -> dict[str, str]:
    bodies = {
        "github_app": {"kind": "github_app", "name": "GitHub", "client_id": "Iv23demo00000001", "app_id": "912345",
                       "private_key_pem": _dummy_pem(), "webhook_secret": "demo-github-webhook-secret"},
        "slack": {"kind": "slack", "name": "Slack", "team_id": "T0DEMO0001", "bot_token": "demo-slack-bot-token",
                  "signing_secret": "demo-slack-signing-secret"},
        "jira": {"kind": "jira", "name": "Jira", "site_url": "https://familysearch.atlassian.net",
                 "email": "armature-bot@familysearch.org", "api_token": "demo-jira-api-token",
                 "webhook_secret": "demo-jira-webhook-secret"},
    }
    ids = {}
    for kind, body in bodies.items():
        row = connections.set_connection(conn, keyring, team_id=team_id, project_id=None, row=None, body=body,
                                         actor_id=user_id, now=now, ip=None)
        conn.execute(update(connection_rows).where(connection_rows.c.id == row.id).values(
            state="ok", verified_at=now, identity=IDENTITIES[kind]))
        ids[kind] = row.id
    conn.execute(update(projects).where(projects.c.team_id == team_id).values(connection_id=ids["github_app"]))
    note("connections GitHub, Slack, and Jira, verified, with dummy sealed credentials")
    return ids


def seed_aws(conn, keyring, user_id: str, now: datetime) -> dict[str, str]:
    environments.set_connection(conn, keyring, {
        "auth_kind": "access_key", "region": "us-east-1", "access_key_id": "AKIADEMO0000000000",
        "secret_access_key": "demo-secret-access-key-not-real", "vpc_id": "vpc-0de0000000000001",
        "subnets": [{"zone": "us-east-1a", "subnet_id": "subnet-0de0000000000001"},
                    {"zone": "us-east-1b", "subnet_id": "subnet-0de0000000000002"}],
        "security_group_id": "sg-0de0000000000001", "instance_profile": "armature-platform-runner",
        "mac_instance_profile": "armature-platform-mac", "launch_template_ids": {"linux": "lt-0de0000000000001",
                                                                                 "mac": "lt-0de0000000000002"},
        "allowed_instance_types": ["m7i.large", "m7i.xlarge", "mac2.metal", "mac2-m2.metal"],
        "stack_name": "armature-platform", "artifact_bucket": "armature-platform-demo-artifacts",
        "max_concurrent": 6, "max_daily_launches": 60, "daily_budget_usd": "150",
    }, actor_id=user_id, now=now, ip=None)
    conn.execute(update(aws_connections).values(state="ok", verified_at=now))
    types = {}
    for body in (
        {"name": "linux-node", "kind": "ec2-linux", "image": "ami-0de0000000000001", "instance_types": ["m7i.large"],
         "capabilities": ["git", "node", "claude-code"], "job_timeout_s": 3600, "hourly_cost_usd": "0.1008"},
        {"name": "android-gradle", "kind": "ec2-linux", "image": "ami-0de0000000000002",
         "instance_types": ["m7i.xlarge"], "capabilities": ["git", "android", "gradle", "claude-code"],
         "job_timeout_s": 5400, "hourly_cost_usd": "0.2016"},
        {"name": "macos-xcode", "kind": "ec2-mac", "image": "ghcr.io/cirruslabs/macos-sequoia-xcode:16.4",
         "instance_types": ["mac2-m2.metal"], "vm_cpu": 4, "vm_memory_gb": 12,
         "capabilities": ["git", "macos", "xcode", "claude-code"], "job_timeout_s": 7200, "hourly_cost_usd": "0.8780"},
    ):
        types[body["name"]] = environments.set_type(conn, body, type_id=None, actor_id=user_id, now=now, ip=None).id
    note("AWS connection, verified, and environment types linux-node, android-gradle, macos-xcode")
    return types


def link_environment_types(conn, project_ids: dict[str, str], types: dict[str, str]) -> None:
    for project, kind in (("mobile-ios", "macos-xcode"), ("android-tree", "android-gradle"),
                          ("mobile-api-nodejs", "linux-node")):
        conn.execute(update(projects).where(projects.c.id == project_ids[project])
                     .values(environment_type_id=types[kind]))


def seed_starters(conn, now: datetime) -> None:
    try:
        from armature.server import starters
    except ImportError:
        skip("S14 starter roles: armature.server.starters isn't on this branch yet")
        return
    added = starters.seed_roles(conn, now)
    note(f"S14 starter roles: {', '.join(added) or 'already present'}")


ADMIN = rbac.Access(is_admin=True)

DEVELOPMENT = """\
name: development
version: "1.0"
description: "Plan, get a lead's approval for risky work, implement on a branch, and open a pull request."
requires: [macos, xcode]

contracts:
  inputs:
    - {name: issue, type: string, required: true}
    - {name: repo, type: string, required: true}
  max_llm_calls: 20
  timeout_hours: 4

stages:
  - id: plan
    agent:
      model: development
      access: read
      prompt: |
        Read issue {{ issue }} and the repository. Write a short plan: the files to change, the risk, and how
        you'll test it.
    output_mode: text
    depends_on: []

  - id: approve_plan
    gate: human
    present: |
      {{ plan.content }}
    approval:
      min_role: project_lead
      expires_hours: 72
    depends_on: [plan]

  - id: implement
    skip_if: "{{ not approve_plan.approved }}"
    agent:
      model: development
      access: write
      prompt: |
        Implement the approved plan for {{ issue }}. Commit on a new branch and build with ./.armature/build.
    output_mode: text
    depends_on: [approve_plan]
"""

CODE_REVIEW = """\
name: code-review
version: "1.0"
description: "Review a pull request with the specialist panel, then let the team lead decide what to post."
requires: [macos, xcode]

contracts:
  inputs:
    - {name: slug, type: string, required: true}
    - {name: number, type: integer, required: true}
    - {name: specialists, type: array, default: [simplicity, correctness, security, performance, beauty, test_adequacy, concurrency, conventions, compatibility]}
  max_llm_calls: 20
  timeout_hours: 2

stages:
  - id: reviews
    agent:
      model: review
      access: read
      prompt: |
        Review pull request {{ number }} of {{ slug }} as the {{ specialist }} specialist.
    output_mode: text
    fan_out: 9
    partition_source: "{{ specialists }}"
    partition_key: specialist
    depends_on: []

  - id: team_lead
    agent:
      model: review
      access: read
      prompt: |
        Decide which of the specialists' findings to keep, drop, or block on for pull request {{ number }}.
    output_mode: text
    depends_on: [reviews]
"""


def ensure_workflow(conn, user_id: str, team_id: str, project_id: str, name: str, body: str, kind: str) -> Any:
    found = defs.resolve_workflow(conn, name, team_id, project_id)
    if found is not None:
        return found
    try:
        row, _ = defs.create(conn, defs.WORKFLOW, access=ADMIN, scope=defs.DefScope("project", team_id, project_id),
                             name=name, body=body, note="Demo workflow.", actor_id=user_id, now=utcnow(), ip=None,
                             doc_kind=kind)
    except defs.DefError as exc:
        errors = (exc.extra.get("validation") or {}).get("errors") or []
        raise SystemExit(f"demo workflow {name} didn't validate: {exc.message} "
                         + "; ".join(e.get("message", "") for e in errors))
    return row


def seed_code_review_workflow(conn, user_id: str, team_id: str, project_id: str) -> Any:
    try:
        from armature.server import starters
        body = starters.body("code-review")
    except (ImportError, KeyError, AttributeError):
        skip("code-review starter isn't on this branch yet, so the demo adds its own code-review workflow")
        return ensure_workflow(conn, user_id, team_id, project_id, "code-review", CODE_REVIEW, "review")
    try:
        for name in ("review-pass", "code-review"):
            ensure_workflow(conn, user_id, team_id, project_id, name, starters.body(name), "review")
        note("code-review starter added to mobile-ios")
        return defs.resolve_workflow(conn, "code-review", team_id, project_id)
    except SystemExit as exc:
        skip(f"code-review starter: {exc}. Using the demo's own")
        return ensure_workflow(conn, user_id, team_id, project_id, "code-review-demo", CODE_REVIEW, "review")


# Webhooks.

def seed_webhook(conn, user_id: str, team_id: str, github_id: str, review_workflow_id: str) -> Any:
    hook = webhooks.create(conn, team_id=team_id, name="github-mobile", body={"source": "github",
                                                                              "connection_id": github_id},
                           actor_id=user_id, now=utcnow(), ip=None)
    webhooks.create_rule(conn, hook, body={"workflow_id": review_workflow_id, "config": {
        "events": ["pull_request"], "shadow": True, "project_from": "repository.full_name",
        "conditions": [{"path": "action", "op": "in", "value": ["opened", "synchronize"]}],
        "inputs": {"slug": "$.repository.full_name", "number": "$.pull_request.number"}}},
        actor_id=user_id, now=utcnow(), ip=None)
    return hook


def held_delivery(cfg: ServerConfig, engine, keyring, hook: Any) -> None:
    """Post a signed pull_request event to the webhook's real URL in-process. A pending webhook holds it."""
    import hmac

    from fastapi.testclient import TestClient

    from armature.server.app import create_app
    from armature.server.app.routes_webhooks import HOOKS_PATH
    payload = {"action": "opened", "number": 486, "sender": {"login": "jkimball-fs", "id": 7781},
               "repository": {"full_name": "fs-eng/mobile-ios", "name": "mobile-ios"},
               "pull_request": {"number": 486, "title": "[FSMOB-1502] Cache tree person portraits on disk",
                                "html_url": "https://github.com/fs-eng/mobile-ios/pull/486",
                                "head": {"ref": "fsmob-1502-portrait-cache", "sha": "9" * 40},
                                "user": {"login": "jkimball-fs"}}}
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"demo-github-webhook-secret", raw, hashlib.sha256).hexdigest()
    app = create_app(cfg, engine, keyring, worker_interval=None)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
        response = client.post(f"{HOOKS_PATH}{hook.path_token}", content=raw, headers={
            "Content-Type": "application/json", "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d1e5a7c0-demo-4c1b-9f00-000000000486", "X-Hub-Signature-256": signature})
    if response.json().get("status") == "held":
        note("webhook github-mobile, pending approval, holding one pull_request delivery")
    else:
        skip(f"held webhook delivery: the server answered {response.status_code} {response.text[:200]}")


def import_ops(data: Path, project_repo: str) -> list[str]:
    """Import the ops workflows with the server's own command, when this branch has it."""
    cli = [sys.executable, "-m", "armature.cli", "server"]
    env = {**os.environ, "ARMATURE_SERVER_CONFIG": str(data / "server.yaml")}
    help_text = subprocess.run([*cli, "--help"], capture_output=True, text=True, env=env).stdout
    if "import-ops" not in help_text:
        skip("import-ops isn't on this branch yet, so pr-merged and build-announce runs use their names only")
        return []
    imported = []
    for workflow in ("build-announce", "pr-merged"):
        done = subprocess.run([*cli, "import-ops", "--workflow", workflow, "--project", project_repo,
                               "--config", str(data / "server.yaml")], capture_output=True, text=True, env=env)
        if done.returncode == 0:
            imported.append(workflow)
            note(f"imported {workflow} with import-ops")
        else:
            skip(f"import-ops {workflow}: {(done.stderr or done.stdout).strip()[:300]}")
    return imported


def rules_dry(conn) -> None:
    if "dry" not in webhooks.RULE_KEYS:
        skip("dry webhook rules aren't on this branch yet, so the rules stay in shadow")
        return
    count = 0
    for rule in conn.execute(select(triggers.c.id, triggers.c.config).where(triggers.c.webhook_id.is_not(None))):
        config = dict(rule.config or {})
        if not config.get("dry"):
            conn.execute(update(triggers).where(triggers.c.id == rule.id).values(config={**config, "dry": True}))
            count += 1
    note(f"webhook rules set to dry ({count} changed)")


def show_ops_runs(conn, user_id: str) -> None:
    """pr-merged and build-announce runs stay out of run lists by default. The demo shows them, labeled."""
    defs.put_setting(conn, team_id=None, key=display.LABELS_KEY, actor_id=user_id, now=utcnow(), ip=None, value={
        "pr-merged": {"label": "PR merged", "color": "", "hidden": False},
        "build-announce": {"label": "Build announce", "color": "", "hidden": False}})


# Replayed runs.

class Replay:
    """Wire events for one run, numbered and timed, sent through a LiveHub like a runner's would be."""

    def __init__(self, token: str, start: datetime, nonce: str):
        self.token = token
        self.at = start
        self.nonce = nonce
        self.count = 0
        self.lines: list[bytes] = []

    def wait(self, seconds: float) -> Replay:
        self.at += timedelta(seconds=seconds)
        return self

    def event(self, kind: str, step: str | None = None, /, **payload: Any) -> Replay:
        self.count += 1
        self.lines.append(json.dumps({"v": 1, "token": self.token, "kind": kind, "step_id": step,
                                      "ts": iso(self.at), "eid": f"{self.nonce}:{self.count}",
                                      "payload": payload}).encode())
        return self


async def send(engine, keyring, replays: list[Replay]) -> None:
    hub = LiveHub(engine, keyring, utcnow)
    await hub.start()
    try:
        for replay in replays:
            for line in replay.lines:
                outcome = await hub.ingest(line)
                if outcome != "accepted":
                    raise SystemExit(f"replayed event refused ({outcome}): {line[:200]!r}")
    finally:
        await hub.stop()


def open_run(conn, keyring, *, team_id, project_id, workflow, name, start, **columns) -> tuple[str, str]:
    row, token = runners.open_run(conn, keyring, team_id=team_id, project_id=project_id,
                                  workflow_id=workflow.id if workflow is not None else None,
                                  workflow_version=workflow.current_version if workflow is not None else None,
                                  workflow_name=name, now=start, **columns)
    return row["id"], token


MAC = location.make(location.AWS_MAC, instance="i-0de0c0de0000ios1", environment_type="macos-xcode",
                    instance_type="mac2-m2.metal")
LINUX = location.make(location.AWS_LINUX, instance="i-0de0c0de0000lnx1", environment_type="linux-node",
                      instance_type="m7i.large")

FINDINGS = [
    # (specialist, path, line, severity, title, body)
    ("correctness", "FamilySearch/Tree/PersonCardViewModel.swift", 118, "high",
     "Lifespan shows the wrong death year when only a burial date is known",
     "`lifespanText` falls back to `burial?.year` but formats it as the birth year, so a person with only a burial "
     "date reads as born that year."),
    ("concurrency", "FamilySearch/Tree/PortraitLoader.swift", 64, "blocking",
     "Portrait cache is mutated off the main actor",
     "`cache[personId] = image` runs inside a detached task, while `PersonCardView` reads `cache` on the main "
     "actor. That's a data race under Swift 6 strict concurrency."),
    ("security", "FamilySearch/Tree/PortraitLoader.swift", 41, "medium",
     "Portrait URLs are logged with their signed query string",
     "`logger.debug(\"loading \\(url)\")` writes the signed CDN token to the device log. Log the path only."),
    ("performance", "FamilySearch/Tree/TreeCanvasView.swift", 212, "medium",
     "Every pan gesture re-lays out all visible person cards",
     "`onChange(of: offset)` invalidates the whole `LazyVGrid`. Only cards entering the viewport need layout."),
    ("test_adequacy", "FamilySearchTests/Tree/PersonCardViewModelTests.swift", None, "medium",
     "No test covers a person with a burial date and no death date",
     "The bug in `lifespanText` would have been caught by one case with `death == nil, burial != nil`."),
    ("beauty", "FamilySearch/Tree/PersonCardView.swift", 88, "low",
     "Lifespan text ignores Dynamic Type",
     "The label uses `.font(.system(size: 12))`. Use `.caption` so it scales with the user's text size."),
    ("simplicity", "FamilySearch/Tree/PortraitLoader.swift", 22, "low",
     "PortraitLoader wraps URLCache in a second in-memory cache",
     "`URLCache` already keeps decoded responses in memory. The extra dictionary doubles memory and adds the race "
     "above."),
    ("conventions", "FamilySearch/Tree/PersonCardView.swift", 15, "low",
     "New view isn't in the Tree feature's preview catalog",
     "CONTRIBUTING.md asks every Tree view to add a `#Preview` with the sample family."),
    ("compatibility", "FamilySearch/Tree/TreeCanvasView.swift", 40, "medium",
     "`onScrollGeometryChange` needs iOS 18, and the app supports iOS 17",
     "Guard it with `if #available(iOS 18, *)` or keep the `GeometryReader` path for iOS 17."),
]

# What the team lead did with each specialist finding: (index, decision, reason, raised_by extra)
DECISIONS = [
    (1, "block", "A data race the Swift 6 build will reject. Fix before merge.", ["concurrency", "simplicity"]),
    (0, "suggest", None, ["correctness", "test_adequacy"]),
    (8, "suggest", None, ["compatibility"]),
    (2, "suggest", None, ["security"]),
    (5, "fixed", "Fixed in the follow-up commit on the branch.", ["beauty"]),
    (3, "drop", "The grid only holds the 30 visible cards, so the relayout is cheap. Measured at 0.4 ms.",
     ["performance"]),
    (7, "drop", "The preview catalog rule is for shared components, and this view is internal to Tree.",
     ["conventions"]),
]


def replay_code_review(conn, keyring, *, team_id, project_id, workflow, start) -> tuple[str, Replay]:
    run_id, token = open_run(conn, keyring, team_id=team_id, project_id=project_id, workflow=workflow,
                             name="code-review", start=start, trigger_kind="pr", actor="mreyes-fs",
                             subject="fs-eng/mobile-ios#482",
                             trigger_ref={"source": "webhook", "repo": "fs-eng/mobile-ios", "pr": 482},
                             location=MAC, head_sha="4b1c0de" + "0" * 33)
    r = Replay(token, start, "demo-review")
    r.event("summary", location=MAC, title="[FSMOB-1423] Show lifespans and portraits on tree person cards",
            links=[{"label": "Pull request", "url": "https://github.com/fs-eng/mobile-ios/pull/482"},
                   {"label": "Issue", "url": "https://familysearch.atlassian.net/browse/FSMOB-1423"}],
            fields={"repo": "fs-eng/mobile-ios", "pr": "482", "head": "4b1c0de"})
    r.event("step_start", "resolve").wait(3)
    r.event("log", "resolve", message="building fs-eng/mobile-ios on aws-mac mac2-m2.metal (macos-xcode)")
    r.wait(9).event("log", "resolve", message="Resolved PR #482: 6 files changed, +214 -37")
    r.event("step_end", "resolve", output={"files": 6, "additions": 214, "deletions": 37})
    r.wait(1).event("step_start", "reviews", branches=len(SPECIALISTS))
    for index, role in enumerate(SPECIALISTS):
        r.wait(0.4).event("log", "reviews", role=role, state="started", message="started", branch=index)
        r.event("tool_call", "reviews", role=role, tool="Read", input={"file_path": FINDINGS[index][1]})
    for index, role in enumerate(SPECIALISTS):
        role_findings = [f for f in FINDINGS if f[0] == role]
        r.wait(14 + index * 6)
        r.event("tool_call", "reviews", role=role, tool="Grep", input={"pattern": "PortraitLoader"})
        for spec, path, line, severity, title, body in role_findings:
            r.wait(1).event("finding", "reviews", role=spec, path=path, line=line, severity=severity, title=title,
                            claim=title, body=body, finding_key=hashlib.sha256(f"{spec}|{title}".encode())
                            .hexdigest()[:32])
        r.event("cost", "reviews", role=role, cost_usd=0.21 + index * 0.03, input_tokens=48000 + index * 2100,
                output_tokens=2600 + index * 180)
        r.event("log", "reviews", role=role, state="finished", message="finished", findings=len(role_findings),
                review=f"Reviewed the six changed files for {role.replace('_', ' ')}.", branch=index)
        r.event("step_end", "reviews", branch=index, output={"role": role, "findings": len(role_findings)})
    r.wait(2).event("step_start", "team_lead")
    r.event("log", "team_lead", role="team_lead", state="started", message="started")
    r.wait(6).event("tool_call", "team_lead", role="team_lead", tool="Read",
                    input={"file_path": "FamilySearch/Tree/PortraitLoader.swift"})
    blocking = 0
    for index, decision, reason, raised in DECISIONS:
        spec, path, line, severity, title, body = FINDINGS[index]
        blocking += decision == "block"
        r.wait(2).event("finding", "team_lead", role="team_lead", path=path, line=line,
                        severity="blocking" if decision == "block" else severity, title=title, claim=title,
                        body=body, decision=decision, reason=reason, raised_by=raised,
                        fix_sha=("7e3fa11" + "0" * 33) if decision == "fixed" else None,
                        finding_key=hashlib.sha256(f"lead|{title}".encode()).hexdigest()[:32])
    kept = sum(1 for _, d, _, _ in DECISIONS if d in ("block", "suggest"))
    r.event("cost", "team_lead", role="team_lead", cost_usd=0.64, input_tokens=91000, output_tokens=5200)
    r.wait(1).event("log", "team_lead", role="team_lead", state="finished", message="finished",
                    findings=kept, review="Kept four findings, one blocking. Dropped two, one already fixed.")
    r.event("log", "team_lead", role="team_lead", verdict="changes_requested", findings=kept, blocking=blocking,
            message="Changes recommended: 1 blocking, 3 should fix")
    r.event("step_end", "team_lead", output={"verdict": "changes_requested", "kept": kept, "blocking": blocking})
    r.wait(2).event("step_start", "post")
    r.wait(3).event("log", "post", message="Posted the review with 4 comments to fs-eng/mobile-ios#482")
    r.event("step_end", "post", output={"comments": 4})
    r.event("summary", verdict="changes_requested", outcome="recommended")
    r.wait(1).event("run_end", status="succeeded")
    return run_id, r


def replay_development(conn, keyring, *, team_id, project_id, workflow, start, now) -> tuple[str, Replay, str, Any]:
    run_id, token = open_run(conn, keyring, team_id=team_id, project_id=project_id, workflow=workflow,
                             name=workflow.name, start=start, trigger_kind="jira", actor="mreyes-fs",
                             subject="FSMOB-1431",
                             trigger_ref={"source": "webhook", "issue": "FSMOB-1431"}, location=MAC)
    job_id = new_id()
    conn.execute(insert(jobs).values(id=job_id, team_id=team_id, project_id=project_id, workflow_id=workflow.id,
                                     run_id=run_id, inputs={"issue": "FSMOB-1431", "repo": "fs-eng/mobile-ios"},
                                     status="running", attempts=1, created_at=start, claimed_at=start,
                                     claimed_by="demo-mac-runner", heartbeat_at=now,
                                     workflow_version=workflow.current_version, requires=["macos", "xcode"],
                                     thread="FSMOB-1431"))
    plan = ("Replace the hand-rolled token refresh in `SessionStore` with the shared `AuthClient`, so sign-in "
            "survives a background refresh.\n\nFiles: `FamilySearch/Auth/SessionStore.swift`, "
            "`FamilySearch/Auth/AuthClient.swift`, `FamilySearch/Auth/Keychain+Session.swift`.\n\n"
            "Risk: high. It touches how sessions and keychain items are stored. Tests: the SessionStore suite plus a "
            "UI test that signs in, backgrounds the app for 70 minutes, and returns.")
    r = Replay(token, start, "demo-dev")
    r.event("summary", location=MAC, title="[FSMOB-1431] Keep users signed in across a background token refresh",
            links=[{"label": "Issue", "url": "https://familysearch.atlassian.net/browse/FSMOB-1431"}])
    r.event("step_start", "plan")
    r.event("log", "plan", role="planner", state="started", message="started")
    for path in ("FamilySearch/Auth/SessionStore.swift", "FamilySearch/Auth/AuthClient.swift",
                 "FamilySearch/Auth/Keychain+Session.swift"):
        r.wait(5).event("tool_call", "plan", role="planner", tool="Read", input={"file_path": path})
    r.event("cost", "plan", role="planner", cost_usd=0.38, input_tokens=62000, output_tokens=3100)
    r.wait(4).event("log", "plan", role="planner", state="finished", message="finished", review=plan)
    r.event("step_end", "plan", output={"content": plan})
    r.wait(1).event("step_start", "approve_plan")
    checkpoint = {"plan": {"content": plan}}
    request = {"step_id": "approve_plan", "key": "approve_plan", "checkpoint": checkpoint, "present": plan}
    signed = {"kind": "run_suspend", "run_id": run_id, "step_id": "approve_plan", "key": "approve_plan",
              "digest": gate_digest(request), "ts": iso(r.at)}
    r.event("run_suspend", None, step_id="approve_plan", key="approve_plan", digest=signed["digest"], ts=signed["ts"],
            sig=sign_engine(runners.engine_key(keyring, run_id), signed))
    return run_id, r, job_id, request


def replay_opened(conn, keyring, *, team_id, project_id, workflow, start) -> tuple[str, Replay]:
    run_id, token = open_run(conn, keyring, team_id=team_id, project_id=project_id, workflow=workflow,
                             name=workflow.name, start=start, trigger_kind="jira", actor="mreyes-fs",
                             subject="FSMOB-1423", trigger_ref={"source": "webhook", "issue": "FSMOB-1423"},
                             location=MAC)
    r = Replay(token, start, "demo-opened")
    r.event("summary", location=MAC, title="[FSMOB-1423] Show lifespans and portraits on tree person cards",
            links=[{"label": "Issue", "url": "https://familysearch.atlassian.net/browse/FSMOB-1423"}])
    for step_id, role, seconds, message in (("plan", "planner", 140, "Planned 6 files across Tree and its tests"),
                                            ("approve_plan", None, 1, "Low risk, so the gate passed on its own"),
                                            ("implement", "developer", 1260, "Built and tested on macos-xcode")):
        r.wait(2).event("step_start", step_id)
        if role:
            r.event("log", step_id, role=role, state="started", message="started")
            r.wait(seconds).event("log", step_id, role=role, state="finished", message="finished", review=message)
        else:
            r.wait(seconds).event("log", step_id, message=message)
        r.event("step_end", step_id, output={"content": message})
    r.event("cost", "implement", role="developer", cost_usd=2.84, input_tokens=612000, output_tokens=41000)
    r.event("summary", outcome="pr_opened",
            links=[{"label": "Pull request", "url": "https://github.com/fs-eng/mobile-ios/pull/482"}])
    r.wait(1).event("run_end", status="succeeded")
    return run_id, r


def replay_running(conn, keyring, *, team_id, project_id, workflow, start) -> tuple[str, Replay]:
    run_id, token = open_run(conn, keyring, team_id=team_id, project_id=project_id, workflow=workflow,
                             name=workflow.name if workflow is not None else "code-review", start=start,
                             trigger_kind="pr", actor="tnguyen-fs", subject="fs-eng/android-tree#219",
                             trigger_ref={"source": "webhook", "repo": "fs-eng/android-tree", "pr": 219},
                             location=location.make(location.AWS_LINUX, instance="i-0de0c0de0000and1",
                                                    environment_type="android-gradle", instance_type="m7i.xlarge"))
    r = Replay(token, start, "demo-live")
    r.event("summary", title="[FSAND-877] Paginate the descendants list on the tree screen",
            links=[{"label": "Pull request", "url": "https://github.com/fs-eng/android-tree/pull/219"}])
    r.event("step_start", "resolve").wait(8)
    r.event("log", "resolve", message="building fs-eng/android-tree on aws-linux m7i.xlarge (android-gradle)")
    r.event("step_end", "resolve", output={"files": 4})
    r.wait(1).event("step_start", "reviews", branches=len(SPECIALISTS))
    for index, role in enumerate(SPECIALISTS):
        r.wait(0.5).event("log", "reviews", role=role, state="started", message="started", branch=index)
    for index, role in enumerate(SPECIALISTS[:4]):
        r.wait(20).event("log", "reviews", role=role, state="finished", message="finished", findings=0,
                         review="Nothing to flag.", branch=index)
        r.event("step_end", "reviews", branch=index, output={"role": role, "findings": 0})
    return run_id, r


LIVE_ACTIONS = [
    ("Read", {"file_path": "app/src/main/java/org/familysearch/tree/DescendantsPager.kt"}),
    ("Grep", {"pattern": "PagingSource<"}),
    ("Read", {"file_path": "app/src/main/java/org/familysearch/tree/DescendantsViewModel.kt"}),
    ("Bash", {"command": "./gradlew :app:testDebugUnitTest --tests '*Descendants*'"}),
    ("Read", {"file_path": "app/src/test/java/org/familysearch/tree/DescendantsPagerTest.kt"}),
    ("Grep", {"pattern": "collectAsLazyPagingItems"}),
]


def replay_dry(conn, keyring, *, team_id, project_id, name, start, steps) -> tuple[str, Replay]:
    workflow = defs.resolve_workflow(conn, name, team_id, project_id)
    run_id, token = open_run(conn, keyring, team_id=team_id, project_id=project_id, workflow=workflow, name=name,
                             start=start, trigger_kind="webhook", actor="github", location=LINUX, **steps["columns"])
    r = Replay(token, start, f"demo-{name}")
    r.event("summary", location=LINUX, **steps["summary"])
    for step_id, message, output in steps["steps"]:
        r.wait(2).event("step_start", step_id)
        r.wait(1).event("log", step_id, message=message)
        r.event("step_end", step_id, output=output)
    r.event("summary", outcome="noop")
    r.wait(1).event("run_end", status="succeeded")
    return run_id, r


def seed_runs(cfg: ServerConfig, engine, keyring, team_id: str, project_ids: dict[str, str],
              review_workflow: Any, dev_workflow: Any) -> None:
    now = utcnow()
    ios = project_ids["mobile-ios"]
    with engine.begin() as conn:
        _, opened = replay_opened(conn, keyring, team_id=team_id, project_id=ios, workflow=dev_workflow,
                                  start=now - timedelta(hours=3, minutes=40))
        review_id, review = replay_code_review(conn, keyring, team_id=team_id, project_id=ios,
                                               workflow=review_workflow, start=now - timedelta(hours=2, minutes=10))
        dev_id, dev, job_id, request = replay_development(conn, keyring, team_id=team_id, project_id=ios,
                                                          workflow=dev_workflow, start=now - timedelta(minutes=48),
                                                          now=now)
        live_id, live = replay_running(conn, keyring, team_id=team_id, project_id=project_ids["android-tree"],
                                       workflow=None, start=now - timedelta(minutes=4))
        merged_id, merged = replay_dry(conn, keyring, team_id=team_id, project_id=ios, name="pr-merged",
                                       start=now - timedelta(hours=1, minutes=25), steps={
            "columns": {"subject": "fs-eng/mobile-ios#479", "trigger_ref": {"dry_run": True, "repo": "fs-eng/mobile-ios",
                                                                         "pr": 479}},
            "summary": {"title": "[FSMOB-1398] Fix tree zoom reset after rotation",
                        "links": [{"label": "Pull request", "url": "https://github.com/fs-eng/mobile-ios/pull/479"},
                                  {"label": "Issue", "url": "https://familysearch.atlassian.net/browse/FSMOB-1398"}]},
            "steps": [("find_issue", "Found FSMOB-1398 in the pull request title", {"issue": "FSMOB-1398"}),
                      ("transition", "Dry run: would move FSMOB-1398 from In Review to Ready for QA",
                       {"dry_run": True, "to": "Ready for QA"}),
                      ("comment", "Dry run: would comment on FSMOB-1398 with the merge commit",
                       {"dry_run": True})]})
        build_id, build = replay_dry(conn, keyring, team_id=team_id, project_id=ios, name="build-announce",
                                     start=now - timedelta(minutes=33), steps={
            "columns": {"subject": "fs-eng/mobile-ios", "trigger_ref": {"dry_run": True, "build": "6.42.0 (18211)"}},
            "summary": {"title": "TestFlight build 6.42.0 (18211) is ready"},
            "steps": [("collect", "Build 6.42.0 (18211) has 7 merged pull requests since 6.41.2",
                       {"prs": 7, "version": "6.42.0", "build": "18211"}),
                      ("announce", "Dry run: would post the release notes to #mobile-ios-builds",
                       {"dry_run": True, "channel": "#mobile-ios-builds"})]})
        paths = ["FamilySearch/Tree/PersonCardViewModel.swift", "FamilySearch/Tree/PortraitLoader.swift",
                 "FamilySearch/Tree/TreeCanvasView.swift", "FamilySearch/Tree/PersonCardView.swift",
                 "FamilySearchTests/Tree/PersonCardViewModelTests.swift", "Podfile.lock"]
        level, reasons = risk.floor(paths)
        risk.record(conn, review_id, level, reasons)
        level, reasons = risk.floor(["FamilySearch/Auth/SessionStore.swift", "FamilySearch/Auth/AuthClient.swift"])
        risk.record(conn, dev_id, level, reasons)
    asyncio.run(send(engine, keyring, [opened, review, dev, live, merged, build]))
    with engine.begin() as conn:
        gates.open_gate(conn, keyring, job_id=job_id, request=request, now=utcnow())
    note("development run on FSMOB-1423 that opened fs-eng/mobile-ios#482")
    note("code-review run on fs-eng/mobile-ios#482: 9 specialists, team lead decisions, medium risk, AWS macOS")
    note("development run on FSMOB-1431 waiting at a high-risk approval gate")
    note("code-review run on fs-eng/android-tree#219 still running, with live events while the server runs")
    note("pr-merged and build-announce dry runs")
    (Path(cfg.database_url.removeprefix("sqlite:///")).parent / LIVE_FILE).write_text(json.dumps({
        "run_id": live_id, "token": live.token, "seq": live.count}), encoding="utf-8")


def seed(data: Path) -> None:
    if (data / "server.db").exists():
        print(f"{data} is already seeded. Run scripts/demo.sh --reset for a fresh one.")
        return
    print(f"Seeding {data}")
    cfg = write_setup(data)
    keyring = keyring_from_source(cfg.master_key)
    engine = make_engine(cfg.database_url)
    now = utcnow()
    with engine.begin() as conn:
        user_id, project_ids, team_id = seed_people(conn, keyring, now)
        github = seed_connections(conn, keyring, user_id, team_id, now)["github_app"]
        link_environment_types(conn, project_ids, seed_aws(conn, keyring, user_id, now))
        seed_starters(conn, now)
        ios = project_ids["mobile-ios"]
        review_workflow = seed_code_review_workflow(conn, user_id, team_id, ios)
        dev_workflow = ensure_workflow(conn, user_id, team_id, ios, "development", DEVELOPMENT, "work")
        hook = seed_webhook(conn, user_id, team_id, github, review_workflow.id)
        defs.put_setting(conn, team_id=team_id, key="slack.build_channel", value="C0DEMOBUILD", actor_id=user_id,
                         now=now, ip=None)
    held_delivery(cfg, engine, keyring, hook)
    import_ops(data, "fs-eng/mobile-ios")
    with engine.begin() as conn:
        rules_dry(conn)
        show_ops_runs(conn, user_id)
    seed_runs(cfg, engine, keyring, team_id, project_ids, review_workflow, dev_workflow)
    engine.dispose()


# Serving.

class OfflineVendor:
    """A vendor module whose Verify answers from the seeded identity instead of calling the vendor."""

    def __init__(self, module: Any, kind: str):
        self._module = module
        self._kind = kind

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)

    def verify(self, creds, config, *, now, transport=None):
        from armature.server.vendors import Verified
        installations = [{"owner": "fs-eng", "installation_id": 55501234,
                          "permissions": {"pull_requests": "write", "contents": "write", "checks": "write"}}] \
            if self._kind == "github_app" else []
        return Verified(identity=IDENTITIES[self._kind], installations=installations)


def offline(app) -> None:
    import httpx
    for kind in ("github_app", "slack", "jira"):
        connections.VENDORS[kind] = OfflineVendor(connections.VENDORS[kind], kind)
    app.state.vendor_transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"message": "The demo server is offline."}))
    envs = app.state.environments
    envs.builder = lambda keyring, row: SimpleNamespace(validate=lambda: [])
    envs.loader = lambda keyring, row: None
    # No launches, no reaping: the AWS connection is a stand-in.
    envs.tick = lambda: setattr(envs, "message", None)


def ticker(data: Path, stop: threading.Event) -> None:
    """Keep the running run's log moving: a tool call by one of its working specialists every few seconds."""
    try:
        live = json.loads((data / LIVE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    roles = SPECIALISTS[4:]
    index = 0
    while not stop.wait(4):
        role = roles[index % len(roles)]
        tool, payload = LIVE_ACTIONS[index % len(LIVE_ACTIONS)]
        index += 1
        event = {"v": 1, "kind": "tool_call", "step_id": "reviews", "ts": iso(utcnow()),
                 "eid": f"demo-tick-{os.getpid()}:{index}", "payload": {"role": role, "tool": tool, "input": payload}}
        request = urllib.request.Request(f"{ORIGIN}/api/events", data=json.dumps(event).encode() + b"\n",
                                         headers={"Authorization": f"Bearer {live['token']}",
                                                  "Content-Type": "application/x-ndjson"}, method="POST")
        try:
            urllib.request.urlopen(request, timeout=5).read()
        except OSError:
            pass


def serve(data: Path) -> None:
    import uvicorn

    from armature.server.app import create_app
    cfg = load_config(data / "server.yaml")
    app = create_app(cfg, make_engine(cfg.database_url), keyring_from_source(cfg.master_key))
    offline(app)
    stop = threading.Event()
    threading.Thread(target=ticker, args=(data, stop), name="demo-ticker", daemon=True).start()
    print(f"\nArmature demo on {ORIGIN}/  (sign in as {USERNAME} / {PASSWORD})\n"
          "The runs are replayed sample data. Ctrl-C stops the server.\n", flush=True)
    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port, workers=1, proxy_headers=False, server_header=False,
                    log_level="warning", timeout_graceful_shutdown=3)
    finally:
        stop.set()


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("seed", "serve"):
        raise SystemExit("usage: demo.py seed|serve <data dir>")
    data = Path(sys.argv[2]).expanduser().resolve()
    seed(data) if sys.argv[1] == "seed" else serve(data)


if __name__ == "__main__":
    main()

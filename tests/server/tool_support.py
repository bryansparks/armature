"""A team with verified connections, a job's run whose workflow names every built-in tool, and fake vendors."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import delete, insert, select, update

from armature.events.sign import sign_engine
from armature.server import connections, defs, runners, vendors
from armature.server.db import new_id
from armature.server.db.schema import github_installations, jobs, projects, teams, tool_calls
from armature.server.vendors import github as github_vendor
from armature.server.db.schema import connections as connections_table
from tests.server.app_support import Clock, make_client

REPO, NUMBER, BOT, AUTHOR = "Acme/App", 7, "armature-bot[bot]", "jstutz4"
GH_LOGIN, GH_TOKEN = "stephan", "gho_" + "c" * 36
APP_ID = 4242
HEAD = "a" * 40
PATCH = "@@ -1,0 +1,12 @@\n" + "\n".join(f"+line {n}" for n in range(1, 13))
BOT_TOKEN = "xoxb-1111-2222-abcdefghijklmnop"
STEPS = {"review": "github/post-review", "comment": "github/pr-comment", "check": "github/check-run",
         "open": "github/open-pr", "checkout": "github/checkout", "push": "github/push", "post": "slack/post",
         "update": "slack/update", "jira": "jira/comment", "move": "jira/transition", "linear": "linear/comment",
         "state": "linear/transition", "risk": "review/risk", "marked": "review/marked", "mark": "review/mark",
         "decisions": "review/decisions", "pushable": "review/pushable", "create": "jira/create",
         "read": "jira/get-issue", "permission": "github/permission", "diff": "github/pr-diff",
         "context": "review/context", "plain": "local/thing"}
BODY = "name: tools\nstages:\n" + "".join(f"  - id: {step}\n    adapter: {ref}\n" for step, ref in STEPS.items())


def marker(fid: str) -> str:
    return f"<!-- armature-finding:{fid} -->"


class Vendors:
    """Every vendor API the tools call, in memory. Each request is recorded, and a path ending in a ``fail`` 502s."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail: tuple[str, ...] = ()
        self.files: list[dict] = [{"filename": "src/a.swift", "status": "modified", "patch": PATCH}]
        self.branches: dict[str, str] = {"feature": HEAD}
        self.opened = 12
        self.branch_prs: set[str] = set()
        self.pr = {"number": NUMBER, "state": "open", "merged": False, "user": {"login": AUTHOR}, "changed_files": 1,
                   "head": {"ref": "feature", "sha": HEAD, "repo": {"full_name": REPO}},
                   "base": {"ref": "main", "repo": {"full_name": REPO}}, "requested_teams": []}
        self.comments: list[dict] = []
        self.reviews: list[dict] = []
        self.issue_comments: list[dict] = []
        self.check_runs: list[dict] = []
        self.statuses: list[dict] = []
        self.status_login = GH_LOGIN
        self.login = BOT
        self.members: set[tuple[str, str, str]] = set()
        self.slack: dict = {"ok": True, "channel": "C0REVIEW", "ts": "1700000000.000100"}
        self.jira_issue = {"fields": {"status": {"name": "In Progress"}, "issuetype": {"name": "Story"}}}
        self.jira_transitions = [{"id": "31", "name": "Send to QA", "to": {"name": "In Test"}}]
        self.linear_states = [{"id": "s1", "name": "Todo"}, {"id": "s2", "name": "In Review"}]
        self.next_id = 500
        self.minted = 0

    def writes(self, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET" and (method is None or r.method == method)
                and not r.url.path.endswith("/access_tokens")]

    def body(self, request: httpx.Request) -> dict:
        return json.loads(request.content or b"{}")

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if any(path.endswith(f) for f in self.fail):
            return httpx.Response(502, text="upstream body that must not leak")
        if path == "/app/installations/7/access_tokens":
            self.minted += 1
            return httpx.Response(201, json={"token": f"ghs_minted{self.minted:032d}",
                                             "expires_at": "2026-10-23T13:00:00Z"})
        if path == "/installation/token":
            return httpx.Response(204)
        repo = f"/repos/{REPO}"
        if path == f"{repo}/pulls/{NUMBER}":
            return httpx.Response(200, json=self.pr)
        if path == f"{repo}/pulls/{NUMBER}/files":
            query = self.query(request)
            size, page = int(query.get("per_page", 30)), int(query.get("page", 1))
            return httpx.Response(200, json=self.files[(page - 1) * size:page * size])
        found = re.fullmatch(rf"{repo}/branches/(.+)", path)
        if found:
            sha = self.branches.get(found.group(1))
            return httpx.Response(200, json={"name": found.group(1), "commit": {"sha": sha}}) if sha \
                else httpx.Response(404)
        if re.fullmatch(rf"{repo}/compare/[^/]+\.\.\.[0-9a-f]{{40}}", path):
            return httpx.Response(200, json={"files": self.files[:300]})
        if path == f"{repo}/pulls/{NUMBER}/comments":
            return httpx.Response(200, json=self.comments)
        if path == f"{repo}/pulls/{NUMBER}/reviews" and method == "GET":
            return httpx.Response(200, json=self.reviews)
        if path == f"{repo}/pulls/{NUMBER}/reviews":
            review_id = self._id()
            for comment in self.body(request).get("comments") or []:
                self.comments.append({"id": self._id(), "review": review_id, "user": {"login": self.login},
                                      "in_reply_to_id": None, "body": comment["body"]})
            return httpx.Response(200, json={"id": review_id, "html_url": f"https://github.com/{REPO}/pull/7#r"})
        found = re.fullmatch(rf"{repo}/pulls/{NUMBER}/reviews/(\d+)/comments", path)
        if found:
            return httpx.Response(200, json=[c for c in self.comments if c.get("review") == int(found.group(1))])
        found = re.fullmatch(rf"{repo}/pulls/{NUMBER}/comments/(\d+)/replies", path)
        if found:
            made = {"id": self._id(), "in_reply_to_id": int(found.group(1)), "user": {"login": self.login},
                    "body": self.body(request)["body"]}
            self.comments.append(made)
            return httpx.Response(201, json=made)
        found = re.fullmatch(rf"{repo}/pulls/comments/(\d+)", path)
        if found:
            hit = [c for c in self.comments if c["id"] == int(found.group(1))]
            return httpx.Response(200, json=hit[0]) if hit else httpx.Response(404)
        if path == f"{repo}/issues/{NUMBER}/comments" and method == "GET":
            return httpx.Response(200, json=self.issue_comments)
        if path == f"{repo}/issues/{NUMBER}/comments":
            made = {"id": self._id(), "user": {"login": self.login}, "body": self.body(request)["body"],
                    "html_url": "https://github.com/c"}
            self.issue_comments.append(made)
            return httpx.Response(201, json=made)
        found = re.fullmatch(rf"{repo}/issues/comments/(\d+)", path)
        if found:
            hit = [c for c in self.issue_comments if c["id"] == int(found.group(1))]
            if not hit:
                return httpx.Response(404)
            hit[0]["body"] = self.body(request)["body"]
            return httpx.Response(200, json=hit[0])
        if re.fullmatch(rf"{repo}/commits/[0-9a-f]{{40}}/check-runs", path):
            return httpx.Response(200, json={"total_count": len(self.check_runs), "check_runs": self.check_runs})
        found = re.fullmatch(rf"{repo}/statuses/([0-9a-f]{{40}})", path)
        if found:
            made = {"id": self._id(), "sha": found.group(1), "creator": {"login": self.status_login},
                    "created_at": f"2026-10-29T00:{self.next_id % 60:02d}:00Z", **self.body(request)}
            self.statuses.append(made)
            return httpx.Response(201, json=made)
        found = re.fullmatch(rf"{repo}/commits/([0-9a-f]{{40}})/statuses", path)
        if found:
            return httpx.Response(200, json=[s for s in reversed(self.statuses) if s["sha"] == found.group(1)])
        if path == f"{repo}/check-runs":
            made = {"id": self._id(), "app": {"slug": "armature-bot", "id": APP_ID}, "html_url": "https://github.com/run",
                    **self.body(request)}
            self.check_runs.append(made)
            return httpx.Response(201, json=made)
        found = re.fullmatch(rf"{repo}/check-runs/(\d+)", path)
        if found:
            hit = [c for c in self.check_runs if c["id"] == int(found.group(1))]
            hit[0].update(self.body(request))
            return httpx.Response(200, json=hit[0])
        if path == f"{repo}/pulls" and method == "GET":
            head = self.query(request).get("head", "").split(":", 1)[-1]
            return httpx.Response(200, json=[{"number": 99}] if head in self.branch_prs else [])
        if path == f"{repo}/pulls":
            return httpx.Response(201, json={"number": self.opened,
                                             "html_url": f"https://github.com/{REPO}/pull/{self.opened}"})
        if path == f"{repo}/pulls/12/requested_reviewers":
            return httpx.Response(201, json={})
        found = re.fullmatch(r"/orgs/([^/]+)/teams/([^/]+)/memberships/([^/]+)", path)
        if found:
            return httpx.Response(200, json={"state": "active"}) if found.groups() in self.members \
                else httpx.Response(404)
        if path in ("/api/chat.postMessage", "/api/chat.update"):
            return httpx.Response(200, json=self.slack)
        if path == "/rest/api/3/issue" and method == "POST":
            return httpx.Response(201, json={"id": "10100", "key": "MOB-99", "self": "https://acme.atlassian.net"})
        if path.startswith("/rest/api/3/issue/"):
            if path.endswith("/transitions") and method == "GET":
                return httpx.Response(200, json={"transitions": self.jira_transitions})
            if path.endswith("/transitions"):
                return httpx.Response(204)
            if path.endswith("/comment"):
                return httpx.Response(201, json={"id": "10001"})
            return httpx.Response(200, json=self.jira_issue) if self.jira_issue else httpx.Response(404)
        if path == "/graphql":
            query = self.body(request)["query"]
            if "issue(id:" in query:
                return httpx.Response(200, json={"data": {"issue": {"id": "uuid-1", "team": {
                    "states": {"nodes": self.linear_states}}}}})
            if "commentCreate" in query:
                return httpx.Response(200, json={"data": {"commentCreate": {"success": True}}})
            if "issueUpdate" in query:
                return httpx.Response(200, json={"data": {"issueUpdate": {"success": True}}})
        return httpx.Response(404)

    def query(self, request: httpx.Request) -> dict:
        return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.setattr(vendors, "resolve_host", lambda host: ["140.82.112.5"])


@pytest.fixture(autouse=True)
def fresh_members():
    from armature.server.tools import github
    github._members.clear()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def vendor():
    return Vendors()


def _connection(engine, keyring, clock, team, kind, name, creds, config, identity, state="ok"):
    row_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(connections_table).values(
            id=row_id, team_id=team, kind=kind, name=name, config=config, identity=identity, state=state,
            sealed=keyring.seal(json.dumps(creds), connections.sealed_aad(row_id, kind), label=connections.LABEL),
            key_id=keyring.key_id, created_at=clock(), updated_at=clock()))
    return row_id


@pytest.fixture
def world(sqlite_engine, keyring, clock, pem):
    team, other, project, workflow = new_id(), new_id(), new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values([{"id": team, "name": "mobile", "created_at": clock()},
                                           {"id": other, "name": "web", "created_at": clock()}]))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", repo=REPO, created_at=clock()))
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="tools", current_version=1,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, BODY, "seed", None, clock())
    github = _connection(sqlite_engine, keyring, clock, team, "github_app", "gh", {"private_key_pem": pem},
                         {"client_id": "Iv1.abcdef12", "api_base": "https://api.github.com"},
                         {"slug": "armature-bot", "bot_login": BOT, "bot_user_id": 9001, "app_id": APP_ID})
    with sqlite_engine.begin() as conn:
        conn.execute(insert(github_installations).values(id=new_id(), connection_id=github, owner="acme",
                                                         installation_id=7, permissions={}))
    slack = _connection(sqlite_engine, keyring, clock, team, "slack", "chat", {"bot_token": BOT_TOKEN}, {},
                        {"team_id": "T0ACME"})
    jira = _connection(sqlite_engine, keyring, clock, team, "jira", "tracker", {"api_token": "jira-api-token-1"},
                       {"site_url": "https://acme.atlassian.net", "email": "bot@acme.test"}, {})
    linear = _connection(sqlite_engine, keyring, clock, team, "linear", "issues",
                         {"api_key": "lin_api_0123456789abcdefghij"}, {}, {})
    return SimpleNamespace(team=team, other=other, project=project, workflow=workflow, github=github, slack=slack,
                           jira=jira, linear=linear)


@pytest.fixture
def gh_cli(sqlite_engine, world, monkeypatch):
    """The team's GitHub connection switched to the gh CLI, with no installations and a fake gh."""
    asked = []
    monkeypatch.setattr(github_vendor, "_tokens", {})
    monkeypatch.setattr(github_vendor, "run_gh", lambda host: asked.append(host) or GH_TOKEN)
    with sqlite_engine.begin() as conn:
        conn.execute(delete(github_installations).where(github_installations.c.connection_id == world.github))
        conn.execute(update(connections_table).where(connections_table.c.id == world.github).values(
            config={"auth": "gh_cli", "api_base": "https://api.github.com"},
            identity={"auth": "gh_cli", "login": GH_LOGIN, "user_id": 1, "scopes": ["repo"]}))
    return asked


def on_runner(app, engine, keyring, clock, world, run, platform, *, team_key=False):
    """Hold the run's job on a runner that said hello with ``platform``, on an instance key unless ``team_key``."""
    with engine.begin() as conn:
        _, key = runners.create_key(conn, keyring, team_id=world.team if team_key else None, name=new_id(),
                                    actor_id=None, now=clock(), ip=None)
    body = {"name": "m1", **({"platform": platform} if platform is not None else {})}
    hello = app.post("/api/runner/hello", json=body, headers={"Authorization": f"Bearer {key}"})
    assert hello.status_code == 200, hello.text
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == run.job).values(runner_id=hello.json()["runner_id"],
                                                                      status="running"))


@pytest.fixture(scope="session")
def pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def start_run(engine, keyring, clock, world, *, trigger_ref=None, inputs=None, version=1, risk="low",
              project="own") -> SimpleNamespace:
    """A job's run in progress at the recorded ``risk``, with its run token."""
    job_id = new_id()
    with engine.begin() as conn:
        project_id = world.project if project == "own" else project
        row, token = runners.open_run(conn, keyring, team_id=world.team, project_id=project_id,
                                      workflow_id=world.workflow, workflow_name="tools", now=clock(),
                                      workflow_version=version, trigger_ref=trigger_ref or {}, risk=risk)
        conn.execute(insert(jobs).values(id=job_id, team_id=world.team, project_id=project_id,
                                         workflow_id=world.workflow, workflow_version=version, run_id=row["id"],
                                         inputs=inputs or {}, status="running", attempts=1, created_at=clock()))
    return SimpleNamespace(id=row["id"], token=token, job=job_id, key=runners.engine_key(keyring, row["id"]),
                           clock=clock)


@pytest.fixture
def app(sqlite_engine, keyring, clock, vendor):
    client = make_client(sqlite_engine, keyring, clock)
    client.app.state.vendor_transport = httpx.MockTransport(vendor)
    return client


def ask_grant(app, run, step, *, tools=None, ts=None, key=None, signed=True, kind="tool_grant", **extra):
    """A grant request as the run's engine makes it, signed with the engine key unless ``signed`` is false."""
    if tools is None:
        tools = [STEPS[step].replace("/", ".", 1)] if step in STEPS else []
    payload = {"kind": kind, "run_id": run.id, "step": step, "tools": sorted(tools),
               "ts": int(run.clock().timestamp()) if ts is None else ts}
    body = {**payload, **({"sig": sign_engine(key or run.key, payload)} if signed else {}), **extra}
    return app.post("/api/tools/grants", json=body, headers={"Authorization": f"Bearer {run.token}"})


def grant(app, run, step):
    response = ask_grant(app, run, step)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def use(app, token, tool, params):
    return app.post(f"/api/tools/{tool}", json={"params": params}, headers={"Authorization": f"Bearer {token}"})


def tool(app, run, step, tool_name, params):
    return use(app, grant(app, run, step), tool_name, params)


def calls(engine):
    with engine.connect() as conn:
        return conn.execute(select(tool_calls).order_by(tool_calls.c.created_at)).all()

"""The tool broker: per-step grants, the built-in tools a grant lets a step call, and git credentials.

A grant is an HMAC over a run, one step, the built-in tools that step's pinned
workflow names, and an expiry, so no row holds it. Every use checks that the
run is still open, so a run's end revokes its grants. Connection credentials
never leave the server, except the one-repository token git asks for through
the credential helper, which is revoked when the run ends.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from typing import Any

import httpx
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from sqlalchemy import Connection, Engine, func, insert, select

from armature.events.sign import sign_engine
from armature.packs import BUILTIN_DIR, Catalog
from armature.packs import catalog as load_catalog
from armature.packs.params import with_errors
from armature.server import (
    audit,
    connections,
    conversations,
    defs,
    ingest,
    runners,
    webhooks,
    worker,
)
from armature.server.crypto import Keyring, SealError
from armature.server.db import new_id
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    environments,
    github_installations,
    jobs,
    projects,
    runner_keys,
    runs,
    settings,
    tool_calls,
    workflows,
)
from armature.server.db.schema import runners as runner_rows
from armature.server.tools import Call, ToolError, marks, registry
from armature.server.tools import github as github_tools
from armature.server.tools import jira as jira_tools
from armature.server.vendors import VendorError
from armature.server.vendors import github as github_vendor

log = logging.getLogger(__name__)

PREFIX = "atg_"
GRANT_MAX = timedelta(hours=1)
GRANT_SLACK = timedelta(seconds=60)
ENGINE_SKEW = timedelta(seconds=60)
TOKEN_MAX = 4096
STEP = re.compile(r"^[^\x00-\x1f]{1,191}$")
# A lost run's grants and tokens are revoked as an ended run's are.
RUN_OPEN = ("queued", "running")
WRITE_TOOLS = ("github.push",)
# Reads a step may make of another project's repository, so long as the project is in the job's team.
TEAM_READS = ("github.pr-diff",)
# Tools whose call can change the run's risk or outcome, which open run pages show.
RUN_TOOLS = ("github.post-review", "github.open-pr", "review.risk", "review.marked", "review.mark")
GIT_HOST = "github.com"
GIT_TOOL = "github.git-credential"
DETAIL_MAX = 2000
DRY_KEY = "tools.dry_run"
AUTHORS_KEY = "github.push_authors"
EXCLUDE_KEY = "github.push_exclude"
CAP_KEY = "github.push_daily_cap"
DEFAULT_PUSH_CAP = 3
# Pushes to one pull request across all runs in a day, whatever the team's daily cap says.
PR_PUSH_CAP = 6

_minted: dict[str, list[tuple[str, str, httpx.BaseTransport | None]]] = {}
_minted_lock = threading.Lock()


class BrokerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Grant:
    run_id: str
    step: str
    tools: tuple[str, ...]
    expires_at: datetime


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _mac(keyring: Keyring, body: str) -> str:
    return _b64(hmac.new(keyring.subkey("grants"), body.encode("ascii"), hashlib.sha256).digest())


def encode(keyring: Keyring, grant: Grant) -> str:
    body = _b64(json.dumps({"r": grant.run_id, "s": grant.step, "t": list(grant.tools),
                            "e": int(grant.expires_at.timestamp())}, separators=(",", ":")).encode())
    return f"{PREFIX}{body}.{_mac(keyring, body)}"


def decode(keyring: Keyring, token: Any, now: datetime) -> Grant | None:
    """The grant ``token`` carries, or None when it is forged, malformed, or expired."""
    if not isinstance(token, str) or len(token) > TOKEN_MAX or not token.startswith(PREFIX):
        return None
    body, _, mac = token[len(PREFIX):].partition(".")
    try:
        if not hmac.compare_digest(mac, _mac(keyring, body)):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        grant = Grant(run_id=str(data["r"]), step=str(data["s"]), tools=tuple(str(t) for t in data["t"]),
                      expires_at=datetime.fromtimestamp(int(data["e"]), now.tzinfo))
    except (UnicodeError, ValueError, binascii.Error, KeyError, TypeError):
        return None
    return grant if grant.expires_at > now else None


@cache
def builtin_catalog() -> Catalog:
    return load_catalog([BUILTIN_DIR])


def _load(text: str | None) -> Any:
    try:
        return YAML(typ="safe").load(text) if text else None
    except YAMLError:
        return None


def step_tools(conn: Connection, job: Any, step: str) -> tuple[list[str], int, bool]:
    """The built-in tools ``step`` names in the job's pinned workflow and the workflows it calls, their timeout, and
    whether any of those workflows has an agent stage.

    A step whose workflow defines its own adapter under a built-in name runs that adapter, so it gets no tool.
    """
    from armature.server import worker

    workflow = conn.execute(select(workflows.c.id, workflows.c.name, workflows.c.scope)
                            .where(workflows.c.id == job.workflow_id)).first()
    body = defs.body_of(conn, defs.WORKFLOW, job.workflow_id, job.workflow_version) if workflow else None
    if body is None:
        return [], 0, False
    bodies = [body]
    try:
        bodies += [text for name, text in worker.bundle_calls(conn, job, workflow, body).items()
                   if name.endswith(".yaml")]
    except worker.JobError:
        pass
    found, timeout, agents = set(), 0, False
    for data in map(_load, bodies):
        agents = agents or bool(worker.agent_steps(data))
        stages = data.get("stages") if isinstance(data, dict) else None
        adapters = data.get("adapters") if isinstance(data, dict) else None
        for stage in stages if isinstance(stages, list) else []:
            ref = stage.get("adapter") if isinstance(stage, dict) and stage.get("id") == step else None
            if not isinstance(ref, str) or (isinstance(adapters, dict) and ref in adapters):
                continue
            hit = builtin_catalog().resolve(ref)
            if hit is not None:
                found.add(ref.replace("/", ".", 1))
                timeout = max(timeout, hit[1].timeout)
    return sorted(found), timeout, agents


def _run_job(conn: Connection, run_id: str) -> tuple[Any, Any]:
    run = conn.execute(select(runs).where(runs.c.id == run_id)).first()
    if run is None or run.status not in RUN_OPEN:
        raise BrokerError(401, "The run has ended.")
    job = conn.execute(select(jobs).where(jobs.c.run_id == run_id)).first()
    if job is None or job.workflow_version is None:
        raise BrokerError(403, "Only a job's run can use built-in tools.")
    return run, job


def _same_user_runner(conn: Connection, job: Any) -> bool:
    """Whether the runner holding ``job`` said at hello that its agent sessions run as its own user.

    Only a runner on an instance key counts, since only an admin mints one and a team lead could mint a team key.
    An environment's key has no team either, but enrollment mints it for any team's job, so it doesn't count.
    """
    if job.runner_id is None or job.status not in worker.HELD:
        return False
    enrolled = select(environments.c.id).where(environments.c.runner_key_id == runner_keys.c.id).exists()
    platform = conn.execute(select(runner_rows.c.platform).join(
        runner_keys, runner_keys.c.id == runner_rows.c.runner_key_id).where(
        runner_rows.c.id == job.runner_id, runner_keys.c.team_id.is_(None), ~enrolled)).scalar()
    return isinstance(platform, dict) and platform.get("same_user") is True


def _engine_signed(keyring: Keyring, run_id: str, body: dict[str, Any], now: datetime) -> None:
    """Refuse a request the run's engine didn't sign just now. A script step holds the run token, never this key."""
    signed = {k: body.get(k) for k in ("kind", "run_id", "step", "tools", "ts")}
    sig, ts = body.get("sig"), signed["ts"]
    if signed["kind"] != "tool_grant" or not isinstance(sig, str) or not isinstance(ts, int) or isinstance(ts, bool) \
            or signed["run_id"] != run_id \
            or not hmac.compare_digest(sig, sign_engine(runners.engine_key(keyring, run_id), signed)):
        raise BrokerError(403, "Only the run's engine can ask for a tool grant.")
    if abs(now.timestamp() - ts) > ENGINE_SKEW.total_seconds():
        raise BrokerError(403, "The grant request is stale.")


def issue_grant(conn: Connection, keyring: Keyring, *, run_token: Any, body: dict[str, Any], now: datetime,
                ip: str | None) -> dict[str, Any]:
    """A grant for one step of a job's run, to the built-in tools the job's pinned workflow gives that step.

    The engine signs the request with its own key. A run with an agent stage gets a write tool only when its runner
    says sessions run as a separate agent user, or the runner holding its job registered with ``--same-user``.
    """
    found = runners.lookup_token(conn, runners.digest(keyring, run_token)) \
        if isinstance(run_token, str) and 0 < len(run_token) <= 200 else None
    if found is None or found.expires_at <= now:
        raise BrokerError(401, "A valid run token is required.")
    step = body.get("step")
    if not isinstance(step, str) or not STEP.fullmatch(step):
        raise BrokerError(400, "step is not valid.")
    _engine_signed(keyring, found.run_id, body, now)
    run, job = _run_job(conn, found.run_id)
    tools, timeout, agents = step_tools(conn, job, step)
    if not tools:
        raise BrokerError(403, f"Step {step} uses no built-in tool.")
    if body.get("tools") != tools:
        raise BrokerError(403, f"Step {step} names {', '.join(tools)} in the job's workflow.")
    if agents and body.get("agent_user") is not True and any(t in WRITE_TOOLS for t in tools) \
            and not _same_user_runner(conn, job):
        raise BrokerError(403, "A workflow with an agent stage pushes only from a runner whose sessions run as a "
                               "separate agent user, or one started with --same-user.")
    grant = Grant(run_id=run.id, step=step, tools=tuple(tools),
                  expires_at=now + min(timedelta(seconds=timeout) + GRANT_SLACK, GRANT_MAX))
    audit.record(conn, action="tool.grant", actor_id=None, team_id=job.team_id, target_kind="run", target_id=run.id,
                 detail={"step": step, "tools": tools}, ip=ip)
    return {"token": encode(keyring, grant), "tools": tools, "expires_at": grant.expires_at.isoformat()}


def _grant(keyring: Keyring, token: Any, now: datetime) -> Grant:
    grant = decode(keyring, token, now)
    if grant is None:
        raise BrokerError(401, "A valid tool grant is required.")
    return grant


def setting(conn: Connection, key: str, team_id: str, project_id: str | None) -> Any:
    """The narrowest value of ``key``: the project's, else the team's, else the instance's."""
    order = [s for s in (project_id, team_id, INSTANCE_SCOPE) if s]
    rows = {r.scope: r.value for r in conn.execute(select(settings.c.scope, settings.c.value).where(
        settings.c.scope.in_(order), settings.c.key == key))}
    return next((rows[s] for s in order if s in rows), None)


def _dry(conn: Connection, run: Any, job: Any) -> bool:
    return (run.trigger_ref or {}).get("dry_run") is True or setting(conn, DRY_KEY, job.team_id,
                                                                      job.project_id) is True


def _target(params: dict[str, Any]) -> str | None:
    if params.get("repo"):
        repo = str(params["repo"]).lower()
        if params.get("number"):
            return f"{repo}#{params['number']}"
        # A push to a new branch, before any pull request exists for it.
        return f"{repo}:{params['branch']}"[:191] if params.get("branch") else repo
    value = params.get("issue") or params.get("channel")
    return str(value)[:191] if value else None


def _context(conn: Connection, keyring: Keyring, public_url: str, run: Any, job: Any, grant: Grant, kind: str,
             params: dict[str, Any], now: datetime, transport: httpx.BaseTransport | None,
             tool: str | None = None) -> Call:
    owner, project_id = None, job.project_id
    if kind == "github_app":
        owner = github_tools.split(params["repo"])[0].lower()
        project_id = _team_repo_project(conn, job, params["repo"]) if tool in TEAM_READS \
            else _repo_project(conn, job, params["repo"])
    try:
        row = connections.resolve(conn, team_id=job.team_id, project_id=project_id, kind=kind,
                                  name=params.get("connection"), owner=owner)
    except connections.ConnError as exc:
        raise BrokerError(409, exc.message) from exc
    if row.state != "ok":
        raise BrokerError(409, f"Connection {row.name} is not verified. Verify it first.")
    try:
        creds = connections.credentials(keyring, row)
    except (SealError, ValueError) as exc:
        raise BrokerError(409, f"The credentials of connection {row.name} can't be opened.") from exc
    installation = conn.execute(select(github_installations.c.installation_id).where(
        github_installations.c.connection_id == row.id, github_installations.c.owner == owner)).scalar() \
        if owner else None
    return Call(run_id=run.id, step_id=grant.step, connection=row, creds=creds, now=now,
                run_url=f"{public_url}/#/run/{run.id}", installation_id=installation, project_id=project_id,
                transport=transport, team_id=job.team_id)


def _team_repo_project(conn: Connection, job: Any, repo: str) -> str:
    """The project of any repository in the job's team: a read across the team's own projects, never outside it."""
    found = webhooks.project_for_repo(conn, job.team_id, repo)
    if found is None:
        raise BrokerError(403, f"{repo} is not the repository of a project in this team.")
    return found


def _repo_project(conn: Connection, job: Any, repo: str) -> str:
    """The project ``repo`` belongs to: the job's own when it has one, else any of the team's."""
    if job.project_id is None:
        found = webhooks.project_for_repo(conn, job.team_id, repo)
    else:
        found = conn.execute(select(projects.c.id).where(projects.c.id == job.project_id,
                                                         func.lower(projects.c.repo) == repo.lower())).scalar()
    if found is None:
        where = "this job's project" if job.project_id else "a project in this team"
        raise BrokerError(403, f"{repo} is not the repository of {where}.")
    return found


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _push_policy(conn: Connection, job: Any, run: Any, project_id: str | None) -> dict[str, Any]:
    """The push settings of the repository's project, and the risk the server recorded for the run."""
    cap = setting(conn, CAP_KEY, job.team_id, project_id)
    return {"authors": _strings(setting(conn, AUTHORS_KEY, job.team_id, project_id)),
            "exclude": _strings(setting(conn, EXCLUDE_KEY, job.team_id, project_id)),
            "daily_cap": cap if isinstance(cap, int) and not isinstance(cap, bool) else DEFAULT_PUSH_CAP,
            "risk": run.risk}


def _admit_push(engine: Engine, grant: Grant, job: Any, result: dict[str, Any], target: str | None,
                cap: int, project_id: str | None, now: datetime, ip: str | None) -> dict[str, Any]:
    """Check the run's own pushes and today's to the target, and record this one, in one transaction under its lock.

    The repo's project row lock serializes every push to the repo across servers, where the database can lock a row.
    """
    with github_tools.pr_lock(result["repo"], result["number"] or result["branch"]), engine.begin() as conn:
        conn.execute(select(projects.c.id).where(projects.c.id == project_id).with_for_update())
        pushes = conn.execute(select(func.count()).select_from(tool_calls).where(
            tool_calls.c.tool == "github.push", tool_calls.c.outcome == "approved", tool_calls.c.target == target,
            tool_calls.c.created_at >= now - timedelta(days=1))).scalar_one()
        refusal = marks.push_refusal(conn, grant.run_id, to_pr=bool(result["number"])) or \
            (f"the pull request reached its cap of {PR_PUSH_CAP} pushes a day"
             if result["number"] and pushes >= PR_PUSH_CAP else None) or \
            (f"the push cap of {cap} a day is reached" if pushes >= cap else None)
        if refusal:
            result = {k: result[k] for k in ("repo", "number", "branch")}
            result.update(approved=False, reason=refusal)
        outcome = "approved" if result["approved"] else "refused"
        _write(conn, grant, job, "github.push", "live", outcome, target,
               {k: result[k] for k in ("reason", "branch", "head", "commit") if result.get(k) is not None}, now, ip)
    return result


def _redacted(value: Any) -> Any:
    value = ingest.redact(value, ())
    if isinstance(value, str):
        return value if len(value) <= DETAIL_MAX else value[:DETAIL_MAX] + "..."
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    return value


def _record(engine: Engine, grant: Grant, job: Any, tool: str, mode: str, outcome: str, target: str | None,
            detail: dict[str, Any] | None, now: datetime, ip: str | None) -> None:
    with engine.begin() as conn:
        _write(conn, grant, job, tool, mode, outcome, target, detail, now, ip)


def _write(conn: Connection, grant: Grant, job: Any, tool: str, mode: str, outcome: str, target: str | None,
           detail: dict[str, Any] | None, now: datetime, ip: str | None) -> None:
    conn.execute(insert(tool_calls).values(id=new_id(), run_id=grant.run_id, team_id=job.team_id,
                                           project_id=job.project_id, step_id=grant.step, tool=tool, mode=mode,
                                           outcome=outcome, target=target, detail=detail, created_at=now))
    audit.record(conn, action="tool.call", actor_id=None, team_id=job.team_id, target_kind="run",
                 target_id=grant.run_id, detail={"tool": tool, "step": grant.step, "mode": mode,
                                                 "outcome": outcome, "target": target}, ip=ip)


def _checked(tool: str, params: Any) -> dict[str, Any]:
    adapter = builtin_catalog().get(tool.replace(".", "/", 1))
    if adapter is None:
        raise BrokerError(404, "No such tool.")
    if not isinstance(params, dict):
        raise BrokerError(400, "params must be an object.")
    problems = with_errors(adapter.params, params, allow_templates=False)
    if problems:
        raise BrokerError(400, "; ".join(problems)[:1000])
    return params


def call_tool(engine: Engine, keyring: Keyring, *, public_url: str, token: Any, tool: str, params: Any,
              now: datetime, ip: str | None, transport: httpx.BaseTransport | None = None,
              packs_dir: str | None = None, hub: Any = None) -> dict[str, Any]:
    """Run one built-in tool for the step the grant names. In a dry run, a tool that only reads runs, and any other
    is recorded and calls no vendor. A stub-mode test run records every tool.

    The live ``hub`` hears of a run whose risk or outcome a tool changed, and takes the findings review.decisions
    records as the step's own events. Without one, the findings are stored directly.
    """
    grant = _grant(keyring, token, now)
    if tool not in grant.tools:
        raise BrokerError(403, f"This step's grant doesn't cover {tool}.")
    if tool in conversations.TOOLS:
        return _conversation_tool(engine, grant, tool, _checked(tool, params), now, ip, packs_dir)
    spec = registry().get(tool)
    if spec is None or spec.run is None:
        raise BrokerError(404, f"{tool} has no broker call. Git asks for its credential itself.")
    params = _checked(tool, params)
    target = _target(params)
    with engine.connect() as conn:
        run, job = _run_job(conn, grant.run_id)
        ctx = _context(conn, keyring, public_url, run, job, grant, spec.kind, params, now, transport, tool)
        ctx.engine = engine
        # A test run in stub mode replays recorded outputs, so it reads nothing for real either.
        any_dry = _dry(conn, run, job)
        dry = any_dry and (not spec.reads or (run.trigger_ref or {}).get("mode") == "stub")
        if tool in ("github.push", "review.pushable"):
            ctx.policy = _push_policy(conn, job, run, ctx.project_id)
        elif tool == "github.open-pr":
            ctx.policy = {"head_sha": run.head_sha}
        elif tool in ("jira.create", "review.context"):
            ctx.policy = {"project": setting(conn, jira_tools.PROJECT_KEY, job.team_id, job.project_id)}
        held = tool == "github.open-pr" and not marks.may_open(conn, run.id)
    if held:
        _record(engine, grant, job, tool, "dry" if dry else "live", "refused", target,
                {"error": marks.HELD_REASON}, now, ip)
        raise BrokerError(403, marks.HELD_REASON)
    if dry:
        shown = _redacted(params)
        _record(engine, grant, job, tool, "dry", "ok", target, {"params": shown}, now, ip)
        return {"dry_run": True, "tool": tool, "params": shown}
    try:
        result = spec.run(ctx, params)
    except ToolError as exc:
        _record(engine, grant, job, tool, "live", "refused", target, {"error": exc.message}, now, ip)
        raise BrokerError(exc.status, exc.message) from exc
    except VendorError as exc:
        _record(engine, grant, job, tool, "live", "failed", target, {"error": str(exc)}, now, ip)
        raise BrokerError(502, str(exc)) from exc
    if tool == "github.push" and result.get("approved") is True:
        return _admit_push(engine, grant, job, result, target, ctx.policy["daily_cap"], ctx.project_id, now, ip)
    approved = result.get("approved")
    outcome = "approved" if approved is True else "refused" if approved is False else "ok"
    detail = {k: result[k] for k in ("reason", "branch", "head") if result.get(k) is not None} or None
    if any_dry and tool == "review.pushable":
        # The answer is real, so a dry run shows whether it would push, but no step may act on it.
        result = {**result, "dry_run": True}
        detail = {**(detail or {}), "would_push": result.get("pushable") is True}
    with engine.begin() as conn:
        if tool == "github.post-review":
            detail = marks.review_detail(params, result)
            marks.reviewed(conn, grant.run_id, detail)
        elif tool == "github.open-pr":
            marks.opened(conn, grant.run_id, result)
        _write(conn, grant, job, tool, "live", outcome, target, detail, now, ip)
    if tool == "review.decisions":
        if hub is not None:
            hub.server_findings(grant.run_id, grant.step, result["findings"])
        else:
            ingest.store_findings(engine, keyring, grant.run_id, grant.step, result["findings"], now)
        result = {"recorded": result["recorded"]}
    if hub is not None and tool in RUN_TOOLS:
        hub.run_changed(grant.run_id)
    return result


def _conversation_tool(engine: Engine, grant: Grant, tool: str, params: dict[str, Any], now: datetime,
                       ip: str | None, packs_dir: str | None) -> dict[str, Any]:
    """A conversation tool, on the conversation whose turn this run is. A Slack reply goes out with the next sweep."""
    with engine.connect() as conn:
        _, job = _run_job(conn, grant.run_id)
    catalog = defs.load_catalog(packs_dir) if tool in ("conversation.history", "conversation.decide") else None
    try:
        result, conversation_id = conversations.call_tool(engine, run_id=grant.run_id, job=job, tool=tool,
                                                          params=params, now=now, catalog=catalog)
    except conversations.ConversationError as exc:
        _record(engine, grant, job, tool, "live", "refused", None, {"error": exc.message}, now, ip)
        raise BrokerError(exc.status, exc.message) from exc
    outcome = "approved" if result.get("go") is True else "ok"
    _record(engine, grant, job, tool, "live", outcome, conversation_id,
            {"action": result["action"]} if "action" in result else None, now, ip)
    return result


def git_credential(engine: Engine, keyring: Keyring, *, public_url: str, token: Any, body: dict[str, Any],
                   now: datetime, ip: str | None, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """An installation token for git on one repository: read for a checkout, write for an approved push."""
    grant = _grant(keyring, token, now)
    access = body.get("access")
    if access not in ("read", "write"):
        raise BrokerError(400, "access must be read or write.")
    allowed = ("github.push",) if access == "write" else ("github.checkout", "github.push")
    if not any(t in grant.tools for t in allowed):
        raise BrokerError(403, f"This step's grant doesn't cover git {access}.")
    if body.get("host") != GIT_HOST:
        raise BrokerError(403, f"Git credentials are only for {GIT_HOST}.")
    path = body.get("path")
    repo = path.strip("/").removesuffix(".git") if isinstance(path, str) else ""
    if not github_tools.SLUG.fullmatch(repo):
        raise BrokerError(400, "path must name one repository as owner/name.")
    params = {"repo": repo}
    with engine.connect() as conn:
        run, job = _run_job(conn, grant.run_id)
        ctx = _context(conn, keyring, public_url, run, job, grant, "github_app", params, now, transport)
        if access == "write":
            if _dry(conn, run, job):
                raise BrokerError(403, "A dry run pushes nothing.")
            approved = conn.execute(select(tool_calls.c.target).where(
                tool_calls.c.run_id == grant.run_id, tool_calls.c.step_id == grant.step,
                tool_calls.c.tool == "github.push", tool_calls.c.outcome == "approved",
                tool_calls.c.created_at >= now - GRANT_MAX)).scalars().all()
            if not any((t or "").startswith((f"{repo.lower()}#", f"{repo.lower()}:")) for t in approved):
                raise BrokerError(403, f"No push to {repo} was approved for this step.")
    if github_vendor.uses_gh_cli(ctx.config):
        with engine.connect() as conn:
            if not _same_user_runner(conn, job):
                raise BrokerError(403, "A gh CLI connection gives git credentials only to a runner on the server "
                                       "as its own OS user. Run this job there, or use a GitHub App connection.")
    try:
        secret = github_tools.git_token(ctx, repo, write=access == "write")
    except ToolError as exc:
        raise BrokerError(exc.status, exc.message) from exc
    except VendorError as exc:
        _record(engine, grant, job, GIT_TOOL, "live", "failed", repo.lower(), {"error": str(exc)}, now, ip)
        raise BrokerError(502, str(exc)) from exc
    if not github_vendor.uses_gh_cli(ctx.config):
        with _minted_lock:
            _minted.setdefault(run.id, []).append((ctx.config.get("api_base") or github_vendor.API, secret,
                                                   transport))
    _record(engine, grant, job, GIT_TOOL, "live", "ok", repo.lower(), {"access": access}, now, ip)
    return {"username": "x-access-token", "password": secret}


def _revoke(found: list[tuple[str, str, httpx.BaseTransport | None]]) -> None:
    for base, secret, transport in found:
        try:
            github_vendor.revoke(base, secret, transport)
        except VendorError as exc:
            log.warning("revoking a git token failed: %s", exc)


def run_ended(run_id: str) -> threading.Thread | None:
    """Revoke the git tokens a run's steps were given, off the caller's thread."""
    with _minted_lock:
        found = _minted.pop(run_id, [])
    if not found:
        return None
    thread = threading.Thread(target=_revoke, args=(found,), name="armature-revoke", daemon=True)
    thread.start()
    return thread

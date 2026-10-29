"""The job worker: claims queued jobs and runs each one as a workflow run the server watches live.

A claim is one compare-and-set UPDATE, so servers sharing a database never
start the same job twice. A claim holds a lease that the worker extends while
the run's process lives. A job whose lease runs out is settled from its run
if the run ended, else marked lost. It is never queued again, so no run is
paid for twice.

Remote runners claim through the runner API instead (``dispatch``). Both build
the same bundle here, so a job runs the same wherever it lands.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets as _random
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from sqlalchemy import Connection, Engine, and_, func, insert, or_, select, update

from armature.events.location import this_machine
from armature.nodes.gate import GATE_DIR_ENV, GATE_FILE, read_gate
from armature.nodes.harness import (
    GIT_HOSTS_ENV,
    HOST_ONLY_ENV,
    MODELS_ENV,
    PROJECT_REPO_ENV,
    ROLES_ENV,
    SKILLS_ENV,
)
from armature.packs import PACKS_PATH_ENV
from armature.packs.tool import URL_ENV as TOOLS_URL_ENV
from armature.runner.process import (
    BUNDLE_VERSION,
    ROLES_DIR,
    SKILLS_DIR,
    job_path,
    write_bundle,
)
from armature.server import (
    calls,
    conversations,
    defs,
    dryrun,
    gatecards,
    gates,
    llm,
    runners,
)
from armature.server import secrets as secret_store
from armature.server.config import ServerConfig
from armature.server.crypto import Keyring
from armature.server.db import new_id, utcnow
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    aws_connections,
    documents,
    environment_types,
    environments,
    jobs,
    projects,
    runs,
    secrets,
    settings,
    trigger_seen,
    triggers,
    versions,
    workflows,
)
from armature.server.ingest import load_summary
from armature.spec.models import AGENT_HARNESSES

log = logging.getLogger(__name__)

INTERVAL_S = 2.0
LEASE = timedelta(minutes=2)
WORK_DIR = "~/.armature/work"
SLOTS_KEY = "worker.slots"
ENABLED_KEY = "worker.enabled"
CAPABILITIES_KEY = "worker.capabilities"
GIT_HOSTS_KEY = "agent.git_hosts"
DEFAULT_SLOTS = 2
CLAIM_SCAN = 100
JOB_KIND = "workflow"
ENV_PREFIX = "env."
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# What Worker.id puts in jobs.claimed_by: host:pid:hex. Runners claim as runner:<id> and environments as env:<id>.
EMBEDDED_CLAIM = re.compile(r"^[^:]{1,150}:\d+:[0-9a-f]{8}$")
TRIGGER_KIND = "webhook"
# What a run started by a poll or schedule trigger says started it.
POLL_RUN_KINDS = {"jira_poll": "jira", "linear_poll": "linear", "schedule": "schedule"}
SUPERSEDED = "superseded by a newer delivery"
HELD = ("provisioning", "claimed", "running")
# A provisioning job's lease is its environment's boot timeout, which the environment reaper enforces.
LEASED = ("claimed", "running")
MAX_ENVIRONMENTS_PER_JOB = 2
# The environment reaper settles a provisioning job first. This catches one it can't reach, with no provider.
PROVISIONING_MARGIN = timedelta(minutes=10)
ENVIRONMENT_UP = ("requested", "booting", "ready", "running")
# A lost run can still send events and end, as dryrun.ended treats it.
RUN_OPEN = ("queued", "running", "lost")
# A run paused at a gate. Its job settles as suspended from the gate request, never from the run's status.
RUN_WAITING = "waiting"
# Required by a job that calls other workflows. A runner has it when it reads call bundles (bundle_version 2).
CALLS_CAPABILITY = "calls:v2"
CALLS_BUNDLE_VERSION = 2
# Required by a job whose agent steps name a role. A runner has it when it reads roles/ (bundle_version 4).
ROLES_CAPABILITY = "roles:v4"
ROLES_BUNDLE_VERSION = 4
# A runner that reads bundles of this version pauses runs at a gate and resumes them from a checkpoint.
GATES_BUNDLE_VERSION = 3
THREAD_ENV = "ARMATURE_THREAD"
STOP_TIMEOUT_S = 30.0


class JobError(Exception):
    pass


def outcome(run_status: str, cancel_requested: bool = False) -> str:
    if run_status == "succeeded":
        return "succeeded"
    return "canceled" if cancel_requested and run_status == "canceled" else "failed"


def _load(body: str | None) -> Any:
    try:
        return YAML(typ="safe").load(body) if body else None
    except YAMLError:
        return None


def agent_steps(data: Any) -> list[dict[str, Any]]:
    """The ``agent:`` mappings of a parsed workflow body's stages."""
    stages = data.get("stages") if isinstance(data, dict) else None
    return [s["agent"] for s in stages if isinstance(s, dict) and isinstance(s.get("agent"), dict)] \
        if isinstance(stages, list) else []


def requirements(body: str | None) -> list[str] | None:
    """The capabilities a workflow body's ``requires:`` lists, plus git and the harness of each agent step."""
    data = _load(body)
    found = data.get("requires") if isinstance(data, dict) else None
    wanted = {str(item) for item in found if isinstance(item, str)} if isinstance(found, list) else set()
    for agent in agent_steps(data):
        wanted |= {"git", str(agent.get("harness") or "claude-code")}
        if agent.get("role") is not None:
            wanted.add(ROLES_CAPABILITY)
    return sorted(wanted) or None


def requirements_with_calls(body: str | None, callees: Iterable[str]) -> list[str] | None:
    """requirements of the body and of every workflow it calls, so a runner that claims the job can run them all."""
    found = set(requirements(body) or ())
    if calls.body_refs(body):
        found.add(CALLS_CAPABILITY)
    for text in callees:
        found.update(requirements(text) or ())
    return sorted(found) or None


def gated(body: str | None) -> bool:
    """Whether a workflow body has a ``gate:`` stage, so its runs may pause and resume."""
    data = _load(body)
    stages = data.get("stages") if isinstance(data, dict) else None
    return isinstance(stages, list) and any(isinstance(s, dict) and s.get("gate") for s in stages)


def queue_job(conn: Connection, *, team_id: str, project_id: str | None, workflow_id: str, inputs: Any,
              now: datetime, trigger_id: str | None = None, delivery_id: str | None = None,
              thread: str | None = None, resume: tuple[str, Any] | None = None, dry_run: bool = False,
              key: str | None = None, not_before: datetime | None = None, supersede: bool = False,
              cancel_running: bool = False, sticky: Iterable[str] = (), rerun_of: str | None = None,
              requested_by: str | None = None) -> str:
    """Queue a workflow job at the workflow's current version, with its callees pinned. Returns the job id.

    ``resume`` is a gate id and the job that paused there: the new job continues that job's run, at the version,
    pins, and dry mode it ran with. Raises ValueError when a called workflow doesn't resolve.

    Jobs of the workflow with the same ``key`` run one at a time. With ``supersede`` the new job replaces the key's
    queued ones and keeps any ``sticky`` input one of them set. With ``cancel_running`` it also asks the key's
    running job to stop. A job with ``not_before`` waits until then to be claimed. The run a job with ``rerun_of``
    opens links back to that run, with its trigger, and names ``requested_by`` as who triggered it.
    """
    if resume is not None:
        gate_id, paused = resume
        pinned = {"workflow_version": paused.workflow_version, "call_pins": paused.call_pins,
                  "requires": paused.requires, "run_id": paused.run_id, "resume_of_gate": gate_id,
                  "dry_run": bool(paused.dry_run)}
    else:
        row = conn.execute(select(workflows.c.current_version, workflows.c.scope, workflows.c.name)
                           .where(workflows.c.id == workflow_id)).first()
        version = row.current_version if row is not None else None
        body = None if version is None else defs.body_of(conn, defs.WORKFLOW, workflow_id, version)
        pins, callees = calls.pin(conn, row.scope, body, row.name) if row is not None else ({}, [])
        pinned = {"workflow_version": version, "call_pins": pins,
                  "requires": requirements_with_calls(body, callees), "dry_run": dry_run}
    job_id = new_id()
    same = (jobs.c.concurrency_key == key, jobs.c.workflow_id == workflow_id)
    older = conn.execute(select(jobs.c.id, jobs.c.inputs).where(*same, jobs.c.status == "queued")
                         .order_by(jobs.c.id).with_for_update()).all() if key is not None and supersede else []
    for name in sticky:
        kept = next((row.inputs[name] for row in older if isinstance(row.inputs, dict) and row.inputs.get(name)),
                    None)
        if kept is not None and isinstance(inputs, dict) and not inputs.get(name):
            inputs = {**inputs, name: kept}
    conn.execute(insert(jobs).values(id=job_id, team_id=team_id, project_id=project_id, workflow_id=workflow_id,
                                     inputs=inputs, status="queued", attempts=0, trigger_id=trigger_id,
                                     delivery_id=delivery_id, thread=thread, created_at=now, concurrency_key=key,
                                     not_before=not_before, rerun_of=rerun_of, requested_by=requested_by,
                                     **pinned))
    if older:
        conn.execute(update(jobs).where(jobs.c.id.in_([row.id for row in older]), jobs.c.status == "queued")
                     .values(status="superseded", superseded_by=job_id, finished_at=now, error=SUPERSEDED))
    if key is not None and cancel_running:
        conn.execute(update(jobs).where(*same, jobs.c.status.in_(HELD), jobs.c.cancel_requested_at.is_(None))
                     .values(cancel_requested_at=now))
    return job_id


def cancel_key(conn: Connection, *, workflow_id: str, key: str, now: datetime, reason: str) -> int:
    """End the key's queued jobs and ask its running one to stop, queuing nothing. Returns how many jobs it touched."""
    same = (jobs.c.concurrency_key == key, jobs.c.workflow_id == workflow_id)
    ended = conn.execute(update(jobs).where(*same, jobs.c.status == "queued")
                         .values(status="superseded", finished_at=now, error=reason)).rowcount
    asked = conn.execute(update(jobs).where(*same, jobs.c.status.in_(HELD), jobs.c.cancel_requested_at.is_(None))
                         .values(cancel_requested_at=now)).rowcount
    return ended + asked


_held = jobs.alias("held_job")


def ready(now: datetime | None) -> Any:
    """Whether a queued job may be claimed: no job of its key is held, and with ``now``, its quiet period is over."""
    held = select(_held.c.id).where(_held.c.concurrency_key == jobs.c.concurrency_key,
                                    _held.c.workflow_id == jobs.c.workflow_id, _held.c.status.in_(HELD)).exists()
    free = or_(jobs.c.concurrency_key.is_(None), ~held)
    return free if now is None else and_(free, or_(jobs.c.not_before.is_(None), jobs.c.not_before <= now))


def claim_guard(conn: Connection, job_id: str, now: datetime) -> list[Any]:
    """The claim UPDATE's conditions past its status: ``ready``, checked again where the claim writes.

    The key's rows are locked first, so two claims of one key take turns where a backend allows it.
    """
    row = conn.execute(select(jobs.c.concurrency_key, jobs.c.workflow_id).where(jobs.c.id == job_id)).first()
    conditions = [or_(jobs.c.not_before.is_(None), jobs.c.not_before <= now)]
    if row is None or row.concurrency_key is None:
        return conditions
    same = (jobs.c.concurrency_key == row.concurrency_key, jobs.c.workflow_id == row.workflow_id)
    conn.execute(select(jobs.c.id).where(*same, jobs.c.status.in_(("queued", *HELD)))
                 .order_by(jobs.c.id).with_for_update()).all()
    # A derived table, since MySQL refuses a subquery on the table an UPDATE writes.
    held = select(jobs.c.id).where(*same, jobs.c.status.in_(HELD)).subquery("held_job")
    conditions.append(~select(held.c.id).exists())
    return conditions


def meets(requires: Iterable[str] | None, capabilities: Iterable[str] | None) -> bool:
    """Whether capabilities cover a job's requirements. None capabilities means no limit."""
    return capabilities is None or set(requires or ()) <= set(capabilities)


def claimable(conn: Connection, *, capabilities: Iterable[str] | None, allowlist: Iterable[str] | None = None,
              team_id: str | None = None, bound_key: str | None = None, now: datetime | None = None) -> list[str]:
    """Queued workflow jobs these capabilities may run, oldest first, from the oldest ``CLAIM_SCAN`` queued.

    A job whose key is held waits. With ``now``, so does one still in its quiet period.

    With ``team_id``, as for a team runner key, only that team's jobs, and none whose trigger names a secret
    set only for the instance: those wait for an instance runner. With ``bound_key``, an environment's ephemeral
    key, only the job that environment was launched for.
    """
    if bound_key is not None:
        rows = conn.execute(select(jobs.c.id, jobs.c.requires)
                            .select_from(jobs.join(environments, environments.c.job_id == jobs.c.id))
                            .where(environments.c.runner_key_id == bound_key, jobs.c.kind == JOB_KIND,
                                   environments.c.state.in_(("requested", "booting", "ready")),
                                   jobs.c.status == "provisioning")).all()
        return [r.id for r in rows if meets(r.requires, allowlist)]
    query = (select(jobs.c.id, jobs.c.requires, triggers.c.config)
             .select_from(jobs.outerjoin(triggers, triggers.c.id == jobs.c.trigger_id))
             .where(jobs.c.status == "queued", jobs.c.kind == JOB_KIND, ready(now)))
    if team_id is not None:
        query = query.where(jobs.c.team_id == team_id)
    rows = [r for r in conn.execute(query.order_by(jobs.c.created_at, jobs.c.id).limit(CLAIM_SCAN)).all()
            if meets(r.requires, capabilities) and meets(r.requires, allowlist)]
    if team_id is None:
        return [r.id for r in rows]
    wanted = {r.id: set((r.config or {}).get("secrets") or []) for r in rows}
    names = set().union(*wanted.values())
    scopes: dict[str, set[str]] = {}
    if names:
        for scope, name in conn.execute(select(secrets.c.scope, secrets.c.name).where(
                secrets.c.name.in_(names), secrets.c.scope.in_([team_id, INSTANCE_SCOPE]))).all():
            scopes.setdefault(name, set()).add(scope)
    return [r.id for r in rows if not any(scopes.get(name) == {INSTANCE_SCOPE} for name in wanted[r.id])]


def pauses(conn: Connection, job_id: str) -> bool:
    """Whether the job resumes a run from a gate, or its workflow's pinned version has a gate it may pause at."""
    job = conn.execute(select(jobs.c.workflow_id, jobs.c.workflow_version, jobs.c.resume_of_gate)
                       .where(jobs.c.id == job_id)).first()
    if job is None:
        return False
    if job.resume_of_gate is not None:
        return True
    return job.workflow_version is not None and gated(
        defs.body_of(conn, defs.WORKFLOW, job.workflow_id, job.workflow_version))


def calls_workflows(conn: Connection, job_id: str) -> bool:
    """Whether the pinned version of the job's workflow has a ``call:`` step."""
    job = conn.execute(select(jobs.c.workflow_id, jobs.c.workflow_version).where(jobs.c.id == job_id)).first()
    if job is None or job.workflow_version is None:
        return False
    return bool(calls.body_refs(defs.body_of(conn, defs.WORKFLOW, job.workflow_id, job.workflow_version)))


def team_env(conn: Connection, team_id: str, project_id: str | None = None) -> dict[str, str]:
    """The ``env.NAME`` settings as NAME: instance, then team, then project, so the narrowest setting wins."""
    order = [INSTANCE_SCOPE, team_id, *([project_id] if project_id else [])]
    rows = conn.execute(select(settings.c.scope, settings.c.key, settings.c.value).where(
        settings.c.scope.in_(order), settings.c.key.startswith(ENV_PREFIX, autoescape=True))).all()
    env = {}
    # LIKE ignores case on some backends, so the prefix is checked again here.
    for row in sorted((r for r in rows if r.key.startswith(ENV_PREFIX)), key=lambda r: order.index(r.scope)):
        name = row.key[len(ENV_PREFIX):]
        if ENV_NAME.fullmatch(name) and isinstance(row.value, str):
            env[name] = row.value
    return env


def _instance_setting(conn: Connection, key: str) -> Any:
    return conn.execute(select(settings.c.value).where(settings.c.scope == INSTANCE_SCOPE,
                                                       settings.c.key == key)).scalar()


@dataclass(frozen=True)
class Bundle:
    """Everything one job's run needs, so whatever runs it never reads the database."""
    job_id: str
    run_id: str
    token: str
    events: str
    workflow_id: str
    workflow_name: str
    workflow_version: int
    workflow: str
    inputs: dict[str, Any]
    args: list[str]
    settings: dict[str, str]
    secrets: dict[str, str]
    calls: dict[str, str]
    summary: dict[str, Any]
    skills: dict[str, str] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    thread: str | None = None
    # None when the workflow has no gate. Else the stage results to resume from, empty on a first run.
    checkpoint: dict[str, Any] | None = None
    # Handed to the engine on a pipe by whatever starts the run, never put in its environment.
    engine_key: str | None = None

    def env(self) -> dict[str, str]:
        """What the run's environment adds to the host's: settings, then secrets, then the event channel."""
        env = {**self.settings, **self.secrets, "ARMATURE_EVENTS": self.events, "ARMATURE_EVENTS_TOKEN": self.token}
        if self.thread:
            env[THREAD_ENV] = self.thread
        return env

    def payload(self) -> dict[str, Any]:
        return {"bundle_version": BUNDLE_VERSION, "job_id": self.job_id, "run_id": self.run_id,
                "run_token": self.token, "events_url": self.events,
                "workflow": {"id": self.workflow_id, "name": self.workflow_name, "version": self.workflow_version,
                             "body": self.workflow},
                "inputs": self.inputs, "args": self.args, "settings": self.settings, "secrets": self.secrets,
                "calls": self.calls, "skills": self.skills, "roles": self.roles, "thread": self.thread, "checkpoint": self.checkpoint,
                "engine_key": self.engine_key}


def bundle_calls(conn: Connection, job: Any, workflow: Any, body: str) -> dict[str, str]:
    """The call bundle a job's ``call:`` steps read, by file name: manifest.json and each <workflow>@<version>.yaml.

    Runners write it to ``<run_dir>/calls/``. A job queued before pins existed resolves its callees now.
    """
    try:
        return calls.bundle(conn, workflow.scope, body, workflow.name, job.call_pins, dryrun.stored_body)
    except ValueError as exc:
        raise JobError(f"A called workflow doesn't resolve: {exc}") from exc


def _scopes(team_id: str, project_id: str | None) -> list[str]:
    return [s for s in (project_id, team_id, INSTANCE_SCOPE) if s]


def git_hosts(conn: Connection, team_id: str, project_id: str | None = None) -> list[str]:
    """The hosts an agent step may clone from: the narrowest ``agent.git_hosts`` setting, else github.com."""
    order = _scopes(team_id, project_id)
    rows = {row.scope: row.value for row in conn.execute(select(settings.c.scope, settings.c.value).where(
        settings.c.scope.in_(order), settings.c.key == GIT_HOSTS_KEY))}
    value = next((rows[s] for s in order if s in rows), None)
    hosts = [h.strip().lower() for h in value if isinstance(h, str) and h.strip()] if isinstance(value, list) else []
    return hosts or ["github.com"]


def _documents(conn: Connection, kind: str, keys: Iterable[str], order: list[str]) -> dict[str, str]:
    """The current body of each named document of ``kind``: the narrowest scope in ``order`` wins."""
    current = and_(versions.c.object_kind == defs.DOCUMENT.object_kind, versions.c.object_id == documents.c.id,
                   versions.c.version == documents.c.current_version)
    rows = conn.execute(select(documents.c.key, documents.c.scope, versions.c.body)
                        .select_from(documents.join(versions, current))
                        .where(documents.c.kind == kind, documents.c.key.in_(sorted(set(keys))),
                               documents.c.scope.in_(order))).all()
    found = {}
    for row in sorted(rows, key=lambda r: order.index(r.scope), reverse=True):
        found[row.key] = row.body
    return found


def bundle_skills(conn: Connection, team_id: str, project_id: str | None, names: Iterable[str]) -> dict[str, str]:
    """The current body of each named skill document: the project's, else the team's, else the instance's."""
    names = sorted(set(names))
    if not names:
        return {}
    found = _documents(conn, "skill", names, _scopes(team_id, project_id))
    missing = sorted(set(names) - found.keys())
    if missing:
        raise JobError(f"missing skill {', '.join(missing)}")
    return found


def bundle_roles(conn: Connection, team_id: str, project_id: str | None, keys: Iterable[str]) -> dict[str, str]:
    """Each named role document's instructions, without its front matter, resolved as skills are."""
    keys = sorted(set(keys))
    if not keys:
        return {}
    found = _documents(conn, "role", keys, _scopes(team_id, project_id))
    missing = sorted(set(keys) - found.keys())
    if missing:
        raise JobError(f"missing role {', '.join(missing)}")
    large = sorted(key for key, body in found.items() if len(body.encode("utf-8")) > defs.SKILL_MAX)
    if large:
        raise JobError(f"role {', '.join(large)} is over {defs.SKILL_MAX // 1024} KB")
    return {key: _without_front_matter(body) for key, body in found.items()}


def _without_front_matter(body: str) -> str:
    match = defs.FRONT_MATTER.match(body)
    return body[match.end():].lstrip("\n") if match else body


def _provider_env(row: Any, credential: str | None) -> dict[str, str]:
    if row.kind == "claude-subscription" and credential:
        return {"CLAUDE_CODE_OAUTH_TOKEN": credential}
    if row.kind == "anthropic" and credential:
        return {"ANTHROPIC_API_KEY": credential}
    if row.kind == "bedrock":
        if credential:
            return llm.bedrock_env(row, credential)
        region = (row.config or {}).get("region")
        return {"CLAUDE_CODE_USE_BEDROCK": "1", **({"AWS_REGION": region} if region else {})}
    if row.kind == "openai-compatible" and credential:
        return {"OPENAI_API_KEY": credential}
    raise JobError(f"A {row.kind} provider without a credential can't run an agent step.")


def agent_models(conn: Connection, keyring: Keyring, team_id: str, project_id: str | None, aliases: Iterable[str],
                 *, instance: bool) -> dict[str, str]:
    """The run env naming each agent step's model id and the credential env of the provider its alias resolves to."""
    models = {}
    for alias in sorted(set(aliases)):
        found = defs.resolve_alias(conn, alias, team_id, project_id)
        if found is None:
            raise JobError(f"No provider defines the model alias {alias}.")
        row, model = found
        if not defs.provider_configured(conn, row):
            raise JobError(f"The credential secret of the provider for {alias} is not set.")
        if not instance and defs.credential_team(row) is None and (row.config or {}).get("credential_secret"):
            raise JobError(f"The provider for {alias} uses an instance secret, and a team runner can't receive one.")
        models[alias] = {"model": model, "env": _provider_env(row, defs.provider_credential(conn, keyring, row))}
    return {MODELS_ENV: json.dumps(models)} if models else {}


def _trigger_ref(conn: Connection, job: Any, trigger: Any) -> tuple[str, dict[str, Any]]:
    """What the run records as its start: a webhook delivery, or the issue or schedule of a poll trigger."""
    if trigger is not None and trigger.kind in POLL_RUN_KINDS:
        if trigger.kind == "schedule":
            ref = {"cron": (trigger.config or {}).get("cron")}
        else:
            ref = {"issue": conn.execute(select(trigger_seen.c.item_key).where(
                trigger_seen.c.trigger_id == job.trigger_id, trigger_seen.c.job_id == job.id)).scalar()}
        return POLL_RUN_KINDS[trigger.kind], {"trigger_id": job.trigger_id,
                                              **{k: v for k, v in ref.items() if v is not None}}
    return TRIGGER_KIND, {key: value for key, value in (("webhook_id", trigger.webhook_id if trigger else None),
                                                        ("delivery_id", job.delivery_id),
                                                        ("trigger_id", job.trigger_id)) if value is not None}


def _rerun_ref(conn: Connection, job: Any) -> tuple[str, dict[str, Any]] | None:
    """A rerun starts as the run it reruns did, since the trigger's seen keys still point at that run's job."""
    if job.rerun_of is None:
        return None
    row = conn.execute(select(runs.c.trigger_kind, runs.c.trigger_ref).where(runs.c.id == job.rerun_of)).first()
    if row is None or row.trigger_kind is None or not isinstance(row.trigger_ref, dict):
        return None
    return row.trigger_kind, {k: v for k, v in row.trigger_ref.items() if k != "dry_run"}


def build_bundle(conn: Connection, keyring: Keyring, job: Any, *, now: datetime, lease: timedelta,
                 events: str, runner_key_id: str | None = None, instance_secrets: bool = True,
                 location: dict[str, Any] | None = None) -> Bundle:
    """Open the run for a claimed job, mark the job running, and gather what its run needs.

    Secrets are decrypted into the bundle only, never written to a row.
    """
    workflow = conn.execute(select(workflows.c.id, workflows.c.name, workflows.c.scope)
                            .where(workflows.c.id == job.workflow_id)).first()
    if workflow is None:
        raise JobError("The workflow no longer exists.")
    body = None if job.workflow_version is None else defs.body_of(conn, defs.WORKFLOW, workflow.id,
                                                                  job.workflow_version)
    if body is None:
        raise JobError(f"Workflow {workflow.name} has no version {job.workflow_version}.")
    trigger = None if job.trigger_id is None else conn.execute(
        select(triggers.c.config, triggers.c.webhook_id, triggers.c.kind).where(triggers.c.id == job.trigger_id)).first()
    names = ((trigger.config or {}) if trigger is not None else {}).get("secrets") or []
    try:
        secret_values = secret_store.resolve_for_run(conn, keyring, job.team_id, list(names),
                                                     instance=instance_secrets)
    except secret_store.MissingSecrets as exc:
        raise JobError(f"missing secret {', '.join(exc.args[0])}") from exc
    except secret_store.InstanceOnlySecrets as exc:
        raise JobError(f"Secret {', '.join(exc.args[0])} is set only for the instance, and a team runner "
                       "can't receive an instance secret. Set it for the team, or run the job on an instance "
                       "runner.") from exc
    try:
        args = dryrun.input_args(job.inputs)
    except dryrun.DryRunError as exc:
        raise JobError(exc.message) from exc
    call_files = bundle_calls(conn, job, workflow, body)
    # Agent steps inside called workflows run in this job too, so their skills and models ride in its bundle.
    bodies = [body, *(text for name, text in call_files.items() if name != calls.MANIFEST)]
    steps = [step for text in bodies for step in agent_steps(_load(text))]
    skills = bundle_skills(conn, job.team_id, job.project_id,
                           [n for a in steps for n in a.get("skills") or [] if isinstance(n, str)])
    roles = bundle_roles(conn, job.team_id, job.project_id,
                         [a["role"] for a in steps if isinstance(a.get("role"), str)])
    # These go in after the trigger's secrets, so a secret of the same name can't widen them.
    for name in (MODELS_ENV, GIT_HOSTS_ENV, PROJECT_REPO_ENV):
        secret_values.pop(name, None)
    secret_values.update(agent_models(conn, keyring, job.team_id, job.project_id,
                                      [a["model"] for a in steps if isinstance(a.get("model"), str)],
                                      instance=instance_secrets))
    if steps:
        secret_values[GIT_HOSTS_ENV] = ",".join(git_hosts(conn, job.team_id, job.project_id))
        repo = None if job.project_id is None else conn.execute(
            select(projects.c.repo).where(projects.c.id == job.project_id)).scalar()
        if repo:
            secret_values[PROJECT_REPO_ENV] = repo
    run_settings = {k: v for k, v in team_env(conn, job.team_id, job.project_id).items()
                    if k not in (MODELS_ENV, GIT_HOSTS_ENV, PROJECT_REPO_ENV, THREAD_ENV)}
    checkpoint = {} if gated(body) else None
    if job.resume_of_gate is not None:
        checkpoint = gates.resume_checkpoint(conn, job)
        if checkpoint is None:
            raise JobError("The gate this job resumes from is gone.")
        run_id, token = runners.reopen_run(conn, keyring, run_id=job.run_id, now=now, runner_key_id=runner_key_id,
                                          location=location)
        if run_id is None:
            raise JobError("The run this job resumes is no longer waiting at a gate.")
    else:
        trigger_kind, trigger_ref = _rerun_ref(conn, job) or _trigger_ref(conn, job, trigger)
        if job.dry_run:
            trigger_ref["dry_run"] = True
        run_row, token = runners.open_run(conn, keyring, team_id=job.team_id, project_id=job.project_id,
                                          workflow_id=workflow.id, workflow_name=workflow.name, now=now,
                                          workflow_version=job.workflow_version, triggered_by=job.requested_by, actor=None,
                                          trigger_kind=trigger_kind, trigger_ref=trigger_ref,
                                          runner_key_id=runner_key_id, thread=job.thread, location=location,
                                          rerun_of=job.rerun_of)
        run_id = run_row["id"]
    conn.execute(update(jobs).where(jobs.c.id == job.id).values(
        run_id=run_id, status="running", heartbeat_at=now, lease_expires_at=now + lease))
    return Bundle(job_id=job.id, run_id=run_id, token=token, events=events, workflow_id=workflow.id,
                  workflow_name=workflow.name, workflow_version=job.workflow_version,
                  workflow=dryrun.stored_body(body), inputs=dict(job.inputs or {}), args=args,
                  settings=run_settings, secrets=secret_values, calls=call_files,
                  summary=load_summary(conn, run_id), skills=skills, roles=roles, thread=job.thread, checkpoint=checkpoint,
                  engine_key=runners.engine_env(keyring, run_id))


class Worker:
    """Polls every ``interval`` seconds while started. Tests leave it stopped and call ``tick`` themselves."""

    def __init__(self, engine: Engine, keyring: Keyring, cfg: ServerConfig, hub: Any, *,
                 clock: Callable[[], datetime] = utcnow, interval: float = INTERVAL_S, lease: timedelta = LEASE,
                 work_dir: str | Path = WORK_DIR, worker_id: str | None = None):
        self.engine = engine
        self.keyring = keyring
        self.cfg = cfg
        self.hub = hub
        self.interval = interval
        self.lease = lease
        self.work_dir = Path(work_dir).expanduser()
        self.id = worker_id or f"{socket.gethostname()[:150]}:{os.getpid()}:{_random.token_hex(4)}"
        self._clock = clock
        self._procs: dict[str, subprocess.Popen] = {}
        self._launches: dict[str, dryrun.Launch] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Tests set this, so the Slack calls the sweeps make reach a fake.
        self.vendor_transport: Any = None

    def start(self) -> None:
        """Settle what an earlier server process left behind, then start polling."""
        self.reap(self._clock())
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="armature-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        # Runs start in their own session, so nothing else stops them, and a later reap would mark them lost.
        for proc in list(self._procs.values()):
            dryrun._stop(proc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("job worker tick failed")
            self._stop.wait(self.interval)

    def tick(self) -> None:
        now = self._clock()
        self.heartbeat(now)
        self.reap(now)
        self.finish(now)
        with self.engine.begin() as conn:
            gates.expire(conn, now)
            gates.prune(conn)
        # Slack posting must never stop the claims below.
        try:
            conversations.sweep(self.engine, self.keyring, now, self.vendor_transport)
        except Exception:
            log.exception("the conversation sweep failed")
        try:
            gatecards.sync(self.engine, self.keyring, f"{self.cfg.public_origin}{self.cfg.base_path}", now,
                           self.vendor_transport)
        except Exception:
            log.exception("the gate card sweep failed")
        with self.engine.connect() as conn:
            enabled = _instance_setting(conn, ENABLED_KEY)
            slots = _instance_setting(conn, SLOTS_KEY)
        if enabled is False:
            return
        if not isinstance(slots, int) or isinstance(slots, bool) or slots < 0:
            slots = DEFAULT_SLOTS
        while self.held() < slots and not self._stop.is_set():
            job_id = self.claim(self._clock())
            if job_id is None:
                return
            self.launch(job_id)

    def held(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(jobs).where(
                jobs.c.claimed_by == self.id, jobs.c.status.in_(HELD))).scalar_one()

    def claim(self, now: datetime) -> str | None:
        """Claim the oldest queued job this worker can run. None when there is none or another worker took it first.

        Without a ``worker.capabilities`` setting it runs only jobs that require nothing, which every job queued
        before ``requires:`` existed is. It never runs agent sessions, which need a runner with ``--agent-user``
        or ``--same-user``.
        """
        with self.engine.connect() as conn:
            capabilities = _instance_setting(conn, CAPABILITIES_KEY)
            capabilities = [c for c in capabilities if c not in AGENT_HARNESSES] \
                if isinstance(capabilities, list) else []
            found = claimable(conn, capabilities=[*capabilities, CALLS_CAPABILITY, ROLES_CAPABILITY], now=now)
        return next((job_id for job_id in found if self.try_claim(job_id, now)), None)

    def try_claim(self, job_id: str, now: datetime) -> bool:
        with self.engine.begin() as conn:
            return conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status == "queued",
                                                   *claim_guard(conn, job_id, now)).values(
                status="claimed", claimed_by=self.id, claimed_at=now, heartbeat_at=now,
                lease_expires_at=now + self.lease, attempts=jobs.c.attempts + 1)).rowcount == 1

    def heartbeat(self, now: datetime) -> None:
        for job_id in [j for j, proc in self._procs.items() if proc.poll() is not None]:
            del self._procs[job_id]
            self._launches.pop(job_id, None)
        if not self._procs:
            return
        with self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id.in_(list(self._procs)), jobs.c.claimed_by == self.id,
                                            jobs.c.status == "running")
                         .values(heartbeat_at=now, lease_expires_at=now + self.lease))
            canceled = conn.execute(select(jobs.c.id).where(jobs.c.id.in_(list(self._launches)),
                                                            jobs.c.cancel_requested_at.is_not(None))).scalars().all()
        for job_id in canceled:
            self.cancel(job_id)

    def cancel(self, job_id: str) -> None:
        """End the run as canceled, then stop its process, so the watcher finds it ended and leaves it."""
        launch = self._launches.pop(job_id, None)
        proc = self._procs.get(job_id)
        if launch is None or proc is None:
            return
        try:
            dryrun.fail_run(self.hub, launch, "Canceled.", getattr(self.hub, "_loop", None), status="canceled")
        finally:
            dryrun._stop(proc)

    def reap(self, now: datetime) -> int:
        """Settle every claimed or running job whose lease ran out, from its run when it ended, else as lost."""
        with self.engine.begin() as conn:
            rows = conn.execute(select(jobs.c.id, jobs.c.status, jobs.c.cancel_requested_at,
                                       runs.c.status.label("run_status"), runs.c.error.label("run_error"))
                                .select_from(jobs.outerjoin(runs, runs.c.id == jobs.c.run_id))
                                .where(jobs.c.status.in_(LEASED), jobs.c.lease_expires_at < now)).all()
            for row in rows:
                if row.run_status == RUN_WAITING:
                    values = {"status": "failed", "error": "The run paused, and no gate opened before the lease ended."}
                elif row.run_status is not None and row.run_status not in RUN_OPEN:
                    values = {"status": outcome(row.run_status, row.cancel_requested_at is not None),
                              "error": row.run_error}
                else:
                    values = {"status": "lost", "error": "lease expired"}
                if conn.execute(update(jobs).where(jobs.c.id == row.id, jobs.c.status == row.status,
                                                   jobs.c.lease_expires_at < now)
                                .values(finished_at=now, **values)).rowcount == 1:
                    gates.fail_waiting(conn, job_id=row.id, error=values["error"], now=now)
        return len(rows) + self.reap_provisioning(now)

    def reap_provisioning(self, now: datetime) -> int:
        """Requeue, once, or fail a provisioning job whose environment never came, with no provider needed.

        An environment still requested or booting well past its boot timeout, as after a restart with no verified
        connection, is marked failed first. The orphan sweep terminates its instance once a provider is back.
        """
        boot = func.coalesce(environment_types.c.boot_timeout_s, aws_connections.c.boot_timeout_s)
        with self.engine.begin() as conn:
            stuck_envs = conn.execute(
                select(environments.c.id, environments.c.job_id, environments.c.runner_key_id, environments.c.requested_at,
                       boot.label("boot"))
                .select_from(environments.join(jobs, jobs.c.id == environments.c.job_id)
                             .join(aws_connections, aws_connections.c.id == environments.c.connection_id)
                             .outerjoin(environment_types, environment_types.c.id == environments.c.type_id))
                .where(jobs.c.status == "provisioning", environments.c.state.in_(("requested", "booting")))).all()
            unstuck = []
            for env in stuck_envs:
                if env.requested_at + timedelta(seconds=env.boot) + PROVISIONING_MARGIN < now:
                    unstuck.append(env.job_id)
                    conn.execute(update(environments).where(environments.c.id == env.id,
                                                            environments.c.state.in_(("requested", "booting")))
                                 .values(state="failed", slot_index=None, terminated_at=now,
                                         error="The environment didn't boot, and no provider could end it."))
                    if env.runner_key_id:
                        runners.revoke_key(conn, team_id=None, key_id=env.runner_key_id, actor_id=None, now=now,
                                           ip=None)
        up = select(environments.c.id).where(environments.c.job_id == jobs.c.id,
                                             environments.c.state.in_(ENVIRONMENT_UP)).exists()
        tries = (select(func.count()).select_from(environments).where(environments.c.job_id == jobs.c.id)
                 .scalar_subquery())
        with self.engine.begin() as conn:
            rows = conn.execute(select(jobs.c.id, tries.label("tries")).where(
                jobs.c.status == "provisioning", ~up,
                or_(jobs.c.lease_expires_at < now - PROVISIONING_MARGIN, jobs.c.id.in_(unstuck)))).all()
            for row in rows:
                stuck = (jobs.c.id == row.id, jobs.c.status == "provisioning")
                if row.tries < MAX_ENVIRONMENTS_PER_JOB:
                    conn.execute(update(jobs).where(*stuck).values(status="queued", claimed_by=None, claimed_at=None,
                                                                   lease_expires_at=None))
                elif conn.execute(update(jobs).where(*stuck).values(
                        status="failed", error="No environment started for this job.", finished_at=now)).rowcount:
                    gates.fail_waiting(conn, job_id=row.id, error="No environment started for this job.", now=now)
        return len(rows)

    def finish(self, now: datetime) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(select(jobs.c.id, jobs.c.cancel_requested_at, runs.c.status, runs.c.error)
                                .join(runs, runs.c.id == jobs.c.run_id)
                                .where(jobs.c.status == "running",
                                       runs.c.status.not_in((*RUN_OPEN, RUN_WAITING)))).all()
            for row in rows:
                conn.execute(update(jobs).where(jobs.c.id == row.id, jobs.c.status == "running").values(
                    status=outcome(row.status, row.cancel_requested_at is not None), error=row.error,
                    finished_at=now))

    def _fail(self, job_id: str, error: str) -> None:
        with self.engine.begin() as conn:
            if conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(HELD))
                            .values(status="failed", error=error, finished_at=self._clock())).rowcount == 1:
                gates.fail_waiting(conn, job_id=job_id, error=error, now=self._clock())

    def launch(self, job_id: str) -> None:
        """Start a job this worker claimed. Any problem before the process starts fails the job instead."""
        workdir = self.work_dir / job_id
        binary = dryrun.armature_binary()
        try:
            with self.engine.begin() as conn:
                job = conn.execute(select(jobs).where(jobs.c.id == job_id, jobs.c.claimed_by == self.id,
                                                      jobs.c.status == "claimed")).first()
                if job is None:
                    return
                launch, args, env = self._prepare(conn, job, binary, workdir)
        except JobError as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            self._fail(job_id, str(exc))
            return
        except Exception:
            log.exception("preparing job %s failed", job_id)
            shutil.rmtree(workdir, ignore_errors=True)
            self._fail(job_id, "The worker could not start this job.")
            return
        try:
            self._procs[job_id] = dryrun.spawn(binary, launch, args, env, self.hub, label="run",
                                               memory_mb=self.cfg.runner.memory_mb or None,
                                               paused=lambda: self._paused(job_id, workdir, launch.run_id))
        except OSError as exc:
            self._fail(job_id, f"The run could not start: {exc.strerror or exc}.")
            return
        self._launches[job_id] = launch
        loop = getattr(self.hub, "_loop", None)
        if loop is not None and self.hub.running:
            try:
                loop.call_soon_threadsafe(self.hub.run_started, launch.summary)
            except RuntimeError:
                log.warning("live view missed the start of job %s: the event loop is closed", job_id)

    def _paused(self, job_id: str, workdir: Path, run_id: str) -> bool:
        """Open the gate a run stopped at, from the request it left in its directory. False when it didn't stop.

        The file alone proves nothing, since any step could write it. The run must also have sent run_suspend.
        """
        if not (workdir / GATE_FILE).is_file():
            return False
        deadline = time.monotonic() + dryrun.END_WAIT_S
        while not self._suspend_reported(run_id):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
        gates.settle_paused(self.engine, self.keyring, job_id=job_id, request=read_gate(workdir), now=self._clock())
        return True

    def _suspend_reported(self, run_id: str) -> bool:
        with self.engine.connect() as conn:
            return gates.suspend_reported(conn, self.keyring, run_id)

    def _prepare(self, conn: Connection, job: Any, binary: str | None,
                 workdir: Path) -> tuple[dryrun.Launch, list[str], dict[str, str]]:
        if binary is None:
            raise JobError("armature executable not found")
        bundle = build_bundle(conn, self.keyring, job, now=self._clock(), lease=self.lease,
                              events=dryrun.events_endpoint(self.hub, self.cfg), location=this_machine())
        write_bundle(workdir, bundle.workflow, bundle.calls, bundle.skills, bundle.checkpoint, bundle.roles)
        launch = dryrun.Launch(bundle.run_id, bundle.token, workdir, bundle.summary, bundle.engine_key)
        # The host's PATH, HOME, and packs path go last, so no setting or secret replaces them.
        env = {**{k: v for k, v in bundle.env().items() if k not in HOST_ONLY_ENV},
               "PATH": job_path(binary), "HOME": os.environ.get("HOME") or str(Path.home()),
               PACKS_PATH_ENV: dryrun.packs_path(self.cfg.packs_dir),
               TOOLS_URL_ENV: f"{self.cfg.public_origin}{self.cfg.base_path}"}
        if bundle.skills:
            env[SKILLS_ENV] = str(workdir / SKILLS_DIR)
        if bundle.roles:
            env[ROLES_ENV] = str(workdir / ROLES_DIR)
        if bundle.checkpoint is not None:
            env[GATE_DIR_ENV] = str(workdir)
        return launch, bundle.args, env

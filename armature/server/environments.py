"""AWS environments: the connection, environment types, and one environment per job that needs the source.

A queued job that no online runner can take, and that needs more than
``llm``, gets an environment of its project's type. The dispatcher moves the
job to ``provisioning`` and records the environment in one transaction, then
asks the provider to launch it. The instance enrolls once with a short-lived
token, which buys an ephemeral runner key bound to that one job, and the
runner claims the job like any other. The reaper tears environments down when
their jobs end, when they fail to boot, or when they outlive their lifetime,
and terminates instances the database doesn't know.

Nothing here imports boto3: the provider comes from ``provider_loader``. The
service is inert until an AWS connection verifies.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets as _random
import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, case, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import audit, provider_loader, runners, worker
from armature.server.auth import valid_token
from armature.server.config import ServerConfig
from armature.server.crypto import Keyring, SealError
from armature.server.db import new_id, utcnow
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    aws_connections,
    environment_hosts,
    environment_types,
    environments,
    jobs,
    projects,
    runner_keys,
    settings,
)
from armature.server.db.schema import (
    runners as runners_table,
)
from armature.server.dispatch import ONLINE_WITHIN, RunnerError
from armature.server.dispatch import capabilities as check_capabilities
from armature.transport.provision import (
    Instance,
    LaunchSpec,
    ProvisionError,
    markets_for,
)

log = logging.getLogger(__name__)

INTERVAL_S = 10.0
STOP_TIMEOUT_S = 30.0
QUEUE_DELAY = timedelta(seconds=10)
RELAUNCH_AFTER = timedelta(minutes=2)
ORPHAN_GRACE = timedelta(minutes=10)
KEY_MARGIN = timedelta(minutes=15)
RETRY_AFTER = timedelta(minutes=1)
MAX_ENVIRONMENTS_PER_JOB = worker.MAX_ENVIRONMENTS_PER_JOB
BREAKER_FAILURES = 3
BREAKER_PAUSE = timedelta(minutes=10)
MAC_MIN_HOLD = timedelta(hours=24)
DAY = timedelta(hours=24)
ENROLL_PREFIX = "are_"
LLM = "llm"
CONNECTION_NAME = "default"
PROJECT_LIMIT_KEY = "environment.max_concurrent"
LIVE = ("requested", "booting", "ready", "running", "terminating")
UP = ("requested", "booting", "ready", "running")
DEFAULTS = {"max_concurrent": 4, "max_daily_launches": 50, "boot_timeout_s": 420, "max_lifetime_s": 5400}
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
ZONE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d[a-z]$")
SUBNET = re.compile(r"^subnet-[0-9a-f]{8,17}$")
VPC = re.compile(r"^vpc-[0-9a-f]{8,17}$")
GROUP = re.compile(r"^sg-[0-9a-f]{8,17}$")
TEMPLATE = re.compile(r"^lt-[0-9a-f]{8,17}$")
TEMPLATE_KINDS = {"ec2-linux": "linux", "ec2-mac": "mac"}
REPOSITORY = re.compile(r"^\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9._/-]{2,255}$")
STACK = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$")
ACCESS_KEY = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{12,124}$")
EXTERNAL_ID = re.compile(r"^[A-Za-z0-9+=,.@:/_-]{2,1224}$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
PROFILE = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,128}$")
TAG_VALUE = re.compile(r"^[A-Za-z0-9 _.:/=+@-]{0,256}$")
TAG_KEY = re.compile(r"^[A-Za-z0-9 _.:/=+@-]{1,128}$")
INSTALL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,11}$")
TYPE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
AMI = re.compile(r"^ami-[0-9a-f]{8,17}$")
TART_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,190}$")
INSTANCE_TYPE = re.compile(r"^[a-z0-9]+[a-z0-9-]*\.[a-z0-9]+$")
INSTALL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RESERVED_TAGS = frozenset({"project", "armature-install", "armature-env", "Name"})


class _NoSlot(Exception):
    pass


class EnvError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _check(value: Any, what: str, pattern: re.Pattern[str], *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise EnvError(400, f"{what} is required.")
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
        raise EnvError(400, f"{what} is not valid.")
    return value.strip()


def _whole(value: Any, what: str, low: int, high: int, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise EnvError(400, f"{what} is a whole number from {low} to {high}.")
    return value


def _money(value: Any, what: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise EnvError(400, f"{what} is a number of dollars.")
    try:
        amount = Decimal(str(value))
    except ArithmeticError as exc:
        raise EnvError(400, f"{what} is a number of dollars.") from exc
    if not amount.is_finite() or amount < 0 or amount > 1_000_000:
        raise EnvError(400, f"{what} is a number of dollars from 0 to 1000000.")
    return amount


def _caps(value: Any, what: str) -> list[str]:
    try:
        return check_capabilities(value, what)
    except RunnerError as exc:
        raise EnvError(exc.status, exc.message) from exc


# Connections

def connection(conn: Connection) -> Any | None:
    return conn.execute(select(aws_connections).order_by(aws_connections.c.created_at).limit(1)).first()


def _hint(credentials: dict[str, str]) -> dict[str, Any]:
    key = credentials.get("access_key_id") or ""
    return {"set": bool(credentials), "access_key_id_last4": key[-4:] if key else None,
            "role_arn": credentials.get("role_arn"), "external_id_set": bool(credentials.get("external_id"))}


def public_connection(keyring: Keyring, row: Any | None) -> dict[str, Any] | None:
    """The connection as the AWS page shows it: credentials only as whether they're set and a key hint."""
    if row is None:
        return None
    try:
        hint = _hint(provider_loader.credentials(keyring, row))
    except (SealError, ValueError):
        hint = {"set": True, "access_key_id_last4": None, "role_arn": None, "external_id_set": False,
                "unreadable": True}
    return {"id": row.id, "name": row.name, "region": row.region, "auth_kind": row.auth_kind, "credentials": hint,
            "vpc_id": row.vpc_id, "subnets": row.subnets or [], "security_group_id": row.security_group_id,
            "instance_profile": row.instance_profile, "launch_template_ids": row.launch_template_ids or {},
            "mac_instance_profile": row.mac_instance_profile, "image_repository_uri": row.image_repository_uri,
            "allowed_instance_types": row.allowed_instance_types or [], "stack_name": row.stack_name,
            "session_role_arn": row.session_role_arn, "artifact_bucket": row.artifact_bucket,
            "tag_project": row.tag_project, "extra_tags": row.extra_tags or {}, "install_id": row.install_id,
            "allow_host_allocation": bool(row.allow_host_allocation), "max_concurrent": row.max_concurrent,
            "max_daily_launches": row.max_daily_launches,
            "daily_budget_usd": float(row.daily_budget_usd) if row.daily_budget_usd is not None else None,
            "boot_timeout_s": row.boot_timeout_s, "max_lifetime_s": row.max_lifetime_s, "state": row.state,
            "last_error": row.last_error, "verified_at": row.verified_at, "updated_at": row.updated_at}


def _credentials(body: dict[str, Any], auth_kind: str) -> dict[str, str]:
    found: dict[str, str] = {}
    if auth_kind in ("access_key", "access_key_assume_role"):
        found["access_key_id"] = _check(body.get("access_key_id"), "access_key_id", ACCESS_KEY, required=True)
        secret = body.get("secret_access_key")
        if not isinstance(secret, str) or not 16 <= len(secret.strip()) <= 128 or not secret.strip().isprintable():
            raise EnvError(400, "secret_access_key is required.")
        found["secret_access_key"] = secret.strip()
    if auth_kind in ("assume_role", "access_key_assume_role"):
        found["role_arn"] = _check(body.get("role_arn"), "role_arn", ROLE_ARN, required=True)
        external = _check(body.get("external_id"), "external_id", EXTERNAL_ID)
        if external:
            found["external_id"] = external
    return found


def _subnets(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise EnvError(400, "subnets is a list of at most 16 {zone, subnet_id} objects.")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise EnvError(400, "Each subnet is a {zone, subnet_id} object.")
        out.append({"zone": _check(item.get("zone"), "A subnet's zone", ZONE, required=True),
                    "subnet_id": _check(item.get("subnet_id"), "A subnet id", SUBNET, required=True)})
    return out


def _templates(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"linux", "mac"}:
        raise EnvError(400, 'launch_template_ids is {"linux": "lt-...", "mac": "lt-..."}.')
    return {kind: _check(template, f"The {kind} launch template", TEMPLATE, required=True)
            for kind, template in value.items() if template}


def _instance_types(value: Any, what: str, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit or not all(
            isinstance(t, str) and INSTANCE_TYPE.fullmatch(t) for t in value):
        raise EnvError(400, f"{what} is a list of at most {limit} EC2 instance types, such as m6i.large.")
    return list(dict.fromkeys(value))


def disallowed(row: Any | None, instance_types: list[str]) -> list[str]:
    """The instance types the connection's IAM policy won't launch. None are when it lists no allowed types."""
    allowed = (row.allowed_instance_types or []) if row is not None else []
    return [t for t in instance_types if allowed and t not in allowed]


def not_allowed_message(name: str, types: list[str]) -> str:
    return (f"{name} uses {', '.join(types)}, which the AWS connection's allowed instance types leave out. "
            "Launches would fail with AccessDenied. Add them to AllowedInstanceTypes in the host-role stack and "
            "to the connection, or pick another type.")


def _tags(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 20:
        raise EnvError(400, "extra_tags is an object of at most 20 tags.")
    out = {}
    for key, tag in value.items():
        if not isinstance(key, str) or not TAG_KEY.fullmatch(key) or key in RESERVED_TAGS or key.startswith("aws:"):
            raise EnvError(400, f"Tag {key!r} is not allowed.")
        if not isinstance(tag, str) or not TAG_VALUE.fullmatch(tag):
            raise EnvError(400, f"Tag {key}'s value is not valid.")
        out[key] = tag
    return out


def set_connection(conn: Connection, keyring: Keyring, body: dict[str, Any], *, actor_id: str, now: datetime,
                   ip: str | None) -> Any:
    """Create or replace the connection's settings. Credentials are write-only and kept when left out.

    Any change makes the connection unverified, which stops launches until it verifies again.
    """
    row = connection(conn)
    auth_kind = body.get("auth_kind") or (row.auth_kind if row else None)
    if auth_kind not in ("access_key", "assume_role", "access_key_assume_role"):
        raise EnvError(400, "auth_kind is access_key, assume_role, or access_key_assume_role.")
    tag_project = (body.get("tag_project") or "armature-platform")
    if not isinstance(tag_project, str) or not TAG_VALUE.fullmatch(tag_project) or not tag_project.strip():
        raise EnvError(400, "tag_project is not valid.")
    if tag_project.strip() == "armature":
        raise EnvError(400, "tag_project can't be armature: armature-ops owns that tag and reaps by it.")
    values = {
        "region": _check(body.get("region"), "region", REGION, required=True),
        "auth_kind": auth_kind,
        "vpc_id": _check(body.get("vpc_id"), "vpc_id", VPC),
        "subnets": _subnets(body.get("subnets")),
        "security_group_id": _check(body.get("security_group_id"), "security_group_id", GROUP),
        "instance_profile": _check(body.get("instance_profile"), "instance_profile", PROFILE),
        "launch_template_ids": _templates(body.get("launch_template_ids")),
        "mac_instance_profile": _check(body.get("mac_instance_profile"), "mac_instance_profile", PROFILE),
        "image_repository_uri": _check(body.get("image_repository_uri"), "image_repository_uri", REPOSITORY),
        "allowed_instance_types": _instance_types(body.get("allowed_instance_types"), "allowed_instance_types", 64),
        "stack_name": _check(body.get("stack_name"), "stack_name", STACK),
        "session_role_arn": _check(body.get("session_role_arn"), "session_role_arn", ROLE_ARN),
        "artifact_bucket": _check(body.get("artifact_bucket"), "artifact_bucket", BUCKET),
        "tag_project": tag_project.strip(),
        "extra_tags": _tags(body.get("extra_tags")),
        "allow_host_allocation": bool(body.get("allow_host_allocation")),
        "max_concurrent": _whole(body.get("max_concurrent"), "max_concurrent", 0, 500,
                                 default=DEFAULTS["max_concurrent"]),
        "max_daily_launches": _whole(body.get("max_daily_launches"), "max_daily_launches", 0, 10_000,
                                     default=DEFAULTS["max_daily_launches"]),
        "daily_budget_usd": _money(body.get("daily_budget_usd"), "daily_budget_usd"),
        "boot_timeout_s": _whole(body.get("boot_timeout_s"), "boot_timeout_s", 60, 7200,
                                 default=DEFAULTS["boot_timeout_s"]),
        "max_lifetime_s": _whole(body.get("max_lifetime_s"), "max_lifetime_s", 600, 7 * 86400,
                                 default=DEFAULTS["max_lifetime_s"]),
        "state": "unverified", "last_error": None, "verified_at": None, "updated_at": now, "updated_by": actor_id,
    }
    connection_id = row.id if row else new_id()
    given = any(body.get(k) for k in ("access_key_id", "secret_access_key", "role_arn", "external_id"))
    if given or row is None or auth_kind != row.auth_kind:
        sealed = keyring.seal(_json(_credentials(body, auth_kind)), provider_loader.sealed_aad(connection_id),
                              label="aws")
        values.update(sealed=sealed, key_id=keyring.key_id)
    install_id = _check(body.get("install_id"), "install_id", INSTALL_ID)
    retagged = row is not None and (values["tag_project"] != row.tag_project
                                    or (install_id is not None and install_id != row.install_id))
    if retagged and _owns_anything(conn, row.id):
        raise EnvError(409, "Terminate every environment and release every Mac host before changing the install id "
                            "or project tag. The server finds its instances by those tags.")
    # Only a change in who the server is, where, or which instances it owns needs a new verify.
    if row is not None and "sealed" not in values and values["region"] == row.region and not retagged:
        for kept in ("state", "last_error", "verified_at"):
            values.pop(kept)
    if row is None:
        conn.execute(insert(aws_connections).values(id=connection_id, name=CONNECTION_NAME, created_at=now,
                                                    install_id=install_id or _random.token_hex(6), **values))
    else:
        if install_id:
            values["install_id"] = install_id
        conn.execute(update(aws_connections).where(aws_connections.c.id == connection_id).values(**values))
    changed = sorted(k for k in values if k not in ("sealed", "key_id", "updated_at", "updated_by", "state",
                                                    "last_error", "verified_at"))
    audit.record(conn, action="aws.connection.set", actor_id=actor_id, target_kind="aws_connection",
                 target_id=connection_id, detail={"fields": changed, "credentials_changed": "sealed" in values},
                 ip=ip)
    return conn.execute(select(aws_connections).where(aws_connections.c.id == connection_id)).one()


def _owns_anything(conn: Connection, connection_id: str) -> bool:
    live = conn.execute(select(environments.c.id).where(environments.c.connection_id == connection_id,
                                                        environments.c.state.in_(LIVE)).limit(1)).first()
    held = conn.execute(select(environment_hosts.c.id).where(environment_hosts.c.connection_id == connection_id,
                                                             environment_hosts.c.state != "released").limit(1)).first()
    return live is not None or held is not None


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


# Environment types

def public_type(row: Any) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "kind": row.kind, "image": row.image, "base_image": row.base_image,
            "installs": row.installs or [], "instance_types": row.instance_types or [], "market": row.market,
            "disk_gb": row.disk_gb, "vm_cpu": row.vm_cpu, "vm_memory_gb": row.vm_memory_gb,
            "capabilities": row.capabilities or [], "boot_timeout_s": row.boot_timeout_s,
            "job_timeout_s": row.job_timeout_s,
            "hourly_cost_usd": float(row.hourly_cost_usd) if row.hourly_cost_usd is not None else None,
            "health_check": row.health_check, "enabled": bool(row.enabled), "updated_at": row.updated_at}


def list_types(conn: Connection) -> list[dict[str, Any]]:
    return [public_type(r) for r in conn.execute(select(environment_types).order_by(environment_types.c.name))]


def _installs(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise EnvError(400, "installs is a list of at most 32 {name, version, url, sha256} objects.")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise EnvError(400, "Each install is a {name, version, url, sha256} object.")
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith("https://") or len(url) > 1000 or not url.isprintable():
            raise EnvError(400, "An install's url is an https:// address.")
        out.append({"name": _check(item.get("name"), "An install's name", INSTALL_NAME, required=True),
                    "version": _check(item.get("version"), "An install's version", INSTALL_NAME, required=True),
                    "url": url, "sha256": _check(item.get("sha256"), "An install's sha256", SHA256,
                                                     required=True)})
    return out


def set_type(conn: Connection, body: dict[str, Any], *, type_id: str | None, actor_id: str, now: datetime,
             ip: str | None) -> Any:
    kind = body.get("kind")
    if kind not in ("ec2-linux", "ec2-mac"):
        raise EnvError(400, "kind is ec2-linux or ec2-mac.")
    image = _check(body.get("image"), "image", AMI if kind == "ec2-linux" else TART_IMAGE, required=True)
    types = _instance_types(body.get("instance_types"), "instance_types", 8)
    if kind == "ec2-linux" and not types:
        raise EnvError(400, "An ec2-linux type needs at least one instance type.")
    refused = disallowed(connection(conn), types)
    if refused:
        raise EnvError(400, not_allowed_message(f"Type {body.get('name')}", refused))
    market = body.get("market") or "spot-then-on-demand"
    if market not in ("spot", "spot-then-on-demand", "on-demand"):
        raise EnvError(400, "market is spot, spot-then-on-demand, or on-demand.")
    health = body.get("health_check")
    if health is not None and not isinstance(health, dict):
        raise EnvError(400, "health_check is an object.")
    values = {
        "name": _check(body.get("name"), "name", TYPE_NAME, required=True), "kind": kind, "image": image,
        "base_image": _check(body.get("base_image"), "base_image", AMI if kind == "ec2-linux" else TART_IMAGE),
        "installs": _installs(body.get("installs")), "instance_types": types, "market": market,
        "disk_gb": _whole(body.get("disk_gb"), "disk_gb", 8, 4096),
        "vm_cpu": _whole(body.get("vm_cpu"), "vm_cpu", 1, 64),
        "vm_memory_gb": _whole(body.get("vm_memory_gb"), "vm_memory_gb", 1, 512),
        "capabilities": _caps(body.get("capabilities"), "capabilities"),
        "boot_timeout_s": _whole(body.get("boot_timeout_s"), "boot_timeout_s", 60, 7200),
        "job_timeout_s": _whole(body.get("job_timeout_s"), "job_timeout_s", 60, 7 * 86400, default=3600),
        "hourly_cost_usd": _money(body.get("hourly_cost_usd"), "hourly_cost_usd"), "health_check": health,
        "enabled": body.get("enabled") is not False, "updated_at": now, "updated_by": actor_id,
    }
    if type_id is None:
        type_id = new_id()
        conn.execute(insert(environment_types).values(id=type_id, created_at=now, **values))
    elif conn.execute(update(environment_types).where(environment_types.c.id == type_id)
                      .values(**values)).rowcount != 1:
        raise EnvError(404, "No such environment type.")
    audit.record(conn, action="env.type.set", actor_id=actor_id, target_kind="environment_type", target_id=type_id,
                 detail={"name": values["name"], "kind": kind, "enabled": values["enabled"]}, ip=ip)
    return conn.execute(select(environment_types).where(environment_types.c.id == type_id)).one()


# Enrollment

def mint_enrollment(keyring: Keyring, env_id: str) -> str:
    """The environment's enrollment token. Derived from its id, so a relaunch renders the same user-data."""
    mac = hmac.new(keyring.subkey("runners"), f"enroll:{env_id}".encode(), hashlib.sha256).digest()
    return ENROLL_PREFIX + base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def enroll(conn: Connection, keyring: Keyring, *, token: Any, name: Any, now: datetime,
           ip: str | None) -> tuple[str, Any]:
    """Trade an environment's enrollment token, once, for its runner key. Returns the key and the environment."""
    if not isinstance(token, str) or not token.startswith(ENROLL_PREFIX) or not valid_token(
            token[len(ENROLL_PREFIX):]):
        raise EnvError(401, "A valid enrollment token is required.")
    env = conn.execute(select(environments).where(environments.c.enroll_hmac == runners.digest(keyring, token))).first()
    if env is None:
        raise EnvError(401, "A valid enrollment token is required.")
    if env.enrolled_at is not None:
        raise EnvError(410, "This enrollment token was already used.")
    if env.enroll_expires_at is None or env.enroll_expires_at <= now or env.state not in ("requested", "booting"):
        raise EnvError(410, "This enrollment token has expired.")
    if conn.execute(update(environments).where(environments.c.id == env.id, environments.c.enrolled_at.is_(None),
                                               environments.c.state.in_(("requested", "booting")))
                    .values(enrolled_at=now)).rowcount != 1:
        raise EnvError(410, "This enrollment token was already used.")
    kind = conn.execute(select(environment_types).where(environment_types.c.id == env.type_id)).first() \
        if env.type_id else None
    if env.purpose == "warm":
        key_kind, caps, expires = "standing", [LLM], None
    else:
        timeout = timedelta(seconds=kind.job_timeout_s if kind else 3600)
        provided = list(kind.capabilities or []) if kind else []
        key_kind, caps, expires = "ephemeral", sorted({*provided, f"job:{env.job_id}"}), now + timeout + KEY_MARGIN
    label = _text_name(name) or f"env-{env.id[:8]}"
    key = runners.KEY_PREFIX + _random.token_urlsafe(32)
    key_id = new_id()
    # The binding to one job is environments.runner_key_id, so the key needs no team: it may then take instance
    # secrets, as the server's own worker does.
    conn.execute(insert(runner_keys).values(id=key_id, team_id=None, name=f"env-{env.id[:8]}",
                                            key_hmac=runners.digest(keyring, key), created_at=now, kind=key_kind,
                                            expires_at=expires, capabilities=caps))
    conn.execute(update(environments).where(environments.c.id == env.id).values(runner_key_id=key_id))
    audit.record(conn, action="env.enroll", actor_id=None, team_id=env.team_id, target_kind="environment",
                 target_id=env.id, detail={"runner_key_id": key_id, "kind": key_kind, "name": label}, ip=ip)
    return key, env


def _text_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value.strip() if ch.isprintable())[:191]
    return cleaned or None


def render_user_data(*, server_url: str, token: str, env_id: str, capabilities: list[str], kind: str,
                     max_lifetime_s: int | None) -> str:
    """The boot script. It carries only the enrollment token, which works once and expires with the boot timeout.

    The runner, git, job, and agent users are separate, so a workflow process can't read the runner key and an
    agent session can't read the run. The image creates them and the sudoers rules. The metadata block for the
    job, git, and agent users repeats the image's own, in case an image lacks it. Mac images have no agent user.
    """
    caps = " ".join(f"--capability {shlex.quote(c)}" for c in capabilities)
    runner = (f"armature runner --server {shlex.quote(server_url)} --enroll-token-env ARMATURE_ENROLL_TOKEN "
              f"--key-file /etc/armature/runner.env --name env-{env_id[:8]} {caps} --slots 1 "
              "--job-user armature-job --work-dir /var/lib/armature-runner")
    deadman = f"shutdown -h +{max(1, max_lifetime_s // 60)}" if max_lifetime_s else ""
    if kind != "ec2-mac":
        runner += " --agent-user armature-agent"
    if kind == "ec2-mac":
        # The Tart image creates the three users and the launchd job, and reads this file from the shared folder.
        return "\n".join(["#!/bin/bash", "set -euo pipefail", "umask 077",
                          f"ARMATURE_ENROLL_TOKEN={token}", f"ARMATURE_RUNNER_COMMAND={shlex.quote(runner)}",
                          deadman, ""])
    return "\n".join([
        "#!/bin/bash",
        "set -euo pipefail",
        "umask 077",
        "for user in armature-runner armature-git armature-job armature-agent; do",
        '  id -u "$user" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$user"',
        "done",
        "usermod -a -G armature-job armature-runner",
        "usermod -a -G armature-agent armature-job",
        "install -d -m 700 -o armature-runner -g armature-runner /etc/armature /var/lib/armature-runner",
        "cat > /etc/armature/enroll.env <<'EOF'",
        f"ARMATURE_ENROLL_TOKEN={token}",
        "EOF",
        "chown armature-runner:armature-runner /etc/armature/enroll.env",
        "chmod 600 /etc/armature/enroll.env",
        "iptables -I OUTPUT -m owner --uid-owner armature-job -d 169.254.169.254 -j REJECT",
        "iptables -I OUTPUT -m owner --uid-owner armature-git -d 169.254.169.254 -j REJECT",
        "iptables -I OUTPUT -m owner --uid-owner armature-agent -d 169.254.169.254 -j REJECT",
        "cat > /etc/systemd/system/armature-runner.service <<'EOF'",
        "[Unit]",
        "Description=Armature runner",
        "After=network-online.target",
        "StartLimitIntervalSec=0",
        "[Service]",
        "User=armature-runner",
        "EnvironmentFile=/etc/armature/enroll.env",
        f"ExecStart=/usr/local/bin/{runner}",
        "Restart=on-failure",
        "RestartSec=10",
        "[Install]",
        "WantedBy=multi-user.target",
        "EOF",
        "rm -f /var/lib/cloud/instance/user-data.txt /var/lib/cloud/instance/user-data.txt.i",
        "rm -f /var/lib/cloud/instance/scripts/part-*",
        "systemctl daemon-reload",
        "systemctl enable --now armature-runner",
        deadman,
        "",
    ])


# Who can run what

@dataclass(frozen=True)
class Taker:
    team_id: str | None
    capabilities: list[str] | None


def takers(conn: Connection, now: datetime) -> list[Taker]:
    """Every runner that could claim a job now, busy or not, and the server's own worker when it's on.

    Bound ephemeral keys are left out, because they take only their own job.
    """
    rows = conn.execute(
        select(runners_table.c.capabilities, runner_keys.c.capabilities.label("allowed"), runner_keys.c.team_id)
        .select_from(runners_table.join(runner_keys, runner_keys.c.id == runners_table.c.runner_key_id))
        .where(runner_keys.c.revoked_at.is_(None), runner_keys.c.kind == "standing",
               or_(runner_keys.c.expires_at.is_(None), runner_keys.c.expires_at > now),
               runners_table.c.last_seen_at > now - ONLINE_WITHIN)).all()
    found = [Taker(r.team_id, sorted(set(r.capabilities or []) & set(r.allowed)) if r.allowed is not None
                   else list(r.capabilities or [])) for r in rows]
    enabled = conn.execute(select(settings.c.value).where(settings.c.scope == INSTANCE_SCOPE,
                                                          settings.c.key == worker.ENABLED_KEY)).scalar()
    if enabled is not False:
        caps = conn.execute(select(settings.c.value).where(settings.c.scope == INSTANCE_SCOPE,
                                                           settings.c.key == worker.CAPABILITIES_KEY)).scalar()
        found.append(Taker(None, caps if isinstance(caps, list) else []))
    return found


def taken(job: Any, found: list[Taker]) -> bool:
    return any((t.team_id is None or t.team_id == job.team_id) and worker.meets(job.requires, t.capabilities)
               for t in found)


def llm_only(requires: list[str] | None) -> bool:
    return set(requires or ()) <= {LLM}


def warnings(conn: Connection, rows: list[Any], now: datetime,
             reasons: dict[str, str] | None = None) -> dict[str, str]:
    """Why each queued job is still waiting, for the jobs that won't start without someone acting."""
    queued = [r for r in rows if r.status == "queued"]
    if not queued:
        return {}
    found = takers(conn, now)
    type_of = dict(conn.execute(select(projects.c.id, environment_types.c.enabled)
                                .select_from(projects.outerjoin(environment_types, environment_types.c.id
                                                                == projects.c.environment_type_id))
                                .where(projects.c.id.in_({r.project_id for r in queued if r.project_id}))).all())
    out = {}
    for row in queued:
        message = job_warning(row, found, type_of.get(row.project_id), (reasons or {}).get(row.id))
        if message:
            out[row.id] = message
    return out


def job_warning(job: Any, found: list[Taker], type_enabled: bool | None, reason: str | None = None) -> str | None:
    if taken(job, found):
        return None
    if llm_only(job.requires):
        return f"No runner with {LLM} is online. Start a warm runner on the AWS page." if job.requires else None
    if job.project_id is None:
        return "No runner can take this job, and it has no project to start an environment for."
    if not type_enabled:
        return "Project has no environment type."
    return reason


# Guards

def _day_spend(conn: Connection, connection_id: str, now: datetime) -> Decimal:
    rows = conn.execute(select(environments.c.launched_at, environments.c.requested_at, environments.c.terminated_at,
                               environment_types.c.hourly_cost_usd)
                        .select_from(environments.join(environment_types,
                                                       environment_types.c.id == environments.c.type_id))
                        .where(environments.c.connection_id == connection_id,
                               or_(environments.c.terminated_at.is_(None),
                                   environments.c.terminated_at > now - DAY))).all()
    total = Decimal(0)
    for row in rows:
        if row.hourly_cost_usd is None:
            continue
        start = max(row.launched_at or row.requested_at, now - DAY)
        end = row.terminated_at or now
        if end > start:
            total += Decimal(row.hourly_cost_usd) * Decimal((end - start).total_seconds()) / Decimal(3600)
    return total


def _project_limit(conn: Connection, team_id: str) -> int | None:
    rows = dict(conn.execute(select(settings.c.scope, settings.c.value).where(
        settings.c.key == PROJECT_LIMIT_KEY, settings.c.scope.in_([team_id, INSTANCE_SCOPE]))).all())
    value = rows.get(team_id, rows.get(INSTANCE_SCOPE))
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def guard(conn: Connection, row: Any, job: Any, now: datetime) -> str | None:
    """Why a launch for this job must wait, checked before any provider call. None when it may go."""
    live = conn.execute(select(func.count()).select_from(environments).where(
        environments.c.connection_id == row.id, environments.c.purpose == "job",
        environments.c.state.in_(LIVE))).scalar_one()
    if live >= row.max_concurrent:
        return f"{live} environments are running, the most the AWS connection allows."
    launched = conn.execute(select(func.count()).select_from(environments).where(
        environments.c.connection_id == row.id, environments.c.requested_at > now - DAY,
        or_(environments.c.state != "failed", environments.c.provider_id.is_not(None)))).scalar_one()
    if launched >= row.max_daily_launches:
        return f"{launched} environments started in the last 24 hours, the most the AWS connection allows."
    if row.daily_budget_usd is not None and _day_spend(conn, row.id, now) >= Decimal(row.daily_budget_usd):
        return f"Environments reached the ${row.daily_budget_usd} daily budget."
    limit = _project_limit(conn, job.team_id)
    if limit is not None and conn.execute(select(func.count()).select_from(environments).where(
            environments.c.project_id == job.project_id, environments.c.purpose == "job",
            environments.c.state.in_(LIVE))).scalar_one() >= limit:
        return f"This project has {limit} environments running, the most its team allows."
    return None


def _host_slot(conn: Connection, row: Any, env_values: dict[str, Any]) -> bool:
    """Insert the environment into the least busy Mac host's free slot. False when every slot is busy."""
    busy = (select(environments.c.host_id, func.count().label("busy")).where(environments.c.slot_index.is_not(None))
            .group_by(environments.c.host_id).subquery())
    hosts = conn.execute(select(environment_hosts.c.id, environment_hosts.c.slots)
                         .select_from(environment_hosts.outerjoin(busy, busy.c.host_id == environment_hosts.c.id))
                         .where(environment_hosts.c.connection_id == row.id, environment_hosts.c.state == "running",
                                environment_hosts.c.instance_id.is_not(None))
                         .order_by(func.coalesce(busy.c.busy, 0), environment_hosts.c.id)).all()
    for host in hosts:
        for slot in range(host.slots):
            try:
                with conn.begin_nested():
                    conn.execute(insert(environments).values(host_id=host.id, slot_index=slot, **env_values))
                return True
            except IntegrityError:
                continue
    return False


# The service

class EnvironmentService:
    """Launches, watches, and reaps environments every ``interval`` seconds while started.

    Tests leave it stopped and call ``tick`` themselves, with a fake provider from ``loader``.
    """

    def __init__(self, engine: Engine, keyring: Keyring, cfg: ServerConfig, *,
                 clock: Callable[[], datetime] = utcnow, interval: float = INTERVAL_S,
                 loader: Callable[[Keyring, Any], Any] = provider_loader.load,
                 builder: Callable[[Keyring, Any], Any] = provider_loader.build):
        self.engine = engine
        self.keyring = keyring
        self.cfg = cfg
        self.interval = interval
        self.loader = loader
        self.builder = builder
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cached: tuple[Any, Any] | None = None
        self._good: tuple[str, Any] | None = None
        self.reasons: dict[str, str] = {}
        self.retry_after: dict[str, datetime] = {}
        self.failures = 0
        self.paused_until: datetime | None = None
        self.message: str | None = provider_loader.NOT_CONFIGURED

    @property
    def server_url(self) -> str:
        return f"{self.cfg.public_origin}{self.cfg.base_path}"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="armature-environments", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("environment tick failed")
            self._stop.wait(self.interval)

    def provisioner(self, row: Any) -> Any | None:
        if row is None:
            return None
        stamp = (row.id, row.updated_at, row.state)
        if self._cached is None or self._cached[0] != stamp:
            self._cached = (stamp, self.loader(self.keyring, row))
            if self._cached[1] is not None:
                self._good = (row.id, self._cached[1])
        return self._cached[1]

    def last_good(self, row: Any) -> Any | None:
        """The provisioner from when this connection last verified, so what it launched still gets reaped."""
        if row is None or self._good is None or self._good[0] != row.id:
            return None
        return self._good[1]

    def snapshot(self) -> dict[str, Any]:
        return {"configured": self.message is None, "message": self.message, "paused_until": self.paused_until,
                "waiting": len(self.reasons), "reasons": sorted(set(self.reasons.values()))}

    def tick(self) -> None:
        with self._lock:
            now = self._clock()
            with self.engine.connect() as conn:
                row = connection(conn)
            provisioner = self.provisioner(row)
            if provisioner is None:
                self.message = provider_loader.NOT_CONFIGURED if row is None or row.state != "ok" \
                    else provider_loader.NO_EXTRA
                # Launches stop, but environments already running are still watched and torn down.
                previous = self.last_good(row)
                if previous is not None:
                    # A relaunch is a launch, which an unverified connection mustn't make.
                    self.reap(previous, row, now, relaunch=False)
                return
            self.message = None
            self.reap(provisioner, row, now)
            self.dispatch(provisioner, row, now)

    # Dispatch

    def dispatch(self, provisioner: Any, row: Any, now: datetime) -> int:
        """Launch an environment for each queued job that needs one. Returns how many it launched."""
        with self.engine.connect() as conn:
            queued = conn.execute(select(jobs.c.id, jobs.c.team_id, jobs.c.project_id, jobs.c.requires,
                                         projects.c.environment_type_id)
                                  .select_from(jobs.join(projects, projects.c.id == jobs.c.project_id))
                                  .where(jobs.c.status == "queued", jobs.c.kind == worker.JOB_KIND,
                                         jobs.c.created_at < now - QUEUE_DELAY, worker.ready(now))
                                  .order_by(jobs.c.created_at, jobs.c.id).limit(worker.CLAIM_SCAN)).all()
            found = takers(conn, now) if queued else []
        reasons: dict[str, str] = {}
        launched = 0
        for job in queued:
            if taken(job, found) or llm_only(job.requires):
                continue
            if self.retry_after.get(job.id, now) > now:
                reasons[job.id] = "Waiting to retry after AWS had no capacity."
                continue
            with self.engine.connect() as conn:
                kind = None if job.environment_type_id is None else conn.execute(
                    select(environment_types).where(environment_types.c.id == job.environment_type_id)).first()
            if kind is None or not kind.enabled:
                continue
            missing = sorted(set(job.requires or ()) - set(kind.capabilities or []))
            if missing:
                self._fail_job(job.id, f"No environment type provides {', '.join(missing)}.", now)
                continue
            if self.paused_until is not None and now < self.paused_until:
                reasons[job.id] = "Launches are paused after repeated AWS failures."
                continue
            with self.engine.connect() as conn:
                reason = guard(conn, row, job, now)
            if reason:
                reasons[job.id] = reason
                continue
            env_id = self.claim(row, kind, job, now)
            if env_id is None:
                reasons[job.id] = "Every Mac host slot is busy." if kind.kind == "ec2-mac" else \
                    "Another server took this job."
                continue
            self.retry_after.pop(job.id, None)
            launched += 1
            self.launch(provisioner, env_id, now)
        self.reasons = reasons
        return launched

    def claim(self, row: Any, kind: Any, job: Any, now: datetime) -> str | None:
        """Move the job to provisioning and record its environment in one transaction. The environment's id, or None."""
        env_id = new_id()
        boot = timedelta(seconds=kind.boot_timeout_s or row.boot_timeout_s)
        try:
            with self.engine.begin() as conn:
                if conn.execute(update(jobs).where(jobs.c.id == job.id, jobs.c.status == "queued",
                                                   *worker.claim_guard(conn, job.id, now)).values(
                        status="provisioning", claimed_by=f"env:{env_id}", claimed_at=now,
                        lease_expires_at=now + boot + RELAUNCH_AFTER)).rowcount != 1:
                    return None
                self._record(conn, row, kind, job, env_id, boot, now)
        except _NoSlot:
            return None
        return env_id

    def _record(self, conn: Connection, row: Any, kind: Any, job: Any, env_id: str, boot: timedelta,
                now: datetime) -> None:
        values = {"id": env_id, "connection_id": row.id, "type_id": kind.id, "purpose": "job", "job_id": job.id,
                  "team_id": job.team_id, "project_id": job.project_id, "state": "requested",
                  "market": kind.market, "launch_attempts": 0, "requested_at": now,
                  "enroll_hmac": runners.digest(self.keyring, mint_enrollment(self.keyring, env_id)),
                  "enroll_expires_at": now + boot}
        if kind.kind == "ec2-mac":
            if not _host_slot(conn, row, values):
                raise _NoSlot
        else:
            conn.execute(insert(environments).values(**values))
        audit.record(conn, action="env.launch", actor_id=None, team_id=job.team_id, target_kind="environment",
                     target_id=env_id, detail={"job_id": job.id, "type": kind.name})

    def spec(self, conn: Connection, row: Any, env: Any) -> LaunchSpec:
        kind = conn.execute(select(environment_types).where(environment_types.c.id == env.type_id)).one()
        host = None if env.host_id is None else conn.execute(
            select(environment_hosts.c.instance_id).where(environment_hosts.c.id == env.host_id)).scalar()
        caps = [LLM] if env.purpose == "warm" else list(kind.capabilities or [])
        lifetime = None if env.purpose == "warm" else row.max_lifetime_s
        return LaunchSpec(
            env_id=env.id, kind=kind.kind, image=kind.image,
            instance_types=tuple(t for t in kind.instance_types or () if not disallowed(row, [t])),
            subnets=tuple((s["zone"], s["subnet_id"]) for s in row.subnets or []),
            markets=("on-demand",) if env.purpose == "warm" else markets_for(kind.market),
            user_data=render_user_data(server_url=self.server_url, token=mint_enrollment(self.keyring, env.id),
                                       env_id=env.id, capabilities=caps, kind=kind.kind, max_lifetime_s=lifetime),
            tags=dict(row.extra_tags or {}), name=f"{row.tag_project}-env-{env.id[:8]}",
            security_group_id=row.security_group_id, instance_profile=row.instance_profile,
            launch_template_id=(row.launch_template_ids or {}).get(TEMPLATE_KINDS[kind.kind]), disk_gb=kind.disk_gb,
            host_instance_id=host,
            vm_cpu=kind.vm_cpu, vm_memory_gb=kind.vm_memory_gb)

    def launch(self, provisioner: Any, env_id: str, now: datetime) -> bool:
        """Ask the provider for the environment's instance, outside any transaction. True when it launched."""
        with self.engine.connect() as conn:
            env = conn.execute(select(environments).where(environments.c.id == env_id)).one()
            if env.state != "requested":
                return False
            row = conn.execute(select(aws_connections).where(aws_connections.c.id == env.connection_id)).one()
            spec = self.spec(conn, row, env)
        with self.engine.begin() as conn:
            # Another server may have read the same row: only the one whose bump lands calls the provider.
            if conn.execute(update(environments).where(environments.c.id == env_id, environments.c.state == "requested",
                                                       environments.c.launch_attempts == env.launch_attempts)
                            .values(launch_attempts=env.launch_attempts + 1)).rowcount != 1:
                return False
        try:
            instance: Instance = provisioner.launch(spec)
        except ProvisionError as exc:
            self._launch_failed(env, exc, now)
            return False
        except Exception:
            log.exception("launching environment %s failed", env_id)
            self._launch_failed(env, ProvisionError("The provider failed unexpectedly."), now)
            return False
        self.failures = 0
        lifetime = None if env.purpose == "warm" else now + timedelta(seconds=row.max_lifetime_s)
        with self.engine.begin() as conn:
            conn.execute(update(environments).where(environments.c.id == env_id,
                                                    environments.c.state == "requested").values(
                state="booting", provider_id=instance.instance_id, launched_at=now, terminate_after=lifetime,
                market=instance.market or env.market, instance_type=instance.instance_type,
                subnet_id=instance.subnet_id))
        return True

    def _launch_failed(self, env: Any, exc: ProvisionError, now: datetime) -> None:
        if not exc.retryable:
            self.failures += 1
            if self.failures >= BREAKER_FAILURES:
                self.paused_until = now + BREAKER_PAUSE
                self.failures = 0
        with self.engine.begin() as conn:
            conn.execute(update(environments).where(environments.c.id == env.id, environments.c.state == "requested")
                         .values(state="failed", error=exc.message, slot_index=None, terminated_at=now))
            if env.job_id is None:
                return
            tries = conn.execute(select(func.count()).select_from(environments)
                                 .where(environments.c.job_id == env.job_id)).scalar_one()
            if exc.retryable and tries < MAX_ENVIRONMENTS_PER_JOB:
                self.retry_after[env.job_id] = now + RETRY_AFTER
                self._requeue(conn, env.job_id)
            else:
                conn.execute(update(jobs).where(jobs.c.id == env.job_id, jobs.c.status == "provisioning").values(
                    status="failed", error=f"The environment could not start: {exc.message}", finished_at=now))

    @staticmethod
    def _requeue(conn: Connection, job_id: str) -> None:
        conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status == "provisioning").values(
            status="queued", claimed_by=None, claimed_at=None, lease_expires_at=None))

    def _fail_job(self, job_id: str, error: str, now: datetime, status: str = "failed",
                  held: tuple[str, ...] = ("queued", "provisioning")) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job_id, jobs.c.status.in_(held))
                         .values(status=status, error=error, finished_at=now))

    # Reaping

    def reap(self, provisioner: Any, row: Any, now: datetime, *, relaunch: bool = True) -> None:
        steps = (self._boot_timeouts, self._settle, self._lifetimes, self._retry_terminating, self._orphans,
                 self._idle_hosts)
        for step in ((self._relaunch, *steps) if relaunch else steps):
            try:
                step(provisioner, row, now)
            except (ProvisionError, EnvError) as exc:
                log.warning("environment reaper: %s", exc.message)

    def end(self, provisioner: Any, env: Any, now: datetime, *, error: str | None = None,
            state: str = "terminated") -> bool:
        """Terminate an environment and revoke its key. False when another pass got there first or AWS refused."""
        with self.engine.begin() as conn:
            if env.state != "terminating" and conn.execute(
                    update(environments).where(environments.c.id == env.id, environments.c.state == env.state)
                    .values(state="terminating", error=error or env.error)).rowcount != 1:
                return False
        if env.provider_id:
            try:
                provisioner.terminate(env.provider_id)
            except ProvisionError as exc:
                log.warning("terminating environment %s: %s", env.id, exc.message)
                return False
        with self.engine.begin() as conn:
            conn.execute(update(environments).where(environments.c.id == env.id).values(
                state=state, terminated_at=now, slot_index=None))
            # Enrollment may have minted a key after env was read.
            key_id = conn.execute(select(environments.c.runner_key_id).where(environments.c.id == env.id)).scalar()
            if key_id:
                runners.revoke_key(conn, team_id=None, key_id=key_id, actor_id=None, now=now, ip=None)
            audit.record(conn, action="env.terminate", actor_id=None, team_id=env.team_id, target_kind="environment",
                         target_id=env.id, detail={"job_id": env.job_id, "error": error})
        return True

    def _envs(self, *where: Any) -> list[Any]:
        with self.engine.connect() as conn:
            return conn.execute(select(environments).where(*where)).all()

    def _relaunch(self, provisioner: Any, row: Any, now: datetime) -> None:
        # The server stopped between recording the environment and launching it. Same id, same ClientToken.
        for env in self._envs(environments.c.state == "requested", environments.c.requested_at < now - RELAUNCH_AFTER):
            self.launch(provisioner, env.id, now)

    def _boot_timeouts(self, provisioner: Any, row: Any, now: datetime) -> None:
        with self.engine.connect() as conn:
            stale = conn.execute(
                select(environments, environment_types.c.boot_timeout_s.label("type_boot"))
                .select_from(environments.join(jobs, jobs.c.id == environments.c.job_id)
                             .outerjoin(environment_types, environment_types.c.id == environments.c.type_id))
                .where(environments.c.purpose == "job", environments.c.state.in_(("booting", "ready")),
                       jobs.c.status == "provisioning")).all()
        for env in stale:
            boot = timedelta(seconds=env.type_boot or row.boot_timeout_s)
            if (env.launched_at or env.requested_at) + boot > now:
                continue
            if not self.end(provisioner, env, now, error="The environment did not boot in time.", state="failed"):
                continue
            with self.engine.begin() as conn:
                tries = conn.execute(select(func.count()).select_from(environments)
                                     .where(environments.c.job_id == env.job_id)).scalar_one()
                if tries < MAX_ENVIRONMENTS_PER_JOB:
                    self._requeue(conn, env.job_id)
                else:
                    conn.execute(update(jobs).where(jobs.c.id == env.job_id, jobs.c.status == "provisioning").values(
                        status="failed", error="environment did not boot", finished_at=now))

    def _settle(self, provisioner: Any, row: Any, now: datetime) -> None:
        with self.engine.connect() as conn:
            done = conn.execute(select(environments).select_from(
                environments.join(jobs, jobs.c.id == environments.c.job_id)).where(
                environments.c.purpose == "job", environments.c.state.in_(UP),
                jobs.c.status.not_in(("queued", *worker.HELD)))).all()
        for env in done:
            self.end(provisioner, env, now)

    def _lifetimes(self, provisioner: Any, row: Any, now: datetime) -> None:
        for env in self._envs(environments.c.purpose == "job", environments.c.state.in_(UP),
                              environments.c.terminate_after < now):
            if self.end(provisioner, env, now, error="The environment reached its lifetime.") and env.job_id:
                self._fail_job(env.job_id, "The environment reached its lifetime.", now, status="lost",
                               held=worker.HELD)

    def _retry_terminating(self, provisioner: Any, row: Any, now: datetime) -> None:
        for env in self._envs(environments.c.state == "terminating"):
            self.end(provisioner, env, now)

    def _orphans(self, provisioner: Any, row: Any, now: datetime) -> None:
        with self.engine.connect() as conn:
            known = conn.execute(select(environments).where(environments.c.connection_id == row.id,
                                                            environments.c.state.in_(LIVE))).all()
        by_provider = {e.provider_id: e for e in known if e.provider_id}
        by_id = {e.id: e for e in known}
        owned = provisioner.list_owned()
        with self.engine.connect() as conn:
            hosts = set(conn.execute(select(environment_hosts.c.instance_id).where(
                environment_hosts.c.instance_id.is_not(None))).scalars())
        for instance in owned:
            # Only environments carry the env tag: Mac host instances and image builds never die here.
            if not instance.env_id or instance.instance_id in hosts:
                continue
            if instance.instance_id in by_provider or instance.env_id in by_id:
                continue
            if instance.launched_at is not None and now - instance.launched_at < ORPHAN_GRACE:
                continue
            log.warning("terminating orphan instance %s", instance.instance_id)
            provisioner.terminate(instance.instance_id)
            with self.engine.begin() as conn:
                audit.record(conn, action="env.terminate", actor_id=None, target_kind="instance",
                             target_id=instance.instance_id, detail={"orphan": True, "env_id": instance.env_id})
        present = {i.instance_id for i in owned if i.live}
        for env in known:
            if env.purpose == "warm" or env.state not in ("booting", "ready", "running") or not env.provider_id \
                    or "/" in env.provider_id or env.provider_id in present:
                continue
            if env.launched_at is not None and now - env.launched_at < ORPHAN_GRACE:
                continue
            self.end(provisioner, env, now, error="The environment's instance is gone.", state="failed")
            if env.job_id:
                self._fail_job(env.job_id, "The environment's instance is gone.", now, held=worker.HELD)

    def _idle_hosts(self, provisioner: Any, row: Any, now: datetime) -> None:
        with self.engine.connect() as conn:
            hosts = conn.execute(select(environment_hosts).where(environment_hosts.c.connection_id == row.id,
                                                                 environment_hosts.c.state == "running")).all()
            busy = set(conn.execute(select(environments.c.host_id).where(
                environments.c.host_id.is_not(None), environments.c.state.in_(LIVE))).scalars())
        for host in hosts:
            if host.id in busy:
                if host.idle_since is not None:
                    with self.engine.begin() as conn:
                        conn.execute(update(environment_hosts).where(environment_hosts.c.id == host.id)
                                     .values(idle_since=None))
                continue
            if host.idle_since is None:
                with self.engine.begin() as conn:
                    conn.execute(update(environment_hosts).where(environment_hosts.c.id == host.id)
                                 .values(idle_since=now))
                continue
            if host.auto_release_after_s is None or now < host.min_release_at \
                    or now - host.idle_since < timedelta(seconds=host.auto_release_after_s):
                continue
            self.release_host(provisioner, host, now, actor_id=None, ip=None)

    # Admin actions

    def verify(self, *, actor_id: str, ip: str | None) -> Any:
        """Check the connection with the provider and record the result. Launches start only once it's ok."""
        now = self._clock()
        with self.engine.connect() as conn:
            row = connection(conn)
        if row is None:
            raise EnvError(404, "Set the AWS connection first.")
        try:
            problems = self.builder(self.keyring, row).validate()
        except ImportError:
            problems = [provider_loader.NO_EXTRA]
        except ProvisionError as exc:
            problems = [exc.message]
        except Exception:
            log.exception("verifying the AWS connection failed")
            problems = ["The provider failed unexpectedly. See the server log."]
        with self.engine.connect() as conn:
            for kind in conn.execute(select(environment_types).where(environment_types.c.enabled.is_(True))):
                refused = disallowed(row, list(kind.instance_types or []))
                if refused:
                    problems.append(not_allowed_message(f"Type {kind.name}", refused))
                templates = row.launch_template_ids or {}
                if kind.kind == "ec2-linux" and not templates.get("linux"):
                    problems.append(f"Type {kind.name} is ec2-linux, and the connection has no Linux launch template.")
                if kind.kind == "ec2-mac" and not row.artifact_bucket:
                    problems.append(f"Type {kind.name} is ec2-mac, which needs the connection's artifact bucket to "
                                    "pass the enrollment file.")
        state = "error" if problems else "ok"
        with self.engine.begin() as conn:
            conn.execute(update(aws_connections).where(aws_connections.c.id == row.id).values(
                state=state, last_error="\n".join(problems)[:2000] or None, verified_at=now if not problems else None))
            audit.record(conn, action="aws.connection.verify", actor_id=actor_id, target_kind="aws_connection",
                         target_id=row.id, detail={"state": state, "problems": len(problems)}, ip=ip)
            return conn.execute(select(aws_connections).where(aws_connections.c.id == row.id)).one()

    def _ready(self, launching: bool = True) -> tuple[Any, Any]:
        with self.engine.connect() as conn:
            row = connection(conn)
        provisioner = self.provisioner(row) or (None if launching else self.last_good(row))
        if provisioner is None:
            raise EnvError(409, "Verify the AWS connection first.")
        return row, provisioner

    def terminate(self, env_id: str, *, actor_id: str, ip: str | None) -> None:
        now = self._clock()
        _, provisioner = self._ready(launching=False)
        with self.engine.connect() as conn:
            env = conn.execute(select(environments).where(environments.c.id == env_id)).first()
        if env is None:
            raise EnvError(404, "No such environment.")
        if env.state not in LIVE:
            raise EnvError(409, "This environment has already ended.")
        if not self.end(provisioner, env, now, error="Terminated by an admin."):
            raise EnvError(502, "AWS didn't terminate it. Try again.")
        if env.job_id:
            self._fail_job(env.job_id, "An admin terminated the job's environment.", now, held=worker.HELD)
        with self.engine.begin() as conn:
            audit.record(conn, action="env.terminate", actor_id=actor_id, team_id=env.team_id,
                         target_kind="environment", target_id=env.id, detail={"by": "admin"}, ip=ip)

    def start_warm(self, type_id: Any, *, actor_id: str, ip: str | None) -> str:
        now = self._clock()
        row, provisioner = self._ready()
        with self.engine.begin() as conn:
            kind = None if not isinstance(type_id, str) else conn.execute(
                select(environment_types).where(environment_types.c.id == type_id)).first()
            if kind is None or kind.kind != "ec2-linux":
                raise EnvError(400, "Pick an ec2-linux environment type for the warm runner.")
            if conn.execute(select(environments.c.id).where(environments.c.purpose == "warm",
                                                            environments.c.state.in_(LIVE))).first():
                raise EnvError(409, "A warm runner is already running.")
            env_id = new_id()
            conn.execute(insert(environments).values(
                id=env_id, connection_id=row.id, type_id=kind.id, purpose="warm", state="requested",
                market="on-demand", launch_attempts=0, requested_at=now,
                enroll_hmac=runners.digest(self.keyring, mint_enrollment(self.keyring, env_id)),
                enroll_expires_at=now + timedelta(seconds=kind.boot_timeout_s or row.boot_timeout_s)))
            audit.record(conn, action="env.warm.start", actor_id=actor_id, target_kind="environment",
                         target_id=env_id, detail={"type": kind.name}, ip=ip)
        if not self.launch(provisioner, env_id, now):
            with self.engine.connect() as conn:
                error = conn.execute(select(environments.c.error).where(environments.c.id == env_id)).scalar()
            raise EnvError(502, f"The warm runner could not start: {error}")
        return env_id

    def stop_warm(self, *, actor_id: str, ip: str | None) -> None:
        now = self._clock()
        _, provisioner = self._ready(launching=False)
        warm = self._envs(environments.c.purpose == "warm", environments.c.state.in_(LIVE))
        if not warm:
            raise EnvError(404, "No warm runner is running.")
        for env in warm:
            if not self.end(provisioner, env, now, error="Stopped by an admin."):
                raise EnvError(502, "AWS didn't terminate the warm runner. Try again.")
        with self.engine.begin() as conn:
            audit.record(conn, action="env.warm.stop", actor_id=actor_id, target_kind="environment",
                         target_id=warm[0].id, ip=ip)

    def allocate_host(self, body: dict[str, Any], *, actor_id: str, ip: str | None) -> Any:
        now = self._clock()
        row, provisioner = self._ready()
        if not row.allow_host_allocation:
            raise EnvError(409, "Ask the account owner to allocate a host")
        if body.get("confirm_cost") is not True:
            raise EnvError(400, "A Mac host bills for at least 24 hours from allocation. Set confirm_cost to "
                                        "true to allocate one.")
        name = _check(body.get("name"), "name", TYPE_NAME, required=True)
        zone = _check(body.get("zone"), "zone", ZONE, required=True)
        instance_type = _check(body.get("instance_type") or "mac2.metal", "instance_type", INSTANCE_TYPE)
        slots = _whole(body.get("slots"), "slots", 1, 2, default=2)
        auto_release = _whole(body.get("auto_release_after_s"), "auto_release_after_s", 3600, 30 * 86400)
        refused = disallowed(row, [instance_type])
        if refused:
            raise EnvError(400, not_allowed_message(f"Host {name}", refused))
        try:
            host = provisioner.allocate_host(name=f"{row.tag_project}-{name}", instance_type=instance_type, zone=zone)
        except ProvisionError as exc:
            raise EnvError(502, f"AWS didn't allocate the host: {exc.message}") from exc
        instance_id = self._start_host_instance(provisioner, row, host.host_id, zone, instance_type, name)
        host_id = new_id()
        with self.engine.begin() as conn:
            conn.execute(insert(environment_hosts).values(
                id=host_id, connection_id=row.id, name=name, dedicated_host_id=host.host_id, zone=zone,
                instance_id=instance_id, instance_type=instance_type, state="running" if instance_id else "allocating",
                slots=slots, allocated_at=now,
                min_release_at=now + MAC_MIN_HOLD, auto_release_after_s=auto_release, created_by=actor_id))
            audit.record(conn, action="env.host.allocate", actor_id=actor_id, target_kind="environment_host",
                         target_id=host_id, detail={"name": name, "zone": zone, "instance_type": instance_type,
                                                    "dedicated_host_id": host.host_id}, ip=ip)
            return conn.execute(select(environment_hosts).where(environment_hosts.c.id == host_id)).one()

    @staticmethod
    def _start_host_instance(provisioner: Any, row: Any, dedicated_host_id: str, zone: str, instance_type: str,
                             name: str) -> str | None:
        """Start the host's macOS instance from the Mac launch template. None when the connection has none."""
        template = (row.launch_template_ids or {}).get("mac")
        subnet = next((s["subnet_id"] for s in row.subnets or [] if s["zone"] == zone), None)
        if not template or not subnet:
            return None
        try:
            return provisioner.launch_host_instance(
                dedicated_host_id=dedicated_host_id, subnet_id=subnet, launch_template_id=template,
                instance_type=instance_type, instance_profile=row.mac_instance_profile,
                name=f"{row.tag_project}-{name}").instance_id
        except ProvisionError as exc:
            log.warning("starting the macOS instance on %s failed: %s", dedicated_host_id, exc.message)
            return None

    def set_host_instance(self, host_id: str, instance_id: Any, *, actor_id: str, ip: str | None) -> Any:
        """Record the macOS instance running on a host, which makes the host's slots available."""
        if not isinstance(instance_id, str) or not re.fullmatch(r"i-[0-9a-f]{8,17}", instance_id):
            raise EnvError(400, "instance_id is an EC2 instance id.")
        with self.engine.begin() as conn:
            if conn.execute(update(environment_hosts).where(environment_hosts.c.id == host_id,
                                                            environment_hosts.c.state.in_(("allocating", "running")))
                            .values(instance_id=instance_id, state="running")).rowcount != 1:
                raise EnvError(404, "No such host.")
            audit.record(conn, action="env.host.allocate", actor_id=actor_id, target_kind="environment_host",
                         target_id=host_id, detail={"instance_id": instance_id}, ip=ip)
            return conn.execute(select(environment_hosts).where(environment_hosts.c.id == host_id)).one()

    def release(self, host_id: str, *, actor_id: str, ip: str | None) -> None:
        _, provisioner = self._ready(launching=False)
        with self.engine.connect() as conn:
            host = conn.execute(select(environment_hosts).where(environment_hosts.c.id == host_id)).first()
        if host is None or host.state == "released":
            raise EnvError(404, "No such host.")
        self.release_host(provisioner, host, self._clock(), actor_id=actor_id, ip=ip)

    def release_host(self, provisioner: Any, host: Any, now: datetime, *, actor_id: str | None,
                     ip: str | None) -> None:
        if now < host.min_release_at:
            raise EnvError(409, f"This host can't be released before {host.min_release_at.isoformat()}: "
                                        "AWS bills a Mac host for at least 24 hours.")
        with self.engine.connect() as conn:
            if conn.execute(select(environments.c.id).where(environments.c.host_id == host.id,
                                                            environments.c.state.in_(LIVE))).first():
                raise EnvError(409, "This host is running environments. Wait for them to end.")
        with self.engine.begin() as conn:
            if conn.execute(update(environment_hosts).where(environment_hosts.c.id == host.id,
                                                            environment_hosts.c.state == host.state)
                            .values(state="releasing")).rowcount != 1:
                return
        try:
            if host.instance_id:
                provisioner.terminate(host.instance_id)
            if host.dedicated_host_id:
                provisioner.release_host(host.dedicated_host_id)
        except ProvisionError as exc:
            with self.engine.begin() as conn:
                conn.execute(update(environment_hosts).where(environment_hosts.c.id == host.id)
                             .values(state=host.state))
            raise EnvError(502, f"AWS didn't release the host: {exc.message}") from exc
        with self.engine.begin() as conn:
            conn.execute(update(environment_hosts).where(environment_hosts.c.id == host.id).values(state="released"))
            audit.record(conn, action="env.host.release", actor_id=actor_id, target_kind="environment_host",
                         target_id=host.id, detail={"auto": actor_id is None}, ip=ip)


def public_host(row: Any) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "instance_id": row.instance_id, "dedicated_host_id": row.dedicated_host_id,
            "zone": row.zone, "instance_type": row.instance_type, "state": row.state, "slots": row.slots,
            "allocated_at": row.allocated_at, "min_release_at": row.min_release_at, "idle_since": row.idle_since,
            "auto_release_after_s": row.auto_release_after_s}


def list_hosts(conn: Connection) -> list[dict[str, Any]]:
    return [public_host(r) for r in conn.execute(select(environment_hosts).where(
        environment_hosts.c.state != "released").order_by(environment_hosts.c.name))]


def list_environments(conn: Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(select(environments, environment_types.c.name.label("type_name"))
                        .select_from(environments.outerjoin(environment_types,
                                                            environment_types.c.id == environments.c.type_id))
                        .order_by(case((environments.c.state.in_(LIVE), 0), else_=1),
                                  environments.c.requested_at.desc())
                        .limit(limit)).all()
    return [{"id": r.id, "purpose": r.purpose, "type": r.type_name, "job_id": r.job_id, "state": r.state,
             "provider_id": r.provider_id, "market": r.market, "instance_type": r.instance_type, "error": r.error,
             "requested_at": r.requested_at, "launched_at": r.launched_at, "ready_at": r.ready_at,
             "terminated_at": r.terminated_at, "host_id": r.host_id} for r in rows]


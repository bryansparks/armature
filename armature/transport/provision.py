"""The environment provider seam: what the server asks of a cloud, with no SDK imported.

The server plans and records environments. A ``Provisioner`` only launches,
describes, and terminates what it owns, and owns exactly what carries both
the project tag and the ``armature-install`` tag. ``ec2.Ec2Provisioner`` is
the AWS implementation and the only module that imports boto3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

ENV_TAG = "armature-env"
INSTALL_TAG = "armature-install"
PROJECT_TAG = "project"
LIVE_STATES = ("pending", "running", "stopping", "stopped")


class ProvisionError(Exception):
    """A provider call failed. ``retryable`` when capacity or throttling may clear by itself."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class LaunchSpec:
    """One environment's launch. ``env_id`` makes the launch idempotent: the same id never starts two instances."""
    env_id: str
    kind: str
    image: str
    instance_types: tuple[str, ...]
    subnets: tuple[tuple[str, str], ...]
    markets: tuple[str, ...]
    user_data: str
    tags: dict[str, str]
    name: str
    security_group_id: str | None = None
    instance_profile: str | None = None
    launch_template_id: str | None = None
    disk_gb: int | None = None
    host_instance_id: str | None = None
    vm_cpu: int | None = None
    vm_memory_gb: int | None = None


@dataclass(frozen=True)
class Instance:
    instance_id: str
    state: str
    env_id: str | None = None
    market: str | None = None
    instance_type: str | None = None
    subnet_id: str | None = None
    zone: str | None = None
    launched_at: datetime | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES


@dataclass(frozen=True)
class Host:
    host_id: str
    state: str
    zone: str | None = None
    instance_type: str | None = None
    allocated_at: datetime | None = None
    instance_id: str | None = None


def markets_for(market: str) -> tuple[str, ...]:
    """The purchase options to try, in order: Spot everywhere before any on-demand."""
    return {"spot": ("spot",), "on-demand": ("on-demand",)}.get(market, ("spot", "on-demand"))


def owned(tags: dict[str, str], project: str, install_id: str) -> bool:
    return tags.get(PROJECT_TAG) == project and tags.get(INSTALL_TAG) == install_id


class Provisioner(Protocol):
    def validate(self) -> list[str]:
        """Problems with the connection, such as a missing permission. Empty when it can launch."""

    def launch(self, spec: LaunchSpec) -> Instance: ...

    def describe(self, instance_id: str) -> Instance | None: ...

    def list_owned(self) -> list[Instance]: ...

    def terminate(self, instance_id: str) -> None:
        """Idempotent. Refuses an instance without this install's tags."""

    def allocate_host(self, *, name: str, instance_type: str, zone: str) -> Host: ...

    def launch_host_instance(self, *, dedicated_host_id: str, subnet_id: str, launch_template_id: str,
                             instance_type: str, instance_profile: str | None, name: str) -> Instance: ...

    def release_host(self, host_id: str) -> None: ...

    def list_hosts(self) -> list[Host]: ...

    def run_on_host(self, instance_id: str, commands: list[str], comment: str) -> str: ...

    def mint_model_credentials(self, role_arn: str, session_policy: dict[str, Any] | None, name: str,
                               ttl_s: int) -> dict[str, str]: ...

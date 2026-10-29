"""A provisioner that records calls instead of reaching AWS, for the server's environment tests."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from armature.transport.provision import Host, Instance, LaunchSpec, ProvisionError

TOKEN = re.compile(r"ARMATURE_ENROLL_TOKEN=(\S+)")


class FakeProvisioner:
    """Launches are idempotent on env_id, like the real one. ``fail`` holds errors the next launches raise."""

    def __init__(self, clock: Any = None):
        self.clock = clock
        self.calls: list[tuple[str, Any]] = []
        self.instances: dict[str, Instance] = {}
        self.specs: dict[str, LaunchSpec] = {}
        self.hosts: dict[str, Host] = {}
        self.fail: list[ProvisionError] = []
        self.problems: list[str] = []
        self.terminated: list[str] = []
        self.released: list[str] = []

    def validate(self) -> list[str]:
        self.calls.append(("validate", None))
        return list(self.problems)

    def launch(self, spec: LaunchSpec) -> Instance:
        self.calls.append(("launch", spec.env_id))
        if self.fail:
            raise self.fail.pop(0)
        for instance in self.instances.values():
            if instance.env_id == spec.env_id and instance.live:
                return instance
        instance_id = f"i-{len(self.instances) + 1:017x}"
        if spec.host_instance_id:
            instance_id = f"{spec.host_instance_id}/env-{spec.env_id}"
        instance = Instance(instance_id=instance_id, state="running", env_id=spec.env_id, market=spec.markets[0],
                            instance_type=(spec.instance_types or ("mac2.metal",))[0],
                            subnet_id=spec.subnets[0][1] if spec.subnets else None,
                            launched_at=self.clock() if self.clock else None)
        self.instances[instance_id] = instance
        self.specs[spec.env_id] = spec
        return instance

    def add_owned(self, instance_id: str, env_id: str | None, launched_at: datetime) -> None:
        self.instances[instance_id] = Instance(instance_id=instance_id, state="running", env_id=env_id,
                                               launched_at=launched_at)

    def describe(self, instance_id: str) -> Instance | None:
        return self.instances.get(instance_id)

    def list_owned(self) -> list[Instance]:
        return [i for i in self.instances.values() if i.live and "/" not in i.instance_id]

    def terminate(self, instance_id: str) -> None:
        self.calls.append(("terminate", instance_id))
        self.terminated.append(instance_id)
        found = self.instances.get(instance_id)
        if found is not None:
            self.instances[instance_id] = Instance(instance_id=instance_id, state="terminated", env_id=found.env_id,
                                                   launched_at=found.launched_at)

    def allocate_host(self, *, name: str, instance_type: str, zone: str) -> Host:
        host = Host(host_id=f"h-{len(self.hosts) + 1:017x}", state="available", zone=zone, instance_type=instance_type)
        self.hosts[host.host_id] = host
        return host

    def launch_host_instance(self, *, dedicated_host_id: str, subnet_id: str, launch_template_id: str,
                             instance_type: str, instance_profile: str | None, name: str) -> Instance:
        self.calls.append(("launch_host_instance", (dedicated_host_id, subnet_id, launch_template_id, instance_type)))
        instance = Instance(instance_id=f"i-{len(self.instances) + 1:017x}", state="running",
                            instance_type=instance_type, subnet_id=subnet_id)
        self.instances[instance.instance_id] = instance
        return instance

    def release_host(self, host_id: str) -> None:
        self.released.append(host_id)
        self.hosts.pop(host_id, None)

    def list_hosts(self) -> list[Host]:
        return list(self.hosts.values())

    def run_on_host(self, instance_id: str, commands: list[str], comment: str) -> str:
        self.calls.append(("run_on_host", instance_id))
        return "cmd"

    def mint_model_credentials(self, role_arn: str, session_policy: Any, name: str, ttl_s: int) -> dict[str, str]:
        return {}

    def token(self, env_id: str) -> str:
        return TOKEN.search(self.specs[env_id].user_data).group(1)

    def boot(self, client: Any, env_id: str, capabilities: list[str] | None = None) -> str:
        """Do what the instance's user-data does: enroll with the real endpoint, then say hello. Returns the key."""
        response = client.post("/api/runner/enroll", json={"token": self.token(env_id), "name": f"env-{env_id[:8]}"})
        assert response.status_code == 200, response.text
        key = response.json()["key"]
        hello = client.post("/api/runner/hello", json={"name": f"env-{env_id[:8]}", "slots": 1,
                                                       "capabilities": capabilities or []},
                            headers={"Authorization": f"Bearer {key}"})
        assert hello.status_code == 200, hello.text
        return key

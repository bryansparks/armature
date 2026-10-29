"""The AWS environment provider: EC2 Linux instances, EC2 Mac hosts, and Tart VMs on them.

Only this module imports boto3. The server reaches it through
``armature.server.provider_loader``, so a server without the ``cloud`` extra
never imports it. Every instance, volume, and host it creates carries the
project tag, the ``armature-install`` tag, and, for an environment, the
``armature-env`` tag, and it never terminates anything without the first two.
"""
from __future__ import annotations

import json
import re
from typing import Any

import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError

from armature.transport.provision import (
    ENV_TAG,
    INSTALL_TAG,
    LIVE_STATES,
    PROJECT_TAG,
    Host,
    Instance,
    LaunchSpec,
    ProvisionError,
    owned,
)

SESSION_NAME = "armature-platform"
ROLE_SESSION_S = 3600
# Capacity and quota errors that another subnet, type, or market may not hit.
CAPACITY = frozenset({"InsufficientInstanceCapacity", "InsufficientCapacity", "InsufficientHostCapacity",
                      "SpotMaxPriceTooLow", "MaxSpotInstanceCountExceeded", "InstanceLimitExceeded",
                      "Unsupported", "InsufficientFreeAddressesInSubnet", "VcpuLimitExceeded"})
THROTTLED = frozenset({"RequestLimitExceeded", "Throttling", "ThrottlingException", "ServiceUnavailable",
                       "InternalError", "Unavailable"})
MESSAGE_MAX = 300
VM_NAME = re.compile(r"^env-[0-9a-f-]{8,36}$")
TART_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,190}$")
HOST_DIR = "/var/root/armature"
ENROLL_PREFIX = "enroll/"


def _error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return str(error.get("Code") or "ClientError"), str(error.get("Message") or "")[:MESSAGE_MAX]
    return exc.__class__.__name__, str(exc)[:MESSAGE_MAX]


def _fail(exc: Exception, what: str) -> ProvisionError:
    code, message = _error(exc)
    return ProvisionError(f"{what}: {code}: {message}".rstrip(": "), retryable=code in CAPACITY | THROTTLED)


def _tags(raw: list[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in raw or []}


def _tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in sorted(tags.items())]


def tag_specifications(tags: dict[str, str], *, spot: bool) -> list[dict[str, Any]]:
    """Tags for everything RunInstances creates. IAM checks these request tags, so they never come from a template."""
    kinds = ["instance", "volume", "network-interface", *(["spot-instances-request"] if spot else [])]
    return [{"ResourceType": kind, "Tags": _tag_list(tags)} for kind in kinds]


METADATA = {"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled"}


def session_for(region: str, credentials: dict[str, str]) -> boto3.Session:
    """A session from static keys, an assumed role, or both. The role's credentials refresh before they expire."""
    keys = {}
    if credentials.get("access_key_id"):
        keys = {"aws_access_key_id": credentials["access_key_id"],
                "aws_secret_access_key": credentials["secret_access_key"]}
    base = boto3.Session(region_name=region, **keys)
    role_arn = credentials.get("role_arn")
    if not role_arn:
        return base
    sts = base.client("sts")
    assume = {"RoleArn": role_arn, "RoleSessionName": SESSION_NAME, "DurationSeconds": ROLE_SESSION_S}
    if credentials.get("external_id"):
        assume["ExternalId"] = credentials["external_id"]

    def refresh() -> dict[str, str]:
        found = sts.assume_role(**assume)["Credentials"]
        return {"access_key": found["AccessKeyId"], "secret_key": found["SecretAccessKey"],
                "token": found["SessionToken"], "expiry_time": found["Expiration"].isoformat()}

    core = botocore.session.get_session()
    core._credentials = RefreshableCredentials.create_from_metadata(metadata=refresh(), refresh_using=refresh,
                                                                    method="sts-assume-role")
    core.set_config_variable("region", region)
    return boto3.Session(botocore_session=core)


class Ec2Provisioner:
    def __init__(self, *, region: str, project: str, install_id: str, session: Any = None,
                 credentials: dict[str, str] | None = None, subnets: list[tuple[str, str]] | None = None,
                 security_group_id: str | None = None, artifact_bucket: str | None = None,
                 launch_template_ids: dict[str, str] | None = None, allowed_instance_types: list[str] | None = None,
                 stack_name: str | None = None):
        if project == "armature":
            raise ProvisionError("The project tag can't be armature, which armature-ops owns.")
        self.region = region
        self.project = project
        self.install_id = install_id
        self.subnets = list(subnets or [])
        self.security_group_id = security_group_id
        self.artifact_bucket = artifact_bucket
        self.launch_template_ids = dict(launch_template_ids or {})
        self.allowed_instance_types = list(allowed_instance_types or [])
        self.stack_name = stack_name
        self._session = session or session_for(region, credentials or {})
        config = Config(retries={"max_attempts": 3, "mode": "standard"})
        self.ec2 = self._session.client("ec2", region_name=region, config=config)
        self._clients: dict[str, Any] = {}
        self._root_devices: dict[str, str] = {}

    def _client(self, name: str) -> Any:
        if name not in self._clients:
            self._clients[name] = self._session.client(name, region_name=self.region)
        return self._clients[name]

    @property
    def ownership(self) -> dict[str, str]:
        return {PROJECT_TAG: self.project, INSTALL_TAG: self.install_id}

    def _filters(self, *extra: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"Name": f"tag:{PROJECT_TAG}", "Values": [self.project]},
                {"Name": f"tag:{INSTALL_TAG}", "Values": [self.install_id]}, *extra]

    def _instance(self, raw: dict[str, Any]) -> Instance:
        tags = _tags(raw.get("Tags"))
        return Instance(instance_id=raw["InstanceId"], state=raw["State"]["Name"], env_id=tags.get(ENV_TAG),
                        market="spot" if raw.get("InstanceLifecycle") == "spot" else "on-demand",
                        instance_type=raw.get("InstanceType"), subnet_id=raw.get("SubnetId"),
                        zone=(raw.get("Placement") or {}).get("AvailabilityZone"), launched_at=raw.get("LaunchTime"),
                        tags=tags)

    def _describe(self, **kwargs: Any) -> list[Instance]:
        found = []
        try:
            for page in self.ec2.get_paginator("describe_instances").paginate(**kwargs):
                for reservation in page["Reservations"]:
                    found.extend(self._instance(raw) for raw in reservation["Instances"])
        except (ClientError, BotoCoreError) as exc:
            raise _fail(exc, "describe instances") from exc
        return found

    def validate(self) -> list[str]:
        problems = []
        try:
            self._client("sts").get_caller_identity()
        except (ClientError, BotoCoreError) as exc:
            return ["The credentials don't work: {}: {}".format(*_error(exc))]
        try:
            self.ec2.describe_instances(Filters=self._filters(), MaxResults=5)
        except (ClientError, BotoCoreError) as exc:
            problems.append("Can't list instances: {}: {}".format(*_error(exc)))
        if not self.subnets:
            problems.append("No subnets are set.")
        else:
            try:
                self.ec2.describe_subnets(SubnetIds=[subnet for _, subnet in self.subnets])
            except (ClientError, BotoCoreError) as exc:
                problems.append("Can't find the subnets: {}: {}".format(*_error(exc)))
        if self.security_group_id:
            try:
                self.ec2.describe_security_groups(GroupIds=[self.security_group_id])
            except (ClientError, BotoCoreError) as exc:
                problems.append("Can't find the security group: {}: {}".format(*_error(exc)))
        if self.artifact_bucket:
            try:
                self._client("s3").head_bucket(Bucket=self.artifact_bucket)
            except (ClientError, BotoCoreError) as exc:
                problems.append("Can't reach the artifact bucket: {}: {}".format(*_error(exc)))
        if self.launch_template_ids:
            try:
                self.ec2.describe_launch_templates(LaunchTemplateIds=sorted(set(self.launch_template_ids.values())))
            except (ClientError, BotoCoreError) as exc:
                problems.append("Can't read the launch templates: {}: {}".format(*_error(exc)))
        if self.stack_name:
            problems.extend(self._stack_problems())
        return problems

    def _stack_problems(self) -> list[str]:
        """Compare the host-role stack's AllowedInstanceTypes with the connection's list, which stays authoritative."""
        try:
            stack = self._client("cloudformation").describe_stacks(StackName=self.stack_name)["Stacks"][0]
        except (ClientError, BotoCoreError) as exc:
            return ["Can't read stack {}: {}: {}".format(self.stack_name, *_error(exc))]
        found = next((p.get("ParameterValue", "") for p in stack.get("Parameters") or []
                      if p.get("ParameterKey") == "AllowedInstanceTypes"), None)
        if found is None:
            return [f"Stack {self.stack_name} has no AllowedInstanceTypes parameter."]
        in_stack = {t.strip() for t in found.split(",") if t.strip()}
        missing = sorted(set(self.allowed_instance_types) - in_stack)
        if missing:
            return [(f"The connection allows {', '.join(missing)}, which stack {self.stack_name}'s "
                     "AllowedInstanceTypes leaves out. Launches of those would fail with AccessDenied.")]
        return []

    def _by_env(self, env_id: str) -> Instance | None:
        live = self._describe(Filters=self._filters({"Name": f"tag:{ENV_TAG}", "Values": [env_id]},
                                                    {"Name": "instance-state-name", "Values": list(LIVE_STATES)}))
        return live[0] if live else None

    def _root_device(self, image: str) -> str:
        if image not in self._root_devices:
            try:
                images = self.ec2.describe_images(ImageIds=[image])["Images"]
            except (ClientError, BotoCoreError) as exc:
                raise _fail(exc, f"describe image {image}") from exc
            if not images:
                raise ProvisionError(f"Image {image} doesn't exist in {self.region}.")
            self._root_devices[image] = images[0].get("RootDeviceName") or "/dev/xvda"
        return self._root_devices[image]

    def _run_params(self, spec: LaunchSpec, market: str, subnet: str, instance_type: str,
                    token: str) -> dict[str, Any]:
        tags = {**spec.tags, **self.ownership, ENV_TAG: spec.env_id, "Name": spec.name}
        nic: dict[str, Any] = {"DeviceIndex": 0, "SubnetId": subnet, "AssociatePublicIpAddress": False,
                               "DeleteOnTermination": True}
        if spec.security_group_id:
            nic["Groups"] = [spec.security_group_id]
        params: dict[str, Any] = {
            "ImageId": spec.image, "InstanceType": instance_type, "MinCount": 1, "MaxCount": 1,
            "ClientToken": token, "UserData": spec.user_data, "NetworkInterfaces": [nic],
            "MetadataOptions": dict(METADATA), "InstanceInitiatedShutdownBehavior": "terminate",
            "TagSpecifications": tag_specifications(tags, spot=market == "spot"),
        }
        if spec.launch_template_id:
            params["LaunchTemplate"] = {"LaunchTemplateId": spec.launch_template_id, "Version": "$Latest"}
        if spec.instance_profile:
            params["IamInstanceProfile"] = {"Name": spec.instance_profile}
        if market == "spot":
            params["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {
                "SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}}
        if spec.disk_gb:
            params["BlockDeviceMappings"] = [{"DeviceName": self._root_device(spec.image), "Ebs": {
                "VolumeSize": spec.disk_gb, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}]
        return params

    def launch(self, spec: LaunchSpec) -> Instance:
        """Start one environment. A second call with the same env_id returns the first call's instance."""
        if spec.kind == "ec2-mac":
            return self._launch_vm(spec)
        existing = self._by_env(spec.env_id)
        if existing is not None:
            return existing
        if not spec.subnets or not spec.instance_types:
            raise ProvisionError("The environment needs at least one subnet and one instance type.")
        last: ProvisionError | None = None
        attempt = 0
        # Every subnet tries every type before the next subnet, and on-demand only after all of Spot.
        for market in spec.markets:
            for _, subnet in spec.subnets:
                for instance_type in spec.instance_types:
                    attempt += 1
                    params = self._run_params(spec, market, subnet, instance_type, f"{spec.env_id}-{attempt}")
                    try:
                        raw = self.ec2.run_instances(**params)["Instances"][0]
                    except (ClientError, BotoCoreError) as exc:
                        last = _fail(exc, f"{market} {instance_type} in {subnet}")
                        if not last.retryable:
                            raise last from exc
                        continue
                    return self._instance(raw)
        raise last or ProvisionError("No launch was tried.", retryable=True)

    def describe(self, instance_id: str) -> Instance | None:
        if "/" in instance_id:
            return Instance(instance_id=instance_id, state="running")
        try:
            found = self._describe(InstanceIds=[instance_id])
        except ProvisionError as exc:
            if "InvalidInstanceID" in exc.message:
                return None
            raise
        return found[0] if found else None

    def list_owned(self) -> list[Instance]:
        return self._describe(Filters=self._filters({"Name": "instance-state-name", "Values": list(LIVE_STATES)}))

    def terminate(self, instance_id: str) -> None:
        if "/" in instance_id:
            self._stop_vm(instance_id)
            return
        found = self.describe(instance_id)
        if found is None or found.state in ("shutting-down", "terminated"):
            return
        if not owned(found.tags, self.project, self.install_id):
            raise ProvisionError(f"Refusing to terminate {instance_id}: it doesn't carry this install's tags.")
        try:
            self.ec2.terminate_instances(InstanceIds=[instance_id])
        except (ClientError, BotoCoreError) as exc:
            if _error(exc)[0].startswith("InvalidInstanceID"):
                return
            raise _fail(exc, f"terminate {instance_id}") from exc

    def allocate_host(self, *, name: str, instance_type: str, zone: str) -> Host:
        tags = _tag_list({**self.ownership, "Name": name})
        try:
            host_id = self.ec2.allocate_hosts(AvailabilityZone=zone, InstanceType=instance_type, Quantity=1,
                                              AutoPlacement="off",
                                              TagSpecifications=[{"ResourceType": "dedicated-host",
                                                                  "Tags": tags}])["HostIds"][0]
        except (ClientError, BotoCoreError) as exc:
            raise _fail(exc, f"allocate a {instance_type} host in {zone}") from exc
        return Host(host_id=host_id, state="pending", zone=zone, instance_type=instance_type)

    def launch_host_instance(self, *, dedicated_host_id: str, subnet_id: str, launch_template_id: str,
                             instance_type: str, instance_profile: str | None, name: str) -> Instance:
        """Start the macOS instance on a Dedicated Host this install owns, from the Mac launch template."""
        if not self._hosts(HostIds=[dedicated_host_id]):
            raise ProvisionError(f"Refusing to launch on {dedicated_host_id}: it doesn't carry this install's tags.")
        params: dict[str, Any] = {
            "LaunchTemplate": {"LaunchTemplateId": launch_template_id, "Version": "$Latest"},
            "InstanceType": instance_type, "MinCount": 1, "MaxCount": 1,
            "ClientToken": f"{dedicated_host_id}-host"[:64],
            "Placement": {"HostId": dedicated_host_id, "Tenancy": "host"},
            "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": subnet_id, "AssociatePublicIpAddress": False,
                                   "DeleteOnTermination": True,
                                   **({"Groups": [self.security_group_id]} if self.security_group_id else {})}],
            "MetadataOptions": dict(METADATA),
            "TagSpecifications": tag_specifications({**self.ownership, "Name": name}, spot=False),
        }
        if instance_profile:
            params["IamInstanceProfile"] = {"Name": instance_profile}
        try:
            return self._instance(self.ec2.run_instances(**params)["Instances"][0])
        except (ClientError, BotoCoreError) as exc:
            raise _fail(exc, f"start the macOS instance on {dedicated_host_id}") from exc

    def _hosts(self, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            raw = self.ec2.describe_hosts(**kwargs)["Hosts"]
        except (ClientError, BotoCoreError) as exc:
            raise _fail(exc, "describe hosts") from exc
        return [h for h in raw if owned(_tags(h.get("Tags")), self.project, self.install_id)]

    def release_host(self, host_id: str) -> None:
        if not self._hosts(HostIds=[host_id]):
            raise ProvisionError(f"Refusing to release {host_id}: it doesn't carry this install's tags.")
        try:
            refused = self.ec2.release_hosts(HostIds=[host_id]).get("Unsuccessful") or []
        except (ClientError, BotoCoreError) as exc:
            raise _fail(exc, f"release {host_id}") from exc
        if refused:
            raise ProvisionError(f"AWS refused to release {host_id}: {refused[0].get('Error', {}).get('Message')}")

    def list_hosts(self) -> list[Host]:
        return [Host(host_id=h["HostId"], state=h.get("State", ""), zone=h.get("AvailabilityZone"),
                     instance_type=(h.get("HostProperties") or {}).get("InstanceType"),
                     allocated_at=h.get("AllocationTime"),
                     instance_id=next((i.get("InstanceId") for i in h.get("Instances") or []), None))
                for h in self._hosts()]

    def run_on_host(self, instance_id: str, commands: list[str], comment: str) -> str:
        found = self.describe(instance_id)
        if found is None or not owned(found.tags, self.project, self.install_id):
            raise ProvisionError(f"Refusing to run commands on {instance_id}: it doesn't carry this install's tags.")
        try:
            sent = self._client("ssm").send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
                                                    Parameters={"commands": commands}, Comment=comment[:100])
        except (ClientError, BotoCoreError) as exc:
            raise _fail(exc, f"run a command on {instance_id}") from exc
        return sent["Command"]["CommandId"]

    def _launch_vm(self, spec: LaunchSpec) -> Instance:
        """Clone and boot a Tart VM on a Mac host. The enrollment file reaches the host through S3, never SSM text."""
        vm = f"env-{spec.env_id}"
        if not spec.host_instance_id or not VM_NAME.fullmatch(vm) or not TART_IMAGE.fullmatch(spec.image):
            raise ProvisionError("A Mac environment needs a host and a valid Tart image.")
        if not self.artifact_bucket:
            raise ProvisionError("A Mac environment needs the connection's artifact bucket.")
        key = f"{self.install_id}/{ENROLL_PREFIX}{spec.env_id}"
        try:
            self._client("s3").put_object(Bucket=self.artifact_bucket, Key=key, Body=spec.user_data.encode(),
                                          ServerSideEncryption="AES256")
        except (ClientError, BotoCoreError) as exc:
            raise _fail(exc, "stage the enrollment file") from exc
        shared = f"{HOST_DIR}/{spec.env_id}"
        commands = ["set -eu", f"install -d -m 700 {shared}",
                    f"aws s3 cp s3://{self.artifact_bucket}/{key} {shared}/user-data.sh --only-show-errors",
                    f"aws s3 rm s3://{self.artifact_bucket}/{key} --only-show-errors",
                    f"chmod 600 {shared}/user-data.sh",
                    f"tart clone {spec.image} {vm}"]
        if spec.vm_cpu:
            commands.append(f"tart set {vm} --cpu {int(spec.vm_cpu)}")
        if spec.vm_memory_gb:
            commands.append(f"tart set {vm} --memory {int(spec.vm_memory_gb) * 1024}")
        commands.append(f"nohup tart run --no-graphics --dir=armature:{shared}:ro {vm} >/dev/null 2>&1 &")
        self.run_on_host(spec.host_instance_id, commands, f"armature start {vm}")
        return Instance(instance_id=f"{spec.host_instance_id}/{vm}", state="pending", env_id=spec.env_id)

    def _stop_vm(self, provider_id: str) -> None:
        host, _, vm = provider_id.partition("/")
        if not VM_NAME.fullmatch(vm):
            raise ProvisionError(f"Refusing to stop {provider_id}: not an environment VM.")
        shared = f"{HOST_DIR}/{vm[len('env-'):]}"
        self.run_on_host(host, [f"tart stop {vm} || true", f"tart delete {vm} || true", f"rm -rf {shared}"],
                         f"armature stop {vm}")

    def mint_model_credentials(self, role_arn: str, session_policy: dict[str, Any] | None, name: str,
                               ttl_s: int) -> dict[str, str]:
        assume: dict[str, Any] = {"RoleArn": role_arn, "RoleSessionName": name[:64], "DurationSeconds": ttl_s}
        if session_policy:
            assume["Policy"] = json.dumps(session_policy)
        try:
            found = self._client("sts").assume_role(**assume)["Credentials"]
        except (ClientError, BotoCoreError) as exc:
            raise _fail(exc, "mint model credentials") from exc
        return {"AWS_ACCESS_KEY_ID": found["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": found["SecretAccessKey"],
                "AWS_SESSION_TOKEN": found["SessionToken"], "AWS_REGION": self.region}

"""The EC2 provider against moto, with Stubber for STS and SSM: launch order, idempotency, tags, and refusals."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("moto")

import boto3
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from moto import mock_aws

from armature.transport.ec2 import Ec2Provisioner
from armature.transport.provision import LaunchSpec, ProvisionError, markets_for

PROJECT = "armature-platform"
INSTALL = "install-1"
COMMAND = "0b7d1c2e-1111-2222-3333-444455556666"


@pytest.fixture
def aws():
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        ec2 = session.client("ec2")
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        subnets = [(zone, ec2.create_subnet(VpcId=vpc, CidrBlock=cidr, AvailabilityZone=zone)["Subnet"]["SubnetId"])
                   for zone, cidr in (("us-east-1a", "10.0.1.0/24"), ("us-east-1b", "10.0.2.0/24"))]
        group = ec2.create_security_group(GroupName="env", Description="env", VpcId=vpc)["GroupId"]
        image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
        yield {"session": session, "ec2": ec2, "subnets": subnets, "group": group, "image": image}


def _provisioner(aws, **overrides):
    values = {"region": "us-east-1", "project": PROJECT, "install_id": INSTALL, "session": aws["session"],
              "subnets": aws["subnets"], "security_group_id": aws["group"], **overrides}
    return Ec2Provisioner(**values)


def _spec(aws, env_id="11111111-2222-3333-4444-555555555555", **overrides):
    values = {"env_id": env_id, "kind": "ec2-linux", "image": aws["image"], "instance_types": ("m6i.large", "m5.large"),
              "subnets": tuple(aws["subnets"]), "markets": markets_for("spot-then-on-demand"),
              "user_data": "#!/bin/bash\necho hi\n", "tags": {"owner": "platform"}, "name": "armature-platform-env",
              "security_group_id": aws["group"], "disk_gb": 40}
    return LaunchSpec(**{**values, **overrides})


def _capacity(code="InsufficientInstanceCapacity"):
    return ClientError({"Error": {"Code": code, "Message": "no capacity"}}, "RunInstances")


def test_launch_tags_the_instance_and_requires_imdsv2(aws):
    provisioner = _provisioner(aws)
    instance = provisioner.launch(_spec(aws))
    raw = aws["ec2"].describe_instances(InstanceIds=[instance.instance_id])["Reservations"][0]["Instances"][0]
    tags = {t["Key"]: t["Value"] for t in raw["Tags"]}
    assert tags["project"] == PROJECT and tags["armature-install"] == INSTALL
    assert tags["armature-env"] == "11111111-2222-3333-4444-555555555555" and tags["owner"] == "platform"
    assert raw["MetadataOptions"]["HttpTokens"] == "required"
    assert raw["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    assert instance.market == "spot" and instance.instance_type == "m6i.large"
    assert instance.subnet_id == aws["subnets"][0][1]


def test_a_second_launch_with_the_same_environment_returns_the_first_instance(aws):
    provisioner = _provisioner(aws)
    first = provisioner.launch(_spec(aws))
    again = provisioner.launch(_spec(aws))
    assert again.instance_id == first.instance_id
    assert len(provisioner.list_owned()) == 1


def test_launch_tries_every_subnet_and_type_on_spot_before_on_demand(aws, monkeypatch):
    provisioner = _provisioner(aws)
    calls = []
    real = provisioner.ec2.run_instances

    def run_instances(**params):
        market = params.get("InstanceMarketOptions", {}).get("MarketType", "on-demand")
        calls.append((market, params["NetworkInterfaces"][0]["SubnetId"], params["InstanceType"],
                      params["ClientToken"]))
        if market == "spot":
            raise _capacity()
        return real(**params)

    monkeypatch.setattr(provisioner.ec2, "run_instances", run_instances)
    instance = provisioner.launch(_spec(aws))
    (_, a), (_, b) = aws["subnets"]
    env = "11111111-2222-3333-4444-555555555555"
    assert [c[:3] for c in calls] == [("spot", a, "m6i.large"), ("spot", a, "m5.large"), ("spot", b, "m6i.large"),
                                      ("spot", b, "m5.large"), ("on-demand", a, "m6i.large")]
    assert [c[3] for c in calls] == [f"{env}-{n}" for n in range(1, 6)]
    assert instance.market == "on-demand"


def test_capacity_everywhere_is_retryable_and_a_bad_request_is_not(aws, monkeypatch):
    provisioner = _provisioner(aws)
    monkeypatch.setattr(provisioner.ec2, "run_instances", lambda **_: (_ for _ in ()).throw(_capacity()))
    with pytest.raises(ProvisionError) as full:
        provisioner.launch(_spec(aws))
    assert full.value.retryable
    calls = []

    def unauthorized(**params):
        calls.append(params)
        raise _capacity("UnauthorizedOperation")

    monkeypatch.setattr(provisioner.ec2, "run_instances", unauthorized)
    with pytest.raises(ProvisionError) as denied:
        provisioner.launch(_spec(aws, env_id="99999999-2222-3333-4444-555555555555"))
    assert not denied.value.retryable and len(calls) == 1 and "UnauthorizedOperation" in denied.value.message


def test_terminate_refuses_an_instance_without_the_install_tags(aws):
    provisioner = _provisioner(aws)
    foreign = aws["ec2"].run_instances(ImageId=aws["image"], MinCount=1, MaxCount=1, TagSpecifications=[
        {"ResourceType": "instance", "Tags": [{"Key": "project", "Value": "armature"}]}])["Instances"][0]["InstanceId"]
    with pytest.raises(ProvisionError, match="Refusing"):
        provisioner.terminate(foreign)
    other_install = _provisioner(aws, install_id="install-2").launch(_spec(aws))
    with pytest.raises(ProvisionError, match="Refusing"):
        provisioner.terminate(other_install.instance_id)
    mine = provisioner.launch(_spec(aws, env_id="22222222-2222-3333-4444-555555555555"))
    provisioner.terminate(mine.instance_id)
    provisioner.terminate(mine.instance_id)
    assert provisioner.describe(mine.instance_id).state == "terminated"
    assert provisioner.describe(foreign).state == "running"


def test_list_owned_sees_only_this_install(aws):
    provisioner = _provisioner(aws)
    mine = provisioner.launch(_spec(aws))
    _provisioner(aws, install_id="install-2").launch(_spec(aws))
    aws["ec2"].run_instances(ImageId=aws["image"], MinCount=1, MaxCount=1)
    assert [i.instance_id for i in provisioner.list_owned()] == [mine.instance_id]


def test_the_armature_project_tag_is_refused(aws):
    with pytest.raises(ProvisionError):
        _provisioner(aws, project="armature")


def test_validate_reports_what_is_missing(aws):
    assert _provisioner(aws).validate() == []
    problems = _provisioner(aws, subnets=[("us-east-1a", "subnet-0000000000000000")], security_group_id="sg-00000000",
                            artifact_bucket="no-such-bucket").validate()
    assert len(problems) == 3 and problems[0].startswith("Can't find the subnets")


def test_hosts_allocate_list_and_release(aws):
    provisioner = _provisioner(aws)
    host = provisioner.allocate_host(name="mac-1", instance_type="mac2.metal", zone="us-east-1a")
    assert [h.host_id for h in provisioner.list_hosts()] == [host.host_id]
    assert _provisioner(aws, install_id="install-2").list_hosts() == []
    with pytest.raises(ProvisionError, match="Refusing"):
        _provisioner(aws, install_id="install-2").release_host(host.host_id)
    provisioner.release_host(host.host_id)


def test_run_on_host_sends_the_commands_through_ssm(aws):
    provisioner = _provisioner(aws)
    host = provisioner.launch(_spec(aws))
    ssm = boto3.client("ssm", region_name="us-east-1")
    provisioner._clients["ssm"] = ssm
    with Stubber(ssm) as stub:
        stub.add_response("send_command", {"Command": {"CommandId": COMMAND}}, {
            "InstanceIds": [host.instance_id], "DocumentName": "AWS-RunShellScript",
            "Parameters": {"commands": ["tart list"]}, "Comment": "armature list"})
        assert provisioner.run_on_host(host.instance_id, ["tart list"], "armature list") == COMMAND
    foreign = aws["ec2"].run_instances(ImageId=aws["image"], MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    with pytest.raises(ProvisionError, match="Refusing"):
        provisioner.run_on_host(foreign, ["id"], "x")


def test_a_mac_launch_stages_the_enrollment_file_and_keeps_it_out_of_the_command(aws):
    s3 = aws["session"].client("s3")
    s3.create_bucket(Bucket="env-artifacts")
    provisioner = _provisioner(aws, artifact_bucket="env-artifacts")
    host = provisioner.launch(_spec(aws))
    sent = []
    provisioner.run_on_host = lambda instance_id, commands, comment: sent.append((instance_id, commands)) or "cmd"
    env = "33333333-2222-3333-4444-555555555555"
    vm = provisioner.launch(_spec(aws, env_id=env, kind="ec2-mac", image="ghcr.io/org/macos:15",
                                  host_instance_id=host.instance_id, user_data="TOKEN=enroll-secret", vm_cpu=4))
    assert vm.instance_id == f"{host.instance_id}/env-{env}"
    assert s3.get_object(Bucket="env-artifacts", Key=f"{INSTALL}/enroll/{env}")["Body"].read() == b"TOKEN=enroll-secret"
    (target, commands), = sent
    assert target == host.instance_id and f"tart set env-{env} --cpu 4" in commands
    assert not [c for c in commands if "enroll-secret" in c]
    fetched = next(i for i, c in enumerate(commands) if c.startswith("aws s3 rm"))
    started = next(i for i, c in enumerate(commands) if c.startswith("nohup tart run"))
    assert fetched < started and "--net-bridged" not in commands[started]
    assert f"--dir=armature:/var/root/armature/{env}:ro" in commands[started]


def test_assumed_role_credentials_carry_the_external_id():
    from armature.transport import ec2

    sts = boto3.client("sts", region_name="us-east-1")
    expires = datetime.now(UTC) + timedelta(hours=1)
    with Stubber(sts) as stub:
        stub.add_response("assume_role", {"Credentials": {"AccessKeyId": "AKIAEXAMPLEEXAMPLE", "SecretAccessKey": "s" * 20,
                                                          "SessionToken": "t" * 20, "Expiration": expires}},
                          {"RoleArn": "arn:aws:iam::123456789012:role/armature-platform-server",
                           "RoleSessionName": "armature-platform", "DurationSeconds": 3600, "ExternalId": "ext-1"})
        real = boto3.Session.client
        try:
            boto3.Session.client = lambda self, name, **kw: sts if name == "sts" else real(self, name, **kw)
            session = ec2.session_for("us-east-1", {"role_arn": "arn:aws:iam::123456789012:role/armature-platform-server",
                                                    "external_id": "ext-1"})
        finally:
            boto3.Session.client = real
        stub.assert_no_pending_responses()
    assert session.get_credentials().get_frozen_credentials().access_key == "AKIAEXAMPLEEXAMPLE"


def _captured(provisioner, fail_spot=False):
    calls = []
    real = provisioner.ec2.run_instances

    def run_instances(**params):
        calls.append(params)
        if fail_spot and "InstanceMarketOptions" in params:
            raise _capacity()
        return real(**params)

    provisioner.ec2.run_instances = run_instances
    return calls


def _tagged(params):
    return {spec["ResourceType"]: {t["Key"]: t["Value"] for t in spec["Tags"]} for spec in params["TagSpecifications"]}


def test_every_run_instances_call_states_its_type_tags_and_imdsv2(aws):
    provisioner = _provisioner(aws)
    calls = _captured(provisioner, fail_spot=True)
    env = "44444444-2222-3333-4444-555555555555"
    template = aws["ec2"].create_launch_template(LaunchTemplateName="linux", LaunchTemplateData={
        "MetadataOptions": {"HttpTokens": "required"}})["LaunchTemplate"]["LaunchTemplateId"]
    provisioner.launch(_spec(aws, env_id=env, launch_template_id=template, instance_types=("m6i.large",),
                             subnets=(aws["subnets"][0],)))
    spot, on_demand = calls
    for params, kinds in ((spot, {"instance", "volume", "network-interface", "spot-instances-request"}),
                          (on_demand, {"instance", "volume", "network-interface"})):
        tagged = _tagged(params)
        assert set(tagged) == kinds
        for tags in tagged.values():
            assert (tags["project"], tags["armature-install"], tags["armature-env"]) == (PROJECT, INSTALL, env)
        assert params["InstanceType"] == "m6i.large"
        assert params["MetadataOptions"]["HttpTokens"] == "required"
        assert params["LaunchTemplate"] == {"LaunchTemplateId": template, "Version": "$Latest"}


def test_the_macos_host_instance_launches_with_explicit_type_tags_and_imdsv2(aws):
    provisioner = _provisioner(aws)
    host = provisioner.allocate_host(name="mac-1", instance_type="mac2.metal", zone="us-east-1a")
    calls = []
    provisioner.ec2.run_instances = lambda **params: calls.append(params) or {"Instances": [
        {"InstanceId": "i-0000000000000mac1", "State": {"Name": "pending"}}]}
    instance = provisioner.launch_host_instance(dedicated_host_id=host.host_id, subnet_id=aws["subnets"][0][1],
                                                launch_template_id="lt-0123456789abcdef1", instance_type="mac2.metal",
                                                instance_profile="mac-profile", name="armature-platform-mac-1")
    (params,) = calls
    assert instance.instance_id == "i-0000000000000mac1"
    assert params["InstanceType"] == "mac2.metal" and params["MetadataOptions"]["HttpTokens"] == "required"
    assert params["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    assert params["Placement"] == {"HostId": host.host_id, "Tenancy": "host"}
    assert set(_tagged(params)) == {"instance", "volume", "network-interface"}
    assert all(t["project"] == PROJECT and t["armature-install"] == INSTALL for t in _tagged(params).values())
    with pytest.raises(ProvisionError, match="Refusing"):
        _provisioner(aws, install_id="install-2").launch_host_instance(
            dedicated_host_id=host.host_id, subnet_id=aws["subnets"][0][1], launch_template_id="lt-0123456789abcdef1",
            instance_type="mac2.metal", instance_profile=None, name="x")


def test_verify_compares_the_stack_allowed_types_with_the_connection(aws):
    provisioner = _provisioner(aws, stack_name="armature-platform-i1-host",
                               allowed_instance_types=["m6i.large", "mac2.metal"])
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    provisioner._clients["cloudformation"] = cfn
    with Stubber(cfn) as stub:
        stub.add_response("describe_stacks", {"Stacks": [{
            "StackName": "armature-platform-i1-host", "CreationTime": datetime.now(UTC), "StackStatus": "CREATE_COMPLETE",
            "Parameters": [{"ParameterKey": "AllowedInstanceTypes", "ParameterValue": "m6i.large"}]}]},
            {"StackName": "armature-platform-i1-host"})
        problems = provisioner.validate()
    assert len(problems) == 1 and "mac2.metal" in problems[0] and "AccessDenied" in problems[0]

"""deploy/aws: the CloudFormation templates and image recipes an account owner runs.

Nothing here touches AWS. The templates are parsed with a loader that knows
CloudFormation's short-form tags, then held to the isolation rules: no
account-specific values, every resource tagged with the install, and no write
that isn't scoped to a resource or to the install's tags."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy" / "aws"
TEMPLATES = sorted(DEPLOY.glob("*.yaml"))
IMAGES = DEPLOY / "images"
INSTALL = IMAGES / "install.sh"

UNTAGGABLE = {"AWS::IAM::ManagedPolicy", "AWS::IAM::Policy", "AWS::IAM::InstanceProfile", "AWS::S3::BucketPolicy"}
TAGGABLE = {"AWS::EC2::SecurityGroup", "AWS::S3::Bucket", "AWS::IAM::Role", "AWS::IAM::User",
            "AWS::EC2::LaunchTemplate", "AWS::ECR::Repository"}
READ_VERBS = ("Describe", "Get", "List")


class _CfnConstructor(SafeConstructor):
    pass


def _short_form(constructor, suffix, node):
    if isinstance(node, ScalarNode):
        value = constructor.construct_scalar(node)
    elif isinstance(node, SequenceNode):
        value = constructor.construct_sequence(node, deep=True)
    elif isinstance(node, MappingNode):
        value = constructor.construct_mapping(node, deep=True)
    if suffix == "Ref":
        return {"Ref": value}
    if suffix == "Condition":
        return {"Condition": value}
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {f"Fn::{suffix}": value}


_CfnConstructor.add_multi_constructor("!", _short_form)


def load(path):
    yaml = YAML(typ="safe", pure=True)
    yaml.Constructor = _CfnConstructor
    return yaml.load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module", params=TEMPLATES, ids=lambda p: p.name)
def template(request):
    return request.param, load(request.param)


def walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def statements(doc, conditions=()):
    """Each policy statement with the conditions (Fn::If names) that guard it."""
    if isinstance(doc, list):
        for item in doc:
            yield from statements(item, conditions)
    elif isinstance(doc, dict) and "Fn::If" in doc:
        name, yes, no = doc["Fn::If"]
        yield from statements(yes, conditions + (name,))
        yield from statements(no, conditions + (f"!{name}",))
    elif isinstance(doc, dict) and "Effect" in doc:
        yield doc, conditions


def identity_policies(resources):
    """(resource id, resource Condition, statement, guarding conditions) for every identity policy."""
    for rid, res in resources.items():
        props = res.get("Properties", {})
        docs = [p["PolicyDocument"] for p in props.get("Policies", [])]
        if res["Type"] in ("AWS::IAM::ManagedPolicy", "AWS::IAM::Policy"):
            docs.append(props["PolicyDocument"])
        for doc in docs:
            for stmt, conds in statements(doc["Statement"]):
                yield rid, res.get("Condition"), stmt, conds


def as_list(value):
    return value if isinstance(value, list) else [value]


def tag_map(tags):
    return {t["Key"]: t["Value"] for t in tags}


def test_templates_exist():
    assert {p.name for p in TEMPLATES} == {"stack.yaml", "host-role.yaml", "mac-host.yaml"}


def test_short_form_tags_parse():
    doc = load(DEPLOY / "stack.yaml")
    props = doc["Resources"]["WorkerInstanceProfile"]["Properties"]
    assert props["Roles"] == [{"Ref": "WorkerRole"}]
    assert props["InstanceProfileName"] == {"Fn::Sub": "${Prefix}-${InstallId}-worker"}
    lt = doc["Resources"]["LinuxLaunchTemplate"]["Properties"]["LaunchTemplateData"]
    assert lt["IamInstanceProfile"]["Arn"] == {"Fn::GetAtt": ["WorkerInstanceProfile", "Arn"]}


HARD_CODED = [
    (r"(?<![\w-])\d{12}(?![\w-])", "account id"),
    (r"\bvpc-[0-9a-f]{8,17}\b", "VPC id"),
    (r"\bsubnet-[0-9a-f]{8,17}\b", "subnet id"),
    (r"\bami-[0-9a-f]{8,17}\b", "AMI id"),
    (r"\bsg-[0-9a-f]{8,17}\b", "security group id"),
    (r"\blt-[0-9a-f]{8,17}\b", "launch template id"),
    (r"\bnat-[0-9a-f]{8,17}\b", "NAT gateway id"),
    (r"stephanh|fh07-", "armature-ops owner value"),
]


def deploy_files():
    return [p for p in sorted(DEPLOY.rglob("*")) if p.is_file()]


@pytest.mark.parametrize("path", deploy_files(), ids=lambda p: str(p.relative_to(DEPLOY)))
def test_no_hard_coded_account_values(path):
    text = path.read_text(encoding="utf-8")
    found = [f"{label}: {m.group(0)}" for pattern, label in HARD_CODED for m in re.finditer(pattern, text)]
    assert not found, f"{path.name} hard-codes {found}"


ACCOUNT_SPECIFIC = {"InstallId", "Owner", "Council", "FinopsTeam", "VpcId", "SubnetIds", "SecurityGroupId",
                    "BucketName", "HostAmiId", "ExternalId", "LaunchTemplateIds", "InstanceRoleArns",
                    "SessionModelArns", "AllowedInstanceTypes"}


def test_account_specific_parameters_have_no_default(template):
    _, doc = template
    for name, param in doc.get("Parameters", {}).items():
        if name in ACCOUNT_SPECIFIC or param["Type"].startswith(("AWS::EC2::", "List<AWS::EC2::")):
            assert "Default" not in param, f"{name} has a default"


def test_no_region_literals(template):
    path, _ = template
    assert not re.search(r"\b(us|eu|ap|ca|sa|me|af)-(east|west|north|south|central|northeast|southeast)-\d\b",
                         path.read_text(encoding="utf-8"))


def test_tag_project_defaults_away_from_armature(template):
    _, doc = template
    param = doc["Parameters"]["TagProject"]
    assert param["Default"] == "armature-platform"
    assert param["Default"] != "armature"
    assertion = doc["Rules"]["ProjectIsNotArmatureOps"]["Assertions"][0]["Assert"]
    assert assertion == {"Fn::Not": [{"Fn::Equals": [{"Ref": "TagProject"}, "armature"]}]}
    assert doc["Parameters"]["Prefix"]["Default"] == "armature-platform"


def test_packer_tag_project_defaults_away_from_armature():
    text = (IMAGES / "base.pkr.hcl").read_text(encoding="utf-8")
    block = re.search(r'variable "tag_project" \{(.*?)\n\}', text, re.S).group(1)
    assert re.search(r'default\s*=\s*"armature-platform"', block)
    assert 'var.tag_project != "armature"' in block
    assert re.search(r'project\s*=\s*var\.tag_project', text)
    assert re.search(r'armature-install\s*=\s*var\.install_id', text)


INSTALL_TAGS = {"project": {"Ref": "TagProject"}, "armature-install": {"Ref": "InstallId"}}


def test_every_taggable_resource_carries_install_tags(template):
    _, doc = template
    for rid, res in doc["Resources"].items():
        kind = res["Type"]
        assert kind in TAGGABLE | UNTAGGABLE, f"{rid}: decide whether {kind} is taggable"
        if kind in UNTAGGABLE:
            continue
        props = res["Properties"]
        if kind == "AWS::EC2::LaunchTemplate":
            specs = {s["ResourceType"]: tag_map(s["Tags"]) for s in props["TagSpecifications"]}
            assert set(specs) == {"launch-template"}, rid
            launched = {s["ResourceType"]: tag_map(s["Tags"])
                        for s in props["LaunchTemplateData"]["TagSpecifications"]}
            assert {"instance", "volume", "network-interface"} <= set(launched), rid
            specs.update(launched)
        else:
            specs = {kind: tag_map(props["Tags"])}
        for resource_type, tags in specs.items():
            for key, value in INSTALL_TAGS.items():
                assert tags.get(key) == value, f"{rid} {resource_type}: {key} is {tags.get(key)!r}"
            for key in ("owner", "council", "bpmeta:finops:team"):
                assert key in tags, f"{rid} {resource_type}: missing {key}"


def _tag_conditioned(stmt):
    keys = [k for op in stmt.get("Condition", {}).values() for k in op]
    return any(re.search(r"(?i)(Resource|Request)Tag/(project|armature-install)$", k) for k in keys)


def _is_write(action):
    _, _, verb = action.partition(":")
    return not verb.startswith(READ_VERBS)


def test_no_write_action_on_every_resource(template):
    _, doc = template
    for rid, _, stmt, _ in identity_policies(doc["Resources"]):
        if stmt["Effect"] != "Allow":
            continue
        writes = [a for a in as_list(stmt["Action"]) if _is_write(a)]
        if writes and "*" in as_list(stmt["Resource"]):
            assert _tag_conditioned(stmt), f"{rid} {stmt.get('Sid')}: {writes} on Resource *"


def test_no_wildcard_actions(template):
    _, doc = template
    for rid, _, stmt, _ in identity_policies(doc["Resources"]):
        if stmt["Effect"] == "Allow":
            assert not any(a.endswith(":*") or a == "*" for a in as_list(stmt["Action"])), rid


def test_tag_conditions_require_both_install_tags(template):
    _, doc = template
    for rid, _, stmt, _ in identity_policies(doc["Resources"]):
        cond = stmt.get("Condition", {})
        tags = {k.split("/", 1)[1]: v for op in cond.values() for k, v in op.items()
                if re.search(r"(?i)(Resource|Request)Tag/", k)}
        if tags:
            assert tags == INSTALL_TAGS, f"{rid} {stmt.get('Sid')}: {tags}"


def test_host_allocation_only_under_its_condition():
    seen = set()
    for path in TEMPLATES:
        doc = load(path)
        for rid, res_cond, stmt, conds in identity_policies(doc["Resources"]):
            actions = {a for a in as_list(stmt["Action"]) if a in ("ec2:AllocateHosts", "ec2:ReleaseHosts")}
            if not actions:
                continue
            seen |= actions
            assert path.name == "host-role.yaml"
            assert stmt["Effect"] == "Allow"
            assert "AllowHostAllocation" in conds or res_cond == "AllowHostAllocation", \
                f"{rid} {stmt.get('Sid')} grants {actions} outside AllowHostAllocation"
            assert not any(c.startswith("!") for c in conds), f"{rid}: granted on the false branch"
            assert _tag_conditioned(stmt), f"{rid} {stmt.get('Sid')}: host actions not held to the install's tags"
        for res in doc["Resources"].values():
            assert res["Type"] != "AWS::EC2::Host", f"{path.name} allocates a host"
    assert seen == {"ec2:AllocateHosts", "ec2:ReleaseHosts"}

    doc = load(DEPLOY / "host-role.yaml")
    assert doc["Parameters"]["AllowHostAllocation"]["Default"] == "false"
    assert doc["Conditions"]["AllowHostAllocation"] == {"Fn::Equals": [{"Ref": "AllowHostAllocation"}, "true"]}


INSTANCE_ROLES = ((DEPLOY / "stack.yaml", "WorkerRole"), (DEPLOY / "mac-host.yaml", "MacHostRole"))


def test_instance_roles_hold_no_bedrock_or_iam():
    for path, rid in INSTANCE_ROLES:
        doc = load(path)
        role = {rid: doc["Resources"][rid]}
        for _, _, stmt, _ in identity_policies(role):
            if stmt["Effect"] != "Allow":
                continue
            for action in as_list(stmt["Action"]):
                assert not action.startswith(("bedrock:", "iam:", "sts:")), f"{path.name} {rid}: {action}"
        managed = role[rid]["Properties"].get("ManagedPolicyArns", [])
        assert managed == [{"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"}]


def test_instance_roles_deny_ssm_parameters():
    wanted = {"ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory"}
    for path, rid in INSTANCE_ROLES:
        role = {rid: load(path)["Resources"][rid]}
        denied = {a for _, _, stmt, _ in identity_policies(role) if stmt["Effect"] == "Deny"
                  and stmt["Resource"] == "*" for a in as_list(stmt["Action"])}
        assert wanted <= denied, f"{path.name} {rid} doesn't deny {wanted - denied}"


def test_mac_host_role_only_pulls_images():
    role = {"MacHostRole": load(DEPLOY / "mac-host.yaml")["Resources"]["MacHostRole"]}
    ecr = {a for _, _, stmt, _ in identity_policies(role) if stmt["Effect"] == "Allow"
           for a in as_list(stmt["Action"]) if a.startswith("ecr:")}
    assert ecr == {"ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage",
                   "ecr:GetDownloadUrlForLayer"}


def test_mac_host_role_takes_only_its_install_enrollment_files():
    role = {"MacHostRole": load(DEPLOY / "mac-host.yaml")["Resources"]["MacHostRole"]}
    grants = {a: json.dumps(stmt["Resource"]) for _, _, stmt, _ in identity_policies(role) if stmt["Effect"] == "Allow"
              for a in as_list(stmt["Action"]) if a.startswith("s3:")}
    assert set(grants) == {"s3:PutObject", "s3:GetObject", "s3:DeleteObject"}
    for action in ("s3:GetObject", "s3:DeleteObject"):
        assert "${InstallId}/enroll/*" in grants[action]
    readme = (DEPLOY / "README.md").read_text()
    assert "`s3:GetObject`, `s3:DeleteObject` | `<bucket>/<install>/enroll/*`" in readme


def test_image_repository_keeps_ten_images():
    repo = load(DEPLOY / "mac-host.yaml")["Resources"]["ImageRepository"]["Properties"]
    rules = json.loads(repo["LifecyclePolicy"]["LifecyclePolicyText"])["rules"]
    assert [r["selection"]["countNumber"] for r in rules] == [10]


def test_host_manager_reads_no_command_listings():
    doc = load(DEPLOY / "host-role.yaml")
    actions = {a for _, _, stmt, _ in identity_policies(doc["Resources"]) for a in as_list(stmt["Action"])}
    assert not {"ssm:ListCommands", "ssm:ListCommandInvocations"} & actions


def _run_instances(sid_resource):
    doc = load(DEPLOY / "host-role.yaml")
    for _, _, stmt, _ in identity_policies(doc["Resources"]):
        if "ec2:RunInstances" in as_list(stmt["Action"]) and sid_resource in json.dumps(stmt["Resource"]):
            yield stmt


def test_instance_launch_is_bound_to_templates_imdsv2_and_types():
    [stmt] = _run_instances(":instance/*")
    assert as_list(stmt["Resource"]) == [
        {"Fn::Sub": "arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:instance/*"}]
    equals = stmt["Condition"]["StringEquals"]
    assert equals["ec2:MetadataHttpTokens"] == "required"
    assert equals["ec2:InstanceType"] == {"Ref": "AllowedInstanceTypes"}
    assert equals["aws:RequestTag/project"] == {"Ref": "TagProject"}
    assert equals["aws:RequestTag/armature-install"] == {"Ref": "InstallId"}
    template = json.dumps(stmt["Condition"]["ArnLike"]["ec2:LaunchTemplate"])
    assert "launch-template/" in template and '"Ref": "LaunchTemplateIds"' in template
    assert "Default" not in load(DEPLOY / "host-role.yaml")["Parameters"]["AllowedInstanceTypes"]


def test_spot_requests_launch_tagged():
    [stmt] = _run_instances("spot-instances-request/*")
    assert stmt["Condition"]["StringEquals"] == {"aws:RequestTag/project": {"Ref": "TagProject"},
                                                 "aws:RequestTag/armature-install": {"Ref": "InstallId"}}
    [tag] = [s for _, _, s, _ in identity_policies(load(DEPLOY / "host-role.yaml")["Resources"])
             if s.get("Sid") == "TagAtLaunch"]
    assert "spot-instances-request/*" in json.dumps(tag["Resource"])
    # The server tags Spot requests itself, so on-demand launches through the template stay valid.
    lt = load(DEPLOY / "stack.yaml")["Resources"]["LinuxLaunchTemplate"]["Properties"]["LaunchTemplateData"]
    assert {s["ResourceType"] for s in lt["TagSpecifications"]} == {"instance", "volume", "network-interface"}


def test_mac_host_metadata_stays_out_of_reach_of_job_vms():
    data = load(DEPLOY / "mac-host.yaml")["Resources"]["MacLaunchTemplate"]["Properties"]["LaunchTemplateData"]
    assert data["MetadataOptions"]["HttpTokens"] == "required"
    assert data["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    readme = (DEPLOY / "README.md").read_text()
    assert "Job VMs on a Mac host can't use the host's role" in readme and "HttpPutResponseHopLimit: 1" in readme


def test_mac_instances_launch_only_on_install_hosts():
    [stmt] = _run_instances("dedicated-host/*")
    assert stmt["Condition"] == {"StringEquals": {"ec2:ResourceTag/project": {"Ref": "TagProject"},
                                                  "ec2:ResourceTag/armature-install": {"Ref": "InstallId"}}}


def test_assume_role_reaches_only_the_session_role_with_an_external_id():
    doc = load(DEPLOY / "host-role.yaml")
    res = doc["Resources"]
    grants = [stmt for _, _, stmt, _ in identity_policies(res) if "sts:AssumeRole" in as_list(stmt["Action"])]
    assert [g["Resource"] for g in grants] == [{"Fn::GetAtt": ["SessionRole", "Arn"]}]
    for rid in ("SessionRole", "HostManagerRole"):
        trust = res[rid]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert all(s["Condition"]["StringEquals"]["sts:ExternalId"] == {"Ref": "ExternalId"} for s in trust), rid
    assert doc["Parameters"]["ExternalId"]["NoEcho"] is True


def test_launch_uses_named_subnets_and_templates_only():
    doc = load(DEPLOY / "host-role.yaml")
    for _, _, stmt, _ in identity_policies(doc["Resources"]):
        if "ec2:RunInstances" not in as_list(stmt["Action"]):
            continue
        rendered = json.dumps(stmt["Resource"])
        assert "subnet/*" not in rendered and "launch-template/*" not in rendered, stmt.get("Sid")
    sids = {stmt.get("Sid") for _, _, stmt, _ in identity_policies(doc["Resources"])}
    assert {"LaunchIntoSubnets", "LaunchFromTemplates"} <= sids


def test_launch_templates_require_imdsv2_and_terminate_on_shutdown():
    for path in (DEPLOY / "stack.yaml", DEPLOY / "mac-host.yaml"):
        for res in load(path)["Resources"].values():
            if res["Type"] != "AWS::EC2::LaunchTemplate":
                continue
            data = res["Properties"]["LaunchTemplateData"]
            assert data["MetadataOptions"]["HttpTokens"] == "required"
            assert data["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
            assert "UserData" not in data
            assert all(b["Ebs"]["Encrypted"] is True for b in data["BlockDeviceMappings"])


def test_outputs_feed_the_aws_settings_page():
    outputs = set()
    for path in TEMPLATES:
        outputs |= set(load(path).get("Outputs", {}))
    assert {"SecurityGroupId", "InstanceProfileName", "LaunchTemplateIds", "BucketName",
            "SessionRoleArn", "HostManagerRoleArn"} <= outputs


README = DEPLOY / "README.md"


def _shell_command(text, start):
    """The full command starting at `start`, following backslash continuations."""
    lines = text[text.index(start):].splitlines()
    command = []
    for line in lines:
        command.append(line)
        if not line.rstrip().endswith("\\"):
            break
    return " ".join(command)


def test_readme_tags_owner_allocated_hosts():
    text = README.read_text(encoding="utf-8")
    allocate = _shell_command(text, "aws ec2 allocate-hosts")
    assert "ResourceType=dedicated-host" in allocate
    tag = _shell_command(text, "aws ec2 create-tags")
    for command in (allocate, tag):
        assert "Key=project,Value=" in command
        assert "Key=armature-install,Value=" in command and "$INSTALL" in command


def test_readme_states_the_launch_contract():
    text = README.read_text(encoding="utf-8")
    for phrase in ("`instance`", "`volume`", "`network-interface`", "`spot-instances-request` for Spot launches only",
                   "`InstanceType` is always passed", "`HttpTokens=required`"):
        assert phrase in text, phrase


def test_ios_build_forwards_constraints(tmp_path):
    build = IMAGES / "ios-vm" / "build.sh"
    text = build.read_text(encoding="utf-8")
    assert text.count('install.sh" ${constraints[@]+"${constraints[@]}"}') == 2
    assert 'vm_scp "$constraints" "$remote_dir/pip-constraints.txt"' in text
    result = subprocess.run(["bash", str(build), "--constraints", str(tmp_path / "nope.txt"), "a", "b", "c", "d"],
                            capture_output=True, text=True, env={**os.environ, "XCODE_VERSION": "1.0"})
    assert result.returncode != 0
    assert "missing constraints file" in result.stderr


def test_base_image_blocks_metadata_for_job_and_git():
    unit = (IMAGES / "armature-imds-block.service").read_text(encoding="utf-8")
    assert "for u in armature-job armature-git" in unit
    assert "-d 169.254.169.254 -m owner --uid-owner \"$u\" -j REJECT" in unit
    assert "WantedBy=multi-user.target" in unit
    packer = (IMAGES / "base.pkr.hcl").read_text(encoding="utf-8")
    assert "systemctl enable --now armature-imds-block.service" in packer
    assert "/etc/systemd/system/armature-imds-block.service" in packer


def test_base_image_sudoers_is_narrow():
    packer = (IMAGES / "base.pkr.hcl").read_text(encoding="utf-8")
    rule = re.search(r"printf '([^']*)' > /etc/sudoers.d/armature-runner", packer).group(1)
    assert rule.split("\\\\n")[:2] == ["armature-runner ALL=(armature-job) NOPASSWD: ALL",
                                         "armature-runner ALL=(armature-git) NOPASSWD: /usr/bin/git"]


def test_cfn_lint():
    if shutil.which("cfn-lint") is None:
        pytest.skip("cfn-lint is not installed")
    result = subprocess.run(["cfn-lint", *map(str, TEMPLATES)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_packer_syntax():
    if shutil.which("packer") is None:
        pytest.skip("packer is not installed")
    result = subprocess.run(["packer", "validate", "-syntax-only", str(IMAGES / "base.pkr.hcl")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# install.sh, run against a fake root with curl replaced by a copy from disk.

def _fake_bin(tmp_path, source, fake_python=True):
    """curl copies `source` and logs its arguments. python3 does nothing unless fake_python is false."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/curl.log"\n'
                    f'while [ "$1" != -o ]; do shift; done\ncp "{source}" "$2"\n')
    curl.chmod(0o755)
    if fake_python:
        python = bin_dir / "python3"
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
    return bin_dir


def _run_install(tmp_path, source, entries, *args, fake_python=True, env=None):
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed")
    listing = tmp_path / "installs.json"
    listing.write_text(json.dumps(entries))
    root = tmp_path / "root"
    env = {**os.environ, **(env or {}), "ARMATURE_INSTALL_ROOT": str(root),
           "PATH": f"{_fake_bin(tmp_path, source, fake_python)}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(INSTALL), *args, str(listing)], capture_output=True, text=True,
                            env=env, stdin=subprocess.DEVNULL)
    return result, root


def _fake_venv(tmp_path, version="26.2.2"):
    """A pip that reports `version` and logs each install's arguments and requirements files in order."""
    pip = tmp_path / "root/opt/armature/venv/bin/pip"
    pip.parent.mkdir(parents=True)
    pip.write_text(f'#!/bin/sh\n[ "$1" != --version ] || {{ echo "pip {version} from /fake"; exit 0; }}\n'
                   f'echo "$@" >> "{tmp_path}/pip.log"\n'
                   f'for a in "$@"; do case "$a" in -r) req=1;; *) [ -z "$req" ] || '
                   f'{{ cp "$a" "{tmp_path}/req.txt"; cat "$a" >> "{tmp_path}/reqs.log"; req=; }};; esac; done\n')
    pip.chmod(0o755)


def _pip_pin():
    text = INSTALL.read_text(encoding="utf-8")
    return (re.search(r"^pip_version=(\S+)$", text, re.M).group(1),
            re.search(r"^pip_sha256=([0-9a-f]{64})$", text, re.M).group(1))


def _sha256(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_install_places_a_verified_binary(tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\necho hi\n")
    entry = {"name": "claude", "version": "1.2.3", "url": "https://example.test/claude", "sha256": _sha256(tool)}
    result, root = _run_install(tmp_path, tool, [entry])
    assert result.returncode == 0, result.stderr
    installed = root / "usr/local/bin/claude"
    assert installed.read_text() == tool.read_text()
    assert os.access(installed, os.X_OK)
    manifest = json.loads((root / "usr/local/share/armature-image-manifest.json").read_text())
    assert manifest == [entry]


def test_install_accepts_an_empty_list(tmp_path):
    result, root = _run_install(tmp_path, tmp_path / "unused", [])
    assert result.returncode == 0, result.stderr
    assert json.loads((root / "usr/local/share/armature-image-manifest.json").read_text()) == []
    assert not (tmp_path / "curl.log").exists()


def test_install_downloads_over_https_only(tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("x\n")
    entry = {"name": "tool", "version": "1", "url": "https://example.test/tool", "sha256": _sha256(tool)}
    result, _ = _run_install(tmp_path, tool, [entry])
    assert result.returncode == 0, result.stderr
    assert "--proto =https --proto-redir =https" in (tmp_path / "curl.log").read_text()


def _wheel(tmp_path):
    wheel = tmp_path / "armature_agents-1.0-py3-none-any.whl"
    wheel.write_text("wheel\n")
    return wheel, {"name": "armature", "version": "1.0", "url": f"https://example.test/{wheel.name}",
                   "sha256": _sha256(wheel)}


def test_install_wheel_without_constraints_installs_plainly(tmp_path):
    wheel, entry = _wheel(tmp_path)
    _fake_venv(tmp_path)
    result, _ = _run_install(tmp_path, wheel, [entry])
    assert result.returncode == 0, result.stderr
    log = (tmp_path / "pip.log").read_text()
    assert "--require-hashes" not in log and log.strip().endswith(wheel.name)


def test_install_wheel_with_constraints_requires_hashes(tmp_path):
    wheel, entry = _wheel(tmp_path)
    _fake_venv(tmp_path)
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("httpx==0.27.0 --hash=sha256:" + "a" * 64 + "\n")
    result, _ = _run_install(tmp_path, wheel, [entry], "--constraints", str(constraints))
    assert result.returncode == 0, result.stderr
    installs = (tmp_path / "pip.log").read_text().splitlines()
    assert len(installs) == 2
    assert "--require-hashes" in installs[0] and "--only-binary :all:" in installs[0] and "-c" not in installs[0]
    assert "--require-hashes" in installs[1] and f"-c {constraints}" in installs[1]
    version, sha256 = _pip_pin()
    pip_req, wheel_req = (tmp_path / "reqs.log").read_text().splitlines()
    assert pip_req == f"pip=={version} --hash=sha256:{sha256}"
    assert wheel_req.endswith(f"{wheel.name} --hash=sha256:{entry['sha256']}")


def test_install_wheel_refuses_an_old_pip_after_pinning(tmp_path):
    wheel, entry = _wheel(tmp_path)
    _fake_venv(tmp_path, version="23.0")
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("")
    result, _ = _run_install(tmp_path, wheel, [entry], "--constraints", str(constraints))
    assert result.returncode != 0
    assert "pip is not 26 or newer" in result.stderr
    assert len((tmp_path / "pip.log").read_text().splitlines()) == 1


def test_pip_pin_is_a_full_hash_of_a_26_release():
    version, sha256 = _pip_pin()
    assert int(version.split(".")[0]) >= 26
    assert re.search(r"^pip_min_major=26$", INSTALL.read_text(encoding="utf-8"), re.M)
    assert len(sha256) == 64


def _system_pip_major():
    python = shutil.which("python3")
    if python is None:
        return 0
    result = subprocess.run([python, "-m", "pip", "--version"], capture_output=True, text=True)
    match = re.match(r"pip (\d+)\.", result.stdout)
    return int(match.group(1)) if result.returncode == 0 and match else 0


def _build_wheel(out, name, version, requires=()):
    import base64
    import hashlib
    import zipfile
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": "",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                            + "".join(f"Requires-Dist: {r}\n" for r in requires),
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(content.encode())}")
    files[f"{dist}/RECORD"] = "\n".join([*record, f"{dist}/RECORD,,"]) + "\n"
    wheel = out / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        for path, content in files.items():
            z.writestr(path, content)
    return wheel


@pytest.fixture(scope="module")
def pinned_pip_wheel(tmp_path_factory):
    """PyPI's wheel for the pinned pip, which install.sh installs before any hashed install."""
    if _system_pip_major() < 24:
        pytest.skip("needs python3 with pip 24 or newer")
    version, sha256 = _pip_pin()
    out = tmp_path_factory.mktemp("pip")
    result = subprocess.run([shutil.which("python3"), "-m", "pip", "download", f"pip=={version}", "--no-deps",
                             "--only-binary", ":all:", "--timeout", "10", "--disable-pip-version-check",
                             "-q", "-d", str(out)], capture_output=True, text=True)
    wheels = list(out.glob("pip-*.whl"))
    if result.returncode != 0 or len(wheels) != 1:
        pytest.skip(f"could not download pip {version}: {result.stderr.strip()[-200:]}")
    assert _sha256(wheels[0]) == sha256, "install.sh's pip_sha256 doesn't match PyPI's wheel"
    return wheels[0]


@pytest.mark.parametrize("good_hash", [True, False], ids=["matching", "tampered"])
def test_install_wheel_with_real_pip_and_hashed_constraints(tmp_path, good_hash, pinned_pip_wheel):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    shutil.copy(pinned_pip_wheel, wheels)
    top = _build_wheel(wheels, "armtop", "1.0", ["armdep==2.0"])
    dep = _build_wheel(wheels, "armdep", "2.0")
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(f"armdep==2.0 --hash=sha256:{_sha256(dep) if good_hash else '0' * 64}\n")
    entry = {"name": "armtop", "version": "1.0", "url": f"https://example.test/{top.name}", "sha256": _sha256(top)}
    result, root = _run_install(tmp_path, top, [entry], "--constraints", str(constraints), fake_python=False,
                                env={"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheels),
                                     "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    site = list((root / "opt/armature/venv/lib").glob("python*/site-packages"))
    if good_hash:
        assert result.returncode == 0, result.stderr
        assert (site[0] / "armdep").is_dir() and (site[0] / "armtop").is_dir()
        assert (site[0] / f"pip-{_pip_pin()[0]}.dist-info").is_dir()
    else:
        assert result.returncode != 0
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
        assert not any((s / "armdep").exists() for s in site)


def test_install_refuses_a_missing_constraints_file(tmp_path):
    result, _ = _run_install(tmp_path, tmp_path / "unused", [], "--constraints", str(tmp_path / "nope.txt"))
    assert result.returncode != 0
    assert "no such constraints file" in result.stderr


def test_install_refuses_a_checksum_mismatch(tmp_path):
    tool = tmp_path / "tool"
    tool.write_text("tampered\n")
    entry = {"name": "codex", "version": "1.0.0", "url": "https://example.test/codex", "sha256": "0" * 64}
    result, root = _run_install(tmp_path, tool, [entry])
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (root / "usr/local/bin/codex").exists()


@pytest.mark.parametrize("field,value,message", [
    ("sha256", "abc", "sha256 must be"),
    ("url", "http://example.test/x", "must be https"),
    ("name", "../evil", "bad name"),
])
def test_install_rejects_malformed_entries(tmp_path, field, value, message):
    tool = tmp_path / "tool"
    tool.write_text("x\n")
    entry = {"name": "tool", "version": "1", "url": "https://example.test/tool", "sha256": _sha256(tool)}
    entry[field] = value
    result, _ = _run_install(tmp_path, tool, [entry])
    assert result.returncode != 0
    assert message in result.stderr


def test_install_unpacks_a_single_file_archive_under_the_entry_name(tmp_path):
    import tarfile
    inner = tmp_path / "codex-x86_64-unknown-linux-musl"
    inner.write_text("#!/bin/sh\n")
    inner.chmod(0o755)
    archive = tmp_path / "codex.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(inner, arcname=inner.name)
    entry = {"name": "codex", "version": "0.9.0", "url": "https://example.test/codex.tar.gz",
             "sha256": _sha256(archive)}
    result, root = _run_install(tmp_path, archive, [entry])
    assert result.returncode == 0, result.stderr
    assert (root / "usr/local/bin/codex").resolve() == (root / "opt/armature/tools/codex/0.9.0" / inner.name)


def test_base_image_runs_agent_sessions_as_their_own_user():
    from armature.nodes.harness import CLAUDE_CREDENTIALS, CODEX_CREDENTIALS, WRITE_ENV
    packer = (IMAGES / "base.pkr.hcl").read_text(encoding="utf-8")
    assert "for u in armature-runner armature-git armature-job armature-agent; do useradd" in packer
    assert "usermod -aG armature-agent armature-job" in packer
    assert "for u in armature-job armature-git armature-agent; do iptables -w -C OUTPUT" in packer
    assert "for u in armature-job armature-git armature-agent; do" in \
        (IMAGES / "armature-imds-block.service").read_text(encoding="utf-8")
    rule = re.search(r"printf '([^']*)' > /etc/sudoers.d/armature-job", packer).group(1)
    job_no_pty, no_pty, keep, allowed, _ = rule.split("\\\\n")
    assert job_no_pty == "Defaults>armature-job !use_pty"
    assert no_pty == "Defaults>armature-agent !use_pty"
    assert allowed == ("armature-job ALL=(armature-agent) NOPASSWD: /usr/local/bin/claude, /usr/local/bin/codex, "
                       "/bin/kill")
    names = set(re.search(r'env_keep = \\\\\\"([^\\\\]*)\\\\\\"', keep).group(1).split())
    assert {"HOME", "TMPDIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "PATH", "TERM", "LANG", *CLAUDE_CREDENTIALS,
            *CODEX_CREDENTIALS, *WRITE_ENV} == names
    assert "SETENV" not in rule
    runner = re.search(r"printf '([^']*)' > /etc/sudoers.d/armature-runner", packer).group(1)
    assert "armature-runner ALL=(armature-agent) NOPASSWD: /bin/kill, /bin/rm, /usr/bin/find" in \
        runner.split("\\\\n")
    assert "visudo -cf /etc/sudoers.d/armature-job" in packer


def test_base_image_runs_the_job_user_without_a_pty_so_the_engine_key_reaches_stdin():
    packer = (IMAGES / "base.pkr.hcl").read_text(encoding="utf-8")
    sudoers = re.search(r"printf '([^']*)' > /etc/sudoers.d/armature-job", packer).group(1).split("\\\\n")
    assert "Defaults>armature-job !use_pty" in sudoers
    runner = re.search(r"printf '([^']*)' > /etc/sudoers.d/armature-runner", packer).group(1)
    assert "armature-runner ALL=(armature-job) NOPASSWD: ALL" in runner.split("\\\\n")

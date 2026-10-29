# Armature environments on AWS

These templates and image recipes give an Armature server somewhere to run
jobs: one short-lived EC2 instance per job, or a VM on a Mac Dedicated Host for
iOS work. The account owner runs everything on this page once per install. After
that, the server launches, enrolls, and terminates environments on its own.

Everything is tagged `project=<TagProject>` and `armature-install=<InstallId>`,
and every write the server can make is held to those tags. `TagProject`
defaults to `armature-platform` and can't be `armature`, so an install never
touches armature-ops resources in the same account.

| File | What it creates |
|------|-----------------|
| `stack.yaml` | Security group, artifact bucket, worker instance role and profile, Linux launch template |
| `mac-host.yaml` | Mac host role and profile, Tart image repository, Mac launch template. Optional. |
| `host-role.yaml` | The host manager role the server assumes, an optional IAM user, and the session role for per-job model credentials |
| `images/base.pkr.hcl` | The Linux AMI for an environment type |
| `images/install.sh` | Installs a `{name, version, url, sha256}` list and refuses any checksum mismatch |
| `images/ios-vm/` | The Tart VM image for an iOS environment type |

## Before you start

You need:

- The AWS CLI, signed in to the target account with rights to create IAM
  roles, EC2 resources, S3 buckets, and ECR repositories.
- A VPC with private subnets and a NAT gateway. Workers get no public address
  and no ingress. They reach the server, GitHub, and SSM through the NAT.
- An install id: 2 to 12 lowercase letters, digits, or dashes, such as `prod`.
  The AWS settings page shows the same value.
- Packer 1.10 or later, to build images.

Set these once in your shell. Every command below uses them.

```sh
export AWS_REGION=<region>
export INSTALL=<install id>
export PREFIX=armature-platform
export OWNER=<owner tag> COUNCIL=<council tag> FINOPS=<finops team tag>
export VPC=<vpc id>
export SUBNETS=<subnet id>,<subnet id>       # private, one or more per zone
export EXTERNAL_ID="$(openssl rand -hex 24)"  # keep it, the settings page needs it
```

If the account has never run Spot instances, create Spot's service-linked role
once:

```sh
aws iam create-service-linked-role --aws-service-name spot.amazonaws.com
```

Name each stack `$PREFIX-$INSTALL-<part>`. The host manager role can read
stacks only by that pattern.

## 1. Deploy the base stack

```sh
aws cloudformation deploy \
  --stack-name "$PREFIX-$INSTALL-base" \
  --template-file deploy/aws/stack.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    InstallId="$INSTALL" Owner="$OWNER" Council="$COUNCIL" FinopsTeam="$FINOPS" \
    VpcId="$VPC"

out() { aws cloudformation describe-stacks --stack-name "$1" \
  --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }
SG=$(out "$PREFIX-$INSTALL-base" SecurityGroupId)
BUCKET=$(out "$PREFIX-$INSTALL-base" BucketName)
PROFILE=$(out "$PREFIX-$INSTALL-base" InstanceProfileName)
LT=$(out "$PREFIX-$INSTALL-base" LaunchTemplateIds)
ROLES=$(out "$PREFIX-$INSTALL-base" WorkerRoleArn)
```

The bucket is retained when you delete the stack.

## 2. Deploy the Mac stack (iOS only)

Skip this step if no environment type uses `ec2-mac`.

```sh
aws cloudformation deploy \
  --stack-name "$PREFIX-$INSTALL-mac" \
  --template-file deploy/aws/mac-host.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    InstallId="$INSTALL" Owner="$OWNER" Council="$COUNCIL" FinopsTeam="$FINOPS" \
    SecurityGroupId="$SG" BucketName="$BUCKET" HostAmiId=<macOS AMI id>

LT="$LT,$(out "$PREFIX-$INSTALL-mac" MacLaunchTemplateId)"
ROLES="$ROLES,$(out "$PREFIX-$INSTALL-mac" MacHostRoleArn)"
```

Pick a macOS AMI at least as new as the guest macOS in your Tart image.

The stack allocates no Dedicated Host, because each host bills a 24 hour
minimum from allocation. When you allocate one yourself, tag it with the
install, or every Mac launch onto it is denied:

```sh
aws ec2 allocate-hosts --instance-type mac-m4.metal --availability-zone <zone> \
  --quantity 1 --auto-placement off \
  --tag-specifications "ResourceType=dedicated-host,Tags=[{Key=project,Value=armature-platform},{Key=armature-install,Value=$INSTALL},{Key=owner,Value=$OWNER},{Key=council,Value=$COUNCIL},{Key=bpmeta:finops:team,Value=$FINOPS}]"
```

Tag a host that already exists the same way:

```sh
aws ec2 create-tags --resources <host id> \
  --tags Key=project,Value=armature-platform Key=armature-install,Value="$INSTALL" \
    Key=owner,Value="$OWNER" Key=council,Value="$COUNCIL" Key=bpmeta:finops:team,Value="$FINOPS"
```

Use your `TagProject` value if you changed it from `armature-platform`. Hosts
an admin allocates from the AWS page get these tags automatically.

Then store the SSH key pair every host uses to reach its VMs:

```sh
ssh-keygen -t ed25519 -N '' -f vm_key
aws secretsmanager create-secret --name "$PREFIX-$INSTALL/mac-host/vm-key" \
  --secret-string "$(jq -n --rawfile private_key vm_key --rawfile public_key vm_key.pub \
    '{private_key: $private_key, public_key: $public_key}')"
shred -u vm_key 2>/dev/null || rm -P vm_key
```

## 3. Deploy the host role

```sh
aws cloudformation deploy \
  --stack-name "$PREFIX-$INSTALL-host" \
  --template-file deploy/aws/host-role.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    InstallId="$INSTALL" Owner="$OWNER" Council="$COUNCIL" FinopsTeam="$FINOPS" \
    ExternalId="$EXTERNAL_ID" \
    SubnetIds="$SUBNETS" SecurityGroupId="$SG" BucketName="$BUCKET" \
    LaunchTemplateIds="$LT" InstanceRoleArns="$ROLES" \
    AllowedInstanceTypes=<every instance type your environment types use> \
    SessionModelArns=<inference profile ARN>,<foundation model ARN>,... \
    TrustedPrincipalArn=<the server's IAM role ARN, or leave out> \
    CreateAccessUser=false \
    AllowHostAllocation=false
```

Choose the parameters that fit how your server runs:

- **The server runs on AWS with its own role.** Set `TrustedPrincipalArn` to
  that role and keep `CreateAccessUser=false`. The server assumes the host
  manager role with the external id.
- **The server runs elsewhere.** Set `CreateAccessUser=true`, then create an
  access key for the user and paste it on the settings page. Create the key
  yourself, so it never appears in stack outputs:

  ```sh
  aws iam create-access-key --user-name "$(out "$PREFIX-$INSTALL-host" AccessUserName)"
  ```

- **`SessionModelArns`** lists the Bedrock inference profile and every
  foundation model ARN it routes to. A cross-region profile routes to several
  regions, so list the model in each. Jobs get one-hour credentials for these
  models only.
- **`SessionEgressCidrs`** is only for workers that reach Bedrock through a
  NAT gateway. Set it to the NAT's public addresses to make model credentials
  useless anywhere else. Leave it empty, which turns the check off, when
  workers reach Bedrock through a VPC endpoint, because those calls carry no
  public source address.
- **`AllowedInstanceTypes`** lists every instance type the server may launch,
  such as `m7i.large,c7i.2xlarge,mac-m4.metal`. Add a type here before an
  environment type uses it.
- **`AllowHostAllocation=true`** lets Armature admins allocate and release Mac
  hosts from the AWS page. Leave it `false` to keep allocation with you. The
  page then answers "Ask the account owner to allocate a host".

When you add the Mac stack later, redeploy this stack with the longer
`LaunchTemplateIds` and `InstanceRoleArns`.

The policy holds every launch to this install, and the server meets it with
an explicit `RunInstances` contract:

- `TagSpecifications` carry `project` and `armature-install` on `instance`,
  `volume`, and `network-interface` for every launch, and on
  `spot-instances-request` for Spot launches only. The policy checks these
  request tags. It doesn't rely on the launch template's tags.
- `InstanceType` is always passed, and must be in `AllowedInstanceTypes`.
- `MetadataOptions` always passes `HttpTokens=required`.
- `LaunchTemplate` is one of `LaunchTemplateIds`.
- A Mac instance lands only on a Dedicated Host tagged with this install. See
  step 2 for tagging hosts you allocate yourself.

### What the instance roles can reach in S3

| Role | Actions | Objects |
|------|---------|---------|
| Worker role (`stack.yaml`) | `s3:PutObject` | `<bucket>/<install>/results/*` |
| Mac host role (`mac-host.yaml`) | `s3:PutObject` | `<bucket>/<install>/results/*` |
| Mac host role (`mac-host.yaml`) | `s3:GetObject`, `s3:DeleteObject` | `<bucket>/<install>/enroll/*` |

The server writes a Mac environment's enrollment file to
`<install>/enroll/<environment id>`. The host copies it into the VM's shared
folder and deletes it, so the token never appears in an SSM command.

## 4. Build the Linux image

Each environment type with kind `ec2-linux` needs an AMI. Copy
`images/base-installs.example.json`, fill in real versions, URLs, and the
sha256 of each file, and keep it with your deployment config. The base image
installs git from the distribution, then Claude Code, Codex, and the armature
runner from that list. It creates four users:

| User | Holds |
|------|-------|
| `armature-runner` | The runner key |
| `armature-git` | The GitHub installation token; runs checkout and push |
| `armature-job` | Nothing; runs workflow processes |
| `armature-agent` | Nothing; runs `agent:` sessions (Claude Code, Codex) |

The base image also enables `armature-imds-block.service`, which rejects any
traffic from `armature-job`, `armature-git`, and `armature-agent` to the
instance metadata address. The runner blocks it too, so either one alone keeps
job code away from the instance role. `armature-runner` may run anything as
`armature-job`, only `/usr/bin/git` as `armature-git`, and only `/bin/kill`,
`/bin/rm`, and `/usr/bin/find` as `armature-agent`. `armature-job` may run only
`/usr/local/bin/claude`, `/usr/local/bin/codex`, and `/bin/kill` as
`armature-agent`. For `armature-agent`, sudo keeps only the session's
allowlisted variables and allocates no pty.
`armature-job` belongs to the `armature-agent` group so it can share session
directories, and `armature-agent` belongs to no other group. Start the runner
with `--job-user armature-job --agent-user armature-agent`.

The type's own installs come from its `installs` field on the environment
types editor, which has the same shape. Save that list as a file and build:

```sh
cd deploy/aws/images
packer init base.pkr.hcl
packer build \
  -var region="$AWS_REGION" -var subnet_id="${SUBNETS%%,*}" \
  -var security_group_id="$SG" -var instance_profile="$PROFILE" \
  -var install_id="$INSTALL" -var owner="$OWNER" -var council="$COUNCIL" -var finops_team="$FINOPS" \
  -var base_installs=/path/to/base-installs.json \
  -var type_name=<type name> -var type_installs=/path/to/<type>-installs.json \
  -var pip_constraints=/path/to/constraints.txt \
  base.pkr.hcl
```

`pip_constraints` is optional. Without it the armature wheel's own sha256 is
checked, but its dependencies install unpinned from PyPI. With it, pip runs in
`--require-hashes` mode, so the file needs a version and hash for every
dependency, for example from `pip-compile --generate-hashes`.

Packer reaches the build instance through SSM, so the build needs no SSH
ingress. Paste the AMI id it prints into the type's **Image** field.

### Job VMs on a Mac host can't use the host's role

A job runs in a Tart VM on the Mac host, and the host role can read every
pending enrollment file under `<install>/enroll/`. A VM never reaches those
credentials:

- The Mac launch template, and the server when it starts a host, set
  `HttpTokens: required` and `HttpPutResponseHopLimit: 1`. Tart VMs use NAT
  through the host, which takes one more hop, so the token response never
  reaches a VM, and without a token the metadata service answers nothing.
- The host copies a VM's enrollment file from S3 and deletes it before the
  VM boots. The VM sees only its own shared folder, which holds only its own
  file.
- The server never starts a VM with bridged networking.

Don't raise the hop limit on the Mac launch template, and don't run job VMs
with `--net-bridged`.

## 5. Build the iOS image

On a Mac host, as the user that owns the VMs and inside its GUI session:

1. Fetch the VM key pair from step 2 onto the host. The host role can read
   that secret:

   ```sh
   secret="$(aws secretsmanager get-secret-value --secret-id "$PREFIX-$INSTALL/mac-host/vm-key" \
     --query SecretString --output text)"
   install -d -m 700 ~/.ssh
   (umask 077; printf '%s' "$secret" | jq -r .private_key > ~/.ssh/armature_vm)
   printf '%s' "$secret" | jq -r .public_key > ~/.ssh/armature_vm.pub
   unset secret
   ```

2. Install Tart, then clone the type's `base_image` to a local VM that trusts
   `~/.ssh/armature_vm.pub`.
3. Build:

   ```sh
   XCODE_VERSION=<x.y> deploy/aws/images/ios-vm/build.sh [--constraints /path/to/constraints.txt] \
     <base vm> <vm name> ~/.ssh/armature_vm /path/to/base-installs.json /path/to/<type>-installs.json
   ```

   `--constraints` works as `pip_constraints` does for the Linux image.

   Use macOS builds in the installs lists. The build verifies Xcode, a
   simulator boot, and a smoke build, then scans for credentials and locks SSH
   to the key.
4. Push with your own credentials. The host role can only pull, so a job on
   the host can never replace an image. Get a registry password on a machine
   signed in as you, and pass it to Tart on the host:

   ```sh
   repo="$(out "$PREFIX-$INSTALL-mac" ImageRepositoryUri)"
   # On your machine:
   aws ecr get-login-password
   # On the host:
   TART_REGISTRY_HOSTNAME="${repo%%/*}" TART_REGISTRY_USERNAME=AWS TART_REGISTRY_PASSWORD=<password> \
     tart push <vm name> "$repo:<tag>"
   ```

5. Paste `<repository uri>:<tag>` into the type's **Image** field.

The repository keeps the last 10 images across every iOS type. If you keep
more types than that in use, raise `countNumber` in `mac-host.yaml`. Homebrew
packages in the VM (coreutils, jq, Python) stay unpinned in this version.

## 6. Fill in the AWS settings page

| Settings page field | Value |
|---------------------|-------|
| Region | `$AWS_REGION` |
| Install id | `$INSTALL` |
| Project tag | `TagProject`, default `armature-platform` |
| Credentials: role ARN | `HostManagerRoleArn` output of the host stack |
| Credentials: external id | `$EXTERNAL_ID` |
| Credentials: access key | The key from `create-access-key`, only with `CreateAccessUser=true` |
| VPC | `$VPC` |
| Subnets | Each subnet in `$SUBNETS` with its zone |
| Security group | `SecurityGroupId` output of the base stack |
| Instance profile | `InstanceProfileName` output of the base stack |
| Launch templates: Linux | `LaunchTemplateIds` output of the base stack |
| Launch templates: Mac | `MacLaunchTemplateId` output of the Mac stack |
| Mac instance profile | `MacInstanceProfileName` output of the Mac stack |
| Mac image repository | `ImageRepositoryUri` output of the Mac stack |
| Session role ARN | `SessionRoleArn` output of the host stack |
| Artifact bucket | `BucketName` output of the base stack |
| Allow host allocation | `AllowHostAllocation` output of the host stack |
| Extra tags | `owner`, `council`, and `bpmeta:finops:team` with the values you deployed |

Select **Verify**. The server checks that it can assume the role, describe the
subnets and security group, and read the launch templates. Environments stay
off until the connection verifies.

## Remove an install

Terminate running environments from the AWS page and release any Mac hosts.
Empty the image repository, or the Mac stack can't delete it. Then delete the
stacks in reverse order: host, Mac, base. The bucket is kept, so empty and
delete it by hand.

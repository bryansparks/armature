packer {
  required_plugins {
    amazon = {
      version = ">= 1.3.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "region" {
  type = string
}

variable "subnet_id" {
  type        = string
  description = "A private subnet the stack's security group can reach SSM from."
}

variable "security_group_id" {
  type        = string
  description = "SecurityGroupId output of stack.yaml."
}

variable "instance_profile" {
  type        = string
  description = "InstanceProfileName output of stack.yaml. Packer reaches the instance through SSM."
}

variable "prefix" {
  type    = string
  default = "armature-platform"
}

variable "install_id" {
  type = string
}

variable "tag_project" {
  type    = string
  default = "armature-platform"
  validation {
    condition     = var.tag_project != "armature"
    error_message = "The project tag can't be armature, which armature-ops owns."
  }
}

variable "owner" {
  type = string
}

variable "council" {
  type = string
}

variable "finops_team" {
  type = string
}

variable "type_name" {
  type        = string
  default     = "base"
  description = "The environment type this image is for. Names the AMI."
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*$", var.type_name))
    error_message = "The type name is lowercase letters, digits, and dashes."
  }
}

variable "base_installs" {
  type        = string
  description = "Path to a JSON file of {name, version, url, sha256} entries that installs Claude Code, Codex, and the armature runner. See base-installs.example.json."
}

variable "type_installs" {
  type        = string
  default     = ""
  description = "Path to a JSON file of the environment type's installs, in the shape of environment_types.installs."
}

variable "pip_constraints" {
  type        = string
  default     = ""
  description = "Path to a pip constraints file with a hash for every dependency of the armature wheel. When set, the wheel installs with --require-hashes."
}

variable "instance_type" {
  type    = string
  default = "m7i.large"
}

variable "volume_size" {
  type    = number
  default = 60
}

data "amazon-parameterstore" "ubuntu" {
  region = var.region
  name   = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

locals {
  type_installs   = var.type_installs != "" ? var.type_installs : "${path.root}/no-installs.json"
  pip_constraints = var.pip_constraints != "" ? var.pip_constraints : "${path.root}/no-constraints.txt"
  tags = {
    project              = var.tag_project
    armature-install     = var.install_id
    owner                = var.owner
    council              = var.council
    "bpmeta:finops:team" = var.finops_team
  }
  name = "${var.prefix}-${var.install_id}-${var.type_name}"
}

source "amazon-ebs" "base" {
  region                      = var.region
  source_ami                  = data.amazon-parameterstore.ubuntu.value
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  security_group_id           = var.security_group_id
  associate_public_ip_address = false
  iam_instance_profile        = var.instance_profile
  communicator                = "ssh"
  ssh_username                = "ubuntu"
  ssh_interface               = "session_manager"

  ami_name        = "${local.name}-{{timestamp}}"
  ami_description = "Armature environment ${var.type_name}: git, Claude Code, Codex, the armature runner, and the type's installs"

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_type           = "gp3"
    volume_size           = var.volume_size
    encrypted             = true
    delete_on_termination = true
  }

  tags            = merge(local.tags, { Name = local.name, "armature-type" = var.type_name })
  run_tags        = merge(local.tags, { Name = "${local.name}-bake" })
  run_volume_tags = merge(local.tags, { Name = "${local.name}-bake" })
  snapshot_tags   = local.tags
}

build {
  sources = ["source.amazon-ebs.base"]

  provisioner "file" {
    source      = "${path.root}/install.sh"
    destination = "/tmp/install.sh"
  }

  provisioner "file" {
    source      = var.base_installs
    destination = "/tmp/base-installs.json"
  }

  provisioner "file" {
    source      = local.type_installs
    destination = "/tmp/type-installs.json"
  }

  provisioner "file" {
    source      = local.pip_constraints
    destination = "/tmp/pip-constraints.txt"
  }

  provisioner "file" {
    source      = "${path.root}/armature-imds-block.service"
    destination = "/tmp/armature-imds-block.service"
  }

  provisioner "shell" {
    execute_command = "sudo -E bash '{{ .Path }}'"
    inline = [
      "set -euo pipefail",
      "export DEBIAN_FRONTEND=noninteractive",
      "cloud-init status --wait >/dev/null || true",
      # Nothing on a worker may contend with the job for the disk at boot.
      "systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service",
      "apt-get update -q",
      "apt-get install -y -q --no-install-recommends git jq curl unzip xz-utils ca-certificates iptables procps python3 python3-venv",
      # The runner holds the runner key, git holds the installation token, job runs workflow code, and agent runs
      # agent sessions. Job shares session directories with agent through agent's group. Agent is in no other group.
      "for u in armature-runner armature-git armature-job armature-agent; do useradd --system --create-home --shell /usr/sbin/nologin \"$u\"; chmod 700 \"/home/$u\"; done",
      "usermod -aG armature-agent armature-job",
      "install -d -m 0750 -o root -g armature-runner /etc/armature",
      "printf 'armature-runner ALL=(armature-job) NOPASSWD: ALL\\narmature-runner ALL=(armature-git) NOPASSWD: /usr/bin/git\\narmature-runner ALL=(armature-agent) NOPASSWD: /bin/kill, /bin/rm, /usr/bin/find\\n' > /etc/sudoers.d/armature-runner",
      "chmod 0440 /etc/sudoers.d/armature-runner",
      "visudo -cf /etc/sudoers.d/armature-runner",
      # Job may start only the agent CLIs as agent, and kill what they leave. Only the session's allowlisted
      # variables survive sudo's env_reset. No pty for job, so the engine key on stdin passes straight through.
      "printf 'Defaults>armature-job !use_pty\\nDefaults>armature-agent !use_pty\\nDefaults>armature-agent env_keep = \\\"HOME TMPDIR CLAUDE_CONFIG_DIR CODEX_HOME PATH TERM LANG ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_USE_BEDROCK AWS_REGION AWS_REGION_NAME AWS_BEARER_TOKEN_BEDROCK OPENAI_API_KEY BASH_DEFAULT_TIMEOUT_MS BASH_MAX_TIMEOUT_MS\\\"\\narmature-job ALL=(armature-agent) NOPASSWD: /usr/local/bin/claude, /usr/local/bin/codex, /bin/kill\\n' > /etc/sudoers.d/armature-job",
      "chmod 0440 /etc/sudoers.d/armature-job",
      "visudo -cf /etc/sudoers.d/armature-job",
      # The runner blocks metadata for job processes too. This rule holds even if it doesn't.
      "install -m 0644 /tmp/armature-imds-block.service /etc/systemd/system/armature-imds-block.service",
      "systemctl enable --now armature-imds-block.service",
      "for u in armature-job armature-git armature-agent; do iptables -w -C OUTPUT -d 169.254.169.254 -m owner --uid-owner \"$u\" -j REJECT; done",
      "constraints=(); if [ -s /tmp/pip-constraints.txt ]; then constraints=(--constraints /tmp/pip-constraints.txt); fi",
      "bash /tmp/install.sh \"$${constraints[@]}\" /tmp/base-installs.json",
      "bash /tmp/install.sh \"$${constraints[@]}\" /tmp/type-installs.json",
      "for t in git claude codex armature; do command -v \"$t\" >/dev/null || { echo \"$t is not installed\" >&2; exit 1; }; done",
      "rm -f /tmp/install.sh /tmp/base-installs.json /tmp/type-installs.json /tmp/pip-constraints.txt /tmp/armature-imds-block.service /root/.bash_history /home/*/.bash_history",
    ]
  }
}

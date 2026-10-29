"""``armature env build <type>``: bake an environment type's image with the templates in ``deploy/aws/images``.

An ec2-linux type builds an AMI with Packer, an ec2-mac type a Tart VM with
``ios-vm/build.sh``. Either way the type's ``installs`` reach the build as a
JSON file, so the image holds exactly what the type declares.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "aws"
PACKER_TEMPLATE = Path("images") / "base.pkr.hcl"
MAC_BUILD = Path("images") / "ios-vm" / "build.sh"
TAG_VARS = {"owner": "owner", "council": "council", "bpmeta:finops:team": "finops_team"}


class BuildError(Exception):
    pass


def write_installs(kind: Any, directory: Path) -> Path:
    path = directory / f"{kind.name}-installs.json"
    path.write_text(json.dumps(kind.installs or [], indent=2) + "\n", encoding="utf-8")
    return path


def packer_vars(kind: Any, connection: Any | None, installs: Path, base_installs: Path, prefix: str,
                constraints: Path | None) -> list[str]:
    """``-var`` arguments for base.pkr.hcl. Values the connection lacks are left for a ``-var-file`` to supply."""
    values: dict[str, Any] = {"type_name": kind.name, "type_installs": str(installs),
                              "base_installs": str(base_installs), "prefix": prefix}
    if constraints is not None:
        values["pip_constraints"] = str(constraints)
    if connection is not None:
        subnet = (connection.subnets or [{}])[0].get("subnet_id")
        values.update({"region": connection.region, "install_id": connection.install_id,
                       "tag_project": connection.tag_project, "subnet_id": subnet,
                       "security_group_id": connection.security_group_id,
                       "instance_profile": connection.instance_profile})
        for tag, name in TAG_VARS.items():
            values[name] = (connection.extra_tags or {}).get(tag)
    if (kind.instance_types or []):
        values["instance_type"] = kind.instance_types[0]
    if kind.disk_gb:
        values["volume_size"] = kind.disk_gb
    args = []
    for name, value in values.items():
        if value is not None and value != "":
            args += ["-var", f"{name}={value}"]
    return args


def command(kind: Any, connection: Any | None, *, installs: Path, base_installs: Path, deploy_dir: Path = DEPLOY_DIR,
            prefix: str = "armature-platform", var_files: list[Path] | None = None, constraints: Path | None = None,
            key_file: Path | None = None, vm_name: str | None = None) -> list[str]:
    if kind.kind == "ec2-mac":
        script = deploy_dir / MAC_BUILD
        if not script.is_file():
            raise BuildError(f"{script} doesn't exist. Run from a checkout with deploy/aws, or pass --deploy-dir.")
        if not kind.base_image:
            raise BuildError(f"Type {kind.name} has no base_image to clone the VM from.")
        if key_file is None:
            raise BuildError("An ec2-mac build needs --key-file, the VM key from the Mac host stack's secret.")
        head = [str(script), *(["--constraints", str(constraints)] if constraints else [])]
        return [*head, kind.base_image, vm_name or kind.name, str(key_file), str(base_installs), str(installs)]
    template = deploy_dir / PACKER_TEMPLATE
    if not template.is_file():
        raise BuildError(f"{template} doesn't exist. Run from a checkout with deploy/aws, or pass --deploy-dir.")
    files = [arg for path in var_files or [] for arg in ("-var-file", str(path))]
    return ["packer", "build", *files,
            *packer_vars(kind, connection, installs, base_installs, prefix, constraints), str(template)]

"""The one way into the AWS provider, so a server without the ``cloud`` extra never imports boto3."""
from __future__ import annotations

import importlib
import json
import logging
from typing import Any

from armature.server.crypto import Keyring, SealError
from armature.transport.provision import ProvisionError

log = logging.getLogger(__name__)

NOT_CONFIGURED = "AWS not configured"
NO_EXTRA = "The server lacks the cloud extra. Install armature-agents[cloud]."
MODULE = "armature.transport.ec2"


def sealed_aad(connection_id: str) -> dict[str, str]:
    return {"aws_connection": connection_id}


def credentials(keyring: Keyring, row: Any) -> dict[str, str]:
    if not row.sealed:
        return {}
    return json.loads(keyring.open_(row.sealed, sealed_aad(row.id), label="aws"))


def build(keyring: Keyring, row: Any) -> Any:
    """A provisioner for a connection row, verified or not. Raises ImportError without the extra."""
    ec2 = importlib.import_module(MODULE)
    return ec2.Ec2Provisioner(region=row.region, project=row.tag_project, install_id=row.install_id,
                              credentials=credentials(keyring, row),
                              subnets=[(s.get("zone", ""), s["subnet_id"]) for s in row.subnets or []],
                              security_group_id=row.security_group_id, artifact_bucket=row.artifact_bucket,
                              launch_template_ids=row.launch_template_ids or {},
                              allowed_instance_types=row.allowed_instance_types or [], stack_name=row.stack_name)


def load(keyring: Keyring, row: Any | None) -> Any | None:
    """The provisioner for a verified connection. None when there is none, it isn't verified, or the extra is missing."""
    if row is None or row.state != "ok":
        return None
    try:
        return build(keyring, row)
    except ImportError:
        log.warning("AWS connection %s is set, but %s", row.name, NO_EXTRA)
        return None
    except (SealError, ProvisionError, ValueError, KeyError) as exc:
        log.warning("AWS connection %s can't be opened: %s", row.name, exc.__class__.__name__)
        return None

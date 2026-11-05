"""Where a run executed: an AWS Linux instance, an AWS Mac, a remote runner's host, or a local machine.

A location is a small object: ``kind``, a display ``label``, and optional
``instance``, ``environment_type``, ``instance_type``, ``chip``, and ``machine``. A local runner started with
``--same-user`` adds ``same_user: true``, since its agent sessions run unisolated, and so may a remote runner.
A remote location's label is the runner's own, such as the name of the service its jobs run on.
It never names a local host, so a laptop's hostname never reaches the page. Both the sender and the server use these checks.
"""
from __future__ import annotations

import functools
import os
import platform
import re
import subprocess
from typing import Any

AWS_LINUX = "aws-linux"
AWS_MAC = "aws-mac"
REMOTE = "remote"
LOCAL = "local"
UNKNOWN = "unknown"
KINDS = (AWS_LINUX, AWS_MAC, REMOTE, LOCAL, UNKNOWN)
LABELS = {AWS_LINUX: "AWS Linux", AWS_MAC: "AWS macOS", REMOTE: "Remote", UNKNOWN: "Unknown"}
SAME_USER_KINDS = (LOCAL, REMOTE)
# Set by a remote runner's launcher inside each run, so the run says where it executed.
LOCATION_KIND_ENV = "ARMATURE_LOCATION_KIND"
LOCATION_LABEL_ENV = "ARMATURE_LOCATION_LABEL"
FIELDS = ("instance", "environment_type", "instance_type", "chip", "machine")
FIELD_MAX = 191
# A derived location replaces a known one when it ranks higher or adds detail, so a local start never hides AWS.
RANK = {UNKNOWN: 0, LOCAL: 1, AWS_LINUX: 2, AWS_MAC: 2, REMOTE: 2}
# ops placement.where values, from development_implement.sh and pr_validate.sh.
PLACEMENTS = {"aws-session": AWS_LINUX, "aws-spot": AWS_LINUX, "aws-linux": AWS_LINUX, "aws-mac": AWS_MAC,
              "m1": LOCAL}
# pr_validate.sh's log line as a build starts on a worker, long before its step ends.
BUILDING_ON = re.compile(r"^building \S+ on (aws-[a-z]+)\b")


def _line(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(ch for ch in value.strip() if ch.isprintable())[:FIELD_MAX] or None


def local_label(chip: str | None, machine: str | None, same_user: bool = False) -> str:
    name = chip.removeprefix("Apple ").strip() if chip and chip.startswith("Apple ") else (chip or machine)
    where = "local, same user" if same_user else "local"
    return f"{name} ({where})" if name else where.capitalize()


def make(kind: str, label: Any = None, **fields: Any) -> dict[str, Any]:
    """A location of ``kind``. Only a remote one keeps the ``label`` it's given."""
    kept = {k: v for k in FIELDS if (v := _line(fields.get(k)))}
    same_user = kind in SAME_USER_KINDS and fields.get("same_user") is True
    if kind == LOCAL:
        label = local_label(kept.get("chip"), kept.get("machine"), same_user)
    elif kind == REMOTE:
        label = _line(label) or LABELS[REMOTE]
    else:
        label = LABELS[kind]
    return {"kind": kind, "label": label, **kept, **({"same_user": True} if same_user else {})}


def parse(value: Any, trusted: bool = False) -> dict[str, Any] | None:
    """A location from a sender, rebuilt from its known fields, or None when it isn't one.

    Only a ``trusted`` sender, the runner at hello or its stored record, keeps ``same_user``.
    """
    if not isinstance(value, dict) or value.get("kind") not in KINDS:
        return None
    return make(value["kind"], value.get("label"), **{k: value.get(k) for k in FIELDS},
                same_user=trusted and value.get("same_user") is True)


def from_placement(value: Any) -> dict[str, Any] | None:
    """The location an ops step's ``placement`` names, or None when it names none or never launched."""
    if not isinstance(value, dict) or value.get("launched") is False:
        return None
    kind = PLACEMENTS.get(value.get("where"))
    if kind is None:
        return None
    if kind == LOCAL:
        return make(LOCAL, chip="Apple M1")
    # A Mac session names its dedicated host's instance as host.
    return make(kind, instance=value.get("instance") or value.get("host"), instance_type=value.get("instance_type"))


def from_output(output: Any) -> dict[str, Any] | None:
    """The best placement in a step's output, or in each branch of a fan-out's."""
    found = None
    for item in output if isinstance(output, list) else [output]:
        placed = from_placement(item.get("placement")) if isinstance(item, dict) else None
        if better(found, placed):
            found = placed
    return found


def from_event(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The location an ops event reveals: a step output's placement, or a worker build's log line."""
    if kind in ("step_end", "step_failed"):
        return from_output(payload.get("output"))
    if kind == "log" and isinstance(payload.get("message"), str):
        match = BUILDING_ON.match(payload["message"])
        return from_placement({"where": match.group(1)}) if match else None
    return None


def better(current: Any, found: dict[str, Any] | None) -> bool:
    if found is None:
        return False
    if not isinstance(current, dict):
        return True
    if found["kind"] == current.get("kind"):
        return len(found) > len(current)
    return RANK[found["kind"]] > RANK.get(current.get("kind"), -1)


@functools.cache
def _chip() -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                             timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def this_machine(same_user: bool = False) -> dict[str, Any]:
    """This machine as a local location, or as the remote one its launcher names."""
    if os.environ.get(LOCATION_KIND_ENV) == REMOTE:
        return make(REMOTE, os.environ.get(LOCATION_LABEL_ENV), machine=platform.machine(), same_user=same_user)
    return make(LOCAL, chip=_chip(), machine=platform.machine(), same_user=same_user)

"""Work-unit naming: deterministic sweep job ids + hostile-id validation.

Moved from armature-dispatch dispatch/job.py @ c5fe34f (slice 4). Mission
and unit ids reach S3 keys (work/<mission>/<unit>.json), job id prefixes,
and task env — validated at EVERY entry point so a hostile id can never
traverse or inject separators."""
from __future__ import annotations

import re


def sweep_job_id(mission: str, unit_id: str, attempt: int) -> str:
    """Deterministic job id for a sweep-launched attempt (never-2PC, design
    §6): the same mission+unit at the same attempt always mints the same id,
    so an overlapping sweep re-drives the same job instead of
    double-submitting. The mission is part of the durable address — unit ids
    are unique only within a mission, and the job id names the S3 keys
    (jobs/<id>/…) whose receipt repair_job settles records from.

    The '~' separators sit OUTSIDE the id grammar (validate_work_names
    allows [A-Za-z0-9_.-]), so the (mission, unit, attempt) triple is
    unambiguous: '-'-delimited ids collided across hyphen splits of
    different missions/units, minting one jobs/ prefix for two units."""
    return f"sweep-{mission}~{unit_id}~{attempt}"


# \Z, not $: Python's `$` also matches just before a trailing newline, so
# `research\n` would pass and reach S3 keys, job ids, and task env
_WORK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def validate_work_names(mission: str, unit_id: str) -> None:
    """Mission and unit ids reach S3 keys (work/<mission>/<unit>.json) and
    job id prefixes — validate at EVERY entry point (submit, sweep, runner
    env) so a hostile id can never traverse or inject separators."""
    for label, value in (("mission", mission), ("unit", unit_id)):
        if not _WORK_NAME_RE.match(value):
            raise ValueError(
                f"invalid {label} id {value!r}: must match "
                f"{_WORK_NAME_RE.pattern}")
"""S3-backed WorkStore — the engine's S3 implementation of the design §5 seam.

Armature stays boto-free: the WorkStore protocol is the seam and this module
is dispatch's implementation of it (design §5, §6). Records are
byte-identical WorkUnitRecord JSON — the same shape LocalWorkStore writes —
so both stores read the same mission state.

Layout (design §6, with one documented deviation):
    work/<mission>/<unit_id>.json                    WorkUnitRecord
    work/<mission>/mission.yml                       the mission document
    work/<mission>/transitions/<seq>-<unit>.json     TransitionRecord

DEVIATION (ruled in planning): §6 sketched a flat `work/transitions/`
prefix; mission-scoped transitions are required so a mission's seq counts
only that mission's moves and a cross-mission unit-id collision cannot
corrupt the audit sequence. Record content is unchanged.

S3 has no read-modify-write: concurrent writers to one record are
last-writer-wins (never-2PC, design §6). Legality still runs on every
apply through armature's TRANSITIONS table — an illegal move raises
before anything is written.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import ValidationError
from ruamel.yaml import YAML, YAMLError

from armature.spec.mission import MissionSpec, WorkUnit, resolve_posture
from armature.state.work import (PENDING, TransitionRecord, WorkUnitRecord,
                                 WorkUnitState, ensure_legal)

_UNIT_RE = re.compile(r"^[^/]+\.json\Z")   # \Z: `$` would admit a trailing newline
_YAML = YAML(typ="safe")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class S3WorkStore:
    """WorkStore-protocol implementation against one S3 bucket."""

    def __init__(self, s3, bucket: str, prefix: str = "work") -> None:
        self.s3 = s3
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")

    # -- keys ---------------------------------------------------------------

    def unit_key(self, mission: str, unit_id: str) -> str:
        return f"{self.prefix}/{mission}/{unit_id}.json"

    def doc_key(self, mission: str) -> str:
        return f"{self.prefix}/{mission}/mission.yml"

    def _transitions_prefix(self, mission: str) -> str:
        return f"{self.prefix}/{mission}/transitions/"

    # -- low-level IO --------------------------------------------------------

    def _get_text(self, key: str) -> str | None:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except self.s3.exceptions.NoSuchKey:
            return None
        return obj["Body"].read().decode("utf-8")

    def _put(self, key: str, body: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body.encode("utf-8"))

    def _list_keys(self, prefix: str) -> list[str]:
        paginator = self.s3.get_paginator("list_objects_v2")
        out: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            out.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(out)

    # -- WorkStore -----------------------------------------------------------

    def load(self, mission: str, unit_id: str) -> WorkUnitRecord | None:
        text = self._get_text(self.unit_key(mission, unit_id))
        if text is None:
            return None
        return WorkUnitRecord.model_validate_json(text)

    def save(self, record: WorkUnitRecord) -> None:
        self._put(self.unit_key(record.mission, record.unit_id),
                  record.model_dump_json(indent=2))

    def list_units(self, mission: str) -> list[WorkUnitRecord]:
        prefix = f"{self.prefix}/{mission}/"
        out: list[WorkUnitRecord] = []
        for key in self._list_keys(prefix):
            rel = key[len(prefix):]
            if not _UNIT_RE.match(rel):   # skips transitions/… and mission.yml
                continue
            out.append(WorkUnitRecord.model_validate_json(self._get_text(key)))
        return out

    def list_transitions(self, mission: str) -> list[TransitionRecord]:
        out: list[TransitionRecord] = []
        for key in self._list_keys(self._transitions_prefix(mission)):
            text = self._get_text(key)
            if text is not None:
                out.append(TransitionRecord.model_validate_json(text))
        out.sort(key=lambda t: t.seq)
        return out

    def ensure_unit(self, mission: MissionSpec, unit: WorkUnit) -> WorkUnitRecord:
        existing = self.load(mission.name, unit.id)
        if existing is not None:
            return existing
        record = WorkUnitRecord(
            mission=mission.name, unit_id=unit.id, title=unit.title,
            objective=unit.objective, workflow=unit.workflow,
            workflow_path=unit.workflow_path, inputs=dict(unit.inputs),
            requires=list(unit.requires),
            posture=resolve_posture(mission, unit),
            state=PENDING, max_attempts=unit.max_attempts,
            max_budget_usd=unit.max_budget_usd,
            timeout_hours=unit.timeout_hours,
        )
        self.save(record)
        self._append_transition(TransitionRecord(
            seq=self._next_seq(mission.name), ts=_now_iso(),
            mission=mission.name, unit_id=unit.id, from_state=None,
            to_state=PENDING, reason="seeded from mission document"))
        return record

    def apply(self, mission: str, unit_id: str, to_state: WorkUnitState, *,
              reason: str = "", job_id: str | None = None,
              actor: str = "system") -> WorkUnitRecord:
        record = self.load(mission, unit_id)
        if record is None:
            raise KeyError(
                f"work unit '{mission}/{unit_id}' has no record; "
                "create one via ensure_unit before applying a transition")
        frm = record.state
        ensure_legal(frm, to_state)
        record.state = to_state
        record.updated_at = _now_iso()
        if job_id is not None:
            record.last_job_id = job_id
        self.save(record)
        self._append_transition(TransitionRecord(
            seq=self._next_seq(mission), ts=_now_iso(), mission=mission,
            unit_id=unit_id, from_state=frm, to_state=to_state, reason=reason,
            job_id=job_id, actor=actor))
        return record

    def seed_follow_on(self, mission: str, unit, *,
                       posture: str = "human-led") -> WorkUnitRecord:
        existing = self.load(mission, unit.id)
        if existing is not None:
            return existing
        record = WorkUnitRecord(
            mission=mission, unit_id=unit.id, title=unit.title,
            objective=unit.objective, workflow=unit.workflow,
            inputs=dict(unit.inputs), posture=posture, state=PENDING)
        self.save(record)
        self._append_transition(TransitionRecord(
            seq=self._next_seq(mission), ts=_now_iso(), mission=mission,
            unit_id=unit.id, from_state=None, to_state=PENDING,
            reason="seeded from a run closure"))
        return record

    # -- mission document ------------------------------------------------------

    def put_mission_doc(self, mission: str, text: str) -> None:
        self._put(self.doc_key(mission), text)

    def get_mission_doc(self, mission: str) -> MissionSpec | None:
        """None when absent. ValueError when present but malformed — loud, so
        the sweep can name the mission and continue its other work."""
        text = self._get_text(self.doc_key(mission))
        if text is None:
            return None
        try:
            data = _YAML.load(text)
        except YAMLError as exc:
            raise ValueError(f"mission document is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("mission document must be a YAML mapping")
        try:
            return MissionSpec.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"invalid mission document: {exc}") from exc

    # -- internals ---------------------------------------------------------------

    def _append_transition(self, tr: TransitionRecord) -> None:
        key = (f"{self._transitions_prefix(tr.mission)}"
               f"{tr.seq:06d}-{tr.unit_id}.json")
        self._put(key, tr.model_dump_json())

    def _next_seq(self, mission: str) -> int:
        return len(self.list_transitions(mission)) + 1
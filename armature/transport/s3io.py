"""S3 helpers + the jobs/ results-layout readers.

Moved from armature-dispatch dispatch/s3ops.py + dispatch/ops.py
(latest_receipt) @ c5fe34f. The results layout is armature's own
ResultsWriter output mirrored verbatim (jobs/<id>/results/<run>/receipt.json)
— the engine owns the contract, so the readers live here. zip_dir/upload_dir
(package UPLOAD mechanics) stay in the deployment repo."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any


def upload_bytes(s3, bucket: str, key: str, data: bytes) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=data)


def download_and_extract(s3, bucket: str, key: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    obj = s3.get_object(Bucket=bucket, Key=key)
    with zipfile.ZipFile(io.BytesIO(obj["Body"].read())) as zf:
        for name in zf.namelist():
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"unsafe zip member: {name}")
        zf.extractall(dest)
    return dest


def put_json(s3, bucket: str, key: str, obj: Any) -> None:
    upload_bytes(s3, bucket, key,
                 json.dumps(obj, indent=2, sort_keys=True).encode("utf-8"))


def get_json(s3, bucket: str, key: str) -> Any:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read())


def list_run_ids(s3, bucket: str, job_prefix: str) -> list[str]:
    """Run ids present under jobs/<id>/results/ — `_pending` never counts as a
    run, and neither does a bare file at the results root (e.g. a digest a
    workflow writes to results/ directly): a run id must own a directory of
    result objects, i.e. appear as the parent of at least one nested key."""
    run_ids: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{job_prefix}/results/"):
        for obj in page.get("Contents", []):
            rest = obj["Key"].split(f"{job_prefix}/results/", 1)[1]
            if not rest or rest.startswith("_pending") or "/" not in rest:
                continue
            run_ids.add(rest.split("/", 1)[0])
    return sorted(run_ids)


def _latest_receipt(s3, bucket: str, job_id: str, run_ids: list[str]):
    for run_id in reversed(run_ids):
        try:
            return get_json(
                s3, bucket, f"jobs/{job_id}/results/{run_id}/receipt.json"), run_id
        except s3.exceptions.NoSuchKey:
            continue
    return None, None


def latest_receipt(s3, bucket: str, job_id: str):
    """The newest receipt for a job and its run id — or (None, None).
    The sweep's repair pass reads this (design §6)."""
    return _latest_receipt(s3, bucket, job_id,
                           list_run_ids(s3, bucket, f"jobs/{job_id}"))
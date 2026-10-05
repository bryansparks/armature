"""s3io — S3 helpers + the jobs/ results-layout readers.

Moved from armature-dispatch tests/unit/test_s3ops.py @ c5fe34f (the
zip_dir/upload_dir package-UPLOAD helpers stay in the deployment repo;
their tests stayed with them)."""
import io
import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("boto3")   # transport suite: requires the cloud extra

from armature.transport import s3io  # noqa: E402

from .conftest import BUCKET  # noqa: E402


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in sorted(files.items()):
            zf.writestr(name, text)
    return buf.getvalue()


def test_download_extract_roundtrip(s3_bucket, tmp_path):
    s3io.upload_bytes(s3_bucket[0], BUCKET, "jobs/j1/package.zip", _zip_bytes({
        "package.yaml": "name: smoke\n", "tools/util.py": "x = 1\n"}))
    dest = tmp_path / "extract"
    out = s3io.download_and_extract(s3_bucket[0], BUCKET, "jobs/j1/package.zip", dest)
    assert out == dest
    assert (dest / "package.yaml").read_text() == "name: smoke\n"
    assert (dest / "tools" / "util.py").read_text() == "x = 1\n"


def test_download_rejects_unsafe_members(s3_bucket, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.txt", "pwn")
    s3io.upload_bytes(s3_bucket[0], BUCKET, "jobs/j2/package.zip", buf.getvalue())
    with pytest.raises(ValueError, match="unsafe zip member"):
        s3io.download_and_extract(s3_bucket[0], BUCKET, "jobs/j2/package.zip",
                                  tmp_path / "out")


def test_json_roundtrip(s3_bucket):
    s3, _ = s3_bucket
    s3io.put_json(s3, BUCKET, "jobs/j3/task.json", {"job_id": "j3", "n": 1})
    assert s3io.get_json(s3, BUCKET, "jobs/j3/task.json") == {"job_id": "j3", "n": 1}


def test_list_run_ids_excludes_pending(s3_bucket):
    s3, _ = s3_bucket
    for key in ("jobs/j5/results/run1/receipt.json",
                "jobs/j5/results/_pending/session/checkpoint.json",
                "jobs/j5/results/run2/receipt.json"):
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}")
    assert s3io.list_run_ids(s3, BUCKET, "jobs/j5") == ["run1", "run2"]


def test_list_run_ids_excludes_bare_results_root_files(s3_bucket):
    # a workflow writing its digest straight to results/ must not register
    # as a run id — a run id must own a directory of result objects
    s3, _ = s3_bucket
    for key in ("jobs/j6/results/run1/receipt.json",
                "jobs/j6/results/digest.md",
                "jobs/j6/results/run1/artifacts/digest.md"):
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}")
    assert s3io.list_run_ids(s3, BUCKET, "jobs/j6") == ["run1"]


def test_latest_receipt_orders_by_finished_at_not_run_id(s3_bucket):
    # Review Important 2: run ids are random hex and the runner's
    # clean-failure path writes the literal run id "failed", which sorts
    # ABOVE every hex id — lexicographic order would hand the repair pass
    # the stale failed receipt after an operator re-drives the job and the
    # second run completes. The newest receipt by finished_at must win.
    from armature.transport.s3io import latest_receipt, put_json
    s3, bucket = s3_bucket
    put_json(s3, bucket, "jobs/j1/results/failed/receipt.json",
             {"status": "failed", "cost_usd": 0.01,
              "finished_at": "2026-10-05T10:00:00+00:00"})
    put_json(s3, bucket, "jobs/j1/results/f00dcafe0123/receipt.json",
             {"status": "complete", "cost_usd": 0.05,
              "finished_at": "2026-10-05T11:00:00+00:00"})
    receipt, run_id = latest_receipt(s3, bucket, "j1")
    assert run_id == "f00dcafe0123" and receipt["status"] == "complete"


def test_latest_receipt_picks_newest_run(s3_bucket):
    from armature.transport.s3io import latest_receipt, put_json
    s3, bucket = s3_bucket
    put_json(s3, bucket, "jobs/j1/results/run-1/receipt.json", {"status": "failed"})
    put_json(s3, bucket, "jobs/j1/results/run-2/receipt.json", {"status": "complete"})
    receipt, run_id = latest_receipt(s3, bucket, "j1")
    assert run_id == "run-2" and receipt["status"] == "complete"
    assert latest_receipt(s3, bucket, "no-such-job") == (None, None)
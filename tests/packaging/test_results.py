# tests/packaging/test_results.py
import json
from pathlib import Path
from armature.packaging.manifest import Destinations, ArtifactSpec, TraceRef
from armature.packaging.results import ResultsWriter
from armature.state.traces import TraceRecord

def test_results_layout_and_receipt(tmp_path: Path):
    w = ResultsWriter(tmp_path / "results")
    dest = Destinations(artifacts=[ArtifactSpec(stage_id="writer", name="brief", format="markdown")],
                        include_trace=True)
    result = {"writer": {"content": "# Hello"}}
    rec = TraceRecord(run_id="r1", workflow_name="demo", stage_id="writer",
                      role_type="worker", model="x")
    run_dir = w.write(run_id="r1", package_name="demo", package_version="1.0",
                      destinations=dest, result=result, trace_records=[rec],
                      status="complete", started_at="t0", finished_at="t1", duration_s=1.0,
                      exit_code=0, armature_version="0.6.0")
    assert (run_dir / "receipt.json").exists()
    assert (run_dir / "result.json").exists()
    assert (run_dir / "artifacts" / "brief.md").read_text() == "# Hello"
    assert (run_dir / "trace.jsonl").exists()
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["status"] == "complete"
    assert receipt["artifacts"][0]["path"] == "artifacts/brief.md"
    assert receipt["trace"]["included"] is True

def test_trace_omitted_when_disabled(tmp_path: Path):
    w = ResultsWriter(tmp_path / "results")
    dest = Destinations(artifacts=[], include_trace=False)
    run_dir = w.write(run_id="r2", package_name="demo", package_version="1.0",
                      destinations=dest, result={}, trace_records=[],
                      status="complete", started_at="t0", finished_at="t1", duration_s=1.0,
                      exit_code=0, armature_version="0.6.0")
    assert not (run_dir / "trace.jsonl").exists()
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["trace"]["included"] is False

def test_json_artifact(tmp_path: Path):
    w = ResultsWriter(tmp_path / "results")
    dest = Destinations(artifacts=[ArtifactSpec(stage_id="judge", name="assess", format="json")])
    result = {"judge": {"accept": True, "score": 0.9}}
    run_dir = w.write(run_id="r3", package_name="demo", package_version="1.0",
                      destinations=dest, result=result, trace_records=[],
                      status="complete", started_at="t0", finished_at="t1", duration_s=1.0,
                      exit_code=0, armature_version="0.6.0")
    data = json.loads((run_dir / "artifacts" / "assess.json").read_text())
    assert data["accept"] is True

# ── opt-in file capture (dispatch: workflows that write real files to disk) ────

def _write_result(**kw):
    """Standard ResultsWriter.write call with sensible defaults."""
    w = kw.pop("writer")
    return w.write(
        run_id=kw.pop("run_id", "r9"), package_name="demo", package_version="1.0",
        destinations=kw.pop("destinations"), result=kw.pop("result", {}),
        trace_records=[], status=kw.pop("status", "complete"),
        started_at="t0", finished_at="t1", duration_s=1.0,
        exit_code=kw.pop("exit_code", 0), armature_version="0.6.0",
        workdir=kw.pop("workdir"), capture=kw.pop("capture", True),
    )


def test_file_capture_single_file(tmp_path: Path):
    import hashlib
    workdir = tmp_path / "job"
    (workdir / "research-output").mkdir(parents=True)
    src = workdir / "research-output" / "report.html"
    src.write_text("<h1>real report</h1>", encoding="utf-8")

    w = ResultsWriter(tmp_path / "results")
    dest = Destinations(artifacts=[
        ArtifactSpec(stage_id="writer", name="report", format="text",
                     source="research-output/report.html")])
    run_dir = _write_result(writer=w, destinations=dest, workdir=workdir)

    captured = run_dir / "artifacts" / "report.html"  # basename preserved
    assert captured.read_text(encoding="utf-8") == "<h1>real report</h1>"
    receipt = json.loads((run_dir / "receipt.json").read_text())
    entry = receipt["artifacts"][0]
    assert entry["path"] == "artifacts/report.html"
    # receipt digests the captured bytes so downstream integrity checks hold
    assert entry["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()


def test_file_capture_glob_multiple_files(tmp_path: Path):
    workdir = tmp_path / "job"
    (workdir / "research-output").mkdir(parents=True)
    (workdir / "research-output" / "a.html").write_text("A", encoding="utf-8")
    (workdir / "research-output" / "b.html").write_text("B", encoding="utf-8")

    w = ResultsWriter(tmp_path / "results")
    dest = Destinations(artifacts=[
        ArtifactSpec(stage_id="writer", name="reports", format="text",
                     source="research-output/*.html")])
    run_dir = _write_result(writer=w, destinations=dest, workdir=workdir)

    assert (run_dir / "artifacts" / "a.html").read_text() == "A"
    assert (run_dir / "artifacts" / "b.html").read_text() == "B"
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert {e["path"] for e in receipt["artifacts"]} == {"artifacts/a.html", "artifacts/b.html"}


def test_file_capture_missing_file_fails_closed(tmp_path: Path):
    """A declared source that matches nothing is a run failure, not a silent
    empty artifact — same fail-closed posture as the completeness checks."""
    import pytest
    workdir = tmp_path / "job"
    workdir.mkdir()
    w = ResultsWriter(tmp_path / "results")
    dest = Destinations(artifacts=[
        ArtifactSpec(stage_id="writer", name="report", format="text",
                     source="research-output/report.html")])
    with pytest.raises(FileNotFoundError, match="matched nothing"):
        _write_result(writer=w, destinations=dest, workdir=workdir)


def test_file_capture_skipped_for_failed_run(tmp_path: Path):
    """The failure path must not re-fire capture errors: a failed run's
    receipt is written with capture off."""
    workdir = tmp_path / "job"
    workdir.mkdir()  # no files
    w = ResultsWriter(tmp_path / "results")
    dest = Destinations(artifacts=[
        ArtifactSpec(stage_id="writer", name="report", format="text",
                     source="research-output/report.html")])
    run_dir = _write_result(writer=w, destinations=dest, workdir=workdir,
                            status="failed", exit_code=1, capture=False)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert not (run_dir / "artifacts" / "report.html").exists()


def test_file_capture_duplicate_basename_fails(tmp_path: Path):
    """Two glob matches sharing a basename would clobber each other in
    artifacts/ — refuse rather than silently lose a file."""
    import pytest
    workdir = tmp_path / "job"
    (workdir / "out1").mkdir(parents=True)
    (workdir / "out2").mkdir(parents=True)
    (workdir / "out1" / "report.html").write_text("one", encoding="utf-8")
    (workdir / "out2" / "report.html").write_text("two", encoding="utf-8")
    w = ResultsWriter(tmp_path / "results")
    dest = Destinations(artifacts=[
        ArtifactSpec(stage_id="writer", name="report", format="text",
                     source="out*/report.html")])
    with pytest.raises(ValueError, match="collision"):
        _write_result(writer=w, destinations=dest, workdir=workdir)

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
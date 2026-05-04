import pytest
from pathlib import Path
from armature.spec.loader import load_spec

FIXTURES = Path(__file__).parent.parent / "fixtures"

def test_load_minimal_spec():
    spec = load_spec(FIXTURES / "minimal.yaml")
    assert spec.name == "minimal-workflow"
    assert len(spec.stages) == 1
    assert spec.stages[0].id == "step1"

def test_load_with_template_vars():
    spec = load_spec(FIXTURES / "minimal.yaml", vars={"run_id": "abc123"})
    assert spec.name == "minimal-workflow"

def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_spec(Path("/nonexistent/spec.yaml"))

"""Tests for HarnessSpec.mission field."""
import pytest
from armature.spec.models import HarnessSpec
from armature.spec.loader import load_spec
from pathlib import Path
import tempfile, textwrap


def _minimal_spec_dict(**kwargs):
    return {
        "name": "test-wf",
        "stages": [{"id": "s1", "depends_on": []}],
        **kwargs,
    }


def test_harnessspec_accepts_mission_field():
    spec = HarnessSpec.model_validate(_minimal_spec_dict(
        mission="Produce a Q3 market report.",
    ))
    assert spec.mission == "Produce a Q3 market report."


def test_harnessspec_mission_defaults_empty():
    spec = HarnessSpec.model_validate(_minimal_spec_dict())
    assert spec.mission == ""

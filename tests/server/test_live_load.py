"""The live-event load target from DESIGN.md. Opt in with ARMATURE_LOAD=1, it takes about a minute per database."""
import os

import pytest

from tests.server.conftest import MATRIX
from tests.server.live_load import run_load

pytestmark = [pytest.mark.load,
              pytest.mark.skipif(os.environ.get("ARMATURE_LOAD") != "1", reason="set ARMATURE_LOAD=1 to run")]


@pytest.mark.parametrize("database", ["sqlite", *MATRIX])
def test_twenty_runs_hundred_viewers_p99_under_a_second(database):
    url = None if database == "sqlite" else os.environ.get(MATRIX[database])
    if database != "sqlite" and not url:
        pytest.skip(f"{MATRIX[database]} is not set")
    result = run_load(url, seconds=10)
    print(result)
    assert result["emitter_dropped_must_keep"] == 0 and result["server_rejected"] == 0
    assert result["stored_events"] == result["emitted"]
    assert result["frames_seen"] == result["emitted"] * result["viewers"] // result["runs"]
    assert result["latency_ms"]["p99"] < 1000

"""A telemetry write must not be able to discard work a stage already finished.

The success-path trace write sits inside the stage's try block, so an
exception there used to be indistinguishable from the stage failing: under
fan-out the branch was replaced with _fan_out_error and the completed work
was dropped. In production this cost one of six specialist code reviews on
every run, each one a finished session, to a transient "database is locked".
"""
import sqlite3
import pytest
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, Adapter,
)
from armature.runtime.engine import Harness


class _ExplodingTraceStore:
    """Every write fails, the way a locked SQLite database fails."""

    def __init__(self):
        self.attempts = 0

    async def init(self):
        return None

    async def record(self, trace):
        self.attempts += 1
        raise sqlite3.OperationalError("database is locked")


def _harness(tmp_path, fan_out=3):
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="jobs", adapter="emit", depends_on=[]),
            Stage(
                id="work",
                adapter="run",
                partition_source="{{ jobs.jobs }}",
                partition_key="job",
                fan_out=fan_out,
                fan_in="list",
                depends_on=["jobs"],
            ),
        ],
        adapters={
            "emit": Adapter(
                name="emit", type="script",
                cmd="""printf '{"jobs": [1, 2, 3, 4, 5, 6]}'""",
                parse="json", timeout=30,
            ),
            "run": Adapter(
                name="run", type="script",
                cmd="""printf '{"done": true}'""",
                parse="json", timeout=30,
            ),
        },
        model_tiers=ModelTiers(
            small=ModelTierConfig(provider="openai", model="gpt-4o-mini")
        ),
    )
    return Harness(spec=spec, session_dir=tmp_path)


async def test_fan_out_keeps_every_branch_when_the_trace_write_fails(tmp_path):
    harness = _harness(tmp_path)
    store = _ExplodingTraceStore()
    harness._traces = store

    result = await harness.run({})

    assert len(result["work"]) == 6
    assert all(branch == {"done": True} for branch in result["work"]), result["work"]
    assert not any("_fan_out_error" in b for b in result["work"])
    assert store.attempts > 0, "the test never exercised the trace write"

"""A fan-out of script-adapter stages runs its items concurrently."""
import time
from armature.spec.models import Adapter, HarnessSpec, ModelTiers, ModelTierConfig, Stage
from armature.runtime.engine import Harness


async def test_script_fan_out_runs_items_concurrently(tmp_path):
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(
            id="s",
            adapter="sleeper",
            partition_source="{{ items }}",
            partition_key="it",
            fan_out=3,
            depends_on=[],
        )],
        adapters={"sleeper": Adapter(name="sleeper", type="script", cmd="sleep 1", timeout=10)},
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    start = time.monotonic()
    result = await harness.run({"items": [1, 2, 3]})
    elapsed = time.monotonic() - start

    assert [r["exit_code"] for r in result["s"]] == [0, 0, 0]
    assert elapsed < 2.2, f"3 parallel 1s scripts should finish in ~1s, got {elapsed:.2f}s"

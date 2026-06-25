import pytest
from datetime import datetime, timezone
from armature.state.traces import TraceStore, TraceRecord
from armature.spec.models import HarnessSpec, ModelTiers, ModelTierConfig
from armature.loop.runner import _account_run, LoopRunner


@pytest.fixture
async def store(tmp_path):
    s = TraceStore(tmp_path / "traces.db")
    await s.init()
    return s


async def _seed(store, run_id, n, in_tok=10, out_tok=5, wf="wf"):
    for i in range(n):
        await store.record(TraceRecord(
            run_id=run_id, workflow_name=wf, stage_id=f"s{i}",
            role_type="worker", model="fake",
            input_tokens=in_tok, output_tokens=out_tok,
        ))


async def test_account_run_counts_rows_and_tokens(store):
    await _seed(store, "r1", 3, in_tok=10, out_tok=5)
    calls, toks = await _account_run(store, "r1")
    assert calls == 3
    assert toks == 45  # 3 * (10 + 5)


async def test_account_run_none_run_id_returns_zeros(store):
    assert await _account_run(store, None) == (0, 0)


async def test_account_run_missing_run_id_returns_zeros(store):
    assert await _account_run(store, "nope") == (0, 0)


def _spec():
    return HarnessSpec(
        name="wf",
        stages=[],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )


class _FakeHarness:
    """Writes deterministic trace rows; returns canned results in order."""
    def __init__(self, spec, traces_db, results, calls_per_iter=2, in_tok=10, out_tok=5):
        self._spec = spec
        self._traces_db = traces_db
        self._results = results
        self._idx = 0
        self._calls_per_iter = calls_per_iter
        self._in_tok = in_tok
        self._out_tok = out_tok

    async def run(self, inputs):
        self._idx += 1
        store = TraceStore(self._traces_db)
        await store.init()
        rid = f"fake-{self._idx}"
        ts = datetime(2026, 1, 1, 0, 0, self._idx, tzinfo=timezone.utc).isoformat()
        for i in range(self._calls_per_iter):
            await store.record(TraceRecord(
                run_id=rid, workflow_name=self._spec.name, stage_id=f"s{i}",
                role_type="worker", model="fake",
                input_tokens=self._in_tok, output_tokens=self._out_tok,
                timestamp=ts,
            ))
        return self._results[(self._idx - 1) % len(self._results)]


def _factory(spec, traces_db, results, **kw):
    """Build a harness_factory closure for a fake with the given canned results."""
    def make():
        return _FakeHarness(spec, traces_db, results)
    return make


async def test_loop_stops_on_until(tmp_path):
    db = tmp_path / "traces.db"
    results = [{"judge": {"accept": False}}, {"judge": {"accept": True}}]
    runner = LoopRunner(
        spec=_spec(), traces_db=db,
        max_iterations=10,
        until="{{ judge.accept }}",
        harness_factory=_factory(_spec(), db, results),
    )
    res = await runner.run()
    assert res.stop_reason == "until_met"
    assert len(res.iterations) == 2
    assert res.final_result == {"judge": {"accept": True}}


async def test_loop_stops_on_max_iterations(tmp_path):
    db = tmp_path / "traces.db"
    results = [{"n": 1}]
    runner = LoopRunner(
        spec=_spec(), traces_db=db,
        max_iterations=3,
        harness_factory=_factory(_spec(), db, results),
    )
    res = await runner.run()
    assert res.stop_reason == "max_iterations"
    assert len(res.iterations) == 3


async def test_loop_stops_on_budget_llm_calls(tmp_path):
    db = tmp_path / "traces.db"
    results = [{"n": 1}]  # 2 calls/iter
    runner = LoopRunner(
        spec=_spec(), traces_db=db,
        max_iterations=100,
        max_llm_calls=5,  # 2 then 4 -> 4 < 5 continue; 6 >= 5 stop after iter 3? see below
        harness_factory=_factory(_spec(), db, results),
    )
    # iter1 acc=2 (<5 continue); iter2 acc=4 (<5 continue); iter3 acc=6 (>=5 stop)
    res = await runner.run()
    assert res.stop_reason == "budget_llm_calls"
    assert len(res.iterations) == 3
    assert res.accumulated["llm_calls"] == 6


async def test_loop_stops_on_converge(tmp_path):
    db = tmp_path / "traces.db"
    results = [{"n": 1}, {"n": 1}]  # identical -> converge on iter 2
    runner = LoopRunner(
        spec=_spec(), traces_db=db,
        max_iterations=10, converge=True,
        harness_factory=_factory(_spec(), db, results),
    )
    res = await runner.run()
    assert res.stop_reason == "converged"
    assert len(res.iterations) == 2


async def test_loop_carries_forward_into_inputs(tmp_path):
    db = tmp_path / "traces.db"
    seen = []
    results = [{"researcher": {"content": "alpha"}}, {"researcher": {"content": "beta"}}]

    class _Capturing(_FakeHarness):
        async def run(self, inputs):
            seen.append(inputs)
            return await super().run(inputs)

    def make():
        return _Capturing(_spec(), db, results)

    runner = LoopRunner(
        spec=_spec(), traces_db=db,
        max_iterations=2, carry_forward="researcher.content",
        harness_factory=make,
    )
    await runner.run()
    # iteration 1: no carry
    assert seen[0]["_iteration"]["num"] == 1
    assert seen[0]["_iteration"]["carry_forward"] == {}
    # iteration 2: researcher.content carried under prior_run and top level
    assert seen[1]["_iteration"]["num"] == 2
    assert seen[1]["prior_run"] == {"researcher": {"content": "alpha"}}
    assert seen[1]["researcher"] == {"content": "alpha"}


async def test_loop_aborts_on_harness_error(tmp_path):
    db = tmp_path / "traces.db"

    class _Boom:
        async def run(self, inputs):
            raise RuntimeError("kaboom")

    runner = LoopRunner(
        spec=_spec(), traces_db=db, max_iterations=5,
        harness_factory=lambda: _Boom(),
    )
    res = await runner.run()
    assert res.stop_reason == "error"
    assert "kaboom" in (res.error or "")
    assert len(res.iterations) == 0
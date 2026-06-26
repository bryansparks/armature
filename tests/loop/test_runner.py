import pytest
import itertools
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
    """Models a fresh Harness per iteration: each instance mints its own run_id
    (the factory-supplied ``idx``) at construction — mirroring the real Harness,
    which mints ``run_id`` in ``__init__`` (engine.py:127). Writes deterministic
    trace rows and returns the canned result for this iteration's index."""

    def __init__(self, spec, traces_db, results, idx, calls_per_iter=2, in_tok=10, out_tok=5):
        self._spec = spec
        self._traces_db = traces_db
        self._results = results
        self._idx = idx
        self._run_id = f"fake-{idx}"
        self._calls_per_iter = calls_per_iter
        self._in_tok = in_tok
        self._out_tok = out_tok

    async def run(self, inputs):
        store = TraceStore(self._traces_db)
        await store.init()
        rid = self._run_id
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
    """harness_factory closure: each call mints a fresh fake with the next index,
    mirroring a fresh Harness per iteration."""
    counter = itertools.count(1)
    def make():
        return _FakeHarness(spec, traces_db, results, idx=next(counter))
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

    counter = itertools.count(1)
    def make():
        return _Capturing(_spec(), db, results, idx=next(counter))

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


async def test_loop_writes_one_summary_trace_row(tmp_path):
    db = tmp_path / "traces.db"
    results = [{"n": 1}, {"n": 1}]
    runner = LoopRunner(
        spec=_spec(), traces_db=db, max_iterations=2, converge=True,
        harness_factory=_factory(_spec(), db, results),
    )
    res = await runner.run()
    # exactly one __loop__ row for this workflow
    store = TraceStore(db)
    await store.init()
    all_rows = await store.query(workflow_name="wf", limit=1000)
    loop_rows = [r for r in all_rows if r.stage_id == "__loop__"]
    assert len(loop_rows) == 1
    row = loop_rows[0]
    assert row.run_id == res.loop_session_id
    assert row.role_type == "orchestrator"
    assert row.model == "loop-driver"
    assert row.success is True
    assert row.outputs["stop_reason"] == "converged"
    assert len(row.outputs["iterations"]) == 2
    # the summary run_id is distinct from the per-iteration run_ids
    iter_run_ids = {ir.run_id for ir in res.iterations}
    assert row.run_id not in iter_run_ids


async def test_loop_summary_does_not_inflate_other_runs_hqs(tmp_path):
    db = tmp_path / "traces.db"
    results = [{"n": 1}]
    runner = LoopRunner(
        spec=_spec(), traces_db=db, max_iterations=1,
        harness_factory=_factory(_spec(), db, results),
    )
    res = await runner.run()
    store = TraceStore(db)
    await store.init()
    # compute_hqs on a per-iteration run_id (which has real worker rows) must work
    # and must NOT be polluted by the __loop__ summary row (different run_id).
    iter_rid = res.iterations[0].run_id
    hqs = await store.compute_hqs(iter_rid)
    assert hqs is not None
    assert hqs.n_traces == 2  # the two fake worker rows for that iteration
    # the summary row's own run_id has exactly one __loop__ row
    summary_rows = await store.query_by_run(res.loop_session_id)
    assert len(summary_rows) == 1
    assert summary_rows[0].stage_id == "__loop__"


class _NoTraceHarness:
    """Models a Harness whose run writes zero trace rows (e.g. a tool_call-only
    workflow). Mints run_id at construction like the real Harness (engine.py:127)
    but records nothing — exercising the runner's run_id discovery path."""

    def __init__(self, spec, traces_db, idx):
        self._spec = spec
        self._traces_db = traces_db
        self._run_id = f"notrace-{idx}"

    async def run(self, inputs):
        return {"ok": True}


async def test_loop_handles_harness_that_writes_no_trace_rows(tmp_path):
    """A workflow whose stages write zero traces (tool_call/subagent_spec only)
    must still get a correct per-iteration run_id and zero budget inflation."""
    db = tmp_path / "traces.db"
    counter = itertools.count(1)
    def make():
        return _NoTraceHarness(_spec(), db, idx=next(counter))
    runner = LoopRunner(
        spec=_spec(), traces_db=db, max_iterations=3,
        harness_factory=make,
    )
    res = await runner.run()
    assert res.stop_reason == "max_iterations"
    assert len(res.iterations) == 3
    # each iteration gets the harness's own run_id — never empty, never the loop session
    for ir in res.iterations:
        assert ir.run_id.startswith("notrace-")
        assert ir.run_id != ""
        assert ir.run_id != res.loop_session_id
    # zero trace rows -> zero llm_calls/tokens accounted, no inflation
    assert res.accumulated["llm_calls"] == 0
    assert res.accumulated["tokens"] == 0

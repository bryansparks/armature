import pytest
from armature.state.traces import TraceStore, TraceRecord
from armature.loop.runner import _account_run


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
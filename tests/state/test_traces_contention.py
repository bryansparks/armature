"""Trace writes have to survive another writer holding the database."""
import asyncio
import sqlite3
import threading
import time
import pytest
from armature.state.traces import TraceStore, TraceRecord


def _trace(i: int) -> TraceRecord:
    return TraceRecord(
        run_id="r", workflow_name="w", stage_id="reviews",
        role_type="script", model="", latency_ms=float(i),
    )


@pytest.fixture
async def store(tmp_path):
    s = TraceStore(tmp_path / "traces.db")
    await s.init()
    return s


async def test_record_waits_out_another_writer(store, tmp_path):
    db = str(tmp_path / "traces.db")
    holding = threading.Event()

    def hold_the_write_lock():
        conn = sqlite3.connect(db, timeout=30.0, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        holding.set()
        time.sleep(0.4)
        conn.commit()
        conn.close()

    t = threading.Thread(target=hold_the_write_lock)
    t.start()
    holding.wait(timeout=5)
    await store.record(_trace(1))
    t.join()

    assert len(await store.query_by_run("r")) == 1


async def test_branches_finishing_together_all_land(store):
    # Every fan-out branch writes its trace the moment the event loop frees up.
    async def branch(i):
        await asyncio.sleep(0.02)
        await store.record(_trace(i))

    await asyncio.gather(*[branch(i) for i in range(16)])

    assert len(await store.query_by_run("r")) == 16

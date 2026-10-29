import tempfile
import time

from armature.events import Emitter
from tests.events.support import DatagramReader, closed_port

N = 20_000
TOTAL_BUDGET_S = 1.0
PER_CALL_BUDGET_S = 0.05
MUST_KEEP_EVERY = 20


def _blast(emitter: Emitter) -> tuple[float, float]:
    worst = 0.0
    t0 = time.perf_counter()
    for i in range(N):
        kind = "step_end" if i % MUST_KEEP_EVERY == 0 else "log"
        s = time.perf_counter()
        emitter.emit(kind, "s", i=i, line="ordinary log line from a build step")
        worst = max(worst, time.perf_counter() - s)
    return time.perf_counter() - t0, worst


def test_unix_no_reader_never_blocks():
    emitter = Emitter(f"unix:{tempfile.mkdtemp(prefix='aev')}/none.sock", "t")
    total, worst = _blast(emitter)
    emitter.close(0.1)
    print(f"\nunix no reader: {N} events in {total:.3f}s, worst call {worst * 1000:.2f}ms")
    assert total < TOTAL_BUDGET_S
    assert worst < PER_CALL_BUDGET_S
    assert emitter.dropped == N


def test_unix_slow_reader_keeps_must_keep():
    reader = DatagramReader(delay_s=0.0002)
    try:
        emitter = Emitter(reader.endpoint, "t")
        total, worst = _blast(emitter)
        assert emitter.flush(30)
        emitter.close()
        print(f"\nunix slow reader: {N} events in {total:.3f}s, worst call {worst * 1000:.2f}ms, "
              f"sent {emitter.sent}, dropped {emitter.dropped}")
        assert total < TOTAL_BUDGET_S
        assert worst < PER_CALL_BUDGET_S
        assert emitter.dropped_must_keep == 0
        reader.wait_for(emitter.sent, timeout=10)
        kept = [e for e in reader.events if e["kind"] == "step_end"]
        assert len(kept) == N // MUST_KEEP_EVERY
        dropped_markers = sum(e["payload"]["count"] for e in reader.events if e["kind"] == "dropped")
        assert dropped_markers == emitter.dropped
        assert len(reader.events) - len([e for e in reader.events if e["kind"] == "dropped"]) == N - emitter.dropped
    finally:
        reader.close()


def test_http_unreachable_never_blocks_and_keeps_must_keep():
    emitter = Emitter(f"http://127.0.0.1:{closed_port()}/events", "t")
    total, worst = _blast(emitter)
    print(f"\nhttp unreachable: {N} events in {total:.3f}s, worst call {worst * 1000:.2f}ms, "
          f"dropped {emitter.dropped}")
    assert total < TOTAL_BUDGET_S
    assert worst < PER_CALL_BUDGET_S
    deadline = time.monotonic() + 5
    while True:
        with emitter._lock:
            if emitter._inflight == 0 or time.monotonic() > deadline:
                kept = sum(b'"kind":"step_end"' in data for _, data in emitter._keep)
                break
        time.sleep(0.005)
    assert emitter.dropped_must_keep == 0
    assert kept == N // MUST_KEEP_EVERY
    assert emitter.dropped >= N - N // MUST_KEEP_EVERY - 2000
    t0 = time.perf_counter()
    emitter.close()
    assert time.perf_counter() - t0 < 1.5

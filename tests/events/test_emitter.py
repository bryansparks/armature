import json
import os
import re
import tempfile
import time

import pytest

from armature.events import DROPPABLE, MUST_KEEP, Emitter, NullEmitter, get_emitter
from armature.events.emitter import MAX_BATCH_BYTES, MAX_EVENT_BYTES
from tests.events.support import DatagramReader, HttpReceiver, closed_port


@pytest.fixture
def reader():
    r = DatagramReader()
    yield r
    r.close()


@pytest.fixture
def http():
    h = HttpReceiver()
    yield h
    h.close()


def test_kind_table_is_disjoint_and_complete():
    assert not MUST_KEEP & DROPPABLE
    assert {"run_start", "run_end", "step_start", "step_end", "step_failed", "step_skipped",
            "retry", "cost", "finding", "tool_call", "tool_result", "session_start",
            "session_end", "error", "dropped", "artifact", "summary"} == MUST_KEEP
    assert {"text", "log", "progress"} == DROPPABLE


def test_unset_env_gives_noop(monkeypatch):
    monkeypatch.delenv("ARMATURE_EVENTS", raising=False)
    emitter = Emitter.from_env()
    assert isinstance(emitter, NullEmitter) and not emitter.enabled
    emitter.emit("step_start", "a", x=1)
    assert emitter.flush() is True
    assert not get_emitter().enabled


def test_unknown_scheme_gives_noop(monkeypatch):
    monkeypatch.setenv("ARMATURE_EVENTS", "ftp://nowhere")
    assert not Emitter.from_env().enabled


def test_unix_wire_format(reader, monkeypatch):
    monkeypatch.setenv("ARMATURE_EVENTS", reader.endpoint)
    monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "tok-1")
    emitter = Emitter.from_env()
    emitter.emit("step_start", "review", role="lead", run_id="spoof", team_id="spoof")
    emitter.close()
    [event] = reader.wait_for(1)
    assert set(event) == {"v", "eid", "token", "kind", "ts", "step_id", "pid", "payload"}
    assert re.fullmatch(r"[0-9a-f]{16}:1", event["eid"])
    assert event["v"] == 1
    assert event["token"] == "tok-1"
    assert event["kind"] == "step_start"
    assert event["step_id"] == "review"
    assert event["pid"] == os.getpid()
    assert event["payload"] == {"role": "lead"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00", event["ts"])


def test_default_redaction(reader, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "hunter2-super-secret")
    monkeypatch.setenv("ARMATURE_REDACT_ENV", "MY_SECRET, MISSING")
    emitter = Emitter(reader.endpoint, "t")
    emitter.emit("tool_result", "s", content=(
        "key ghp_abcdefghijklmnopqrstuvwxyz0123 and xoxb-123-abc "
        "sk-ant-api03-zzz AKIAABCDEFGHIJKLMNOP "
        "Authorization: Bearer abc.def and hunter2-super-secret"
    ), nested={"list": ["github_pat_11AAA_bbb", 3]})
    emitter.close()
    [event] = reader.wait_for(1)
    text = event["payload"]["content"]
    for secret in ("ghp_", "xoxb-", "sk-ant-", "AKIA", "abc.def", "hunter2"):
        assert secret not in text
    assert "Authorization: [redacted]" in text
    assert event["payload"]["nested"] == {"list": ["[redacted]", 3]}


def test_custom_redactor(reader):
    emitter = Emitter(reader.endpoint, "t", redact=lambda p: {k: "x" for k in p})
    emitter.emit("log", None, a="secret")
    emitter.close()
    assert reader.wait_for(1)[0]["payload"] == {"a": "x"}


def test_redactor_failure_never_raises(reader):
    def broken(payload):
        raise RuntimeError("boom")

    emitter = Emitter(reader.endpoint, "t", redact=broken)
    emitter.emit("error", None, a="b")
    emitter.close()
    assert reader.wait_for(1)[0]["payload"] == {"redaction_failed": True}


def test_oversize_payload_truncated_per_field(reader):
    emitter = Emitter(reader.endpoint, "t")
    emitter.emit("tool_result", "s", content="x" * 200_000, tool="Read")
    emitter.close()
    reader.wait_for(1)
    assert len(reader.raw[0]) <= MAX_EVENT_BYTES
    payload = reader.events[0]["payload"]
    assert payload["truncated"] is True
    assert payload["tool"] == "Read"
    assert 0 < len(payload["content"]) < 200_000


def test_text_is_coalesced_per_step_keeping_latest(reader):
    emitter = Emitter(reader.endpoint, "t")
    for i in range(50):
        emitter.emit("text", "a", text=f"a{i}")
        emitter.emit("text", "b", text=f"b{i}")
    emitter.close()
    events = reader.wait_for(4)
    texts = [e["payload"]["text"] for e in events]
    assert texts[:2] == ["a0", "b0"]
    assert {"a49", "b49"} <= set(texts)
    assert len(texts) <= 4
    assert emitter.coalesced >= 90


def test_pending_text_goes_out_before_the_steps_next_event(reader):
    emitter = Emitter(reader.endpoint, "t")
    emitter.emit("text", "a", text="first")
    emitter.emit("text", "a", text="second")
    emitter.emit("tool_call", "a", tool="Read")
    emitter.close()
    events = reader.wait_for(3)
    assert [(e["kind"], e["payload"].get("text")) for e in events] == [
        ("text", "first"), ("text", "second"), ("tool_call", None),
    ]


def test_pending_text_is_released_after_the_interval(reader):
    emitter = Emitter(reader.endpoint, "t")
    emitter.emit("text", "a", text="first")
    emitter.emit("text", "a", text="latest")
    events = reader.wait_for(2, timeout=2)
    assert [e["payload"]["text"] for e in events] == ["first", "latest"]
    emitter.close()


def test_no_reader_drops_without_raising():
    emitter = Emitter(f"unix:{tempfile.mkdtemp(prefix='aev')}/missing.sock", "t")
    t0 = time.perf_counter()
    for _ in range(100):
        emitter.emit("step_start", "s")
    assert time.perf_counter() - t0 < 0.5
    assert emitter.dropped == 100
    emitter.close()


def test_drop_marker_follows_a_gap():
    emitter = Emitter(f"unix:{tempfile.mkdtemp(prefix='aev')}/x.sock", "t")
    emitter.emit("step_start", "s")
    r = DatagramReader()
    try:
        emitter._sock_path = r.path
        emitter.emit("step_end", "s")
        emitter.close()
        events = r.wait_for(2)
        assert [e["kind"] for e in events] == ["dropped", "step_end"]
        assert events[0]["payload"] == {"count": 1}
    finally:
        r.close()


def test_http_batches_ndjson_with_bearer(http):
    emitter = Emitter(http.endpoint, "secret-token")
    for i in range(450):
        emitter.emit("tool_call", "s", i=i)
    assert emitter.flush(5)
    emitter.close()
    events = http.events
    assert [e["payload"]["i"] for e in events] == list(range(450))
    assert all(len(lines) <= 200 for _, lines in http.requests)
    assert all("token" not in e for e in events)
    headers = http.requests[0][0]
    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["Content-Type"] == "application/x-ndjson"


def test_http_failure_requeues_must_keep_only():
    h = HttpReceiver(fail_first=1)
    try:
        emitter = Emitter(h.endpoint, "t")
        emitter.emit("step_start", "s")
        emitter.emit("log", "s", line="lost")
        emitter.emit("step_end", "s")
        assert emitter.flush(5)
        emitter.close()
        kinds = [e["kind"] for e in h.events]
        assert kinds == ["dropped", "step_start", "step_end"]
        assert h.events[0]["payload"] == {"count": 1}
    finally:
        h.close()


def test_overflow_drops_oldest_droppable_first(monkeypatch):
    emitter = Emitter(f"http://127.0.0.1:{closed_port()}/", "t", queue_size=4)
    monkeypatch.setattr(emitter, "_ensure_thread_locked", lambda: None)
    emitter.emit("log", None, n=1)
    emitter.emit("step_start", None, n=2)
    emitter.emit("log", None, n=3)
    emitter.emit("step_end", None, n=4)
    emitter.emit("cost", None, n=5)
    emitter.emit("log", None, n=6)
    emitter.emit("finding", None, n=7)
    kept = sorted(emitter._keep + emitter._drop)
    assert [e[1].count(b'"kind":"log"') for e in kept] == [0, 0, 0, 0]
    assert emitter.dropped == 3 and emitter.dropped_must_keep == 0
    emitter.emit("error", None, n=8)
    assert emitter.dropped_must_keep == 1
    emitter._closed = True


def test_get_emitter_reads_env_each_time(reader, monkeypatch):
    monkeypatch.setenv("ARMATURE_EVENTS", reader.endpoint)
    monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "a")
    first = get_emitter()
    second = get_emitter()
    assert first is not second and first.token == "a"
    first.close()
    second.close()


def test_payload_may_reuse_parameter_names(reader):
    emitter = Emitter(reader.endpoint, "t")
    emitter.emit("step_start", "s", kind="script", step_id="other", self=1)
    emitter.close()
    [event] = reader.wait_for(1)
    assert event["kind"] == "step_start" and event["step_id"] == "s"
    assert event["payload"] == {"kind": "script", "step_id": "other", "self": 1}


def test_eids_are_unique_and_survive_a_retry():
    h = HttpReceiver(fail_first=1)
    try:
        emitter = Emitter(h.endpoint, "t")
        for i in range(5):
            emitter.emit("tool_call", "s", i=i)
        assert emitter.flush(5)
        emitter.close()
        [(status, body)] = h.rejected
        first_try = [json.loads(line)["eid"] for line in body.decode().splitlines()]
        delivered = [e["eid"] for e in h.events]
        assert status == 503 and first_try == delivered
        assert len(set(delivered)) == 5
        nonce = {eid.split(":")[0] for eid in delivered}
        assert len(nonce) == 1 and len(nonce.pop()) == 16
        assert Emitter(h.endpoint, "t")._nonce != emitter._nonce
    finally:
        h.close()


def test_http_batches_are_capped_by_bytes(http):
    emitter = Emitter(http.endpoint, "t")
    for i in range(200):
        emitter.emit("tool_result", "s", i=i, content="z" * 10_000)
    assert emitter.flush(10)
    emitter.close()
    assert [e["payload"]["i"] for e in http.events] == list(range(200))
    assert len(http.requests) >= 3
    assert max(http.sizes) <= MAX_BATCH_BYTES


def test_413_splits_the_batch_and_keeps_droppable_events():
    h = HttpReceiver(max_body=30_000)
    try:
        emitter = Emitter(h.endpoint, "t")
        for i in range(40):
            emitter.emit("log", "s", i=i, line="l" * 2_000)
        assert emitter.flush(10)
        emitter.close()
        assert [e["payload"]["i"] for e in h.events] == list(range(40))
        assert any(status == 413 for status, _ in h.rejected)
        assert emitter.dropped == 0
    finally:
        h.close()


def test_own_token_runner_key_and_ark_keys_are_redacted(reader, monkeypatch):
    monkeypatch.setenv("ARMATURE_RUNNER_KEY", "runner-key-value-1234")
    emitter = Emitter(reader.endpoint, "the-run-token-5678", redact=lambda p: p)
    ark = "ark_" + "a" * 44
    emitter.emit("log", None, line=f"t=the-run-token-5678 k=runner-key-value-1234 {ark}")
    emitter.close()
    line = reader.wait_for(1)[0]["payload"]["line"]
    assert "the-run-token" not in line and "runner-key-value" not in line
    assert line == "t=[redacted] k=[redacted] " + ark


def test_ark_pattern_redacted_by_default(reader):
    emitter = Emitter(reader.endpoint, "t")
    emitter.emit("log", None, line="key ark_" + "Ab-_" * 11)
    emitter.close()
    assert reader.wait_for(1)[0]["payload"]["line"] == "key [redacted]"


def test_plain_http_to_a_remote_host_is_refused(monkeypatch):
    with pytest.raises(ValueError):
        Emitter("http://10.1.2.3/events", "t")
    monkeypatch.setenv("ARMATURE_EVENTS", "http://events.example.com/x")
    monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "t")
    assert not Emitter.from_env().enabled
    for url in ("http://127.0.0.1:9/x", "http://localhost/x", "http://[::1]/x", "https://events.example.com/x"):
        Emitter(url, "t").close(0)


def _hwm_accept(events):
    """The server's dedupe: per nonce, keep only counters above the highest seen."""
    high: dict[str, int] = {}
    accepted = []
    for event in events:
        nonce, counter = event["eid"].rsplit(":", 1)
        if int(counter) > high.get(nonce, 0):
            high[nonce] = int(counter)
            accepted.append(event)
    return accepted


def test_late_coalesced_text_passes_a_high_water_mark(reader):
    emitter = Emitter(reader.endpoint, "t")
    emitter.emit("text", "a", text="first")
    emitter.emit("text", "a", text="late")
    emitter.emit("tool_call", "b", tool="Read")
    events = reader.wait_for(3, timeout=2)
    emitter.close()
    assert [e["payload"].get("text") or e["kind"] for e in events] == ["first", "tool_call", "late"]
    assert _hwm_accept(events) == events


def test_retried_and_split_batches_pass_a_high_water_mark():
    h = HttpReceiver(fail_first=1, max_body=30_000)
    try:
        emitter = Emitter(h.endpoint, "t")
        for i in range(30):
            emitter.emit("tool_call", "s", i=i, line="l" * 2_000)
            if i % 7 == 0:
                emitter.emit("text", "s", text=f"t{i}")
        emitter.emit("log", "s", end=True)
        assert emitter.flush(10)
        emitter.close()
        events = h.events
        assert [e["payload"]["i"] for e in events if "i" in e["payload"]] == list(range(30))
        assert _hwm_accept(events) == events
    finally:
        h.close()


def test_drop_marker_has_its_own_counter(reader):
    emitter = Emitter(reader.endpoint, "t", queue_size=1)
    emitter._unreported_drops = 2
    emitter.emit("step_start", "s")
    emitter.close()
    events = reader.wait_for(2)
    assert [e["kind"] for e in events] == ["dropped", "step_start"]
    assert events[0]["eid"].split(":")[0] != events[1]["eid"].split(":")[0]
    assert _hwm_accept(events) == events

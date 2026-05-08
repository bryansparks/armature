import pytest
import asyncio
from pathlib import Path
from armature.state.session import SessionLog, SessionEvent


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "session.jsonl"


async def test_append_and_read(log_path):
    log = SessionLog(log_path)
    await log.append(SessionEvent(type="message", data={"role": "user", "content": "hello"}))
    await log.append(SessionEvent(type="tool_result", data={"tool": "shell", "exit_code": 0}))

    events = await log.read_all()
    assert len(events) == 2
    assert events[0].type == "message"
    assert events[1].type == "tool_result"


async def test_replay_reconstructs_events(log_path):
    log = SessionLog(log_path)
    await log.append(SessionEvent(type="start", data={"run_id": "abc"}))
    await log.append(SessionEvent(type="stage_complete", data={"stage": "s1", "output": "done"}))

    log2 = SessionLog(log_path)
    events = await log2.read_all()
    assert events[0].data["run_id"] == "abc"
    assert events[1].data["stage"] == "s1"


async def test_missing_log_returns_empty(tmp_path):
    log = SessionLog(tmp_path / "nonexistent.jsonl")
    events = await log.read_all()
    assert events == []


async def test_event_has_timestamp(log_path):
    log = SessionLog(log_path)
    await log.append(SessionEvent(type="ping", data={}))
    events = await log.read_all()
    assert events[0].timestamp is not None
    assert "T" in events[0].timestamp  # ISO format


async def test_session_log_accepts_str_path(tmp_path):
    path = str(tmp_path / "log.jsonl")
    log = SessionLog(path)
    await log.append(SessionEvent(type="test", data={"x": 1}))
    events = await log.read_all()
    assert len(events) == 1


async def test_append_creates_parent_directories(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "log.jsonl"
    log = SessionLog(nested)
    await log.append(SessionEvent(type="init", data={}))
    assert nested.exists()


async def test_event_data_preserved_exactly(log_path):
    payload = {"key": "value", "nested": {"x": [1, 2, 3]}, "num": 42}
    log = SessionLog(log_path)
    await log.append(SessionEvent(type="complex", data=payload))
    events = await log.read_all()
    assert events[0].data == payload


async def test_concurrent_appends_do_not_corrupt(log_path):
    """Multiple concurrent appends are safe due to async lock."""
    log = SessionLog(log_path)
    await asyncio.gather(
        *[log.append(SessionEvent(type="concurrent", data={"i": i})) for i in range(10)]
    )
    events = await log.read_all()
    assert len(events) == 10

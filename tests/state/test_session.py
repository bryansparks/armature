import pytest
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

    log2 = SessionLog(log_path)  # fresh instance, same file
    events = await log2.read_all()
    assert events[0].data["run_id"] == "abc"
    assert events[1].data["stage"] == "s1"

async def test_missing_log_returns_empty(tmp_path):
    log = SessionLog(tmp_path / "nonexistent.jsonl")
    events = await log.read_all()
    assert events == []

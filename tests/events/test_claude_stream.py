import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from armature.events import Emitter
from armature.events.claude_stream import SUMMARY_CHARS, ClaudeStreamParser, pipe
from tests.events.support import DatagramReader

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream.jsonl"
REPO = Path(__file__).resolve().parents[2]


class Recorder:
    enabled = True

    def __init__(self, redact=lambda payload: payload):
        self.events = []
        self.redact = redact

    def emit(self, kind, step_id=None, /, **payload):
        self.events.append((kind, step_id, payload))


def _parse(lines):
    recorder = Recorder()
    parser = ClaudeStreamParser(recorder, "review")
    for line in lines:
        parser.feed(line)
    parser.finish()
    return recorder.events


def test_fixture_maps_to_events():
    events = _parse(FIXTURE.read_text().splitlines())
    assert [kind for kind, _, _ in events] == [
        "session_start", "text", "tool_call", "tool_result", "tool_call",
        "tool_result", "text", "cost", "session_end",
    ]
    assert all(step == "review" for _, step, _ in events)
    by_kind = {}
    for kind, _, payload in events:
        by_kind.setdefault(kind, []).append(payload)
    assert by_kind["session_start"] == [{
        "model": "us.anthropic.claude-sonnet-5-5",
        "session_id": "0f6c2b7e-6a1d-4c55-9a8e-2f1d3c4b5a69",
    }]
    read_call, _ = by_kind["tool_call"]
    assert read_call["tool"] == "Read" and "Main.kt" in read_call["input"]
    read_result, bash_result = by_kind["tool_result"]
    assert read_result["tool"] == "Read" and read_result["is_error"] is False
    assert "package app" in read_result["content"]
    assert bash_result == {
        "tool": "Bash", "tool_use_id": "toolu_01B", "is_error": True,
        "content": "fatal: not a git repository",
    }
    [cost] = by_kind["cost"]
    assert cost == {
        "model": "us.anthropic.claude-sonnet-5-5",
        "input_tokens": 4100,
        "output_tokens": 90,
        "cost_usd": 0.04213,
        "cache_creation_input_tokens": 2048,
        "cache_read_input_tokens": 8192,
        "session_id": "0f6c2b7e-6a1d-4c55-9a8e-2f1d3c4b5a69",
    }
    [end] = by_kind["session_end"]
    assert end["status"] == "success" and end["is_error"] is False and end["num_turns"] == 3


def test_tool_input_and_result_capped():
    big = "y" * 10_000
    events = _parse([
        '{"type":"system","subtype":"init","session_id":"s","model":"m"}',
        (
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Write",'
            f'"input":{{"content":"{big}"}}}}]}}}}'
        ),
        f'{{"type":"user","message":{{"content":[{{"type":"tool_result","tool_use_id":"t1","content":"{big}"}}]}}}}',
    ])
    call = next(p for k, _, p in events if k == "tool_call")
    result = next(p for k, _, p in events if k == "tool_result")
    assert len(call["input"]) == SUMMARY_CHARS
    assert len(result["content"]) == SUMMARY_CHARS
    assert result["tool"] == "Write"


def test_secret_at_the_cut_is_redacted_before_cutting(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "SuperSecretValue123")
    monkeypatch.setenv("ARMATURE_REDACT_ENV", "SESSION_SECRET")
    emitter = Emitter("unix:/nonexistent/x.sock", "t")
    recorder = Recorder(redact=emitter.redact)
    parser = ClaudeStreamParser(recorder)
    content = "x" * 2040 + "SuperSecretValue123"
    parser.feed(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": content}]}}))
    [(_, _, payload)] = recorder.events
    assert "SuperSec" not in payload["content"]
    assert payload["content"].startswith("x" * 2040 + "[redacte")
    emitter.close()


def test_garbage_lines_ignored_and_truncated_stream_ends_session():
    events = _parse([
        "not json",
        "{broken",
        "[1,2]",
        '{"type":"system","subtype":"init","session_id":"s","model":"m"}',
        '{"type":"unknown"}',
    ])
    assert [k for k, _, _ in events] == ["session_start", "session_end"]
    assert events[-1][2]["status"] == "incomplete"


def test_pipe_copies_every_line_unchanged():
    raw = FIXTURE.read_bytes() + b"\xff\xfe not utf8\n" + b"last line without newline"
    out = io.BytesIO()
    recorder = Recorder()
    pipe(recorder, "x", io.BytesIO(raw), out)
    assert out.getvalue() == raw
    assert recorder.events[-1][0] == "session_end"


def test_pipe_keeps_reading_after_stdout_closes():
    class Closed(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError

    recorder = Recorder()
    pipe(recorder, None, io.BytesIO(FIXTURE.read_bytes()), Closed())
    assert recorder.events[-1][0] == "session_end"


def _cli(args, stdin, env, entry=("armature", "emit")):
    return subprocess.run(
        [sys.executable, "-m", *entry, *args],
        input=stdin, capture_output=True, cwd=REPO, env=env, timeout=60, check=False,
    )


def test_cli_stream_claude_passthrough_with_server_down():
    env = {**os.environ, "ARMATURE_EVENTS": f"unix:{tempfile.mkdtemp(prefix='aev')}/none.sock", "ARMATURE_EVENTS_TOKEN": "t"}
    raw = FIXTURE.read_bytes()
    proc = _cli(["--stream-claude", "--step", "lead"], raw, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == raw


def test_cli_stream_claude_sends_events():
    reader = DatagramReader()
    try:
        env = {**os.environ, "ARMATURE_EVENTS": reader.endpoint, "ARMATURE_EVENTS_TOKEN": "t"}
        raw = FIXTURE.read_bytes()
        proc = _cli(["--stream-claude", "--step", "lead"], raw, env)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == raw
        events = reader.wait_for(9)
        assert [e["kind"] for e in events] == [
            "session_start", "text", "tool_call", "tool_result", "tool_call",
            "tool_result", "text", "cost", "session_end",
        ]
        assert all(e["step_id"] == "lead" for e in events)
        assert "ghp_" not in events[3]["payload"]["content"]
    finally:
        reader.close()


def test_cli_single_event_fields_and_json():
    reader = DatagramReader()
    try:
        env = {**os.environ, "ARMATURE_EVENTS": reader.endpoint, "ARMATURE_EVENTS_TOKEN": "t"}
        proc = _cli(["finding", "--step", "s", "--json", '{"severity":"high"}',
                     "--field", "line=42", "--field", "path=a/b.kt"], b"", env)
        assert proc.returncode == 0, proc.stderr
        [event] = reader.wait_for(1)
        assert event["kind"] == "finding" and event["step_id"] == "s"
        assert event["payload"] == {"severity": "high", "line": 42, "path": "a/b.kt"}
    finally:
        reader.close()


def test_cli_step_defaults_to_the_running_step():
    reader = DatagramReader()
    try:
        env = {**os.environ, "ARMATURE_EVENTS": reader.endpoint, "ARMATURE_EVENTS_TOKEN": "t",
               "ARMATURE_STEP_ID": "hello"}
        proc = _cli(["log", "--field", "message=hi"], b"", env)
        assert proc.returncode == 0, proc.stderr
        [event] = reader.wait_for(1)
        assert event["step_id"] == "hello"
    finally:
        reader.close()


def test_cli_rejects_bad_input():
    env = {k: v for k, v in os.environ.items() if k != "ARMATURE_EVENTS"}
    assert _cli(["finding", "--json", "[1]"], b"", env).returncode != 0
    assert _cli(["finding", "--field", "novalue"], b"", env).returncode != 0
    assert _cli([], b"", env).returncode != 0
    assert _cli(["log", "--field", "a=b"], b"", env).returncode == 0


def test_light_entry_point_matches_and_skips_the_engine():
    reader = DatagramReader()
    try:
        env = {**os.environ, "ARMATURE_EVENTS": reader.endpoint, "ARMATURE_EVENTS_TOKEN": "t"}
        proc = _cli(["finding", "--field", "line=7"], b"", env, entry=("armature.events",))
        assert proc.returncode == 0, proc.stderr
        assert reader.wait_for(1)[0]["payload"] == {"line": 7}
        probe = subprocess.run(
            [sys.executable, "-c", (
                "import sys, armature.events.cli; "
                "print('litellm' in sys.modules, 'armature.runtime.engine' in sys.modules)"
            )],
            capture_output=True, text=True, cwd=REPO, timeout=60, check=False,
        )
        assert probe.stdout.split() == ["False", "False"]
    finally:
        reader.close()

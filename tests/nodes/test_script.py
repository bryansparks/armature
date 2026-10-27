import asyncio
import os
import pytest
import subprocess
import time
from armature.nodes.script import ScriptNode
from armature.spec.models import Adapter


def _adapter(cmd, timeout=60, parse=None):
    return Adapter(name="t", type="script", cmd=cmd, timeout=timeout, parse=parse)


async def test_script_node_runs_command():
    node = ScriptNode(adapter=_adapter("echo hello"))
    result = await node.execute({})
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


async def test_script_node_captures_failure():
    node = ScriptNode(adapter=_adapter("exit 1"))
    result = await node.execute({})
    assert result["exit_code"] == 1


async def test_script_node_captures_stderr():
    node = ScriptNode(adapter=_adapter("echo error >&2"))
    result = await node.execute({})
    assert result["exit_code"] == 0
    assert "error" in result["stderr"]


async def test_script_node_result_has_all_keys():
    node = ScriptNode(adapter=_adapter("echo out"))
    result = await node.execute({})
    assert "stdout" in result
    assert "stderr" in result
    assert "exit_code" in result


async def test_script_node_renders_context_in_cmd():
    node = ScriptNode(adapter=_adapter("echo {{ greeting }}"))
    result = await node.execute({"greeting": "world"})
    assert result["exit_code"] == 0
    assert "world" in result["stdout"]


async def test_script_node_no_template_no_rendering():
    node = ScriptNode(adapter=_adapter("echo literal"))
    result = await node.execute({"unused": "value"})
    assert "literal" in result["stdout"]


async def test_script_node_blocks_destructive_command():
    node = ScriptNode(adapter=_adapter("rm -rf /tmp/test_armature_dir"))
    try:
        await node.execute({})
        assert False, "Expected PermissionError"
    except PermissionError as e:
        assert "DESTRUCTIVE" in str(e)


async def test_script_node_blocks_sudo():
    node = ScriptNode(adapter=_adapter("sudo ls"))
    try:
        await node.execute({})
        assert False, "Expected PermissionError"
    except PermissionError:
        pass


async def test_script_node_allows_read_only_command():
    node = ScriptNode(adapter=_adapter("echo allowed"))
    result = await node.execute({})
    assert result["exit_code"] == 0


async def test_script_node_timeout_raises():
    node = ScriptNode(adapter=_adapter("sleep 10", timeout=1))
    try:
        await node.execute({})
        assert False, "Expected TimeoutExpired"
    except subprocess.TimeoutExpired:
        pass


async def test_script_node_injects_armature_context_env():
    import json
    node = ScriptNode(adapter=_adapter("echo $ARMATURE_CONTEXT"))
    result = await node.execute({"greeting": "hello", "count": 3})
    assert result["exit_code"] == 0
    ctx = json.loads(result["stdout"].strip())
    assert ctx["greeting"] == "hello"
    assert ctx["count"] == 3


async def test_script_node_armature_context_is_valid_json():
    import json
    node = ScriptNode(adapter=_adapter("echo $ARMATURE_CONTEXT"))
    result = await node.execute({"nested": {"a": 1}, "items": [1, 2, 3]})
    assert result["exit_code"] == 0
    ctx = json.loads(result["stdout"].strip())
    assert ctx["nested"] == {"a": 1}
    assert ctx["items"] == [1, 2, 3]


# --- parse: json -------------------------------------------------------

async def test_script_node_parse_json_returns_parsed_object():
    node = ScriptNode(adapter=_adapter("echo '{\"selected\": [\"a\", \"b\"]}'", parse="json"))
    result = await node.execute({})
    assert result == {"selected": ["a", "b"]}


async def test_script_node_parse_json_error_contains_stderr_tail():
    node = ScriptNode(adapter=_adapter("echo boom >&2; exit 3", parse="json"))
    try:
        await node.execute({})
        assert False, "Expected RuntimeError"
    except RuntimeError as e:
        assert "exited 3" in str(e)
        assert "boom" in str(e)


async def test_script_node_parse_json_invalid_json_raises():
    node = ScriptNode(adapter=_adapter("echo not-json", parse="json"))
    try:
        await node.execute({})
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "not valid JSON" in str(e)


async def test_script_node_parse_json_requires_top_level_object():
    node = ScriptNode(adapter=_adapter("echo '[1, 2]'", parse="json"))
    try:
        await node.execute({})
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "JSON object" in str(e)


async def test_script_node_parse_json_empty_stdout_raises():
    node = ScriptNode(adapter=_adapter("true", parse="json"))
    try:
        await node.execute({})
        assert False, "Expected ValueError"
    except ValueError:
        pass


# --- async subprocess --------------------------------------------------

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_gone(pid, deadline=2.0):
    for _ in range(int(deadline / 0.05)):
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


def _background_sleep(pidfile):
    return f"sleep 30 & echo $! > {pidfile}; wait"


async def test_script_nodes_run_concurrently():
    nodes = [ScriptNode(adapter=_adapter("sleep 2")) for _ in range(2)]
    start = time.monotonic()
    results = await asyncio.gather(*(n.execute({}) for n in nodes))
    assert time.monotonic() - start < 3.5
    assert all(r["exit_code"] == 0 for r in results)


async def test_script_node_timeout_kills_child(tmp_path):
    pidfile = tmp_path / "pid"
    node = ScriptNode(adapter=_adapter(_background_sleep(pidfile), timeout=1))
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        await node.execute({})
    assert time.monotonic() - start < 5
    assert await _wait_gone(int(pidfile.read_text()))


async def test_script_node_cancel_kills_child(tmp_path):
    pidfile = tmp_path / "pid"
    node = ScriptNode(adapter=_adapter(_background_sleep(pidfile)))
    task = asyncio.create_task(node.execute({}))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.02)
    assert pidfile.exists() and pidfile.read_text().strip()
    pid = int(pidfile.read_text())
    assert _pid_alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_gone(pid)


async def test_script_node_nonzero_exit_keeps_stderr():
    node = ScriptNode(adapter=_adapter("echo err >&2; exit 3"))
    result = await node.execute({})
    assert result["exit_code"] == 3
    assert result["stderr"].strip() == "err"


async def test_script_node_drains_large_output_on_both_streams():
    cmd = "head -c 200000 /dev/zero | tr '\\0' x; head -c 200000 /dev/zero | tr '\\0' y >&2"
    node = ScriptNode(adapter=_adapter(cmd))
    result = await node.execute({})
    assert result["exit_code"] == 0
    assert len(result["stdout"]) == 200000
    assert len(result["stderr"]) == 200000


async def test_script_node_replaces_invalid_utf8():
    node = ScriptNode(adapter=_adapter("printf '\\377'"))
    result = await node.execute({})
    assert "\ufffd" in result["stdout"]


def _recorder():
    calls = []

    def emit(kind, step_id=None, /, **payload):
        calls.append((kind, step_id, payload))

    return calls, emit


def _messages(calls):
    return [p["message"] for _, _, p in calls]


async def test_stderr_lines_become_ordered_log_events():
    calls, emit = _recorder()
    cmd = "echo one >&2; echo >&2; printf '\\033[31mtwo\\033[0m\\n' >&2; echo '{\"ok\": 1}'"
    node = ScriptNode(adapter=_adapter(cmd, parse="json"), emit=emit, step_id="build")
    result = await node.execute({})
    assert result == {"ok": 1}
    assert calls == [
        ("log", "build", {"stream": "stderr", "message": "one"}),
        ("log", "build", {"stream": "stderr", "message": "two"}),
    ]


async def test_stderr_envelope_unchanged_when_streaming():
    calls, emit = _recorder()
    node = ScriptNode(adapter=_adapter("echo a >&2; echo b >&2; echo out; exit 2"), emit=emit, step_id="s")
    result = await node.execute({})
    assert result == {"stdout": "out\n", "stderr": "a\nb\n", "exit_code": 2}


async def test_long_stderr_line_is_truncated():
    calls, emit = _recorder()
    node = ScriptNode(adapter=_adapter("head -c 3000 /dev/zero | tr '\\0' x >&2; echo >&2"), emit=emit, step_id="s")
    result = await node.execute({})
    assert len(result["stderr"]) == 3001
    assert _messages(calls) == ["x" * 2000]


async def test_chatty_stderr_is_rate_limited():
    calls, emit = _recorder()
    node = ScriptNode(adapter=_adapter("for i in $(seq 1 100); do echo line$i >&2; done"), emit=emit, step_id="s")
    await node.execute({})
    messages = _messages(calls)
    lines = [m for m in messages if m.startswith("line")]
    skipped = [int(m.split()[0]) for m in messages if m.endswith("lines skipped")]
    assert lines[:20] == [f"line{i}" for i in range(1, 21)]
    assert skipped
    assert len(lines) + sum(skipped) == 100


async def test_partial_stderr_line_flushes_after_a_second():
    calls, emit = _recorder()
    node = ScriptNode(adapter=_adapter("printf 'working' >&2; sleep 1.6; echo ' done' >&2"), emit=emit, step_id="s")
    start = time.monotonic()
    task = asyncio.create_task(node.execute({}))
    while not calls and time.monotonic() - start < 1.5:
        await asyncio.sleep(0.02)
    assert _messages(calls) == ["working"]
    result = await task
    assert _messages(calls) == ["working", " done"]
    assert result["stderr"] == "working done\n"


async def test_large_partial_stderr_flushes_at_4kb():
    calls, emit = _recorder()
    cmd = "head -c 5000 /dev/zero | tr '\\0' x >&2; sleep 0.3; echo y >&2"
    node = ScriptNode(adapter=_adapter(cmd), emit=emit, step_id="s")
    result = await node.execute({})
    assert len(result["stderr"]) == 5002
    assert _messages(calls)[0] == "x" * 2000
    assert len(calls) >= 2


async def test_streaming_timeout_still_kills_child(tmp_path):
    calls, emit = _recorder()
    pidfile = tmp_path / "pid"
    node = ScriptNode(adapter=_adapter("echo started >&2; " + _background_sleep(pidfile), timeout=1),
                      emit=emit, step_id="s")
    with pytest.raises(subprocess.TimeoutExpired):
        await node.execute({})
    assert _messages(calls) == ["started"]
    assert await _wait_gone(int(pidfile.read_text()))


async def test_no_emitter_streams_nothing(monkeypatch):
    async def fail(*_):
        raise AssertionError("tailed stderr without an emitter")

    monkeypatch.setattr(ScriptNode, "_tail_stderr", fail)
    node = ScriptNode(adapter=_adapter("echo err >&2"))
    result = await node.execute({})
    assert result["stderr"] == "err\n"

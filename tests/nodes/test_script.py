import pytest
import subprocess
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

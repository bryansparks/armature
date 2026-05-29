"""Unit tests for built-in tool handlers."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── file_read ──────────────────────────────────────────────────────────────────

async def test_file_read_returns_content(tmp_path):
    from armature.registry.builtins import _file_read
    f = tmp_path / "hello.txt"
    f.write_text("hello world")
    result = await _file_read({"path": str(f)})
    assert result["content"] == "hello world"


async def test_file_read_missing_returns_error(tmp_path):
    from armature.registry.builtins import _file_read
    result = await _file_read({"path": str(tmp_path / "nope.txt")})
    assert "error" in result
    assert "nope.txt" in result["error"]


async def test_file_read_utf8_content(tmp_path):
    from armature.registry.builtins import _file_read
    f = tmp_path / "uni.txt"
    f.write_text("héllo wörld", encoding="utf-8")
    result = await _file_read({"path": str(f)})
    assert result["content"] == "héllo wörld"


async def test_file_read_multiline(tmp_path):
    from armature.registry.builtins import _file_read
    f = tmp_path / "multi.txt"
    f.write_text("line1\nline2\nline3")
    result = await _file_read({"path": str(f)})
    assert result["content"].count("\n") == 2


# ── file_write ─────────────────────────────────────────────────────────────────

async def test_file_write_creates_file(tmp_path):
    from armature.registry.builtins import _file_write
    target = tmp_path / "out.txt"
    result = await _file_write({"path": str(target), "content": "written"})
    assert target.read_text() == "written"
    assert result["written"] == str(target)


async def test_file_write_creates_parent_dirs(tmp_path):
    from armature.registry.builtins import _file_write
    target = tmp_path / "a" / "b" / "c" / "file.txt"
    await _file_write({"path": str(target), "content": "deep"})
    assert target.exists()
    assert target.read_text() == "deep"


async def test_file_write_overwrites_existing(tmp_path):
    from armature.registry.builtins import _file_write
    target = tmp_path / "file.txt"
    target.write_text("old content")
    await _file_write({"path": str(target), "content": "new content"})
    assert target.read_text() == "new content"


async def test_file_write_returns_written_path(tmp_path):
    from armature.registry.builtins import _file_write
    target = tmp_path / "result.txt"
    result = await _file_write({"path": str(target), "content": "x"})
    assert "written" in result
    assert result["written"] == str(target)


# ── shell_run ──────────────────────────────────────────────────────────────────

async def test_shell_run_echo():
    from armature.registry.builtins import _shell_run
    result = await _shell_run({"cmd": "echo hello"})
    assert result["stdout"].strip() == "hello"
    assert result["exit_code"] == 0


async def test_shell_run_failing_command():
    from armature.registry.builtins import _shell_run
    result = await _shell_run({"cmd": "exit 1"})
    assert result["exit_code"] == 1


async def test_shell_run_stderr_captured():
    from armature.registry.builtins import _shell_run
    result = await _shell_run({"cmd": "echo error >&2"})
    assert "error" in result["stderr"]


async def test_shell_run_returns_all_keys():
    from armature.registry.builtins import _shell_run
    result = await _shell_run({"cmd": "true"})
    assert "stdout" in result
    assert "stderr" in result
    assert "exit_code" in result


# ── http_get ───────────────────────────────────────────────────────────────────

async def test_http_get_returns_status_and_body():
    from armature.registry.builtins import _http_get

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = '{"ok": true}'

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await _http_get({"url": "http://example.com"})

    assert result["status"] == 200
    assert result["body"] == '{"ok": true}'


async def test_http_get_passes_url():
    from armature.registry.builtins import _http_get

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "not found"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await _http_get({"url": "http://myhost/path"})

    call_url = mock_client.get.call_args[0][0]
    assert call_url == "http://myhost/path"


# ── http_post ──────────────────────────────────────────────────────────────────

async def test_http_post_returns_status_and_body():
    from armature.registry.builtins import _http_post

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = '{"id": "img-123"}'

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await _http_post({"url": "http://api.example.com/generate"})

    assert result["status"] == 200
    assert result["body"] == '{"id": "img-123"}'


async def test_http_post_passes_url():
    from armature.registry.builtins import _http_post

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.text = "{}"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await _http_post({"url": "http://myhost/v1/images"})

    call_url = mock_client.post.call_args[0][0]
    assert call_url == "http://myhost/v1/images"


async def test_http_post_sends_json_body():
    from armature.registry.builtins import _http_post

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "{}"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await _http_post({"url": "http://api/run", "body": {"model": "dall-e-3", "prompt": "a pretzel"}})

    _, kwargs = mock_client.post.call_args
    assert kwargs.get("json") == {"model": "dall-e-3", "prompt": "a pretzel"}


async def test_http_post_sends_headers():
    from armature.registry.builtins import _http_post

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "{}"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await _http_post({
            "url": "http://api/run",
            "headers": {"Authorization": "Bearer sk-test", "Content-Type": "application/json"},
        })

    _, kwargs = mock_client.post.call_args
    assert kwargs.get("headers", {}).get("Authorization") == "Bearer sk-test"


async def test_http_post_non_200_returned_as_is():
    from armature.registry.builtins import _http_post

    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.text = '{"error": "invalid prompt"}'

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await _http_post({"url": "http://api/run"})

    assert result["status"] == 422
    assert "invalid prompt" in result["body"]


async def test_http_post_no_body_sends_no_json():
    from armature.registry.builtins import _http_post

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "{}"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await _http_post({"url": "http://api/ping"})

    _, kwargs = mock_client.post.call_args
    assert kwargs.get("json") is None


# ── register_builtins ──────────────────────────────────────────────────────────

def test_register_builtins_registers_expected_tools():
    from armature.registry.registry import ToolRegistry
    from armature.registry.builtins import register_builtins

    registry = ToolRegistry()
    register_builtins(registry)
    names = {d["name"] for d in registry.descriptors()}
    assert "file_read" in names
    assert "file_write" in names
    assert "shell" in names
    assert "http_get" in names
    assert "http_post" in names


def test_register_builtins_tool_count():
    from armature.registry.registry import ToolRegistry
    from armature.registry.builtins import register_builtins

    registry = ToolRegistry()
    register_builtins(registry)
    assert len(registry.descriptors()) == 5


# ── Reversibility metadata ─────────────────────────────────────────────────────

def test_file_read_reversibility_full():
    from armature.registry.registry import ToolRegistry
    from armature.registry.builtins import register_builtins
    from armature.permissions.permissions import Reversibility
    registry = ToolRegistry()
    register_builtins(registry)
    assert registry.get("file_read").reversibility == Reversibility.FULL


def test_file_write_reversibility_partial():
    from armature.registry.registry import ToolRegistry
    from armature.registry.builtins import register_builtins
    from armature.permissions.permissions import Reversibility
    registry = ToolRegistry()
    register_builtins(registry)
    assert registry.get("file_write").reversibility == Reversibility.PARTIAL


def test_shell_reversibility_none():
    from armature.registry.registry import ToolRegistry
    from armature.registry.builtins import register_builtins
    from armature.permissions.permissions import Reversibility
    registry = ToolRegistry()
    register_builtins(registry)
    assert registry.get("shell").reversibility == Reversibility.NONE


def test_http_get_reversibility_full():
    from armature.registry.registry import ToolRegistry
    from armature.registry.builtins import register_builtins
    from armature.permissions.permissions import Reversibility
    registry = ToolRegistry()
    register_builtins(registry)
    assert registry.get("http_get").reversibility == Reversibility.FULL


def test_http_post_reversibility_none():
    from armature.registry.registry import ToolRegistry
    from armature.registry.builtins import register_builtins
    from armature.permissions.permissions import Reversibility
    registry = ToolRegistry()
    register_builtins(registry)
    assert registry.get("http_post").reversibility == Reversibility.NONE



def test_tool_descriptor_default_reversibility_is_full():
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel, Reversibility

    async def _noop(args): return {}
    desc = ToolDescriptor(name="x", description="x", permission=PermissionLevel.READ_ONLY, handler=_noop)
    assert desc.reversibility == Reversibility.FULL

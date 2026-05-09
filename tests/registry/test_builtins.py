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
    assert "quorum.deliberate" in names
    assert "tessera.retrieve" in names
    assert "alembic.submit" in names


def test_register_builtins_tool_count():
    from armature.registry.registry import ToolRegistry
    from armature.registry.builtins import register_builtins

    registry = ToolRegistry()
    register_builtins(registry)
    assert len(registry.descriptors()) == 7

"""TDD RED tests for Sandbox Isolation feature.

All production imports are deferred inside test functions so that collection
succeeds even before the production code exists. Every test in this file is
expected to FAIL until the feature is implemented.
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry_with_builtins():
    """Return a ToolRegistry pre-loaded with the three sandbox-wrapped tools."""
    from armature.registry.registry import ToolRegistry, ToolDescriptor
    from armature.permissions.permissions import PermissionLevel

    registry = ToolRegistry()

    async def _shell(args):
        return {"stdout": "", "stderr": "", "exit_code": 0}

    async def _file_write(args):
        return {"written": args.get("path", "")}

    async def _file_read(args):
        return {"content": ""}

    for name, handler, perm in [
        ("shell", _shell, PermissionLevel.WORKSPACE),
        ("file_write", _file_write, PermissionLevel.WORKSPACE),
        ("file_read", _file_read, PermissionLevel.READ_ONLY),
    ]:
        registry.register(ToolDescriptor(
            name=name,
            description=f"Builtin {name}",
            permission=perm,
            handler=handler,
        ))

    return registry


# ---------------------------------------------------------------------------
# 1. SandboxMode enum
# ---------------------------------------------------------------------------

def test_sandbox_mode_enum_exists():
    """SandboxMode must be importable from armature.spec.models."""
    from armature.spec.models import SandboxMode  # noqa: F401


def test_sandbox_mode_has_none_value():
    """SandboxMode.NONE must equal the string 'none'."""
    from armature.spec.models import SandboxMode
    assert SandboxMode.NONE == "none"


def test_sandbox_mode_has_docker_value():
    """SandboxMode.DOCKER must equal the string 'docker'."""
    from armature.spec.models import SandboxMode
    assert SandboxMode.DOCKER == "docker"


# ---------------------------------------------------------------------------
# 2. SandboxConfig model defaults
# ---------------------------------------------------------------------------

def test_sandbox_config_exists():
    """SandboxConfig must be importable from armature.spec.models."""
    from armature.spec.models import SandboxConfig  # noqa: F401


def test_sandbox_config_default_mode_is_none():
    """SandboxConfig() with no args defaults mode to SandboxMode.NONE."""
    from armature.spec.models import SandboxConfig, SandboxMode
    cfg = SandboxConfig()
    assert cfg.mode == SandboxMode.NONE


def test_sandbox_config_default_image():
    """SandboxConfig defaults image to 'python:3.11-slim'."""
    from armature.spec.models import SandboxConfig
    assert SandboxConfig().image == "python:3.11-slim"


def test_sandbox_config_default_timeout():
    """SandboxConfig defaults timeout_s to 300.0."""
    from armature.spec.models import SandboxConfig
    assert SandboxConfig().timeout_s == 300.0


def test_sandbox_config_default_allow_network_is_false():
    """SandboxConfig defaults allow_network to False."""
    from armature.spec.models import SandboxConfig
    assert SandboxConfig().allow_network is False


def test_sandbox_config_default_workspace():
    """SandboxConfig defaults workspace to '/workspace'."""
    from armature.spec.models import SandboxConfig
    assert SandboxConfig().workspace == "/workspace"


def test_sandbox_config_default_host_workspace_is_dot():
    """SandboxConfig defaults host_workspace to '.' (resolved at engine init)."""
    from armature.spec.models import SandboxConfig
    assert SandboxConfig().host_workspace == "."


def test_sandbox_config_host_workspace_round_trips():
    """SandboxConfig stores a custom host_workspace path."""
    from armature.spec.models import SandboxConfig, SandboxMode
    cfg = SandboxConfig(mode=SandboxMode.DOCKER, host_workspace="/tmp/project")
    assert cfg.host_workspace == "/tmp/project"


def test_sandbox_config_default_env_is_empty_dict():
    """SandboxConfig defaults env to an empty dict."""
    from armature.spec.models import SandboxConfig
    cfg = SandboxConfig()
    assert cfg.env == {}


def test_sandbox_config_round_trips_docker_mode():
    """SandboxConfig correctly stores all fields when mode=DOCKER."""
    from armature.spec.models import SandboxConfig, SandboxMode
    cfg = SandboxConfig(
        mode=SandboxMode.DOCKER,
        image="ubuntu:22.04",
        timeout_s=60.0,
        allow_network=True,
        workspace="/data",
        env={"TOKEN": "abc"},
    )
    assert cfg.mode == SandboxMode.DOCKER
    assert cfg.image == "ubuntu:22.04"
    assert cfg.timeout_s == 60.0
    assert cfg.allow_network is True
    assert cfg.workspace == "/data"
    assert cfg.env == {"TOKEN": "abc"}


# ---------------------------------------------------------------------------
# 3. HarnessSpec.sandbox field
# ---------------------------------------------------------------------------

def test_harness_spec_has_sandbox_field():
    """HarnessSpec must expose a .sandbox attribute."""
    from armature.spec.models import HarnessSpec
    spec = HarnessSpec(name="test", stages=[])
    assert hasattr(spec, "sandbox")


def test_harness_spec_sandbox_defaults_to_sandbox_config():
    """HarnessSpec.sandbox defaults to a SandboxConfig instance."""
    from armature.spec.models import HarnessSpec, SandboxConfig
    spec = HarnessSpec(name="test", stages=[])
    assert isinstance(spec.sandbox, SandboxConfig)


def test_harness_spec_sandbox_default_mode_is_none():
    """HarnessSpec.sandbox.mode defaults to SandboxMode.NONE."""
    from armature.spec.models import HarnessSpec, SandboxMode
    spec = HarnessSpec(name="test", stages=[])
    assert spec.sandbox.mode == SandboxMode.NONE


def test_harness_spec_sandbox_parses_docker_config():
    """HarnessSpec correctly parses a sandbox section with mode=docker."""
    from armature.spec.models import HarnessSpec, SandboxMode
    spec = HarnessSpec.model_validate({
        "name": "sandboxed-wf",
        "stages": [],
        "sandbox": {
            "mode": "docker",
            "image": "python:3.11-slim",
            "timeout_s": 300,
            "allow_network": False,
            "workspace": "/workspace",
            "env": {"MY_VAR": "value"},
        },
    })
    assert spec.sandbox.mode == SandboxMode.DOCKER
    assert spec.sandbox.env == {"MY_VAR": "value"}


# ---------------------------------------------------------------------------
# 4. DockerSandboxProvider importability
# ---------------------------------------------------------------------------

def test_docker_sandbox_provider_importable():
    """DockerSandboxProvider must be importable from armature.sandbox.docker."""
    from armature.sandbox.docker import DockerSandboxProvider  # noqa: F401


# ---------------------------------------------------------------------------
# 5. wrap_registry callable
# ---------------------------------------------------------------------------

def test_wrap_registry_is_callable():
    """DockerSandboxProvider.wrap_registry must be a callable attribute."""
    from armature.sandbox.docker import DockerSandboxProvider
    assert callable(DockerSandboxProvider.wrap_registry)


# ---------------------------------------------------------------------------
# 6. wrap_registry replaces shell handler when mode=docker
# ---------------------------------------------------------------------------

def test_wrap_registry_replaces_shell_handler(tmp_path):
    """After wrap_registry(), the 'shell' tool must have a different handler."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    original_shell_handler = registry.get("shell").handler

    sandbox = SandboxConfig(mode=SandboxMode.DOCKER)
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    new_shell_handler = registry.get("shell").handler
    assert new_shell_handler is not original_shell_handler


def test_wrap_registry_replaces_file_write_handler(tmp_path):
    """After wrap_registry(), the 'file_write' tool must have a different handler."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    original = registry.get("file_write").handler

    sandbox = SandboxConfig(mode=SandboxMode.DOCKER)
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    assert registry.get("file_write").handler is not original


def test_wrap_registry_replaces_file_read_handler(tmp_path):
    """After wrap_registry(), the 'file_read' tool must have a different handler."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    original = registry.get("file_read").handler

    sandbox = SandboxConfig(mode=SandboxMode.DOCKER)
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    assert registry.get("file_read").handler is not original


# ---------------------------------------------------------------------------
# 7. Wrapped shell handler calls docker with correct command structure
# ---------------------------------------------------------------------------

async def test_shell_handler_invokes_docker_run(tmp_path):
    """The wrapped shell handler must call 'docker run' with the expected args."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(
        mode=SandboxMode.DOCKER,
        image="python:3.11-slim",
        allow_network=False,
        workspace="/workspace",
    )
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    mock_run = MagicMock(return_value=MagicMock(stdout="ok\n", stderr="", returncode=0))

    with patch("subprocess.run", mock_run):
        await registry.get("shell").handler({"cmd": "echo hello"})

    assert mock_run.called
    invoked_cmd = mock_run.call_args[0][0]
    # The command must be a string or list that contains docker invocation tokens
    cmd_str = invoked_cmd if isinstance(invoked_cmd, str) else " ".join(invoked_cmd)
    assert "docker" in cmd_str
    assert "run" in cmd_str
    assert "python:3.11-slim" in cmd_str
    assert "echo hello" in cmd_str


async def test_shell_handler_mounts_host_workspace(tmp_path):
    """The docker command must bind-mount host_workspace to the container workspace."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(
        mode=SandboxMode.DOCKER,
        image="python:3.11-slim",
        workspace="/workspace",
        allow_network=False,
    )
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    mock_run = MagicMock(return_value=MagicMock(stdout="", stderr="", returncode=0))

    with patch("subprocess.run", mock_run):
        await registry.get("shell").handler({"cmd": "ls"})

    cmd_str = mock_run.call_args[0][0]
    cmd_str = cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str)
    expected_mount = f"{tmp_path}:/workspace"
    assert expected_mount in cmd_str


async def test_shell_handler_uses_rm_flag(tmp_path):
    """The docker command must include --rm so containers are auto-removed."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(mode=SandboxMode.DOCKER, image="python:3.11-slim")
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    mock_run = MagicMock(return_value=MagicMock(stdout="", stderr="", returncode=0))

    with patch("subprocess.run", mock_run):
        await registry.get("shell").handler({"cmd": "pwd"})

    cmd_str = mock_run.call_args[0][0]
    cmd_str = cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str)
    assert "--rm" in cmd_str


# ---------------------------------------------------------------------------
# 8. allow_network=False adds --network none
# ---------------------------------------------------------------------------

async def test_allow_network_false_adds_network_none_flag(tmp_path):
    """When allow_network=False the docker command must include '--network none'."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(
        mode=SandboxMode.DOCKER,
        image="python:3.11-slim",
        allow_network=False,
    )
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    mock_run = MagicMock(return_value=MagicMock(stdout="", stderr="", returncode=0))

    with patch("subprocess.run", mock_run):
        await registry.get("shell").handler({"cmd": "echo test"})

    cmd_str = mock_run.call_args[0][0]
    cmd_str = cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str)
    assert "--network" in cmd_str
    assert "none" in cmd_str


# ---------------------------------------------------------------------------
# 9. allow_network=True omits --network none
# ---------------------------------------------------------------------------

async def test_allow_network_true_omits_network_none_flag(tmp_path):
    """When allow_network=True the docker command must NOT include '--network none'."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(
        mode=SandboxMode.DOCKER,
        image="python:3.11-slim",
        allow_network=True,
    )
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    mock_run = MagicMock(return_value=MagicMock(stdout="", stderr="", returncode=0))

    with patch("subprocess.run", mock_run):
        await registry.get("shell").handler({"cmd": "curl http://example.com"})

    cmd_str = mock_run.call_args[0][0]
    cmd_str = cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str)
    # "--network none" must not appear together
    assert "--network none" not in cmd_str


# ---------------------------------------------------------------------------
# 10. env dict is passed via -e flags
# ---------------------------------------------------------------------------

async def test_env_dict_passed_as_docker_e_flags(tmp_path):
    """Each env var in SandboxConfig.env must appear as a -e KEY=VALUE docker flag."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(
        mode=SandboxMode.DOCKER,
        image="python:3.11-slim",
        allow_network=False,
        env={"MY_VAR": "hello", "ANOTHER": "world"},
    )
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    mock_run = MagicMock(return_value=MagicMock(stdout="", stderr="", returncode=0))

    with patch("subprocess.run", mock_run):
        await registry.get("shell").handler({"cmd": "env"})

    cmd_str = mock_run.call_args[0][0]
    cmd_str = cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str)
    assert "-e" in cmd_str
    assert "MY_VAR=hello" in cmd_str
    assert "ANOTHER=world" in cmd_str


# ---------------------------------------------------------------------------
# 11. file_write handler writes to host_workspace/path (no docker)
# ---------------------------------------------------------------------------

async def test_file_write_writes_to_host_workspace(tmp_path):
    """The wrapped file_write handler writes directly to host_workspace/path."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(mode=SandboxMode.DOCKER, image="python:3.11-slim")
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    result = await registry.get("file_write").handler(
        {"path": "output.txt", "content": "sandbox content"}
    )

    expected = tmp_path / "output.txt"
    assert expected.exists(), "file_write should create the file in host_workspace"
    assert expected.read_text() == "sandbox content"
    assert "written" in result


async def test_file_write_creates_subdirectory(tmp_path):
    """The wrapped file_write handler creates intermediate directories."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(mode=SandboxMode.DOCKER, image="python:3.11-slim")
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    await registry.get("file_write").handler(
        {"path": "subdir/nested/file.txt", "content": "deep"}
    )

    expected = tmp_path / "subdir" / "nested" / "file.txt"
    assert expected.exists()
    assert expected.read_text() == "deep"


# ---------------------------------------------------------------------------
# 12. file_read handler reads from host_workspace/path (no docker)
# ---------------------------------------------------------------------------

async def test_file_read_reads_from_host_workspace(tmp_path):
    """The wrapped file_read handler reads directly from host_workspace/path."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(mode=SandboxMode.DOCKER, image="python:3.11-slim")
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    source = tmp_path / "data.txt"
    source.write_text("container data")

    result = await registry.get("file_read").handler({"path": "data.txt"})

    assert result.get("content") == "container data"


async def test_file_read_missing_file_returns_error(tmp_path):
    """The wrapped file_read handler returns an error dict for missing files."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(mode=SandboxMode.DOCKER, image="python:3.11-slim")
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    result = await registry.get("file_read").handler({"path": "nonexistent.txt"})

    assert "error" in result


# ---------------------------------------------------------------------------
# 13. mode=none leaves handlers untouched
# ---------------------------------------------------------------------------

def test_wrap_registry_mode_none_does_not_replace_shell(tmp_path):
    """When mode=NONE, wrap_registry() must not replace the shell handler."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    original_shell_handler = registry.get("shell").handler

    sandbox = SandboxConfig(mode=SandboxMode.NONE)
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    assert registry.get("shell").handler is original_shell_handler


def test_wrap_registry_mode_none_does_not_replace_file_write(tmp_path):
    """When mode=NONE, wrap_registry() must not replace the file_write handler."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    original = registry.get("file_write").handler

    sandbox = SandboxConfig(mode=SandboxMode.NONE)
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    assert registry.get("file_write").handler is original


def test_wrap_registry_mode_none_does_not_replace_file_read(tmp_path):
    """When mode=NONE, wrap_registry() must not replace the file_read handler."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    original = registry.get("file_read").handler

    sandbox = SandboxConfig(mode=SandboxMode.NONE)
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    assert registry.get("file_read").handler is original


# ---------------------------------------------------------------------------
# 14. Timeout is respected in the docker command
# ---------------------------------------------------------------------------

async def test_shell_handler_passes_timeout_to_subprocess(tmp_path):
    """The wrapped shell handler passes timeout_s to subprocess.run as timeout."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(
        mode=SandboxMode.DOCKER,
        image="python:3.11-slim",
        timeout_s=42.0,
        allow_network=False,
    )
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    mock_run = MagicMock(return_value=MagicMock(stdout="", stderr="", returncode=0))

    with patch("subprocess.run", mock_run):
        await registry.get("shell").handler({"cmd": "sleep 1"})

    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") == 42.0


async def test_shell_handler_returns_stdout_stderr_exit_code(tmp_path):
    """The wrapped shell handler returns stdout, stderr, and exit_code keys."""
    from armature.sandbox.docker import DockerSandboxProvider
    from armature.spec.models import SandboxConfig, SandboxMode

    registry = _make_registry_with_builtins()
    sandbox = SandboxConfig(mode=SandboxMode.DOCKER, image="python:3.11-slim")
    DockerSandboxProvider.wrap_registry(registry, sandbox, tmp_path)

    fake_proc = MagicMock(stdout="hello\n", stderr="", returncode=0)
    mock_run = MagicMock(return_value=fake_proc)

    with patch("subprocess.run", mock_run):
        result = await registry.get("shell").handler({"cmd": "echo hello"})

    assert "stdout" in result
    assert "stderr" in result
    assert "exit_code" in result
    assert result["stdout"] == "hello\n"
    assert result["exit_code"] == 0

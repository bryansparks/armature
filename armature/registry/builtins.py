import subprocess
import httpx
from pathlib import Path
from armature.registry.registry import ToolRegistry, ToolDescriptor, PermissionLevel
from armature.skills import quorum as _quorum_skill
from armature.skills import tessera as _tessera_skill


async def _file_read(args: dict) -> dict:
    path = Path(args["path"])
    if not path.exists():
        return {"error": f"File not found: {path}"}
    return {"content": path.read_text(encoding="utf-8")}


async def _file_write(args: dict) -> dict:
    path = Path(args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    return {"written": str(path)}


async def _shell_run(args: dict) -> dict:
    result = subprocess.run(
        args["cmd"], shell=True, capture_output=True, text=True, timeout=30
    )
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}


async def _http_get(args: dict) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(args["url"], timeout=10)
        return {"status": response.status_code, "body": response.text}


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(ToolDescriptor(
        name="file_read", description="Read a file from disk",
        permission=PermissionLevel.READ_ONLY, handler=_file_read,
        parameters={"path": {"type": "string", "description": "Absolute file path"}},
    ))
    registry.register(ToolDescriptor(
        name="file_write", description="Write content to a file",
        permission=PermissionLevel.WORKSPACE, handler=_file_write,
        parameters={"path": {"type": "string"}, "content": {"type": "string"}},
    ))
    registry.register(ToolDescriptor(
        name="shell", description="Run a shell command",
        permission=PermissionLevel.WORKSPACE, handler=_shell_run,
        parameters={"cmd": {"type": "string", "description": "Shell command to execute"}},
    ))
    registry.register(ToolDescriptor(
        name="http_get", description="Make an HTTP GET request",
        permission=PermissionLevel.NETWORK, handler=_http_get,
        parameters={"url": {"type": "string"}},
    ))
    registry.register(ToolDescriptor(
        name="quorum.deliberate",
        description="Run structured multi-agent deliberation on a topic via Quorum",
        permission=PermissionLevel.NETWORK,
        handler=_quorum_skill.deliberate,
        parameters={
            "topic": {"type": "string"},
            "brief": {"type": "string", "optional": True},
            "agents": {"type": "array", "optional": True},
        },
    ))
    registry.register(ToolDescriptor(
        name="tessera.retrieve",
        description="Retrieve relevant document chunks from Tessera RAG",
        permission=PermissionLevel.NETWORK,
        handler=_tessera_skill.retrieve,
        parameters={
            "query": {"type": "string"},
            "top_k": {"type": "integer", "optional": True},
        },
    ))

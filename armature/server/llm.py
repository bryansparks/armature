"""One structured-output model call on a team's provider, for server features such as the assistant.

The credential comes from the team's secrets and is passed to litellm, or to
the ``claude`` CLI's environment (a subscription, or Claude Code on Bedrock),
for that one call. It never enters the server's own environment.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, desc, select

from armature.server import defs
from armature.server.db.schema import audit, providers, users

TIMEOUT_S = 120
MAX_TOKENS = 8192
NO_KEY = "none"
CLAUDE_BINARY = "claude"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
EMPTY_MCP = json.dumps({"mcpServers": {}})


class LLMError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Completion:
    data: dict[str, Any]
    model: str
    provider_kind: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


def litellm_completion(**kwargs: Any) -> Any:
    import litellm
    return litellm.completion(**kwargs)


def claude_binary() -> str | None:
    return shutil.which(CLAUDE_BINARY)


def team_aliases(conn: Connection, team_id: str) -> list[str]:
    rows = conn.execute(select(providers.c.aliases).where(providers.c.team_id == team_id)).all()
    return sorted({alias for r in rows for alias in (r.aliases or {})})


def resolve_provider(conn: Connection, team_id: str, alias: str) -> tuple[Any, str]:
    """The team's provider that lists ``alias``, oldest first, and the model id the alias names."""
    rows = conn.execute(select(providers).where(providers.c.team_id == team_id)
                        .order_by(providers.c.created_at, providers.c.id)).all()
    for row in rows:
        model = (row.aliases or {}).get(alias)
        if isinstance(model, str) and model:
            return row, model
    raise LLMError(400, f"No provider of this team defines the alias {alias}.")


def _parse_json(text: Any) -> dict[str, Any]:
    from armature.nodes.llm import _extract_json_from_response
    if isinstance(text, dict):
        return text
    data = _extract_json_from_response(str(text or ""))
    if not isinstance(data, dict):
        raise LLMError(502, "The model did not return a JSON object.")
    return data


def _litellm_call(row: Any, model_id: str, credential: str | None, system: str, messages: list[dict[str, Any]],
                  schema: dict[str, Any]) -> Completion:
    from armature.nodes.llm import _response_cost
    config = row.config or {}
    kwargs: dict[str, Any] = {
        "messages": [{"role": "system", "content": system}, *messages],
        "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}},
        "timeout": TIMEOUT_S,
        "max_tokens": MAX_TOKENS,
    }
    if credential:
        kwargs["api_key"] = credential
    elif row.kind in ("openai-compatible", "ollama"):
        kwargs["api_key"] = NO_KEY
    if row.kind == "bedrock":
        model = f"bedrock/{model_id}"
        kwargs["aws_region_name"] = config.get("region")
    elif row.kind == "anthropic":
        model = f"anthropic/{model_id}"
    elif row.kind == "openai-compatible":
        model = f"openai/{model_id}"
        kwargs["api_base"] = config.get("api_base")
    elif row.kind == "ollama":
        model = f"ollama_chat/{model_id}"
        if config.get("api_base"):
            kwargs["api_base"] = config["api_base"]
    else:
        raise LLMError(400, f"A {row.kind} provider can't serve the assistant.")
    try:
        response = litellm_completion(model=model, **kwargs)
    except Exception as exc:
        raise LLMError(502, f"The model call failed: {type(exc).__name__}.") from exc
    usage = getattr(response, "usage", None)
    return Completion(data=_parse_json(response.choices[0].message.content), model=model, provider_kind=row.kind,
                      cost_usd=_response_cost(response), input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                      output_tokens=int(getattr(usage, "completion_tokens", 0) or 0))


def _prompt_text(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(str(m.get("content", "")) for m in messages)


# env -i with a home of its own, so the CLI reads no settings, credentials, or MCP servers of this host's user.
def _claude_call(model_id: str, credentials: dict[str, str], system: str, messages: list[dict[str, Any]],
                 schema: dict[str, Any], provider_kind: str) -> Completion:
    binary = claude_binary()
    if binary is None:
        raise LLMError(503, "The claude CLI is not installed on this server.")
    with tempfile.TemporaryDirectory(prefix="armature-claude-") as home:
        env = {"HOME": home, **credentials, "PATH": f"{os.path.dirname(binary)}:{SYSTEM_PATH}"}
        command = [binary, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
                   "--model", model_id, "--system-prompt", system, "--permission-mode", "manual",
                   "--permission-prompts", "none", "--strict-mcp-config", "--mcp-config", EMPTY_MCP,
                   "--tools", "", "--allowed-tools", "", "--max-turns", "2"]
        try:
            done = subprocess.run(command, input=_prompt_text(messages), capture_output=True, text=True, env=env,
                                  cwd=home, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise LLMError(504, f"The model did not answer within {TIMEOUT_S} seconds.") from exc
    try:
        result = json.loads(done.stdout)
    except ValueError as exc:
        raise LLMError(502, f"The claude CLI failed with exit status {done.returncode}.") from exc
    if done.returncode != 0 or not isinstance(result, dict) or result.get("is_error"):
        raise LLMError(502, f"The claude CLI failed with exit status {done.returncode}.")
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return Completion(data=_parse_json(result.get("structured_output")), model=model_id,
                      provider_kind=provider_kind, cost_usd=float(result.get("total_cost_usd") or 0),
                      input_tokens=int(usage.get("input_tokens") or 0),
                      output_tokens=int(usage.get("output_tokens") or 0))


def bedrock_env(row: Any, token: str) -> dict[str, str]:
    env = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": token}
    region = (row.config or {}).get("region")
    if region:
        env["AWS_REGION"] = region
    return env


@dataclass(frozen=True)
class Call:
    """A resolved provider, model, and credential, so the model call needs no database connection."""

    row: Any
    model_id: str
    credential: str | None = field(repr=False)

    def complete(self, system: str, messages: list[dict[str, Any]], schema: dict[str, Any]) -> Completion:
        if self.row.kind == "claude-subscription":
            if not self.credential:
                raise LLMError(400, "The provider's credential secret is not set.")
            return _claude_call(self.model_id, {"CLAUDE_CODE_OAUTH_TOKEN": self.credential}, system, messages,
                                schema, self.row.kind)
        # Without a bearer token, or without the CLI, Bedrock stays on litellm and the host's AWS chain.
        if self.row.kind == "bedrock" and self.credential and claude_binary() is not None:
            return _claude_call(self.model_id, bedrock_env(self.row, self.credential), system, messages, schema,
                                self.row.kind)
        return _litellm_call(self.row, self.model_id, self.credential, system, messages, schema)


def saved_by_admin(conn: Connection, row: Any) -> bool:
    """Whether the provider's last save was an admin's, or the server's own with no user."""
    last = conn.execute(select(audit.c.actor_id).where(
        audit.c.target_kind == "provider", audit.c.target_id == row.id,
        audit.c.action.in_(("provider.create", "provider.update"))).order_by(desc(audit.c.at)).limit(1)).first()
    if last is None:
        return False
    if last.actor_id is None:
        return True
    return bool(conn.execute(select(users.c.is_admin).where(users.c.id == last.actor_id)).scalar())


# The address may have changed since a non-admin saved it, so it is checked again before every call.
def check_endpoint(conn: Connection, row: Any) -> None:
    if row.kind not in ("openai-compatible", "ollama") or saved_by_admin(conn, row):
        return
    api_base = (row.config or {}).get("api_base")
    if not api_base:
        raise LLMError(400, "An ollama provider with no api_base works only when an admin saved it.")
    try:
        defs._check_public_api_base(api_base)
    except defs.DefError as exc:
        raise LLMError(400, exc.message) from exc


def prepare(conn: Connection, keyring: Any, team_id: str, alias: str) -> Call:
    row, model_id = resolve_provider(conn, team_id, alias)
    if not defs.provider_configured(conn, row):
        raise LLMError(400, "The provider's credential secret is not set.")
    check_endpoint(conn, row)
    return Call(row=row, model_id=model_id, credential=defs.provider_credential(conn, keyring, row))


def complete_json(conn: Connection, keyring: Any, team_id: str, alias: str, system: str,
                  messages: list[dict[str, Any]], schema: dict[str, Any]) -> Completion:
    return prepare(conn, keyring, team_id, alias).complete(system, messages, schema)

"""The server's one-shot model call: provider and alias resolution, litellm kwargs, and the claude CLI's box."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from armature.server import audit, defs, llm
from armature.server import secrets as secret_store
from armature.server.db import new_id
from armature.server.db.schema import providers, teams
from tests.server.app_support import Clock, add_user

SCHEMA = {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]}
MESSAGES = [{"role": "user", "content": "Rename the step."}]


def _team(engine, clock):
    team = new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name=f"team-{team[:8]}", created_at=clock()))
    return team


def _provider(engine, keyring, clock, team, kind, config, aliases, secret=None):
    row_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(providers).values(id=row_id, team_id=team, kind=kind, config=config, aliases=aliases,
                                              created_at=clock(), updated_at=clock()))
        if secret is not None:
            secret_store.set_secret(conn, keyring, team_id=team, name=config["credential_secret"], value=secret,
                                    actor_id=None)
    clock.advance(seconds=1)
    return row_id


class FakeLitellm:
    def __init__(self, *answers):
        self.answers = list(answers) or [{"body": "ok"}]
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        content = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
                               usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
                               _hidden_params={"response_cost": 0.0125})


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    addresses = ["93.184.216.34"]
    monkeypatch.setattr(defs, "resolve_host", lambda host: addresses)
    return addresses


@pytest.fixture
def fake_litellm(monkeypatch):
    fake = FakeLitellm()
    monkeypatch.setattr(llm, "litellm_completion", fake)
    return fake


def _call(engine, keyring, team, alias="lead"):
    with engine.connect() as conn:
        return llm.complete_json(conn, keyring, team, alias, "system", MESSAGES, SCHEMA)


@pytest.mark.parametrize(("kind", "config", "model", "extra"), [
    ("bedrock", {"region": "us-east-1", "credential_secret": "BEDROCK_TOKEN"}, "bedrock/us.anthropic.claude-opus-5-5",
     {"aws_region_name": "us-east-1"}),
    ("anthropic", {"credential_secret": "ANTHROPIC_KEY"}, "anthropic/us.anthropic.claude-opus-5-5", {}),
    ("openai-compatible", {"api_base": "https://llm.example.com/v1", "credential_secret": "OPENAI_KEY"},
     "openai/us.anthropic.claude-opus-5-5", {"api_base": "https://llm.example.com/v1"}),
])
def test_litellm_gets_the_model_string_and_credential_as_kwargs(engine, keyring, fake_litellm, monkeypatch, kind,
                                                                config, model, extra):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setattr(llm, "claude_binary", lambda: None)
    clock = Clock()
    team = _team(engine, clock)
    _provider(engine, keyring, clock, team, kind, config, {"lead": "us.anthropic.claude-opus-5-5"}, secret="s3cret")
    before = dict(os.environ)
    result = _call(engine, keyring, team)
    assert dict(os.environ) == before
    call = fake_litellm.calls[0]
    assert call["model"] == model
    assert call["api_key"] == "s3cret"
    for key, value in extra.items():
        assert call[key] == value
    assert call["messages"][0] == {"role": "system", "content": "system"}
    assert call["response_format"]["json_schema"]["schema"] == SCHEMA
    assert call["max_tokens"] == llm.MAX_TOKENS
    assert result.data == {"body": "ok"}
    assert (result.cost_usd, result.input_tokens, result.output_tokens) == (0.0125, 11, 7)


def test_the_provider_that_lists_the_alias_is_used(engine, keyring, fake_litellm):
    clock = Clock()
    team = _team(engine, clock)
    _provider(engine, keyring, clock, team, "anthropic", {}, {"review": "claude-sonnet"})
    _provider(engine, keyring, clock, team, "openai-compatible", {"api_base": "https://llm.example.com/v1"},
              {"lead": "big-model"})
    _call(engine, keyring, team)
    assert fake_litellm.calls[0]["model"] == "openai/big-model"
    with pytest.raises(llm.LLMError) as missing:
        _call(engine, keyring, team, alias="nope")
    assert missing.value.status == 400


def test_a_provider_without_its_secret_is_refused(engine, keyring, fake_litellm):
    clock = Clock()
    team = _team(engine, clock)
    _provider(engine, keyring, clock, team, "anthropic", {"credential_secret": "ANTHROPIC_KEY"}, {"lead": "m"})
    with pytest.raises(llm.LLMError, match="credential"):
        _call(engine, keyring, team)
    assert fake_litellm.calls == []


def test_a_failed_model_call_is_a_502_without_its_message(engine, keyring, monkeypatch):
    clock = Clock()
    team = _team(engine, clock)
    _provider(engine, keyring, clock, team, "anthropic", {"credential_secret": "K"}, {"lead": "m"}, secret="s3cret")

    def boom(**kwargs):
        raise RuntimeError("bad key s3cret")
    monkeypatch.setattr(llm, "litellm_completion", boom)
    with pytest.raises(llm.LLMError) as failed:
        _call(engine, keyring, team)
    assert failed.value.status == 502 and "s3cret" not in failed.value.message


FAKE_CLAUDE = """#!{python}
import json, os, sys, time
time.sleep({sleep})
with open({record!r}, "w") as out:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "stdin": sys.stdin.read(), "cwd": os.getcwd()}}, out)
print(json.dumps({{"type": "result", "is_error": False, "structured_output": {{"body": "from claude"}},
                  "total_cost_usd": 0.03, "usage": {{"input_tokens": 5, "output_tokens": 4}}}}))
"""


def _fake_claude(tmp_path, monkeypatch, sleep=0):
    record = tmp_path / "claude-call.json"
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir()
    binary.write_text(FAKE_CLAUDE.format(python=sys.executable, sleep=sleep, record=str(record)))
    binary.chmod(0o755)
    monkeypatch.setattr(llm, "claude_binary", lambda: str(binary))
    return record


def test_claude_subscription_runs_the_cli_with_no_inherited_env(engine, keyring, tmp_path, monkeypatch):
    monkeypatch.setenv("PLANTED_SECRET", "do-not-pass")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "server-key")
    record = _fake_claude(tmp_path, monkeypatch)
    clock = Clock()
    team = _team(engine, clock)
    _provider(engine, keyring, clock, team, "claude-subscription", {"credential_secret": "CLAUDE_OAUTH"},
              {"lead": "claude-opus-5-5"}, secret="oauth-token")
    result = _call(engine, keyring, team)
    seen = json.loads(record.read_text())
    env = seen["env"]
    # macOS and Python's locale coercion add these in the child itself.
    for added in ("__CF_USER_TEXT_ENCODING", "LC_CTYPE"):
        env.pop(added, None)
    assert set(env) == {"HOME", "CLAUDE_CODE_OAUTH_TOKEN", "PATH"}
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert "do-not-pass" not in json.dumps(seen) and "server-key" not in json.dumps(seen)
    assert env["HOME"] != os.environ.get("HOME")
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(env["HOME"])
    assert not os.path.exists(env["HOME"])
    argv = seen["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert argv[argv.index("--allowed-tools") + 1] == ""
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--max-turns") + 1] == "2"
    assert "oauth-token" not in argv
    assert seen["stdin"] == "Rename the step."
    assert result.data == {"body": "from claude"}
    assert (result.cost_usd, result.input_tokens, result.output_tokens) == (0.03, 5, 4)


def test_bedrock_runs_claude_code_on_bedrock_with_the_token_and_region(engine, keyring, tmp_path, monkeypatch,
                                                                       fake_litellm):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "host-key")
    record = _fake_claude(tmp_path, monkeypatch)
    clock = Clock()
    team = _team(engine, clock)
    _provider(engine, keyring, clock, team, "bedrock", {"region": "us-west-2", "credential_secret": "BEDROCK_TOKEN"},
              {"lead": "us.anthropic.claude-opus-5-5"}, secret="bedrock-token")
    result = _call(engine, keyring, team)
    seen = json.loads(record.read_text())
    env = seen["env"]
    for added in ("__CF_USER_TEXT_ENCODING", "LC_CTYPE"):
        env.pop(added, None)
    assert set(env) == {"HOME", "PATH", "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_BEARER_TOKEN_BEDROCK"}
    assert (env["CLAUDE_CODE_USE_BEDROCK"], env["AWS_REGION"], env["AWS_BEARER_TOKEN_BEDROCK"]) == (
        "1", "us-west-2", "bedrock-token")
    assert "host-key" not in json.dumps(seen)
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "us.anthropic.claude-opus-5-5"
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    assert argv[argv.index("--max-turns") + 1] == "2"
    assert "bedrock-token" not in argv
    assert fake_litellm.calls == []
    assert result.provider_kind == "bedrock" and result.data == {"body": "from claude"}
    assert (result.cost_usd, result.input_tokens, result.output_tokens) == (0.03, 5, 4)


def test_bedrock_falls_back_to_litellm_without_the_cli_or_a_token(engine, keyring, monkeypatch, fake_litellm):
    monkeypatch.setattr(llm, "claude_binary", lambda: None)
    clock = Clock()
    team = _team(engine, clock)
    _provider(engine, keyring, clock, team, "bedrock", {"region": "us-east-1", "credential_secret": "BEDROCK_TOKEN"},
              {"lead": "us.anthropic.claude-opus-5-5"}, secret="bedrock-token")
    assert _call(engine, keyring, team).provider_kind == "bedrock"
    assert fake_litellm.calls[-1]["model"] == "bedrock/us.anthropic.claude-opus-5-5"
    monkeypatch.setattr(llm, "claude_binary", lambda: "/nonexistent/claude")
    call = llm.Call(row=SimpleNamespace(kind="bedrock", config={"region": "us-east-1"}), model_id="m",
                    credential=None)
    call.complete("system", MESSAGES, SCHEMA)
    assert fake_litellm.calls[-1]["model"] == "bedrock/m" and "api_key" not in fake_litellm.calls[-1]


def test_claude_subscription_times_out(engine, keyring, tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, sleep=5)
    monkeypatch.setattr(llm, "TIMEOUT_S", 0.5)
    clock = Clock()
    team = _team(engine, clock)
    _provider(engine, keyring, clock, team, "claude-subscription", {"credential_secret": "CLAUDE_OAUTH"},
              {"lead": "claude-opus-5-5"}, secret="oauth-token")
    with pytest.raises(llm.LLMError) as slow:
        _call(engine, keyring, team)
    assert slow.value.status == 504


def _saved_by(engine, keyring, clock, provider_id, is_admin):
    user = add_user(engine, keyring, clock, f"saver-{provider_id[:6]}-{int(is_admin)}", is_admin=is_admin)[0]
    with engine.begin() as conn:
        audit.record(conn, action="provider.update", actor_id=user, target_kind="provider", target_id=provider_id)


def test_keyless_openai_and_ollama_get_a_placeholder_key(engine, keyring, fake_litellm):
    clock = Clock()
    team = _team(engine, clock)
    openai = _provider(engine, keyring, clock, team, "openai-compatible", {"api_base": "https://llm.example.com/v1"},
                       {"lead": "big"})
    _call(engine, keyring, team)
    assert fake_litellm.calls[-1]["api_key"] == "none"
    with engine.begin() as conn:
        conn.execute(providers.delete().where(providers.c.id == openai))
    _provider(engine, keyring, clock, team, "ollama", {"api_base": "https://ollama.example.com"}, {"lead": "llama3"})
    _call(engine, keyring, team)
    call = fake_litellm.calls[-1]
    assert call["model"] == "ollama_chat/llama3" and call["api_key"] == "none"
    assert call["api_base"] == "https://ollama.example.com"


def test_a_non_admin_api_base_is_checked_again_at_call_time(engine, keyring, fake_litellm, public_dns):
    clock = Clock()
    team = _team(engine, clock)
    provider = _provider(engine, keyring, clock, team, "openai-compatible",
                         {"api_base": "https://llm.example.com/v1"}, {"lead": "big"})
    _saved_by(engine, keyring, clock, provider, is_admin=False)
    _call(engine, keyring, team)
    public_dns[:] = ["10.0.0.5"]
    with pytest.raises(llm.LLMError) as private:
        _call(engine, keyring, team)
    assert private.value.status == 400 and len(fake_litellm.calls) == 1
    clock.advance(seconds=5)
    _saved_by(engine, keyring, clock, provider, is_admin=True)
    _call(engine, keyring, team)
    assert len(fake_litellm.calls) == 2


def test_ollama_without_api_base_needs_an_admin_save(engine, keyring, fake_litellm):
    clock = Clock()
    team = _team(engine, clock)
    provider = _provider(engine, keyring, clock, team, "ollama", {}, {"lead": "llama3"})
    _saved_by(engine, keyring, clock, provider, is_admin=False)
    with pytest.raises(llm.LLMError, match="admin"):
        _call(engine, keyring, team)
    clock.advance(seconds=5)
    _saved_by(engine, keyring, clock, provider, is_admin=True)
    _call(engine, keyring, team)
    assert "api_base" not in fake_litellm.calls[0]

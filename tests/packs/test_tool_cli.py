"""``armature tool`` on the runner: the broker client, git's credential helper, and the engine's per-step grant."""
from __future__ import annotations

import io
import json
import os
import sys
import textwrap

import httpx
import pytest

from armature.events import sign
from armature.packs import BUILTIN_DIR, catalog, is_builtin
from armature.packs import tool as tool_cli
from armature.runtime.engine import Harness
from armature.spec.loader import load_spec
from tests.events.support import DatagramReader


@pytest.fixture
def server(monkeypatch):
    """A fake tool broker, recording each request."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/api/tools/git-credential"):
            return httpx.Response(200, json={"username": "x-access-token", "password": "ghs_secret"})
        if request.url.path.endswith("/api/tools/slack.post"):
            return httpx.Response(200, json={"posted": True, "ts": "1.2"})
        return httpx.Response(403, json={"error": "nope"})

    transport = httpx.MockTransport(answer)
    real = tool_cli._post
    monkeypatch.setattr(tool_cli, "_post", lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    monkeypatch.setenv(tool_cli.URL_ENV, "https://arm.test/base")
    monkeypatch.setenv(tool_cli.TOKEN_ENV, "atg_grant")
    return seen


def test_the_builtin_packs_load_and_every_cmd_is_the_tool_cli():
    cat = catalog([BUILTIN_DIR])
    assert cat.errors == [] and [p.name for p in cat.packs()] == ["conversation", "github", "jira", "linear",
                                                                  "review", "slack"]
    for pack in cat.packs():
        assert pack.builtin and is_builtin(pack.dir)
        for name, adapter in pack.adapters.items():
            assert adapter.cmd == f"armature tool {pack.name}.{name}"
            assert adapter.writes == (name not in {"checkout", "history", "risk", "marked", "pushable", "get-issue",
                                                        "permission", "pr-diff", "context"})


def test_a_tool_posts_its_with_and_prints_only_the_result(server, monkeypatch, capsys):
    monkeypatch.setenv(tool_cli.WITH_ENV, json.dumps({"channel": "C1", "text": "hi"}))
    assert tool_cli.main(["slack.post"]) == 0
    assert json.loads(capsys.readouterr().out) == {"posted": True, "ts": "1.2"}
    [request] = server
    assert str(request.url) == "https://arm.test/base/api/tools/slack.post"
    assert request.headers["authorization"] == "Bearer atg_grant"
    assert json.loads(request.content) == {"params": {"channel": "C1", "text": "hi"}}


def test_a_refusal_fails_the_step_and_a_bad_name_never_reaches_the_server(server, monkeypatch, capsys):
    monkeypatch.setenv(tool_cli.WITH_ENV, "{}")
    assert tool_cli.main(["jira.comment"]) == 1
    assert "HTTP 403: nope" in capsys.readouterr().err
    assert tool_cli.main(["../runs"]) == 2 and len(server) == 1


def test_without_a_grant_a_tool_says_it_needs_a_server_job(monkeypatch, capsys):
    monkeypatch.delenv(tool_cli.TOKEN_ENV, raising=False)
    monkeypatch.setenv(tool_cli.WITH_ENV, "{}")
    assert tool_cli.main(["slack.post"]) == 1
    assert "only in a server's job" in capsys.readouterr().err


def test_the_credential_helper_answers_get_from_the_server(server):
    out = io.StringIO()
    request = io.StringIO("protocol=https\nhost=github.com\npath=Acme/App.git\n\n")
    assert tool_cli.credential(["write", "get"], stdin=request, stdout=out) == 0
    assert out.getvalue() == "username=x-access-token\npassword=ghs_secret\n"
    assert json.loads(server[0].content) == {"access": "write", "host": "github.com", "path": "Acme/App.git"}
    for args in (["write", "store"], ["write", "erase"]):
        assert tool_cli.credential(args, stdin=io.StringIO("password=x\n\n"), stdout=io.StringIO()) == 0
    assert tool_cli.credential(["read", "get"], stdin=io.StringIO("protocol=http\nhost=x\n\n"),
                               stdout=io.StringIO()) == 0
    assert len(server) == 1


def test_git_runs_with_no_token_in_argv_or_env_and_only_the_helper(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "store")
    argv, env = tool_cli.git_argv("read", "fetch"), tool_cli.git_env()
    helpers = [argv[i + 1] for i, a in enumerate(argv) if a == "-c" and argv[i + 1].startswith("credential.helper")]
    assert helpers == ["credential.helper=",
                       f"credential.helper=!{sys.executable} -m armature tool git-credential read"]
    assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv
    assert not any(k.startswith("GIT_CONFIG_KEY") for k in env) and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_ALLOW_PROTOCOL"] == "https"
    assert "ghs_" not in " ".join(argv) and not any("ghs_" in v for v in env.values())


def test_git_asks_the_helper_and_never_stores_the_token(tmp_path, monkeypatch):
    """A real git credential fill through the helper argv, with the broker call stubbed."""
    stub = tmp_path / "helper.py"
    stub.write_text("import sys\nsys.stdin.read()\nprint('username=x-access-token\\npassword=ghs_from_server')\n")
    monkeypatch.setattr(tool_cli, "helper", lambda access: f"!{sys.executable} {stub}")
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess
    done = subprocess.run(tool_cli.git_argv("read", "credential", "fill"), env=tool_cli.git_env(), text=True,
                          input="protocol=https\nhost=github.com\npath=Acme/App.git\n\n", capture_output=True)
    assert done.returncode == 0 and "password=ghs_from_server" in done.stdout
    assert not list(tmp_path.glob(".git-credentials*"))


def test_push_skips_git_when_the_server_refuses_or_runs_dry(monkeypatch):
    ran = []
    monkeypatch.setattr(tool_cli, "_run", lambda *a: ran.append(a) or "")
    params = {"repo": "Acme/App", "number": 7, "branch": "feature", "commit": "a" * 40}
    monkeypatch.setattr(tool_cli, "call", lambda name, p: {"approved": False, "reason": "fork"})
    assert tool_cli.push(params) == {"pushed": False, "branch": "feature", "reason": "fork"}
    monkeypatch.setattr(tool_cli, "call", lambda name, p: {"dry_run": True})
    assert tool_cli.push(params)["dry_run"] is True and ran == []
    with pytest.raises(tool_cli.ToolFailed):
        tool_cli.push({**params, "branch": "-f"})
    with pytest.raises(tool_cli.ToolFailed):
        tool_cli.push({**params, "dir": ".."})
    with pytest.raises(tool_cli.ToolFailed, match="commit"):
        tool_cli.push({k: v for k, v in params.items() if k != "commit"})
    with pytest.raises(tool_cli.ToolFailed, match="worktree"):
        tool_cli.push({**params, "worktree": "relative/path"})


def _git(*args, cwd=None):
    import subprocess
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd or "/"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def test_push_runs_credentialed_git_only_in_a_bare_repo_it_made(tmp_path, monkeypatch):
    """A work tree's own config and hooks never run or take effect when its HEAD is pushed."""
    marker = tmp_path / "pwned"
    evil = tmp_path / "evil.sh"
    evil.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    evil.chmod(0o755)
    remote = tmp_path / "remote.git"
    _git("init", "--quiet", "--bare", str(remote))
    work = tmp_path / "work" / "src"
    work.mkdir(parents=True)
    _git("init", "--quiet", cwd=work)
    (work / ".gitattributes").write_text("* filter=x\n")
    (work / "a.txt").write_text("a\n")
    _git("add", ".", cwd=work)
    _git("commit", "--quiet", "-m", "one", cwd=work)
    for key, value in (("push.gpgSign", "true"), ("gpg.program", str(evil)), ("http.proxy", "http://127.0.0.1:9"),
                       ("http.sslVerify", "false"), ("filter.x.smudge", str(evil)), ("filter.x.clean", str(evil)),
                       ("core.hooksPath", str(tmp_path / "hooks")), ("core.fsmonitor", str(evil))):
        _git("config", key, value, cwd=work)
    (tmp_path / "hooks").mkdir()
    for hook in ("pre-push", "reference-transaction", "post-update"):
        (tmp_path / "hooks" / hook).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (tmp_path / "hooks" / hook).chmod(0o755)
    head = _git("rev-parse", "HEAD", cwd=work)

    calls = []
    real = tool_cli._run
    monkeypatch.setattr(tool_cli, "_run", lambda argv, env, cwd: calls.append((argv, env)) or real(argv, env, cwd))
    monkeypatch.setattr(tool_cli, "REMOTE_PROTOCOL", "file")
    monkeypatch.setattr(tool_cli, "_url", lambda repo: str(remote))
    monkeypatch.setattr(tool_cli, "call", lambda name, p: {"approved": True, "commit": p["commit"]})
    monkeypatch.setenv(tool_cli.TOKEN_ENV, "atg_grant")
    monkeypatch.chdir(tmp_path / "work")
    out = tool_cli.push({"repo": "Acme/App", "number": 7, "branch": "feature", "dir": "src", "commit": head})

    assert out == {"pushed": True, "branch": "feature", "commit": head}
    assert _git("rev-parse", "refs/heads/feature", cwd=remote) == head
    assert not marker.exists()
    local, credentialed = calls[:-1], calls[-1]
    for argv, env in local:
        assert not any(a.startswith("credential.helper=!") for a in argv)
        assert tool_cli.TOKEN_ENV not in env and env["GIT_ALLOW_PROTOCOL"] == "file"
    argv, env = credentialed
    bare = argv[argv.index("-C") + 1]
    assert "armature-push-" in bare and str(work) not in " ".join(argv)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "core.hooksPath=/dev/null" in argv and "credential.helper=" in argv
    assert not os.path.exists(bare)


def _push_world(tmp_path, monkeypatch, gate=None):
    remote = tmp_path / "remote.git"
    _git("init", "--quiet", "--bare", str(remote))
    work = tmp_path / "worktree"
    work.mkdir()
    _git("init", "--quiet", cwd=work)
    for text in ("one\n", "two\n"):
        (work / "a.txt").write_text(text)
        _git("add", ".", cwd=work)
        _git("commit", "--quiet", "-m", text.strip(), cwd=work)
    monkeypatch.setattr(tool_cli, "REMOTE_PROTOCOL", "file")
    monkeypatch.setattr(tool_cli, "_url", lambda repo: str(remote))
    monkeypatch.setattr(tool_cli, "call", gate or (lambda name, p: {"approved": True, "commit": p["commit"]}))
    monkeypatch.setenv(tool_cli.TOKEN_ENV, "atg_grant")
    return remote, work


def test_push_sends_exactly_the_approved_commit_from_a_worktree(tmp_path, monkeypatch):
    remote, work = _push_world(tmp_path, monkeypatch)
    first = _git("rev-parse", "HEAD~1", cwd=work)
    out = tool_cli.push({"repo": "Acme/App", "number": 7, "branch": "feature", "worktree": str(work),
                         "commit": first})
    assert out == {"pushed": True, "branch": "feature", "commit": first}
    assert _git("rev-parse", "refs/heads/feature", cwd=remote) == first


def test_push_refuses_a_commit_the_source_lacks_or_the_server_did_not_approve(tmp_path, monkeypatch):
    remote, work = _push_world(tmp_path, monkeypatch, gate=lambda name, p: {"approved": True, "commit": "b" * 40})
    head = _git("rev-parse", "HEAD", cwd=work)
    params = {"repo": "Acme/App", "number": 7, "branch": "feature", "worktree": str(work), "commit": head}
    assert tool_cli.push(params)["reason"] == "the server approved a different commit"
    monkeypatch.setattr(tool_cli, "call", lambda name, p: {"approved": True, "commit": p["commit"]})
    assert tool_cli.push({**params, "commit": "c" * 40}) == {
        "pushed": False, "branch": "feature", "reason": "the commit to push is not in the source"}
    import subprocess
    assert subprocess.run(["git", "-C", str(remote), "rev-parse", "--verify", "-q", "refs/heads/feature"],
                          capture_output=True, check=False).returncode != 0


def test_checkout_refuses_a_dir_that_already_holds_a_repository(tmp_path, monkeypatch):
    (tmp_path / "src" / ".git").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tool_cli, "_run", lambda *a: pytest.fail("git ran"))
    with pytest.raises(tool_cli.ToolFailed, match="already holds a repository"):
        tool_cli.checkout({"repo": "Acme/App"})


async def test_only_a_builtin_step_gets_a_grant_and_only_in_its_own_env(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = ("import json,os,sys; print(json.dumps({k: os.environ.get(k) for k in "
             "['ARMATURE_TOOL_TOKEN', 'ARMATURE_TOOLS_URL', 'ARMATURE_WITH', 'ARMATURE_ENGINE_FD']} "
             "| {'argv': sys.argv[1:]}))")
    fake = bin_dir / "armature"
    fake.write_text(f"#!{sys.executable}\n{probe}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(tool_cli.URL_ENV, "https://arm.test")
    monkeypatch.setenv(sign.ENGINE_FD_ENV, str(sign.engine_pipe("run-1:secret")))
    asked = []
    monkeypatch.setattr(tool_cli, "request_grant", lambda url, token, step, tools, engine:
                        asked.append((url, token, step, tools, engine)) or f"atg_{step}")
    path = tmp_path / "wf.yml"
    path.write_text(textwrap.dedent("""
        name: t
        adapters:
          plain: {name: plain, type: script, parse: json, cmd: armature plain}
        stages:
          - id: post
            adapter: slack/post
            with: {channel: C1, text: hi}
          - id: plain
            adapter: plain
            depends_on: [post]
    """))
    reader = DatagramReader()
    try:
        monkeypatch.setenv("ARMATURE_EVENTS", reader.endpoint)
        monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "run-token")
        harness = Harness(load_spec(path), session_dir=tmp_path / "s", traces_db=tmp_path / "t.db", use_cache=False)
        assert sign.ENGINE_FD_ENV not in os.environ
        result = await harness.run({})
    finally:
        reader.close()
    assert asked == [("https://arm.test", "run-token", "post", ["slack.post"], ("run-1", "secret"))]
    assert sign.ENGINE_FD_ENV not in os.environ
    for step in ("post", "plain"):
        assert result[step]["ARMATURE_ENGINE_FD"] is None
    assert result["post"]["ARMATURE_TOOL_TOKEN"] == "atg_post" and result["post"]["argv"] == ["tool", "slack.post"]
    assert json.loads(result["post"]["ARMATURE_WITH"]) == {"channel": "C1", "text": "hi"}
    assert result["plain"]["ARMATURE_TOOL_TOKEN"] is None


def test_a_token_never_goes_over_plain_http_to_another_host(monkeypatch, capsys):
    monkeypatch.setenv(tool_cli.URL_ENV, "http://arm.example")
    monkeypatch.setenv(tool_cli.TOKEN_ENV, "atg_grant")
    monkeypatch.setenv(tool_cli.WITH_ENV, "{}")
    assert tool_cli.main(["slack.post"]) == 1 and "plain http" in capsys.readouterr().err


def test_a_grant_request_is_signed_by_the_engine(monkeypatch):
    seen = []

    def answer(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"token": "atg_x"})
    monkeypatch.setenv("ARMATURE_AGENT_USER", "agent")
    monkeypatch.setattr(tool_cli.time, "time", lambda: 1_700_000_000.5)
    token = tool_cli.request_grant("https://arm.test", "run-token", "push", ["github.push"], ("run-1", "k"),
                                   transport=httpx.MockTransport(answer))
    [body] = seen
    signed = {"kind": "tool_grant", "run_id": "run-1", "step": "push", "tools": ["github.push"], "ts": 1_700_000_000}
    assert token == "atg_x" and body == {**signed, "sig": sign.sign_engine("k", signed), "agent_user": True}


def test_the_engine_key_comes_on_a_pipe_and_never_in_any_environment_block(tmp_path):
    """A step reading its parent's original env block (ps -E, /proc/PPID/environ) finds neither key nor pipe."""
    from armature.runner.process import popen
    engine = tmp_path / "engine.py"
    engine.write_text(textwrap.dedent("""
        import json, os, subprocess, sys
        from armature.events.sign import read_engine_key
        found = read_engine_key()
        if sys.platform == "linux":
            step = "tr '\\\\000' ' ' < /proc/$PPID/environ"
        else:
            step = "ps -E -ww -p $PPID -o command="
        seen = subprocess.run(["/bin/sh", "-c", step], capture_output=True, text=True).stdout
        print(json.dumps({"found": found, "seen": seen, "fds": sorted(int(f) for f in os.listdir("/dev/fd"))}))
    """))
    fake = tmp_path / "fake-armature"
    fake.write_text(f"#!/bin/sh\nexec {sys.executable} {engine}\n")
    fake.chmod(0o755)
    workdir = tmp_path / "job"
    workdir.mkdir()
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": os.getcwd()}
    proc = popen(str(fake), workdir, [], env, engine_key="run-1:s3cret-engine-key")
    proc.wait(60)
    out = json.loads((workdir / "run.log").read_text().splitlines()[-1])
    assert out["found"] == ["run-1", "s3cret-engine-key"]
    assert "ARMATURE_ENGINE_FD=" in out["seen"]
    assert "s3cret-engine-key" not in out["seen"]


def test_under_sudo_the_key_pipe_is_stdin_and_reading_it_leaves_dev_null(tmp_path):
    """sudo closes every descriptor above 2, so the pipe rides on fd 0, and no step may inherit it after."""
    from armature.runner.process import popen
    engine = tmp_path / "engine.py"
    engine.write_text(textwrap.dedent("""
        import json, os, sys
        from armature.events.sign import read_engine_key
        found = read_engine_key()
        print(json.dumps({"found": found, "fd": os.environ.get("ARMATURE_ENGINE_FD"), "stdin": sys.stdin.read()}))
    """))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text("#!/bin/sh\nwhile [ \"$1\" != \"--\" ]; do shift; done\nshift\nexec \"$@\"\n")
    sudo.chmod(0o755)
    fake = tmp_path / "fake-armature"
    fake.write_text(f"#!/bin/sh\nexec {sys.executable} {engine}\n")
    fake.chmod(0o755)
    workdir = tmp_path / "job"
    workdir.mkdir()
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "PYTHONPATH": os.getcwd()}
    proc = popen(str(fake), workdir, [], env, job_user="someone", engine_key="run-1:k")
    proc.wait(60)
    out = json.loads((workdir / "run.log").read_text().splitlines()[-1])
    assert out == {"found": ["run-1", "k"], "fd": None, "stdin": ""}


async def test_a_tool_step_in_a_called_workflow_signs_with_the_root_runs_key(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "armature"
    fake.write_text(f"#!{sys.executable}\nimport json, os\n"
                    "print(json.dumps({'token': os.environ.get('ARMATURE_TOOL_TOKEN')}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(tool_cli.URL_ENV, "https://arm.test")
    monkeypatch.setenv(sign.ENGINE_FD_ENV, str(sign.engine_pipe("root-run:secret")))
    asked = []
    monkeypatch.setattr(tool_cli, "request_grant", lambda url, token, step, tools, engine:
                        asked.append((step, tools, engine)) or f"atg_{step}")
    calls = tmp_path / "calls"
    calls.mkdir()
    (calls / "manifest.json").write_text(json.dumps({"pins": {"reviewer": 1}}))
    (calls / "reviewer@1.yaml").write_text("name: reviewer\nstages:\n  - id: publish\n    adapter: slack/post\n"
                                           "    with: {channel: C1, text: hi}\n")
    path = tmp_path / "wf.yml"
    path.write_text("name: t\nstages:\n  - id: review\n    call: {workflow: reviewer}\n")
    reader = DatagramReader()
    try:
        monkeypatch.setenv("ARMATURE_EVENTS", reader.endpoint)
        monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "run-token")
        result = await Harness(load_spec(path), session_dir=tmp_path / "s", traces_db=tmp_path / "t.db",
                               use_cache=False).run({})
    finally:
        reader.close()
    assert asked == [("publish", ["slack.post"], ("root-run", "secret"))]
    assert result["review"] == {"token": "atg_publish"}


async def test_a_script_step_never_inherits_standard_input(tmp_path):
    from armature.nodes.script import ScriptNode
    from armature.spec.models import Adapter
    read_end, write_end = os.pipe()
    os.write(write_end, b"the-key")
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    os.close(read_end)
    try:
        peek = "import json,sys; print(json.dumps({\"in\": sys.stdin.read()}))"
        node = ScriptNode(Adapter(name="peek", type="script", parse="json", cmd=f"{sys.executable} -c '{peek}'"))
        result = await node.execute({})
    finally:
        os.dup2(saved, 0)
        os.close(saved)
    assert result == {"in": ""}


def test_under_sudo_a_lost_fd_name_still_leaves_stdin_on_dev_null(monkeypatch):
    read_end, write_end = os.pipe()
    os.write(write_end, b"run-1:k")
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    os.close(read_end)
    monkeypatch.setenv("SUDO_USER", "runner")
    monkeypatch.delenv(sign.ENGINE_FD_ENV, raising=False)
    try:
        assert sign.read_engine_key() is None
        assert os.read(0, 100) == b""
    finally:
        os.dup2(saved, 0)
        os.close(saved)

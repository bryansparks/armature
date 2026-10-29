"""``armature tool``: a built-in pack adapter's step, as a thin client of the server's tool broker.

It reads the step's ``with:`` from $ARMATURE_WITH, sends it with the step's
grant from $ARMATURE_TOOL_TOKEN, and prints only the result. Checkout and push
run git here. Git gets its one-repository token from ``armature tool
git-credential``, which asks the server and answers git on stdout, so the
token is never in an argument, the environment, a file, or a log.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from armature.events.emitter import plain_http_to_remote
from armature.events.sign import sign_engine
from armature.nodes.agent import SAFE_GIT
from armature.nodes.harness import AGENT_USER_ENV

TOKEN_ENV = "ARMATURE_TOOL_TOKEN"
URL_ENV = "ARMATURE_TOOLS_URL"
WITH_ENV = "ARMATURE_WITH"
HTTP_TIMEOUT_S = 120.0
GIT_TIMEOUT_S = 540
GIT_HOST = "github.com"
REMOTE_PROTOCOL = "https"
PUSH_REF = "refs/heads/armature-push"
TOOL = re.compile(r"^[a-z]+\.[a-z][a-z-]{0,62}$")
REF = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$")
DIR = re.compile(r"^(?!\.\.?$)[A-Za-z0-9._-]{1,100}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
PUSH_FAILED = "Could not push, most likely because the branch moved. Comment /armature fix to try again."


class ToolFailed(Exception):
    pass


def _post(path: str, body: dict[str, Any], token: str | None, *, url: str | None = None,
          transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    base = url or os.environ.get(URL_ENV)
    if not base or not token:
        raise ToolFailed(f"built-in tools run only in a server's job, which sets {URL_ENV} and {TOKEN_ENV}")
    if plain_http_to_remote(base):
        raise ToolFailed(f"refusing plain http to a non-loopback host: {base}")
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, transport=transport, trust_env=False) as http:
            response = http.post(f"{base.rstrip('/')}{path}", json=body, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise ToolFailed(f"could not reach the server: {exc.__class__.__name__}") from exc
    try:
        found = response.json()
    except ValueError:
        found = None
    if response.status_code >= 400:
        message = found.get("error") if isinstance(found, dict) else None
        raise ToolFailed(f"HTTP {response.status_code}: {message or response.reason_phrase}")
    if not isinstance(found, dict):
        raise ToolFailed("the server did not answer with a JSON object")
    return found


def request_grant(url: str, run_token: str, step: str, tools: list[str], engine: tuple[str, str],
                  transport: httpx.BaseTransport | None = None) -> str:
    """The grant for one step of this run, traded for the run's own token and signed with the engine's key.

    It says whether this runner runs agent sessions as a separate user, since only then may a run with one push.
    """
    run_id, key = engine
    signed = {"kind": "tool_grant", "run_id": run_id, "step": step, "tools": sorted(tools), "ts": int(time.time())}
    body = {**signed, "sig": sign_engine(key, signed), "agent_user": bool(os.environ.get(AGENT_USER_ENV))}
    return str(_post("/api/tools/grants", body, run_token, url=url, transport=transport)["token"])


def call(tool: str, params: dict[str, Any], transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    return _post(f"/api/tools/{tool}", {"params": params}, os.environ.get(TOKEN_ENV), transport=transport)


def helper(access: str) -> str:
    return f"!{shlex.quote(sys.executable)} -m armature tool git-credential {access}"


def git_argv(access: str, *args: str) -> list[str]:
    # SAFE_GIT clears every configured helper first, so this one alone answers.
    return ["git", *SAFE_GIT, "-c", f"credential.helper={helper(access)}", "-c", "credential.useHttpPath=true",
            "-c", "http.followRedirects=false", *args]


def git_env(protocol: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ALLOW_PROTOCOL=protocol or REMOTE_PROTOCOL)
    return env


def _run(argv: list[str], env: dict[str, str], cwd: Path | None) -> str:
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise ToolFailed("git timed out") from exc
    if done.returncode != 0:
        raise ToolFailed(f"git failed: {done.stderr.strip()[-500:]}")
    return done.stdout.strip()


def _git(access: str, *args: str, cwd: Path | None = None) -> str:
    """Git that can ask the server for a token. It runs only in a repository this tool made itself."""
    return _run(git_argv(access, *args), git_env(), cwd)


def _local(*args: str) -> str:
    """Git with no credential helper, no grant in its env, and only local repositories to reach."""
    env = git_env("file")
    env.pop(TOKEN_ENV, None)
    return _run(["git", *SAFE_GIT, *args], env, None)


def _dir(params: dict[str, Any]) -> Path:
    name = params.get("dir") or "src"
    if not DIR.fullmatch(name):
        raise ToolFailed("dir must be one plain directory name")
    return Path(name)


def _url(repo: str) -> str:
    return f"https://{GIT_HOST}/{repo}.git"


def checkout(params: dict[str, Any]) -> dict[str, Any]:
    repo, ref, target = params["repo"], params.get("ref"), _dir(params)
    if ref is not None and not REF.fullmatch(ref):
        raise ToolFailed("ref is not a usable git ref")
    if (target / ".git").exists():
        raise ToolFailed(f"{target} already holds a repository. Check out into a new dir.")
    _git("read", "clone", "--quiet", "--no-checkout", "--", _url(repo), str(target))
    if ref:
        _git("read", "fetch", "--quiet", _url(repo), ref, cwd=target)
        commit = _git("read", "rev-parse", "FETCH_HEAD", cwd=target)
    else:
        commit = _git("read", "rev-parse", "HEAD", cwd=target)
    _git("read", "checkout", "--quiet", "--detach", commit, cwd=target)
    return {"repo": repo, "dir": str(target.resolve()), "commit": commit}


def _source(params: dict[str, Any]) -> Path:
    """The repository the commit is fetched from: an agent session's worktree, else dir under the step's cwd."""
    worktree = params.get("worktree")
    if worktree is None:
        return _dir(params).resolve()
    path = Path(worktree)
    if not path.is_absolute() or not path.is_dir():
        raise ToolFailed("worktree must be an existing absolute directory")
    return path


def push(params: dict[str, Any]) -> dict[str, Any]:
    """Push exactly ``commit`` to the pull request's branch once the server approves it. Never forced."""
    source, branch, wanted = _source(params), params["branch"], params.get("commit")
    if not REF.fullmatch(branch):
        raise ToolFailed("branch is not a usable branch name")
    if not isinstance(wanted, str) or not COMMIT.fullmatch(wanted):
        raise ToolFailed("commit must be the 40-character commit to push")
    gate = call("github.push", params)
    if gate.get("dry_run"):
        return {"pushed": False, "dry_run": True, "branch": branch}
    if not gate.get("approved"):
        return {"pushed": False, "branch": branch, "reason": gate.get("reason")}
    if gate.get("commit") != wanted:
        return {"pushed": False, "branch": branch, "reason": "the server approved a different commit"}
    # The run could write the source's config, so credentialed git runs only in a bare repository made here.
    with tempfile.TemporaryDirectory(prefix="armature-push-") as tmp:
        bare = str(Path(tmp) / "push.git")
        _local("init", "--quiet", "--bare", bare)
        try:
            _local("-C", bare, "fetch", "--quiet", "--no-tags", str(source), f"{wanted}:{PUSH_REF}")
        except ToolFailed:
            return {"pushed": False, "branch": branch, "reason": "the commit to push is not in the source"}
        commit = _local("-C", bare, "rev-parse", PUSH_REF)
        if commit != wanted:
            return {"pushed": False, "branch": branch, "reason": "the source holds a different commit"}
        try:
            _git("write", "-C", bare, "push", "--no-verify", "--quiet", _url(params["repo"]),
                 f"{PUSH_REF}:refs/heads/{branch}")
        except ToolFailed as exc:
            print(str(exc), file=sys.stderr)
            return {"pushed": False, "branch": branch, "commit": commit, "reason": PUSH_FAILED}
    return {"pushed": True, "branch": branch, "commit": commit}


def credential(args: list[str], stdin=None, stdout=None, transport: httpx.BaseTransport | None = None) -> int:
    """Git's credential helper protocol: read the request, answer ``get`` with the server's token, ignore the rest."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    access, action = (args + ["", ""])[:2]
    if action != "get":
        return 0
    fields = {}
    for line in stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        fields[key] = value
    if fields.get("protocol") != "https":
        return 0
    try:
        found = _post("/api/tools/git-credential", {"access": access, "host": fields.get("host"),
                                                    "path": fields.get("path")},
                      os.environ.get(TOKEN_ENV), transport=transport)
    except ToolFailed as exc:
        print(f"armature git-credential: {exc}", file=sys.stderr)
        return 1
    stdout.write(f"username={found['username']}\npassword={found['password']}\n")
    stdout.flush()
    return 0


def main(args: list[str]) -> int:
    if not args:
        print("usage: armature tool <pack>.<adapter> | git-credential <read|write> <action>", file=sys.stderr)
        return 2
    name = args[0]
    if name == "git-credential":
        return credential(args[1:])
    if not TOOL.fullmatch(name):
        print(f"armature tool: {name} is not a tool name", file=sys.stderr)
        return 2
    try:
        params = json.loads(os.environ.get(WITH_ENV) or "{}")
        if not isinstance(params, dict):
            raise ToolFailed(f"{WITH_ENV} must hold a JSON object")
        if name == "github.checkout":
            result = checkout(params)
        elif name == "github.push":
            result = push(params)
        else:
            result = call(name, params)
    except (ToolFailed, ValueError, KeyError) as exc:
        print(f"armature tool {name}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0

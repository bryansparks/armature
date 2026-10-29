"""Record what armature-ops does for each fixture delivery, as the golden ``ops`` block the dry-run tests compare to.

Run by hand when a fixture changes: ``python tests/fixtures/ops/capture.py <armature-ops checkout> <workflow> [case]``.

It copies only ops' scripts, templates, specs, prompts, and output schemas to
a scratch directory, so no ops.env and no credential is ever read. A fixture's
``files`` stand in for ops' config, and its ``repos`` become local git
repositories. It shapes the payload as ops' gateway does, then runs the
workflow's stages in spec order, or only the fixture's ``stages`` from a
recorded ``context``.

``gh``, ``jira``, ``curl``, ``claude``, ``aws``, ``sandbox-exec``, and git's
network commands are fakes that record each call and answer from the
fixture's ``fakes``. Local git runs for real. A role stage is never sent to a
model: its rendered prompt is recorded and its output comes from ``stubs``. A
Claude session is recorded with its prompt, flags, and the files its flags
name, and answers with the fixture's canned result. A dead proxy catches
anything else, so nothing leaves the machine.

The ``ops`` block holds ``effects`` (each recorded call but local git and
Claude, with known Slack and Jira writes normalized), ``sessions`` (role
prompts and Claude sessions), ``outputs`` (each stage's output), and
``result`` (the last stage's output).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from jinja2 import Environment
from jinja2.nativetypes import NativeEnvironment
from ruamel.yaml import YAML

HERE = Path(__file__).parent
SLACK_TS = "1700000000.000100"
COPIED = ("scripts", "templates", "specs", "prompts")
# Output schemas, the only config copied: they hold no secret and no deployment detail.
SCHEMAS = ("team-lead-schema.json", "incremental-schema.json")
# The watchlist reader post_pr_review.py imports. It reads config/teams.json, which a fixture supplies.
CODE = ("gateway/teams.py",)
FAKED = ("gh", "jira", "curl", "claude", "git", "aws", "sandbox-exec", "ssh", "scp")
NETWORK_GIT = ("push", "fetch", "clone", "ls-remote", "pull")
FILE_MAX = 200_000
REV = re.compile(r"\{\{rev:([^:}]+):([^}]+)\}\}")

FAKE = """#!PYTHON
import json, os, re, select, subprocess, sys
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
state = os.path.join(os.environ["HOME"], ".capture")
fakes = json.load(open(os.path.join(state, "fakes.json")))
used_path = os.path.join(state, "used.json")

def git_command(args):
    i = 0
    while i < len(args):
        if args[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i]
    return ""

if name == "git" and git_command(args) not in NETWORK:
    os.execv("/usr/bin/git", ["git", *args])
if name == "sandbox-exec":
    os.execvp(args[2], args[2:])

stdin = ""
if not sys.stdin.isatty() and select.select([sys.stdin], [], [], 0.5)[0]:
    stdin = sys.stdin.read()
files = {}
for a in args:
    if a.startswith("/") and len(a) < 4096 and os.path.isfile(a) and os.path.getsize(a) < FILE_MAX:
        try:
            files[a] = open(a).read()
        except (OSError, UnicodeDecodeError):
            pass
call = {"tool": name, "args": args, "cwd": os.getcwd()}
if stdin:
    call["stdin"] = stdin
if files:
    call["files"] = files
line = " ".join(args)
used = json.load(open(used_path)) if os.path.exists(used_path) else {}
answer = None
for index, entry in enumerate(fakes.get(name, [])):
    key = f"{name}:{index}"
    if entry.get("times") is not None and used.get(key, 0) >= entry["times"]:
        continue
    if re.search(entry.get("match", ""), line, re.S):
        used[key] = used.get(key, 0) + 1
        answer = entry
        break
json.dump(used, open(used_path, "w"))
if answer is None:
    if name == "jira" and fakes.get("_jira_state"):
        jira = fakes["_jira_state"]
        if args[:2] == ["issue", "view"]:
            answer = {"stdout": json.dumps(jira["issue"])}
        elif args[:2] == ["issue", "move"] and args[3].lower() not in [t["name"].lower() for t in jira["transitions"]]:
            answer = {"stdout": "Error: invalid transition", "exit": 1}
        else:
            answer = {}
    elif name == "curl" and any(a.endswith("/chat.postMessage") for a in args):
        data = json.loads(args[args.index("--data") + 1])
        answer = {"stdout": {"ok": True, "channel": data["channel"], "ts": SLACK_TS}}
    elif name == "git":
        answer = {}
    else:
        call["unanswered"] = True
        answer = {"stderr": f"capture: no canned answer for {name} {line[:300]}", "exit": 97}
if name == "claude" and "result" in answer:
    result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "duration_ms": 61000,
              "total_cost_usd": 0.42, "usage": {"input_tokens": 1200, "output_tokens": 800},
              "session_id": "capture-session"}
    result.update(answer["result"] if isinstance(answer["result"], dict) else {"result": answer["result"]})
    answer = {**answer, "stdout": json.dumps({"type": "system", "subtype": "init", "session_id": "capture-session"})
              + "\\n" + json.dumps(result)}
with open(os.path.join(state, "calls.jsonl"), "a") as out:
    out.write(json.dumps(call) + "\\n")
stdout = answer.get("stdout", "")
sys.stdout.write(stdout if isinstance(stdout, str) else json.dumps(stdout))
sys.stderr.write(answer.get("stderr", ""))
sys.exit(answer.get("exit", 0))
"""


def gateway_build_announce(fixture: dict) -> dict | None:
    return fixture["payload"]


def gateway_pr_merged(fixture: dict) -> dict | None:
    """ops gateway/app.py's merge branch of /gh/pr."""
    payload = fixture["payload"]
    pr = payload.get("pull_request") or {}
    if payload.get("action") != "closed" or not pr.get("merged"):
        return None
    return {"repo": (payload.get("repository") or {}).get("full_name"), "platform": fixture.get("platform", ""),
            "number": pr.get("number"), "title": pr.get("title"), "body": pr.get("body") or "",
            "head_ref": (pr.get("head") or {}).get("ref"), "base_ref": (pr.get("base") or {}).get("ref"),
            "merged_by": ((pr.get("merged_by") or {}).get("login")), "html_url": pr.get("html_url")}


def gateway_slack_intake(fixture: dict) -> dict | None:
    """ops gateway/app.py's /slack/events, for a channel message."""
    payload, headers = fixture["payload"], {k.lower(): v for k, v in fixture.get("headers", {}).items()}
    event = payload.get("event") or {}
    channels = (fixture.get("env") or {}).get("SLACK_INTAKE_CHANNEL", "").split()
    if (payload.get("type") == "url_verification" or headers.get("x-slack-retry-num")
            or event.get("type") != "message" or event.get("bot_id") or event.get("subtype")
            or event.get("channel_type") == "im" or event.get("channel") not in channels):
        return None
    return {"channel": event.get("channel"), "user": event.get("user"), "text": event.get("text", ""),
            "ts": event.get("ts"), "thread_ts": event.get("thread_ts")}


GATEWAY = {"build-announce": gateway_build_announce, "pr-merged": gateway_pr_merged,
           "slack-intake": gateway_slack_intake}


def effects(calls: list[dict]) -> list[dict]:
    out = []
    for call in calls:
        args = call.get("args", [])
        data = None
        if call["tool"] == "curl" and "--data" in args:
            try:
                data = json.loads(args[args.index("--data") + 1])
            except ValueError:
                data = None
        if call["tool"] == "curl" and isinstance(data, dict) and any(a.endswith("/chat.postMessage") for a in args):
            out.append({"vendor": "slack", "op": "post",
                        **{k: data[k] for k in ("channel", "text", "thread_ts", "unfurl_links",
                                                "unfurl_media") if k in data}})
        elif call["tool"] == "jira" and args[:2] == ["issue", "view"]:
            continue
        elif call["tool"] == "jira" and args[:2] == ["issue", "move"]:
            made = {"vendor": "jira", "op": "transition", "issue": args[2], "transition": args[3]}
            if "--resolution" in args:
                made["resolution"] = args[args.index("--resolution") + 1]
            out.append(made)
        elif call["tool"] == "jira" and args[:2] == ["issue", "create"]:
            flags = {args[i].lstrip("-"): args[i + 1] for i in range(2, len(args) - 1) if args[i].startswith("--")}
            out.append({"vendor": "jira", "op": "create", **{k: flags[k] for k in ("project", "type", "summary",
                                                                                 "body") if k in flags}})
        elif call["tool"] == "jira" and args[:3] == ["issue", "comment", "add"]:
            out.append({"vendor": "jira", "op": "comment", "issue": args[3], "body": args[4]})
        else:
            out.append({k: v for k, v in call.items() if k not in ("cwd", "files")})
    return out


def example_env(ops: Path) -> dict[str, str]:
    """The JIRA_TRANSITION_ values from ops.env.example, the one config file that holds no secret."""
    found = {}
    for line in (ops / "config/ops.env.example").read_text().splitlines():
        name, eq, value = line.partition("=")
        if eq and name.startswith("JIRA_TRANSITION_"):
            found[name] = value.strip().strip('"')
    return found


def _normalized(value, home: str):
    if isinstance(value, str):
        return value.replace(home, "$OPS_HOME")
    if isinstance(value, list):
        return [_normalized(v, home) for v in value]
    if isinstance(value, dict):
        return {_normalized(k, home): _normalized(v, home) for k, v in value.items()}
    return value


def _repo(path: Path, spec: dict) -> None:
    """A local repository: a base commit, then each of ``commits`` on ``branch``, with origin named for GitHub."""
    path.mkdir(parents=True)
    git = ["/usr/bin/git", "-C", str(path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
           "-c", "commit.gpgsign=false"]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(path), "GIT_AUTHOR_DATE": "2026-10-27T12:00:00",
           "GIT_COMMITTER_DATE": "2026-10-27T12:00:00"}
    subprocess.run([*git, "init", "-q", "-b", spec.get("base", "main")], check=True, env=env)
    for index, commit in enumerate([{"files": spec.get("files", {}), "message": "base"}, *spec.get("commits", [])]):
        if index == 1 and spec.get("branch"):
            subprocess.run([*git, "checkout", "-q", "-b", spec["branch"]], check=True, env=env)
        for name, text in commit["files"].items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            if name.endswith(".sh"):
                target.chmod(0o755)
        subprocess.run([*git, "add", "-A"], check=True, env=env)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", commit["message"]], check=True, env=env)
    if spec.get("remote"):
        subprocess.run([*git, "remote", "add", "origin", f"https://github.com/{spec['remote']}.git"], check=True,
                       env=env)
        base = spec.get("base", "main")
        for ref in {base, spec.get("branch") or base}:
            subprocess.run([*git, "update-ref", f"refs/remotes/origin/{ref}", ref], check=True, env=env)
        subprocess.run([*git, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{base}"],
                       check=True, env=env)
        subprocess.run([*git, "checkout", "-q", base], check=True, env=env)


def _revs(value, home: Path):
    """``{{rev:<repo path>:<rev>}}`` in a fixture string as that local repository's commit id, and ``{{home}}`` as the
    scratch OPS_HOME."""
    if isinstance(value, str):
        value = value.replace("{{home}}", str(home))
        return REV.sub(lambda m: subprocess.run(["/usr/bin/git", "-C", str(home / m[1]), "rev-parse", m[2]],
                                                capture_output=True, text=True, check=True).stdout.strip(), value)
    if isinstance(value, list):
        return [_revs(v, home) for v in value]
    if isinstance(value, dict):
        return {k: _revs(v, home) for k, v in value.items()}
    return value


def _home(ops: Path, root: Path, fixture: dict) -> Path:
    home = root / "home"
    for part in COPIED:
        shutil.copytree(ops / part, home / part, ignore=shutil.ignore_patterns("__pycache__"))
    for part in ("fakebin", ".local/bin", ".capture", "tmp", "logs", "state", "config"):
        (home / part).mkdir(parents=True, exist_ok=True)
    for name in SCHEMAS:
        shutil.copy(ops / "config" / name, home / "config" / name)
    for name in CODE:
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ops / name, home / name)
    # macOS has no coreutils timeout, and no capture runs long, so this one only drops the limit.
    shim = home / "fakebin" / "timeout"
    shim.write_text('#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do case "$1" in -k|-s) shift 2 ;; *) shift ;; esac; done\n'
                    'shift\nexec "$@"\n')
    shim.chmod(0o755)
    for name in FAKED:
        fake = home / "fakebin" / name
        fake.write_text(FAKE.replace("#!PYTHON", f"#!{sys.executable}").replace("NETWORK", repr(NETWORK_GIT))
                        .replace("FILE_MAX", str(FILE_MAX)).replace("SLACK_TS", repr(SLACK_TS)))
        fake.chmod(0o755)
    for name, spec in (fixture.get("repos") or {}).items():
        _repo(home / name, spec)
    for name, content in _revs(fixture.get("files") or {}, home).items():
        target = home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    fakes = _revs(dict(fixture.get("fakes") or {}), home)
    if fixture.get("jira") and "issue" in fixture["jira"]:
        fakes["_jira_state"] = fixture["jira"]
    (home / ".capture/fakes.json").write_text(json.dumps(fakes))
    (home / ".capture/calls.jsonl").write_text("")
    return home


def _skip(stage: dict, context: dict) -> bool:
    test = stage.get("skip_if")
    if not test:
        return False
    return Environment().from_string(test).render(**context).strip() in ("True", "true", "1")


def _run_cmd(cmd: str, env: dict, context: dict, home: Path) -> object:
    done = subprocess.run(["/bin/sh", "-c", cmd], env={**env, "ARMATURE_CONTEXT": json.dumps(context)}, cwd=home,
                          capture_output=True, text=True, timeout=300, check=False, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise RuntimeError(f"exit {done.returncode}: {done.stderr[-3000:]}")
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {"text": done.stdout}


def run(ops: Path, workflow: str, fixture: dict, root: Path) -> dict:
    gateway = GATEWAY.get(workflow)
    trigger = gateway(fixture) if gateway else (fixture.get("context") or {}).get("trigger_payload")
    if gateway and trigger is None:
        return {"effects": [], "result": None}
    home = _home(ops, root, fixture)
    spec = YAML(typ="safe").load((home / f"specs/{workflow}.yml").read_text())
    env = {"PATH": f"{home}/fakebin:/opt/homebrew/bin:{home}/.local/bin:/usr/bin:/bin", "HOME": str(home),
           "TMPDIR": f"{home}/tmp", "LANG": "en_US.UTF-8",
           **{k: "http://127.0.0.1:9" for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
                                                "ALL_PROXY")},
           "JIRA_API_TOKEN": "fake", "JIRA_SERVER": "https://example.atlassian.net", "JIRA_LOGIN": "fake",
           "SLACK_BOT_TOKEN": "xoxb-fake", "AWS_BEARER_TOKEN_BEDROCK": "fake-bedrock", "OPS_HOME": str(home),
           "ARMATURE_VENV": sys.prefix,
           **example_env(ops), **fixture.get("env", {})}
    context: dict = {"trigger_payload": trigger, **{k: v for k, v in _revs(fixture.get("context") or {}, home).items()
                                                    if k != "trigger_payload"}}
    wanted = fixture.get("stages")
    stubs = fixture.get("stubs") or {}
    sessions, outputs, result = [], {}, None
    for stage in spec["stages"]:
        if stage.get("post_run") or (wanted is not None and stage["id"] not in wanted) or _skip(stage, context):
            continue
        if stage.get("role"):
            prompt = Environment().from_string(stage["role"].get("description") or "").render(**context)
            sessions.append({"stage": stage["id"], "kind": "role", "role": stage["role"].get("name"),
                             "type": stage["role"].get("type"), "output_mode": stage.get("output_mode"),
                             "prompt": prompt})
        if stage["id"] in stubs:
            output = stubs[stage["id"]]
        elif stage.get("role"):
            raise SystemExit(f"role stage {stage['id']} needs a stub")
        elif stage.get("fan_out"):
            items = NativeEnvironment().from_string(stage["partition_source"]).render(**context)
            output = [_run_cmd(spec["adapters"][stage["adapter"]]["cmd"], env,
                               {**context, stage["partition_key"]: item}, home) for item in items or []]
        else:
            output = _run_cmd(spec["adapters"][stage["adapter"]]["cmd"], env, context, home)
        context[stage["id"]] = outputs[stage["id"]] = result = output
    calls = [json.loads(line) for line in (home / ".capture/calls.jsonl").read_text().splitlines()]
    unanswered = [c for c in calls if c.get("unanswered")]
    if unanswered:
        raise SystemExit("unanswered calls:\n" + "\n".join(f"  {c['tool']} {' '.join(c['args'])[:300]}"
                                                         for c in unanswered))
    sessions += [{"kind": "claude", **{k: v for k, v in c.items() if k != "tool"}} for c in calls
                 if c["tool"] == "claude"]
    block = {"effects": effects([c for c in calls if c["tool"] != "claude"]), "result": result}
    if workflow not in ("build-announce", "pr-merged"):
        block.update(sessions=sessions, outputs=outputs)
    return _normalized(block, str(home))


def main(ops: Path, workflow: str, only: str | None = None) -> None:
    for path in sorted((HERE / workflow).glob("*.json")):
        if only and path.stem != only:
            continue
        fixture = json.loads(path.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            fixture["ops"] = run(ops, workflow, fixture, Path(tmp).resolve())
        path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
        print(f"{path.name}: {len(fixture['ops']['effects'])} effect(s), "
              f"{len(fixture['ops'].get('sessions', []))} session(s)")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)

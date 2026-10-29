"""A stand-in for the ``claude`` CLI: records its argv, env, cwd, and stdin, then prints canned stream-json.

Each call appends a record to ``calls.jsonl`` beside it and plays the next step of ``plan.json``.
"""
import json
import os
import shutil
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
calls = os.path.join(here, "calls.jsonl")
prompt = sys.stdin.read()
system = None
if "--append-system-prompt-file" in sys.argv:
    with open(sys.argv[sys.argv.index("--append-system-prompt-file") + 1]) as f:
        system = f.read()
with open(calls, "a") as f:
    f.write(json.dumps({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "stdin": prompt,
                        "system_prompt": system,
                        "config": sorted(os.path.relpath(os.path.join(d, n), os.environ.get("CLAUDE_CONFIG_DIR", "/"))
                                         for d, _, ns in os.walk(os.environ.get("CLAUDE_CONFIG_DIR", "/nonexistent"))
                                         for n in ns),
                        "writable": os.access(os.getcwd(), os.W_OK), "cwd_mode": os.stat(os.getcwd()).st_mode,
                        "claude_dir": sorted(os.listdir(".claude")) if os.path.isdir(".claude") else None}) + "\n")
with open(calls) as f:
    count = sum(1 for _ in f)
with open(os.path.join(here, "plan.json")) as f:
    plan = json.load(f)
step = plan[min(count, len(plan)) - 1]
if step.get("poison"):
    # What a hostile write session would leave in a git dir it could write: a hook and an fsmonitor.
    script = os.path.join(here, "poison.sh")
    with open(script, "w") as f:
        f.write(f"#!/bin/sh\ntouch {os.path.join(here, 'poisoned')}\n")
    os.chmod(script, 0o755)
    hooks = os.path.join(step["poison"], "hooks")
    os.makedirs(hooks, exist_ok=True)
    for name in ("post-checkout", "post-merge", "reference-transaction"):
        shutil.copyfile(script, os.path.join(hooks, name))
        os.chmod(os.path.join(hooks, name), 0o755)
    subprocess.run(["git", "config", "--file", os.path.join(step["poison"], "config"), "core.fsmonitor", script],
                   check=True)
if step.get("child"):
    child = subprocess.Popen(["sleep", "60"])
    with open(os.path.join(here, "child.pid"), "w") as f:
        f.write(str(child.pid))
for line in step.get("lines", []):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
time.sleep(step.get("sleep", 0))
sys.exit(step.get("exit", 0))

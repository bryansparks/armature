"""`armature runner`: claim jobs from an Armature server and run them on this host."""
from __future__ import annotations

import os
import pwd
from pathlib import Path

import typer

from armature.runner.serving import (
    DEFAULT_KEY_ENV,
    DEFAULT_KEY_FILE,
    check_job_user,
    check_name,
    parse_labels,
    serve,
)
from armature.runner.serving import fail as _fail

runner_app = typer.Typer(name="runner", help="Claim jobs from an Armature server and run them on this host")


@runner_app.callback(invoke_without_command=True)
def runner(
    server: str = typer.Option(..., "--server", help="The server's address, such as https://armature.example.com"),
    key_env: str = typer.Option(DEFAULT_KEY_ENV, "--key-env",
                                help="Environment variable that holds the runner key. Never pass the key itself"),
    capability: list[str] = typer.Option([], "--capability", "-c",
                                         help="A capability this host has, such as llm, git, or platform:ios. "
                                              "Repeat for each"),
    slots: int = typer.Option(1, "--slots", min=1, max=64, help="Jobs to run at once"),
    name: str | None = typer.Option(None, "--name", help="This runner's name on the Runners page (default: host name)"),
    label: list[str] = typer.Option([], "--label", help="NAME=VALUE shown on the Runners page. Repeat for each"),
    work_dir: Path = typer.Option(Path("~/.armature/runner"), "--work-dir",
                                  help="Where each job's private directory goes"),
    poll: float = typer.Option(5.0, "--poll", min=0.1, help="Seconds between claims when idle"),
    enroll_token_env: str | None = typer.Option(
        None, "--enroll-token-env",
        help="Environment variable that holds a one-time enrollment token, as an AWS environment's runner has. "
             "The runner trades it for its key"),
    key_file: Path = typer.Option(DEFAULT_KEY_FILE, "--key-file",
                                  help="With --enroll-token-env, where the enrolled key is kept, readable only by "
                                       "this user, so a restart doesn't enroll again"),
    job_user: str | None = typer.Option(None, "--job-user",
                                        help="Run each workflow as this OS user through sudo, so it can't read the "
                                             "runner key or other jobs' bundles"),
    agent_user: str | None = typer.Option(None, "--agent-user",
                                          help="With --job-user, run each agent: session as this OS user, so it "
                                               "can't read the run or its secrets. access: write needs it"),
    same_user: bool = typer.Option(False, "--same-user",
                                   help="Run each agent: session as the user running the runner, without sudo. "
                                        "A session can read everything this user can"),
    memory_mb: int = typer.Option(8192, "--memory-mb", min=0,
                                  help="Cap each run's address space at this many MiB on Linux. 0 turns it off. "
                                       "macOS ignores it"),
):
    """Run jobs from the server until stopped with Ctrl-C or SIGTERM."""
    from armature import __version__
    from armature.runner.client import Runner
    from armature.runner.process import armature_binary
    from armature.spec.models import AGENT_HARNESSES, CAPABILITY

    if same_user and (job_user is not None or agent_user is not None):
        _fail("--same-user can't go with --job-user or --agent-user.")
    if job_user is not None:
        if slots > 1:
            _fail("--job-user needs --slots 1: a job's cleanup stops every process of that user, and would stop "
                  "the other jobs too.")
        check_job_user(job_user)
    if agent_user is not None:
        if job_user is None:
            _fail("--agent-user needs --job-user.")
        if agent_user == job_user:
            _fail("--agent-user must be another user than --job-user.")
        try:
            pwd.getpwnam(agent_user)
        except KeyError:
            _fail(f"--agent-user {agent_user!r} is not a user on this host.")
    check_name(name)
    for item in capability:
        if not CAPABILITY.fullmatch(item):
            _fail(f"--capability {item!r} is not a capability. Use names like llm, git, or platform:ios.")
        if item in AGENT_HARNESSES and agent_user is None and not same_user:
            _fail(f"--capability {item} needs --job-user and --agent-user, so a session runs as its own OS user, "
                  "or --same-user.")
    binary = armature_binary()
    if binary is None:
        _fail("The armature command is not on PATH.")
    labels = parse_labels(label)

    def make(client, runner_name, host):
        if same_user:
            typer.echo(f"Warning: agent sessions run as {pwd.getpwuid(os.getuid()).pw_name} and can read "
                       "everything this user can.", err=True)
        return Runner(client, name=runner_name, capabilities=sorted(set(capability)), slots=slots,
                      work_dir=work_dir.expanduser(), binary=binary, labels=labels, version=__version__, host=host,
                      poll=poll, job_user=job_user, agent_user=agent_user, same_user=same_user,
                      memory_mb=memory_mb), None

    serve(make, server=server, key_env=key_env, enroll_token_env=enroll_token_env, key_file=key_file, name=name,
          how=f"with {slots} slot(s)")

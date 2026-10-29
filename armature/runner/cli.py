"""`armature runner`: claim jobs from an Armature server and run them on this host."""
from __future__ import annotations

import grp
import logging
import os
import pwd
import shutil
import signal
import socket
import threading
from pathlib import Path

import typer

runner_app = typer.Typer(name="runner", help="Claim jobs from an Armature server and run them on this host")

DEFAULT_KEY_ENV = "ARMATURE_RUNNER_KEY"
DEFAULT_KEY_FILE = Path("/etc/armature/runner.env")


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _labels(values: list[str]) -> dict[str, str]:
    labels = {}
    for value in values:
        name, sep, text = value.partition("=")
        if not sep or not name:
            _fail(f"--label {value!r} is not NAME=VALUE.")
        labels[name] = text
    return labels


def _stored_key(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _fail(f"Can't read {path}: {exc.strerror}.")
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == DEFAULT_KEY_ENV and value.strip():
            return value.strip()
    return None


def _store_key(path: Path, key: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        os.fchmod(fd, 0o600)
        out.write(f"{DEFAULT_KEY_ENV}={key}\n")


def _enrolled_key(server: str, token_env: str, key_file: Path, name: str) -> str:
    """The key this environment enrolled for: from ``key_file`` after a restart, else by trading the token once."""
    from armature.runner.client import ServerError, enroll

    stored = _stored_key(key_file)
    if stored:
        return stored
    token = os.environ.get(token_env, "").strip()
    if not token:
        _fail(f"Set {token_env} to this environment's enrollment token.")
    try:
        key = enroll(server, token, name)
    except ServerError as exc:
        _fail(f"Enrollment failed: {exc.message}")
    try:
        _store_key(key_file, key)
    except OSError as exc:
        _fail(f"Enrolled, but can't keep the key in {key_file}: {exc.strerror}.")
    return key


def _check_job_user(job_user: str) -> None:
    try:
        pwd.getpwnam(job_user)
        grp.getgrnam(job_user)
    except KeyError:
        _fail(f"--job-user {job_user!r} needs a user and a group of that name on this host.")
    if job_user == pwd.getpwuid(os.getuid()).pw_name:
        _fail("--job-user must be another user than the one running the runner.")
    if shutil.which("sudo") is None:
        _fail("--job-user needs sudo on this host.")


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
    from armature.events.emitter import plain_http_to_remote
    from armature.runner.client import Client, Runner, RunnerLocked
    from armature.runner.process import armature_binary
    from armature.spec.models import AGENT_HARNESSES, CAPABILITY

    if not server.startswith(("https://", "http://")):
        _fail("--server is an http:// or https:// address.")
    if plain_http_to_remote(server):
        _fail("--server must use https unless it is on this host, so the key and secrets never travel in clear.")
    if job_user is not None:
        if slots > 1:
            _fail("--job-user needs --slots 1: a job's cleanup stops every process of that user, and would stop "
                  "the other jobs too.")
        _check_job_user(job_user)
    if same_user and (job_user is not None or agent_user is not None):
        _fail("--same-user can't go with --job-user or --agent-user.")
    if agent_user is not None:
        if job_user is None:
            _fail("--agent-user needs --job-user.")
        if agent_user == job_user:
            _fail("--agent-user must be another user than --job-user.")
        try:
            pwd.getpwnam(agent_user)
        except KeyError:
            _fail(f"--agent-user {agent_user!r} is not a user on this host.")
    if name is not None and (not name.strip() or "/" in name or name.strip() in (".", "..")):
        _fail("--name can't be empty, contain '/', or be '.' or '..'.")
    for item in capability:
        if not CAPABILITY.fullmatch(item):
            _fail(f"--capability {item!r} is not a capability. Use names like llm, git, or platform:ios.")
        if item in AGENT_HARNESSES and agent_user is None and not same_user:
            _fail(f"--capability {item} needs --job-user and --agent-user, so a session runs as its own OS user, "
                  "or --same-user.")
    binary = armature_binary()
    if binary is None:
        _fail("The armature command is not on PATH.")
    host = socket.gethostname()
    key = os.environ.get(key_env, "").strip()
    if not key and enroll_token_env:
        key = _enrolled_key(server, enroll_token_env, key_file.expanduser(), name or host)
    if not key:
        _fail(f"Set {key_env} to a runner key. Create one on the server's Runner keys page.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = Client(server, key)
    job_runner = Runner(client, name=name or host, capabilities=sorted(set(capability)), slots=slots,
                        work_dir=work_dir.expanduser(), binary=binary, labels=_labels(label),
                        version=__version__, host=host, poll=poll, job_user=job_user, agent_user=agent_user,
                        same_user=same_user, memory_mb=memory_mb)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if same_user:
        typer.echo(f"Warning: agent sessions run as {pwd.getpwuid(os.getuid()).pw_name} and can read everything "
                   "this user can.", err=True)
    typer.echo(f"Runner {job_runner.name} polling {server} with {slots} slot(s). Ctrl-C stops it.")
    try:
        job_runner.run(stop)
    except RunnerLocked as exc:
        _fail(str(exc))
    finally:
        client.close()

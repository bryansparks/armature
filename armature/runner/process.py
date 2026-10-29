"""Starting and stopping ``armature run`` in its own session, shared by the server's worker and ``armature runner``.

Nothing here imports the server, so a runner host needs only the base install.
"""
from __future__ import annotations

import grp
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from armature.events.sign import ENGINE_FD_ENV, engine_pipe
from armature.nodes.gate import CHECKPOINT_FILE
from armature.spec.models import ROLE_KEY, SKILL_NAME

KILL_GRACE_S = 5
LOG_TAIL = 1500
WORKFLOW_FILE = "workflow.yaml"
LOG_FILE = "run.log"
CALLS_DIR = "calls"
CALL_FILE = re.compile(r"^(manifest\.json|[A-Za-z0-9][A-Za-z0-9._-]{0,127}@[1-9][0-9]{0,8}\.yaml)$")
# 2 added the call bundle: calls/manifest.json and calls/<workflow>@<version>.yaml.
# 3 added the checkpoint a run resumes from, and pausing at a gate.
# 4 added roles/<key>.md, the role documents agent steps name.
BUNDLE_VERSION = 4
SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"
ROLES_DIR = "roles"
SHARED_PREFIX = "armature-job-"
AGENT_PREFIX = "armature-agent-"
SUDO = "sudo"
SESSIONS_DIR = "sessions"


def armature_binary() -> str | None:
    beside = Path(sys.executable).parent / "armature"
    return str(beside) if beside.is_file() else shutil.which("armature")


def job_path(binary: str | None) -> str:
    """The host's PATH with the running armature's directory first, so a step's `armature tool` finds it."""
    host = os.environ.get("PATH", "/usr/bin:/bin")
    folder = os.path.dirname(binary) if binary else ""
    return f"{folder}:{host}" if folder else host


# macOS refuses to lower RLIMIT_AS or RLIMIT_DATA from unlimited, so a memory cap applies on Linux only.
MEMORY_LIMIT = None if sys.platform == "darwin" else resource.RLIMIT_AS


def popen(binary: str, workdir: Path, args: list[str], env: dict[str, str], *, job_user: str | None = None,
          run_dir: Path | None = None, engine_key: str | None = None,
          memory_mb: int | None = None) -> subprocess.Popen:
    """Start ``armature run`` on the workflow file in ``run_dir`` (default: the workdir), logging to the workdir.

    With ``job_user`` the run starts through sudo as that user. Only the variables' names reach the command line.
    ``engine_key`` reaches the engine on a pipe, never in the environment. sudo closes every descriptor but the
    first three, so under sudo the pipe is standard input.
    With ``memory_mb`` the process can't grow past that many MiB, where the platform can enforce it.
    """
    run_dir = run_dir or workdir
    pipe = engine_pipe(engine_key) if engine_key else None
    if pipe is not None:
        env = {**env, ENGINE_FD_ENV: "0" if job_user else str(pipe)}
    command = [binary, "run", str(run_dir / WORKFLOW_FILE), "--quiet", "--no-cache", *args]
    if job_user:
        command = [SUDO, "-n", "-u", job_user, f"--preserve-env={','.join(sorted(env))}", "--", *command]
    stdin = pipe if pipe is not None and job_user else subprocess.DEVNULL
    try:
        with open(workdir / LOG_FILE, "wb") as out:
            return subprocess.Popen(command, env=env, cwd=run_dir, stdin=stdin, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True, pass_fds=() if pipe is None or job_user else (pipe,),
                                    preexec_fn=None if memory_mb is None or MEMORY_LIMIT is None
                                    else _memory_limit(memory_mb))
    finally:
        if pipe is not None:
            os.close(pipe)


def _memory_limit(memory_mb: int):
    def limit() -> None:
        resource.setrlimit(MEMORY_LIMIT, (memory_mb << 20, memory_mb << 20))
    return limit


def share(workdir: Path, job_id: str, job_user: str, base: Path | None = None) -> Path:
    """Copy a bundle's workflow files where the job user can work, and where no other user can read them.

    The bundle directory stays the runner's own. The copy goes to a directory in the job user's group, which the
    runner must belong to.
    """
    group = grp.getgrnam(job_user).gr_gid
    run_dir = (base or Path(tempfile.gettempdir())) / f"{SHARED_PREFIX}{job_id}"
    run_dir.mkdir(mode=0o700)
    os.chown(run_dir, -1, group)
    os.chmod(run_dir, 0o2770)
    checkpoint = workdir / CHECKPOINT_FILE
    for source in [workdir / WORKFLOW_FILE, *sorted(p for p in (workdir / CALLS_DIR).glob("*")
                                                    if CALL_FILE.fullmatch(p.name)),
                   *sorted((workdir / SKILLS_DIR).glob(f"*/{SKILL_FILE}")),
                   *sorted((workdir / ROLES_DIR).glob("*.md")),
                   *([checkpoint] if checkpoint.is_file() else [])]:
        target = run_dir / source.relative_to(workdir)
        for parent in reversed(target.parents):
            if parent.is_relative_to(run_dir) and parent != run_dir and not parent.exists():
                parent.mkdir(mode=0o700)
                os.chown(parent, -1, group)
                os.chmod(parent, 0o2770)
        shutil.copyfile(source, target)
        os.chown(target, -1, group)
        os.chmod(target, 0o640)
    return run_dir


def unshare(run_dir: Path, job_user: str, agent_user: str | None = None, agent_dir: Path | None = None) -> None:
    """Stop whatever the job and agent users left running, then remove the job's directories, as each user."""
    users = [u for u in (agent_user, job_user) if u]
    for user in users:
        subprocess.run([SUDO, "-n", "-u", user, "/bin/kill", "-KILL", "-1"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    for path in [p for p in (run_dir, agent_dir) if p is not None]:
        shutil.rmtree(path, ignore_errors=True)
        for user in users:
            if path.exists():
                subprocess.run([SUDO, "-n", "-u", user, "/bin/rm", "-rf", str(path)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if agent_user:
        # Whatever a session wrote outside its own directories, such as temp files or its real home.
        subprocess.run([SUDO, "-n", "-u", agent_user, "/usr/bin/find", "/tmp", "/var/tmp", "/dev/shm",
                        os.path.expanduser(f"~{agent_user}"), "-mindepth", "1", "-user", agent_user, "-delete"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def stop(proc: subprocess.Popen) -> None:
    """Stop the run's whole process group, politely first."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(KILL_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def stop_sessions(sessions: Path) -> None:
    """Stop the process group of each agent session a same-user run recorded in ``sessions``, politely first.

    It never signals group 0 or 1 or the runner's own group, and never stops processes by user.
    """
    own = os.getpgrp()
    names = [p.name for p in sessions.iterdir()] if sessions.is_dir() and not sessions.is_symlink() else []
    groups = {int(n) for n in names if n.isdigit() and len(n) <= 10} - {0, 1, own}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        groups = {g for g in groups if _signal_group(g, sig)}
        deadline = time.monotonic() + KILL_GRACE_S
        while groups and sig == signal.SIGTERM and time.monotonic() < deadline:
            time.sleep(0.1)
            groups = {g for g in groups if _signal_group(g, 0)}


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-LOG_TAIL:].strip()


def check_bundle_version(version: object) -> None:
    """Refuse a bundle laid out by a newer server than this runner knows. A bundle with no version is 1."""
    if version is None:
        return
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= BUNDLE_VERSION:
        raise ValueError(f"The server sent a job bundle of version {version!r}, and this runner reads up to "
                         f"{BUNDLE_VERSION}. Upgrade armature on the runner host.")


def write_bundle(workdir: Path, workflow: str, calls: dict[str, str], skills: dict[str, str] | None = None,
                 checkpoint: dict[str, Any] | None = None, roles: dict[str, str] | None = None) -> None:
    """Write a job's workflow, its call bundle into ``calls/``, its skills and roles, and the checkpoint a resumed
    run reads, into a directory only we can read."""
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(workdir, 0o700)
    (workdir / WORKFLOW_FILE).write_text(workflow, encoding="utf-8")
    if checkpoint:
        (workdir / CHECKPOINT_FILE).write_text(json.dumps(checkpoint), encoding="utf-8")
    if calls:
        children = workdir / CALLS_DIR
        children.mkdir(mode=0o700, exist_ok=True)
        for name, body in calls.items():
            if not CALL_FILE.fullmatch(name) or ".." in name:
                raise ValueError(f"call bundle file {name!r} is not valid")
            fd = os.open(children / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(body)
    for name, body in (skills or {}).items():
        if not SKILL_NAME.fullmatch(name):
            raise ValueError(f"skill name {name!r} is not valid")
        skill = workdir / SKILLS_DIR / name
        skill.mkdir(mode=0o700, parents=True, exist_ok=True)
        (skill / SKILL_FILE).write_text(body, encoding="utf-8")
    for key, body in (roles or {}).items():
        if not ROLE_KEY.fullmatch(key):
            raise ValueError(f"role {key!r} is not valid")
        (workdir / ROLES_DIR).mkdir(mode=0o700, exist_ok=True)
        (workdir / ROLES_DIR / f"{key}.md").write_text(body, encoding="utf-8")

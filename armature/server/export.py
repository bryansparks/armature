"""Write an instance's definitions to files (`armature server export`), and again after each change.

The export holds workflows, roles, prompts, missions, teams and projects,
settings, and providers. It never holds a secret value, a user, or history.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, event, select

from armature.server import audit
from armature.server.config import ExportConfig
from armature.server.db import utcnow
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    documents,
    projects,
    providers,
    settings,
    teams,
    versions,
    workflows,
)
from armature.server.defs import CREDENTIAL_KEYS, SECRET_WORDS, is_secret_name
from armature.spec.loader import STORED_WORKFLOW_HEADER

log = logging.getLogger(__name__)

FORMAT = 1
MANIFEST = "manifest.json"
DOCUMENT_DIRS = {"role": "roles", "prompt": "prompts", "mission": "missions"}
INSTANCE_ID_SETTING = "instance.id"
COMMAND_TIMEOUT = 120
ERROR_MAX = 500
SUMMARY_MAX = 4000
# The command's exit status for "finished, but left something for a person to look at".
WARNING_EXIT = 2
ACTOR = "export"


class ExportRefused(Exception):
    """The destination is not an export this command may replace."""


def _safe_part(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and not name.startswith(".") and "\0" not in name


def _team_dir(row: Any) -> str:
    return f"team-{row.slug or row.id}"


def _scope_names(conn: Connection) -> dict[str, str]:
    """Every scope id (instance, team id, project id) mapped to its export directory name."""
    names = {INSTANCE_SCOPE: "instance"}
    team_rows = {r.id: r for r in conn.execute(select(teams))}
    for row in team_rows.values():
        names[row.id] = _team_dir(row)
    for row in conn.execute(select(projects)):
        team = team_rows.get(row.team_id)
        team_part = (team.slug or team.id) if team else row.team_id
        names[row.id] = f"project-{team_part}-{row.name}"
    return names


def _instance_id(conn: Connection) -> str:
    stored = conn.execute(select(settings.c.value).where(settings.c.scope == INSTANCE_SCOPE,
                                                         settings.c.key == INSTANCE_ID_SETTING)).scalar()
    if isinstance(stored, str) and stored:
        return stored
    return str(uuid.uuid5(uuid.NAMESPACE_URL, conn.engine.url.render_as_string(hide_password=True)))


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _public_config(config: dict[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (config or {}).items()
            if key in CREDENTIAL_KEYS or not any(word in key.lower() for word in SECRET_WORDS)}


class _Builder:
    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, str] = {}
        self.objects: list[dict[str, Any]] = []

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        with open(path, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        self.files[rel] = hashlib.sha256(data).hexdigest()


def _team_filter(conn: Connection, team: str | None) -> tuple[str | None, set[str] | None]:
    if team is None:
        return None, None
    row = conn.execute(select(teams).where(teams.c.slug == team)).first()
    if row is None:
        raise ValueError(f"no team with slug {team!r}")
    project_ids = set(conn.execute(select(projects.c.id).where(projects.c.team_id == row.id)).scalars())
    return row.id, {INSTANCE_SCOPE, row.id, *project_ids}


def _definitions(conn: Connection, builder: _Builder, scopes: dict[str, str], keep: set[str] | None) -> None:
    for table, kind in ((workflows, "workflow"), (documents, "document")):
        rows = conn.execute(select(table, versions.c.body).join(versions, (versions.c.object_kind == kind)
                                                                & (versions.c.object_id == table.c.id)
                                                                & (versions.c.version == table.c.current_version))
                            .order_by(table.c.scope, table.c.id)).all()
        for row in rows:
            if keep is not None and row.scope not in keep:
                continue
            name = row.name if kind == "workflow" else row.key
            scope = scopes.get(row.scope)
            if scope is None or not _safe_part(name) or not _safe_part(scope):
                log.warning("export skipped %s %r in scope %s: unsafe file name", kind, name, row.scope)
                continue
            if kind == "workflow":
                rel, text = f"workflows/{scope}/{name}.yml", f"{STORED_WORKFLOW_HEADER}\n{row.body}"
            else:
                rel, text = f"{DOCUMENT_DIRS[row.kind]}/{scope}/{name}.md", row.body
            builder.write(rel, text)
            builder.objects.append({"path": rel, "kind": kind if kind == "workflow" else row.kind, "scope": scope,
                                    "name": name, "version": row.current_version})


def _teams(conn: Connection, team_id: str | None) -> list[dict[str, Any]]:
    query = select(teams).order_by(teams.c.created_at, teams.c.name)
    if team_id is not None:
        query = query.where(teams.c.id == team_id)
    out = []
    for team in conn.execute(query):
        rows = conn.execute(select(projects).where(projects.c.team_id == team.id)
                            .order_by(projects.c.created_at, projects.c.name))
        out.append({"slug": team.slug, "name": team.name, "projects": [
            {"name": p.name, "repo": p.repo, "platform": p.platform, "enabled_workflows": p.enabled_workflows or [],
             "branch_filters": p.branch_filters or []} for p in rows]})
    return out


def _settings(conn: Connection, scopes: dict[str, str], keep: set[str] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(select(settings).order_by(settings.c.scope, settings.c.key)):
        if (keep is not None and row.scope not in keep) or is_secret_name(row.key.upper()) \
                or row.key == INSTANCE_ID_SETTING or row.key.startswith("_"):
            continue
        out.setdefault(scopes.get(row.scope, row.scope), {})[row.key] = row.value
    return out


def _providers(conn: Connection, scopes: dict[str, str], team_id: str | None) -> list[dict[str, Any]]:
    query = select(providers).order_by(providers.c.team_id, providers.c.kind)
    if team_id is not None:
        query = query.where(providers.c.team_id == team_id)
    return [{"scope": scopes.get(r.team_id, r.team_id), "kind": r.kind, "config": _public_config(r.config),
             "aliases": r.aliases or {}} for r in conn.execute(query)]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_dest(dest: Path) -> None:
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        raise ExportRefused(f"{dest} is not a directory")
    if dest.is_dir() and any(dest.iterdir()) and not (dest / MANIFEST).is_file():
        raise ExportRefused(f"{dest} is not empty and holds no {MANIFEST}, so it is not an export")


def _swap(tmp: Path, dest: Path) -> None:
    old = None
    if dest.exists():
        old = dest.with_name(f"{dest.name}.old-{time.time_ns()}")
        os.rename(dest, old)
    try:
        os.rename(tmp, dest)
    except OSError:
        if old is not None:
            os.rename(old, dest)
        raise
    _fsync_dir(dest.parent)
    if old is not None:
        shutil.rmtree(old)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _clear_leftovers(dest: Path) -> None:
    """Remove what an earlier export left beside ``dest`` when it died mid-swap."""
    if not dest.parent.is_dir():
        return
    for sibling in dest.parent.iterdir():
        name = sibling.name
        if not name.startswith(f"{dest.name}.") or sibling.is_symlink() or not sibling.is_dir():
            continue
        suffix = name[len(dest.name) + 1:]
        kind, _, tag = suffix.partition("-")
        stale_tmp = kind == "tmp" and tag.isdigit() and int(tag) != os.getpid() and not _alive(int(tag))
        if (kind == "old" and tag.isdigit()) or stale_tmp:
            shutil.rmtree(sibling, ignore_errors=True)


def export_definitions(conn: Connection, dest: Path, *, team: str | None = None,
                       now: datetime | None = None) -> dict[str, Any]:
    """Write every definition under ``dest``, replacing an earlier export whole. Returns the manifest."""
    dest = Path(dest).expanduser().absolute()
    _check_dest(dest)
    _clear_leftovers(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    try:
        team_id, keep = _team_filter(conn, team)
        scopes = _scope_names(conn)
        builder = _Builder(tmp)
        _definitions(conn, builder, scopes, keep)
        builder.write("teams.json", _json({"teams": _teams(conn, team_id)}))
        builder.write("settings.json", _json(_settings(conn, scopes, keep)))
        builder.write("providers.json", _json({"providers": _providers(conn, scopes, team_id)}))
        manifest = {"format": FORMAT, "instance_id": _instance_id(conn),
                    "exported_at": (now or utcnow()).isoformat(), "team": team, "objects": builder.objects,
                    "files": dict(sorted(builder.files.items()))}
        builder.write(MANIFEST, _json(manifest))
        for path in sorted({p.parent for p in tmp.rglob("*")} | {tmp}, key=lambda p: len(p.parts), reverse=True):
            _fsync_dir(path)
        _check_dest(dest)
        _swap(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return manifest


def _command_env(dest: Path) -> dict[str, str]:
    env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG") if name in os.environ}
    env["ARMATURE_EXPORT_DIR"] = str(dest)
    return env


def _summary(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= ERROR_MAX else "..." + text[-ERROR_MAX:]


class ExportHook:
    """Re-export after each committed definition write: debounced, coalesced, one at a time, off the event loop."""

    def __init__(self, engine: Engine, cfg: ExportConfig, *, clock: Callable[[], datetime] = utcnow,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.engine = engine
        self.cfg = cfg
        self.dest = Path(cfg.dir).expanduser().absolute()
        self._clock = clock
        self._run_command = run
        self._cond = threading.Condition()
        self._due: float | None = None
        self._running = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.last_run_at: datetime | None = None
        self.last_status: str | None = None
        self.last_error: str | None = None
        self.last_summary: str | None = None

    def attach(self) -> None:
        event.listen(self.engine, "begin", self._on_begin)
        event.listen(self.engine, "commit", self._on_commit)

    def detach(self) -> None:
        event.remove(self.engine, "begin", self._on_begin)
        event.remove(self.engine, "commit", self._on_commit)

    @staticmethod
    def _on_begin(conn: Connection) -> None:
        conn.info.pop(audit.DEFINITIONS_CHANGED, None)

    def _on_commit(self, conn: Connection) -> None:
        if conn.info.pop(audit.DEFINITIONS_CHANGED, None):
            self.schedule()

    def schedule(self) -> None:
        with self._cond:
            if self._stopping:
                return
            self._due = time.monotonic() + self.cfg.debounce_seconds
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="armature-export", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._due is None and not self._stopping:
                    self._cond.wait()
                if self._due is None:
                    return
                wait = self._due - time.monotonic()
                if wait > 0 and not self._stopping:
                    self._cond.wait(wait)
                    continue
                self._due, self._running = None, True
            try:
                self.run_once()
            finally:
                with self._cond:
                    self._running = False
                    self._cond.notify_all()

    def run_once(self) -> bool:
        """Export, then run the command. Records the outcome and returns False only when something failed."""
        error = summary = None
        returncode = None
        try:
            with self.engine.connect() as conn:
                export_definitions(conn, self.dest, now=self._clock())
            if self.cfg.command:
                done = self._run_command(self.cfg.command, cwd=self.dest, env=_command_env(self.dest),
                                         timeout=COMMAND_TIMEOUT, capture_output=True, text=True, check=False)
                returncode = done.returncode
                if done.returncode == WARNING_EXIT:
                    lines = (done.stdout or "").strip().splitlines()
                    summary = lines[-1][:SUMMARY_MAX] if lines else "command reported warnings"
                elif done.returncode != 0:
                    error = f"command exited {done.returncode}: {_summary(done.stderr or done.stdout or '')}"
        except subprocess.TimeoutExpired:
            error = f"command timed out after {COMMAND_TIMEOUT} s"
        except Exception as exc:  # noqa: BLE001
            error = _summary(f"{exc.__class__.__name__}: {exc}")
        status = "failed" if error else "warning" if summary else "ok"
        with self._cond:
            self.last_run_at, self.last_status, self.last_error = self._clock(), status, error
            self.last_summary = summary
        if error:
            log.warning("export to %s failed: %s", self.dest, error)
        elif summary:
            log.warning("export to %s left something alone: %s", self.dest, summary)
        else:
            log.info("export to %s done", self.dest)
        try:
            with self.engine.begin() as conn:
                audit.record(conn, action="export.run", actor_id=ACTOR, target_kind="export",
                             target_id=str(self.dest)[:191],
                             detail={"status": status, "error": error, "summary": summary,
                                     "returncode": returncode, "command": bool(self.cfg.command)})
        except Exception:
            log.exception("export audit row not written")
        return error is None

    def flush(self, timeout: float = 30) -> bool:
        """Wait until no export is due or running. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._due is not None or self._running:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    def stop(self, timeout: float = COMMAND_TIMEOUT + 10) -> None:
        """Run any export still due, then end the thread."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def status(self) -> dict[str, Any]:
        with self._cond:
            return {"configured": True, "dir": str(self.dest), "command": bool(self.cfg.command),
                    "last_run_at": self.last_run_at, "status": self.last_status, "error": self.last_error,
                    "summary": self.last_summary, "pending": self._due is not None, "running": self._running}

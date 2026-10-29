"""Installed packs on the server: a cached catalog, and the admin CLI's install and remove.

Installs are CLI-only, because a pack is code that runs on this host. The
catalog reloads on its own when a pack directory changes, so a running server
sees an install without a restart.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Engine, delete, insert, select, update

from armature.packs import (
    NAME_RE,
    PACK_FILE,
    Catalog,
    PackError,
    load_pack,
    pack_dirs,
    search_path,
    version_key,
)
from armature.packs import catalog as load_catalog
from armature.server import audit
from armature.server.db import new_id
from armature.server.db.schema import packs

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_TIMEOUT_S = 600

_lock = threading.Lock()
_cache: dict[tuple[Path, ...], tuple[tuple, Catalog]] = {}


class InstallError(ValueError):
    """An install or removal that was refused. Nothing changed."""


def _configured_dir() -> Path:
    from armature.server.config import load_config
    try:
        return Path(load_config().packs_dir).expanduser()
    except ValueError:
        return Path("~/.armature/packs").expanduser()


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_ino)


def _signature(roots: Iterable[Path]) -> tuple:
    return tuple((root, _stat(root), tuple((d, _stat(d / PACK_FILE)) for d in pack_dirs(root))) for root in roots)


def catalog(packs_dir: Path | str | None = None) -> Catalog:
    """Every pack on ARMATURE_PACKS_PATH and in the server's ``packs_dir``, reloaded when one changes."""
    roots = tuple(search_path(default_dir=packs_dir or _configured_dir()))
    signature = _signature(roots)
    with _lock:
        cached = _cache.get(roots)
        if cached is not None and cached[0] == signature:
            return cached[1]
    loaded = load_catalog(roots)
    with _lock:
        _cache[roots] = (signature, loaded)
    return loaded


def reload() -> None:
    with _lock:
        _cache.clear()


@dataclass(frozen=True)
class Installed:
    name: str
    version: str
    previous_version: str | None
    source: str
    pinned_ref: str
    path: Path


def parse_source(spec: str) -> tuple[str, str, str | None]:
    """Split ``<dir>`` or ``<git-url-or-repo>@<sha>`` into (kind, location, ref)."""
    path = Path(spec).expanduser()
    if path.is_dir():
        return "dir", str(path.resolve()), None
    location, sep, ref = spec.rpartition("@")
    if not sep or not location:
        raise InstallError("give a pack directory, or a git URL or repository path with @<commit sha>")
    if not SHA_RE.fullmatch(ref):
        raise InstallError("a git install must pin a full 40-character commit sha")
    if location.startswith("-"):
        raise InstallError("a git URL must not start with '-'")
    return "git", location, ref


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme and "@" in parts.netloc:
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
    return url


def _git(*args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", *args],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        raise InstallError(f"git failed: {exc.stderr.strip()[:500]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallError("git timed out") from exc
    return done.stdout


def _checkout(url: str, sha: str, dest: Path) -> None:
    _git("clone", "--no-checkout", "--quiet", "--", url, str(dest))
    _git("-C", str(dest), "checkout", "--quiet", "--detach", sha)
    head = _git("-C", str(dest), "rev-parse", "HEAD").strip()
    if head != sha:
        raise InstallError(f"checked out {head}, not {sha}")


def _files(root: Path) -> list[Path]:
    found = []
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in [*dirs, *names]:
            path = Path(current) / name
            if path.is_symlink():
                raise InstallError(f"{path.relative_to(root)} is a symlink, which a pack may not contain")
        found.extend(Path(current) / name for name in sorted(names))
    return found


def _copy_tree(src: Path, dest: Path) -> None:
    _files(src)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _files(root):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _known_version(version: str | None) -> tuple | None:
    try:
        return version_key(version) if version else None
    except ValueError:
        return None


def _disk_version(target: Path) -> str | None:
    try:
        return load_pack(target).version
    except PackError:
        return None


def install(engine: Engine, packs_root: Path, source: str, *, now: datetime, force: bool = False) -> Installed:
    """Copy a pack into ``packs_root/<name>`` and record it, refusing a downgrade unless ``force``."""
    kind, location, ref = parse_source(source)
    packs_root = packs_root.expanduser()
    packs_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=packs_root, prefix=".install-") as tmp:
        work = Path(tmp)
        staged = work / "pack"
        if kind == "git":
            _checkout(location, ref, work / "src")
            _copy_tree(work / "src", staged)
            recorded_source, pinned_ref = redact_url(location), ref
        else:
            _copy_tree(Path(location), staged)
            recorded_source, pinned_ref = location, f"sha256:{tree_hash(staged)}"
        try:
            pack = load_pack(staged)
        except PackError as exc:
            raise InstallError(str(exc)) from exc
        target = packs_root / pack.name
        with engine.begin() as conn:
            row = conn.execute(select(packs).where(packs.c.name == pack.name)).first()
            previous = row.version if row is not None else _disk_version(target)
            older = _known_version(previous)
            if older is not None and version_key(pack.version) < older and not force:
                raise InstallError(f"{pack.name} {previous} is installed, so {pack.version} would be a downgrade. "
                                   "Pass --force to install it anyway")
            values = {"version": pack.version, "source": recorded_source, "pinned_ref": pinned_ref,
                      "installed_by": "cli", "installed_at": now}
            if row is not None:
                row_id = row.id
                conn.execute(update(packs).where(packs.c.id == row_id).values(**values))
            else:
                row_id = new_id()
                conn.execute(insert(packs).values(id=row_id, name=pack.name, **values))
            audit.record(conn, action="pack.install", actor_id=None, target_kind="pack", target_id=row_id,
                         detail={"name": pack.name, "version": pack.version, "previous_version": previous,
                                 "source": recorded_source, "pinned_ref": pinned_ref, "force": force})
            # Swapped last, inside the transaction, so a failed swap rolls the row back too.
            if target.exists():
                target.rename(work / "old")
            try:
                staged.rename(target)
            except OSError:
                if (work / "old").exists():
                    (work / "old").rename(target)
                raise
    reload()
    return Installed(name=pack.name, version=pack.version, previous_version=previous, source=recorded_source,
                     pinned_ref=pinned_ref, path=target)


def remove(engine: Engine, packs_root: Path, name: str) -> str | None:
    """Delete an installed pack's files and row. Returns the version removed, if recorded."""
    if not NAME_RE.fullmatch(name):
        raise InstallError(f"pack name must match {NAME_RE.pattern}")
    target = packs_root.expanduser() / name
    with engine.begin() as conn:
        row = conn.execute(select(packs).where(packs.c.name == name)).first()
        if row is None and not target.exists():
            raise InstallError(f"pack {name} is not installed")
        if row is not None:
            conn.execute(delete(packs).where(packs.c.id == row.id))
        version = row.version if row is not None else _disk_version(target)
        audit.record(conn, action="pack.remove", actor_id=None, target_kind="pack",
                     target_id=row.id if row is not None else None, detail={"name": name, "version": version})
        if target.exists():
            with tempfile.TemporaryDirectory(dir=target.parent, prefix=".remove-") as tmp:
                target.rename(Path(tmp) / name)
    reload()
    return version

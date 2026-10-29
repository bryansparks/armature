"""First-run setup, shared by `armature server init` and the browser setup page."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from armature.server.config import ENV_PREFIX, ServerConfig, load_config, write_config
from armature.server.crypto import keyring_from_source, write_key_file
from armature.server.db import make_engine, migrate
from armature.server.users import has_admin, has_sealed_values

BACKENDS = ("sqlite", "postgresql", "mysql")


class SetupError(Exception):
    """A setup step that cannot go ahead. The message is safe to show to the person setting up."""


@dataclass(frozen=True)
class Prepared:
    cfg: ServerConfig
    wrote_config: bool
    new_key: Path | None
    has_admin: bool


def check_database_url(url: str) -> str:
    try:
        backend = make_url(url.strip()).get_backend_name()
    except (ArgumentError, ValueError) as exc:
        raise SetupError("That is not a database URL.") from exc
    if backend not in BACKENDS:
        raise SetupError("Use a SQLite, PostgreSQL, or MySQL URL.")
    return url.strip()


def try_database(url: str) -> None:
    """Connect once, so a typo shows before anything is written. The message never holds the URL."""
    url = check_database_url(url)
    _sqlite_folder(url)
    try:
        engine = make_engine(url)
    except ImportError as exc:
        raise SetupError("The driver for this database is not installed.") from exc
    try:
        with engine.connect():
            pass
    except SQLAlchemyError as exc:
        reason = str(getattr(exc, "orig", None) or exc.__class__.__name__).splitlines()[0][:200]
        raise SetupError(f"Could not connect: {reason}") from exc
    finally:
        engine.dispose()


def _sqlite_folder(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def write_new_config(path: Path, *, database_url: str | None = None, key_file: Path | None = None) -> bool:
    """Write a config with these values when none exists. Returns whether it wrote one."""
    if path.exists():
        return False
    values = {}
    database_url = database_url or os.environ.get(ENV_PREFIX + "DATABASE_URL")
    if database_url:
        values["database_url"] = database_url
    if key_file:
        values["master_key"] = f"file:{key_file.expanduser()}"
    write_config(ServerConfig(**values), path)
    return True


def key_file_of(cfg: ServerConfig) -> Path | None:
    scheme, _, rest = cfg.master_key.partition(":")
    return Path(rest).expanduser() if scheme == "file" else None


def prepare(path: Path, *, database_url: str | None = None, key_file: Path | None = None) -> Prepared:
    """Write a config if missing, migrate, and create the master key file if nothing needs an old one."""
    wrote = write_new_config(path, database_url=database_url, key_file=key_file)
    cfg = load_config(path)
    _sqlite_folder(cfg.database_url)
    migrate(cfg.database_url)
    engine = make_engine(cfg.database_url)
    try:
        with engine.connect() as conn:
            sealed, exists = has_sealed_values(conn), has_admin(conn, cfg.require_totp)
    finally:
        engine.dispose()
    key = key_file_of(cfg)
    new_key = None
    if key is not None and not os.path.lexists(key):
        if sealed:
            raise SetupError(f"Master key file {key} is missing, but the database holds values sealed with a master "
                             "key. Restore the key file from backup. A new key could never open them.")
        write_key_file(key)
        new_key = key
    keyring_from_source(cfg.master_key)
    return Prepared(cfg=cfg, wrote_config=wrote, new_key=new_key, has_admin=exists)


def needs_setup(cfg: ServerConfig) -> bool:
    """True until the database is migrated and holds an admin. Raises SQLAlchemyError when it can't be reached."""
    url = make_url(cfg.database_url)
    if url.get_backend_name() == "sqlite" and url.database and not Path(url.database).exists():
        return True
    engine = make_engine(cfg.database_url)
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("users"):
                return True
            return not has_admin(conn, cfg.require_totp)
    finally:
        engine.dispose()

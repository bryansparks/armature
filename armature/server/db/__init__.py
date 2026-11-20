"""Database access: engine creation, migrations, and shared helpers."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event

from armature.server.db.schema import metadata
from armature.server.secretref import SecretRefError, resolve

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_engine(url: str, *, password_ref: str | None = None, provider: str | None = None) -> Engine:
    """An engine for ``url``. A ``password_ref`` is resolved on each new connection and never enters the URL."""
    engine = create_engine(url, future=True)
    if password_ref is not None and engine.dialect.name != "sqlite":
        @event.listens_for(engine, "do_connect")
        def _password(dialect, _record, cargs, cparams):
            try:
                password = resolve(password_ref, provider=provider)
            except SecretRefError as exc:
                raise dialect.loaded_dbapi.OperationalError(str(exc)) from None
            return dialect.connect(*cargs, **{**cparams, "password": password})
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL lets the event writer commit while request threads read.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


def engine_for(cfg) -> Engine:
    return make_engine(cfg.database_url, password_ref=cfg.database_password, provider=cfg.secret_provider)


def alembic_config(url: str, *, password_ref: str | None = None, provider: str | None = None):
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes.update(password_ref=password_ref, secret_provider=provider)
    return cfg


def migrate(url: str, revision: str = "head") -> None:
    from alembic import command
    command.upgrade(alembic_config(url), revision)


def migrate_for(cfg) -> None:
    from alembic import command
    command.upgrade(alembic_config(cfg.database_url, password_ref=cfg.database_password,
                                   provider=cfg.secret_provider), "head")


__all__ = ["MIGRATIONS_DIR", "engine_for", "make_engine", "metadata", "migrate", "migrate_for", "new_id", "utcnow"]

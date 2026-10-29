import os

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")
pytest.importorskip("cryptography")

from sqlalchemy import MetaData

from armature.server.crypto import Keyring
from armature.server.db import make_engine, migrate

MATRIX = {
    "postgres": "ARMATURE_TEST_PG_URL",
    "mysql": "ARMATURE_TEST_MYSQL_URL",
}


def _drop_everything(url: str) -> None:
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            existing = MetaData()
            existing.reflect(conn)
            existing.drop_all(conn)
    finally:
        engine.dispose()


@pytest.fixture(params=["sqlite", *(pytest.param(name, marks=pytest.mark.db_matrix) for name in MATRIX)])
def db_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'server.db'}"
        return
    url = os.environ.get(MATRIX[request.param])
    if not url:
        pytest.skip(f"{MATRIX[request.param]} is not set")
    _drop_everything(url)
    yield url
    _drop_everything(url)


@pytest.fixture
def engine(db_url):
    migrate(db_url)
    eng = make_engine(db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def sqlite_engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'server.db'}"
    migrate(url)
    eng = make_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture
def keyring():
    return Keyring(b"k" * 32)

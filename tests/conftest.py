import pytest

from armature.runtime import engine


@pytest.fixture(autouse=True)
def _traces_db_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_GLOBAL_TRACES_DB", tmp_path / "armature" / "traces.db")

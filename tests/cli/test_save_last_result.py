"""Tests for atomic persistence of run results (last/<workflow>.json and runs/<id>/result.json)."""
import json
import os
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from armature.cli import _save_last_result, app

ECHO = Path(__file__).parent.parent / "fixtures" / "echo-workflow.yaml"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _last(home: Path) -> Path:
    return home / ".armature" / "last" / "wf.json"


def test_save_last_result_writes_last_and_run_dir_with_no_temp_files(home):
    run_dir = home / ".armature" / "runs" / "r1"
    payload = {"s": {"content": "x"}, "p": Path("/x")}

    _save_last_result("wf", payload, run_dir)

    expected = json.dumps(payload, indent=2, default=str)
    assert _last(home).read_text() == expected
    assert (run_dir / "result.json").read_text() == expected
    assert os.listdir(_last(home).parent) == ["wf.json"]
    assert os.listdir(run_dir) == ["result.json"]


def test_save_last_result_never_exposes_partial_file(home, monkeypatch):
    run_dir = home / ".armature" / "runs" / "r1"
    old = {"old": True}
    new = {"new": True, "blob": "x" * 100_000}
    _save_last_result("wf", old, run_dir)
    real_replace = os.replace
    seen = []

    def checked_replace(src, dst):
        assert json.loads(Path(dst).read_text()) == old
        assert json.loads(Path(src).read_text()) == new
        seen.append(dst)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", checked_replace)
    _save_last_result("wf", new, run_dir)

    assert len(seen) == 2
    assert json.loads(_last(home).read_text()) == new


def test_save_last_result_reader_never_sees_torn_file(home):
    run_dir = home / ".armature" / "runs" / "r1"
    payload = {"blob": "x" * 2_000_000}
    _save_last_result("wf", payload, run_dir)
    stop = threading.Event()
    errors: list[Exception] = []

    def read():
        while not stop.is_set():
            try:
                assert json.loads(_last(home).read_text()) == payload
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    reader = threading.Thread(target=read)
    reader.start()
    for _ in range(200):
        _save_last_result("wf", payload, run_dir)
    stop.set()
    reader.join()

    assert not errors


def test_save_last_result_swallows_errors_and_cleans_temp(home, monkeypatch):
    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    run_dir = home / ".armature" / "runs" / "r1"

    _save_last_result("wf", {"a": 1}, run_dir)

    assert os.listdir(_last(home).parent) == []
    assert os.listdir(run_dir) == []


def test_save_last_result_writes_run_dir_when_last_fails(home, monkeypatch):
    real_replace = os.replace

    def fail_last(src, dst):
        if Path(dst).parent.name == "last":
            raise OSError("last unwritable")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_last)
    run_dir = home / ".armature" / "runs" / "r1"

    _save_last_result("wf", {"a": 1}, run_dir)

    assert json.loads((run_dir / "result.json").read_text()) == {"a": 1}
    assert os.listdir(_last(home).parent) == []


def test_run_command_saves_result_in_run_dir(home):
    result = CliRunner().invoke(app, ["run", str(ECHO), "--input", "message=hi", "--quiet"])
    assert result.exit_code == 0

    last = json.loads((home / ".armature" / "last" / "echo-workflow.json").read_text())
    run_results = list((home / ".armature" / "runs").glob("*/result.json"))
    assert len(run_results) == 1
    assert json.loads(run_results[0].read_text()) == last
    assert last["echo"]["exit_code"] == 0

import os

from typer.testing import CliRunner

from armature.cli import app
from armature.packs import PACKS_PATH_ENV
from tests.packs.support import HELLO, write_pack


def test_pack_validate():
    result = CliRunner().invoke(app, ["pack", "validate", str(HELLO)])
    assert result.exit_code == 0, result.output
    assert "hello 1.0.0 is valid (2 adapters)" in result.output and "hello/greet" in result.output


def test_pack_validate_refuses_a_templated_cmd(tmp_path):
    write_pack(tmp_path / "bad", x={"cmd": "echo {{ name }}"})
    result = CliRunner().invoke(app, ["pack", "validate", str(tmp_path / "bad")])
    assert result.exit_code == 1 and "template markers" in result.output


def test_pack_list_shows_packs_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_pack(tmp_path / "extra" / "hello", name="hello", version="9.0.0")
    monkeypatch.setenv(PACKS_PATH_ENV, os.pathsep.join([str(HELLO), str(tmp_path / "extra")]))
    result = CliRunner().invoke(app, ["pack", "list"])
    assert result.exit_code == 1
    assert "hello 1.0.0" in result.output and "hello/wait" in result.output
    assert "duplicate pack 'hello'" in result.output


def test_pack_list_with_nothing_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(PACKS_PATH_ENV, raising=False)
    result = CliRunner().invoke(app, ["pack", "list"])
    assert result.exit_code == 0 and "No packs installed." in result.output

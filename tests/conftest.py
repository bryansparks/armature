import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Runs before any test module imports armature, so import-time home paths never see the real home.
_real_docker = Path.home() / ".docker"
if "DOCKER_CONFIG" not in os.environ and _real_docker.is_dir():
    os.environ["DOCKER_CONFIG"] = str(_real_docker)
if "PIP_CACHE_DIR" not in os.environ:
    if sys.platform == "darwin":
        os.environ["PIP_CACHE_DIR"] = str(Path.home() / "Library" / "Caches" / "pip")
    else:
        os.environ["PIP_CACHE_DIR"] = str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pip")
# Short on purpose: unix socket paths under HOME must fit the 104-byte sun_path limit on macOS.
_process_home = tempfile.mkdtemp(prefix="armature-home-", dir="/tmp" if os.path.isdir("/tmp") else None)
os.environ["HOME"] = _process_home

_GITCONFIG = "[user]\n\tname = Armature Tests\n\temail = tests@armature.invalid\n"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch):
    from armature import config
    from armature.adapters import registry

    home = Path(tempfile.mkdtemp(dir=_process_home))
    (home / ".gitconfig").write_text(_GITCONFIG)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", home / ".armature" / "defaults.config")
    monkeypatch.setattr(registry, "DEFAULT_ADAPTER_DIR", home / ".armature" / "adapters")
    return home


def pytest_unconfigure(config):
    shutil.rmtree(_process_home, ignore_errors=True)


def pytest_collection_modifyitems(config, items):
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        return
    skip = pytest.mark.skip(reason="wall-clock budgets run in the serial pass: pytest -n0 -m timing")
    for item in items:
        if "timing" in item.keywords:
            item.add_marker(skip)

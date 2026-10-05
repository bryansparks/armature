"""Transport-suite fixtures: hermetic AWS region + the jobs bucket.

Fixture pattern moved from armature-dispatch tests/conftest.py @ c5fe34f.
boto3/moto are imported lazily inside the fixtures so this conftest (and
tests/transport/test_boundary.py, which needs no AWS at all) still load
in environments without the cloud extra — the default CI job."""
import pytest

BUCKET = "dispatch-jobs-123456789012-us-east-1"  # moto-standard account id


@pytest.fixture(autouse=True)
def pinned_aws_region(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def s3_bucket():
    pytest.importorskip("moto")  # the cloud-dev marker — moto implies boto3
    import boto3
    from moto import mock_aws
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        yield s3, BUCKET


# -- package fixtures (pattern from armature-dispatch tests/conftest.py @ c5fe34f) --
# armature_cmd prefers the interpreter's own -m entry point over `which
# armature`: on dev machines a stale global install can shadow the checkout
# under test, and fixtures must build with the armature this suite imports.

import shutil
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def armature_cmd() -> list[str]:
    return [sys.executable, "-m", "armature"]


def _build(armature_cmd: list[str], spec: Path, out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    result = subprocess.run(armature_cmd + ["package", "build",
                                            "--spec", str(spec), "--out", str(out)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"fixture build failed for {spec.name}:\n{result.stderr}")
    return out


@pytest.fixture(scope="session")
def work_echo_pkg(tmp_path_factory, armature_cmd) -> Path:
    """Offline package that renders the injected work_unit record to stdout."""
    return _build(armature_cmd, FIXTURES / "work-echo.yaml",
                  tmp_path_factory.mktemp("pkg-work-echo") / "work-echo")


@pytest.fixture(scope="session")
def work_echo_fail_pkg(tmp_path_factory, armature_cmd) -> Path:
    """Offline package whose stage exits 3 — the settle failure-path fixture."""
    return _build(armature_cmd, FIXTURES / "work-echo-fail.yaml",
                  tmp_path_factory.mktemp("pkg-work-echo-fail") / "work-echo-fail")
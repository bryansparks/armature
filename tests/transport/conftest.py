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
    boto3 = pytest.importorskip("boto3")   # the transport suite needs the extra
    from moto import mock_aws
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        yield s3, BUCKET
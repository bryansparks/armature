"""Tests for the remote adapter factory dispatcher."""
from __future__ import annotations

from pathlib import Path

import pytest
from armature.adapters.backends.remote import (
    ModalRemoteClient,
    RemoteAdapterFactory,
    make_remote_client,
)
from armature.adapters.factory import AdapterRequest
from armature.adapters.registry import AdapterRegistry
from armature.spec.models import SkillDef


class FakeRemoteClient:
    """Remote client that transitions from running to done on the second poll."""

    def __init__(self, artifact_dir: Path | None = None) -> None:
        self._jobs: dict[str, dict] = {}
        self._uploads: list[tuple[Path, str]] = []
        self._artifact_dir = artifact_dir
        self._polled: set[str] = set()

    def upload(self, local_path: Path, remote_name: str) -> str:
        self._uploads.append((local_path, remote_name))
        job_id = "fake-1"
        self._jobs[job_id] = {"status": "running", "artifact_url": None}
        return job_id

    def poll(self, job_id: str) -> dict:
        info = self._jobs.get(job_id, {"status": "failed"})
        if job_id in self._polled and self._artifact_dir is not None:
            info = {"status": "done"}
        self._polled.add(job_id)
        return info

    def download(self, job_id: str, destination: Path) -> Path:
        if self._artifact_dir is None:
            raise RuntimeError("no artifact")
        # Simulate a download by copying the dummy artifact directory.
        import shutil

        target = destination / "artifact"
        shutil.copytree(self._artifact_dir, target)
        return target


@pytest.fixture
def artifact_dir(tmp_path) -> Path:
    d = tmp_path / "remote-artifact"
    d.mkdir()
    (d / "adapter_config.json").write_text("{}")
    (d / "adapter.safetensors").write_bytes(b"REMOTE")
    return d


async def test_submit_creates_queued_job(tmp_path):
    f = RemoteAdapterFactory(
        provider="modal",
        registry=AdapterRegistry(base_dir=tmp_path / "adapters"),
        client=FakeRemoteClient(),
    )
    skill = SkillDef(id="tdd", description="TDD", content="Write tests first.")
    job = await f.submit(AdapterRequest(name="tdd", base_model="m", skill=skill))
    assert job.backend == "remote:modal"
    assert job.status == "queued"


async def test_poll_uploads_and_downloads(artifact_dir, tmp_path):
    f = RemoteAdapterFactory(
        provider="modal",
        registry=AdapterRegistry(base_dir=tmp_path / "adapters"),
        client=FakeRemoteClient(artifact_dir=artifact_dir),
    )
    skill = SkillDef(id="tdd", description="TDD", content="Write tests first.")
    job = await f.submit(AdapterRequest(name="tdd", base_model="m", skill=skill))
    while job.status != "done":
        job = await f.poll(job)
    assert job.status == "done"
    assert job.artifact_path is not None


async def test_poll_fails_when_remote_fails(tmp_path):
    class FailingClient:
        def upload(self, local_path, remote_name):
            return "fail-1"

        def poll(self, job_id):
            return {"status": "failed"}

        def download(self, job_id, destination):
            raise RuntimeError("should not reach download")

    f = RemoteAdapterFactory(
        provider="modal",
        registry=AdapterRegistry(base_dir=tmp_path / "adapters"),
        client=FailingClient(),
    )
    skill = SkillDef(id="tdd", description="TDD", content="Write tests first.")
    job = await f.submit(AdapterRequest(name="tdd", base_model="m", skill=skill))
    while job.status == "queued":
        job = await f.poll(job)
    # After upload, the remote job reports failed.
    while job.status == "running":
        job = await f.poll(job)
    assert job.status == "failed"


def test_make_remote_client_known_providers():
    assert isinstance(make_remote_client("modal"), ModalRemoteClient)
    assert make_remote_client("together")._jobs == {}
    assert make_remote_client("runpod")._endpoint_id == ""
    assert make_remote_client("replicate")._model_owner == ""

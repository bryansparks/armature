"""Remote training backend dispatcher.

Uploads a training tarball to a remote GPU provider and polls for completion.
Concrete providers implement only upload/poll primitives; the actual training
logic runs on the remote container using the same LocalAdapterFactory code so
behavior stays consistent across providers.
"""
from __future__ import annotations

import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from armature.adapters.factory import AdapterFactory, AdapterJob, AdapterRequest
from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry

if TYPE_CHECKING:
    pass


class RemoteClient(Protocol):
    """Minimal interface a remote provider must implement."""

    def upload(self, local_path: Path, remote_name: str) -> str:
        """Upload a tarball and return a remote job id."""

    def poll(self, job_id: str) -> dict[str, Any]:
        """Return {'status': ..., 'artifact_url': ... | None}."""

    def download(self, job_id: str, destination: Path) -> Path:
        """Download the trained artifact into destination and return the path."""


class ModalRemoteClient(RemoteClient):
    """Stub for Modal-hosted training.

    In a real deployment this calls `modal.Function.lookup(...).spawn(...)` and
    polls the returned call ID. The stub records inputs for tests.
    """

    def __init__(self, modal_function_ref: str = "armature-train") -> None:
        self._modal_function_ref = modal_function_ref
        self._jobs: dict[str, dict[str, Any]] = {}
        self._uploads: list[tuple[Path, str]] = []

    def upload(self, local_path: Path, remote_name: str) -> str:
        self._uploads.append((local_path, remote_name))
        job_id = f"modal-{uuid.uuid4().hex[:8]}"
        self._jobs[job_id] = {"status": "running", "artifact_url": None}
        return job_id

    def poll(self, job_id: str) -> dict[str, Any]:
        return self._jobs.get(job_id, {"status": "failed"})

    def download(self, job_id: str, destination: Path) -> Path:
        raise NotImplementedError("download is provider-specific")


class TogetherRemoteClient(RemoteClient):
    """Stub for Together AI fine-tuning API."""

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        self._jobs: dict[str, dict[str, Any]] = {}
        self._uploads: list[tuple[Path, str]] = []

    def upload(self, local_path: Path, remote_name: str) -> str:
        self._uploads.append((local_path, remote_name))
        job_id = f"together-{uuid.uuid4().hex[:8]}"
        self._jobs[job_id] = {"status": "running", "artifact_url": None}
        return job_id

    def poll(self, job_id: str) -> dict[str, Any]:
        return self._jobs.get(job_id, {"status": "failed"})

    def download(self, job_id: str, destination: Path) -> Path:
        raise NotImplementedError("download is provider-specific")


class RunPodRemoteClient(RemoteClient):
    """Stub for RunPod serverless training endpoint."""

    def __init__(self, endpoint_id: str = "") -> None:
        self._endpoint_id = endpoint_id
        self._jobs: dict[str, dict[str, Any]] = {}
        self._uploads: list[tuple[Path, str]] = []

    def upload(self, local_path: Path, remote_name: str) -> str:
        self._uploads.append((local_path, remote_name))
        job_id = f"runpod-{uuid.uuid4().hex[:8]}"
        self._jobs[job_id] = {"status": "running", "artifact_url": None}
        return job_id

    def poll(self, job_id: str) -> dict[str, Any]:
        return self._jobs.get(job_id, {"status": "failed"})

    def download(self, job_id: str, destination: Path) -> Path:
        raise NotImplementedError("download is provider-specific")


class ReplicateRemoteClient(RemoteClient):
    """Stub for Replicate training API."""

    def __init__(self, model_owner: str = "", model_name: str = "") -> None:
        self._model_owner = model_owner
        self._model_name = model_name
        self._jobs: dict[str, dict[str, Any]] = {}
        self._uploads: list[tuple[Path, str]] = []

    def upload(self, local_path: Path, remote_name: str) -> str:
        self._uploads.append((local_path, remote_name))
        job_id = f"replicate-{uuid.uuid4().hex[:8]}"
        self._jobs[job_id] = {"status": "running", "artifact_url": None}
        return job_id

    def poll(self, job_id: str) -> dict[str, Any]:
        return self._jobs.get(job_id, {"status": "failed"})

    def download(self, job_id: str, destination: Path) -> Path:
        raise NotImplementedError("download is provider-specific")


def make_remote_client(provider: str, **kwargs: Any) -> RemoteClient:
    """Factory function for concrete remote clients."""
    if provider == "modal":
        return ModalRemoteClient(**kwargs)
    if provider == "together":
        return TogetherRemoteClient(**kwargs)
    if provider == "runpod":
        return RunPodRemoteClient(**kwargs)
    if provider == "replicate":
        return ReplicateRemoteClient(**kwargs)
    raise ValueError(f"Unsupported remote provider: {provider}")


class RemoteAdapterFactory(AdapterFactory):
    """Dispatch adapter training to a remote GPU provider."""

    def __init__(
        self,
        provider: str,
        registry: AdapterRegistry | None = None,
        client: RemoteClient | None = None,
        **client_kwargs: Any,
    ) -> None:
        self._provider = provider
        self._registry = registry or AdapterRegistry()
        self._client = client or make_remote_client(provider, **client_kwargs)
        self._remote_jobs: dict[str, str] = {}

    def available(self) -> bool:
        return True

    async def submit(self, request: AdapterRequest) -> AdapterJob:
        if request.skill is None and request.traces_path is None:
            raise ValueError("RemoteAdapterFactory requires skill or traces_path")
        job_id = f"remote-{uuid.uuid4().hex[:8]}"
        version = _fresh_version(self._registry, request.name)
        metadata = AdapterMetadata(
            name=request.name,
            version=version,
            base_model=request.base_model,
            rank=request.rank,
            alpha=request.alpha,
            target_modules=list(request.target_modules),
            backend=f"remote:{self._provider}",
            job_id=job_id,
        )
        return AdapterJob(
            job_id=job_id,
            backend=f"remote:{self._provider}",
            status="queued",
            metadata=metadata,
            request=request,
        )

    async def poll(self, job: AdapterJob) -> AdapterJob:
        if job.status in ("done", "failed"):
            return job
        if job.status == "queued":
            if job.request is None or job.metadata is None:
                job.status = "failed"
                job.logs.append("invalid job state")
                return job
            tarball = _pack_request(job.request)
            remote_job_id = self._client.upload(tarball, job.metadata.name)
            self._remote_jobs[job.job_id] = remote_job_id
            job.status = "running"
            job.logs.append(f"uploaded to {self._provider} job {remote_job_id}")
            return job
        if job.status != "running" or job.metadata is None:
            job.status = "failed"
            job.logs.append("invalid job state")
            return job
        remote_job_id = self._remote_jobs.get(job.job_id)
        if remote_job_id is None:
            job.status = "failed"
            job.logs.append("lost remote job id")
            return job
        info = self._client.poll(remote_job_id)
        remote_status = info.get("status", "failed")
        if remote_status == "running":
            return job
        if remote_status != "done":
            job.status = "failed"
            job.logs.append(f"remote job failed: {remote_status}")
            return job
        # Download the artifact and register it locally.
        try:
            download_dir = Path(tempfile.mkdtemp(prefix="armature-remote-"))
            artifact_dir = self._client.download(remote_job_id, download_dir)
            self._registry.register(job.metadata, artifact_dir)
            job.artifact_path = self._registry.get(
                job.metadata.name, job.metadata.version
            ).artifact_dir
            job.status = "done"
            job.logs.append(
                f"registered adapter {job.metadata.name}@{job.metadata.version}"
            )
        except Exception as exc:
            job.status = "failed"
            job.logs.append(f"download/registration failed: {exc}")
        return job


def _pack_request(request: AdapterRequest) -> Path:
    """Serialize a request and its training data into a tarball for upload."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="armature-remote-request-"))
    request_file = tmp_dir / "request.json"
    import json

    request_file.write_text(
        json.dumps(
            {
                "name": request.name,
                "base_model": request.base_model,
                "rank": request.rank,
                "alpha": request.alpha,
                "target_modules": request.target_modules,
                "max_tokens_per_example": request.max_tokens_per_example,
                "output_max_tokens": request.output_max_tokens,
                "extra": request.extra,
            },
            default=str,
        ),
        encoding="utf-8",
    )

    if request.skill is not None:
        skill_file = tmp_dir / "skill.json"
        skill_file.write_text(
            json.dumps(
                {
                    "id": request.skill.id,
                    "description": request.skill.description,
                    "content": request.skill.content,
                    "path": request.skill.path,
                },
                default=str,
            ),
            encoding="utf-8",
        )
    elif request.traces_path is not None:
        shutil.copy(request.traces_path, tmp_dir / "traces.jsonl")

    tarball = tmp_dir.with_suffix(".tar.gz")
    with tarfile.open(tarball, "w:gz") as tar:
        for child in tmp_dir.iterdir():
            tar.add(child, arcname=child.name)
    return tarball


def _fresh_version(registry: AdapterRegistry, name: str) -> str:
    try:
        latest = registry._manifest(name).latest_version()
    except ValueError:
        latest = None
    if latest is None:
        return "1"
    try:
        return str(int(latest) + 1)
    except ValueError:
        return f"{latest}-next"

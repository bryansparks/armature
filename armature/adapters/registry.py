"""Local adapter registry.

Stores immutable LoRA artifacts under a canonical directory layout and
provides metadata lookup. The registry does not perform training; it only
manages artifacts and manifests.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from armature.adapters.manifest import AdapterMetadata, Manifest
from armature.adapters.policy import AlwaysPromotePolicy, PromotionPolicy

DEFAULT_ADAPTER_DIR = Path("~/.armature/adapters").expanduser()


@dataclass
class ResolvedAdapter:
    """A resolved adapter plus the path to its artifact directory."""

    metadata: AdapterMetadata
    artifact_dir: Path


class AdapterRegistry:
    """Versioned local cache of LoRA adapter artifacts."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base_dir = Path(base_dir).expanduser() if base_dir else DEFAULT_ADAPTER_DIR
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _manifest(self, name: str) -> Manifest:
        return Manifest(self._base_dir / name / "manifest.json")

    def get(self, name: str, version: str | None = None) -> ResolvedAdapter:
        """Return metadata and artifact directory for an adapter.

        If version is None, resolves to the manifest's `latest` pointer.
        Raises ValueError if the adapter or version is unknown.
        """
        manifest = self._manifest(name)
        versions = manifest.versions()
        if not versions:
            raise ValueError(f"Adapter '{name}' not found in registry")

        target = version or manifest.latest_version()
        if target is None:
            raise ValueError(f"Adapter '{name}' has no versions and no latest pointer")
        if target not in versions:
            raise ValueError(
                f"Adapter '{name}' version '{target}' not found; "
                f"known versions: {sorted(versions)}"
            )

        meta = versions[target]
        return ResolvedAdapter(metadata=meta, artifact_dir=self._artifact_dir(name, target))

    def register(
        self,
        metadata: AdapterMetadata,
        artifact_dir: Path,
        *,
        promote: bool = True,
        policy: PromotionPolicy | None = None,
    ) -> None:
        """Copy an artifact directory into the registry and record metadata.

        The existing artifact_dir may contain multiple files; the entire
        directory is copied under the adapter name/version path.

        If ``policy`` is provided and ``promote`` is True, the policy decides
        whether the new version becomes the ``latest`` pointer. If ``promote`` is
        False, the latest pointer is never advanced. When no policy is provided,
        the historical behavior (always promote) is preserved.
        """
        target_dir = self._artifact_dir(metadata.name, metadata.version)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(artifact_dir, target_dir)

        manifest = self._manifest(metadata.name)
        manifest.add(metadata)
        if promote:
            if policy is None:
                policy = AlwaysPromotePolicy()
            current = None
            current_version = manifest.latest_version()
            if current_version is not None:
                current = manifest.versions().get(current_version)
            if policy.should_promote(metadata, current):
                manifest.set_latest(metadata.version)

    def promote(
        self,
        name: str,
        version: str,
        *,
        policy: PromotionPolicy | None = None,
        force: bool = False,
    ) -> None:
        """Set the `latest` pointer for an adapter to a specific version.

        If ``force`` is True, the promotion happens unconditionally. Otherwise an
        optional ``policy`` is consulted against the current latest version.
        """
        manifest = self._manifest(name)
        if force or policy is None:
            manifest.set_latest(version)
            return
        current_version = manifest.latest_version()
        current = manifest.versions().get(current_version) if current_version else None
        target = manifest.versions().get(version)
        if target is None:
            raise ValueError(f"Cannot promote unknown version '{version}'")
        if policy.should_promote(target, current):
            manifest.set_latest(version)
        else:
            raise ValueError(
                f"Promotion policy rejected {name}@{version} over {name}@{current_version}"
            )

    def list(
        self,
        name: str | None = None,
    ) -> Iterable[tuple[AdapterMetadata, Path]]:
        """Yield (metadata, artifact_dir) for all adapters or one adapter name."""
        names = [name] if name else [p.name for p in self._base_dir.iterdir() if p.is_dir()]
        for adapter_name in names:
            manifest = self._manifest(adapter_name)
            for version, meta in manifest.versions().items():
                yield meta, self._artifact_dir(adapter_name, version)

    def update_metadata(self, metadata: AdapterMetadata) -> None:
        """Update the stored metadata for an existing version without moving files."""
        manifest = self._manifest(metadata.name)
        if metadata.version not in manifest.versions():
            raise ValueError(
                f"Cannot update metadata for unknown version "
                f"{metadata.name}@{metadata.version}"
            )
        manifest.add(metadata)

    def _artifact_dir(self, name: str, version: str) -> Path:
        return self._base_dir / name / version

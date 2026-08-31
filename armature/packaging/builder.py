# armature/packaging/builder.py
from __future__ import annotations
import shutil
from datetime import datetime, timezone
from pathlib import Path
from ruamel.yaml import YAML

from armature.packaging.manifest import (
    PackageManifest, SecretsFile, SecretRequirement, Destinations, ArtifactSpec, PACKAGE_API_VERSION,
)
from armature.packaging.verifier import CompletenessVerifier, collect_api_key_envs
from armature.packaging.integrity import write_manifest_sha256

_Y = YAML()


class PackageBuildError(Exception):
    pass


class PackageBuilder:
    def build(self, *, spec: Path, out: Path, inputs: dict | None = None,
              tools: Path | None = None, requirements: Path | None = None,
              destinations: Path | None = None, runtime_inputs: list[str] | None = None,
              profile_env: dict[str, str] | None = None, archive: str | None = None) -> Path:
        # 1. validate + load spec
        from armature.spec.loader import load_spec
        try:
            loaded = load_spec(spec)
        except Exception as exc:
            raise PackageBuildError(f"spec invalid: {exc}") from exc

        # 2. create dir + copy spec
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(spec, out / "workflow.yaml")

        # 2.5 bundle context-layer src: files (covered by manifest.sha256)
        self._bundle_context_layer_srcs(loaded, spec, out)

        # 3. inputs
        _Y.dump(inputs or {}, out / "inputs.yaml")

        # 4. vendor tools
        tools_dir_rel = None
        if tools is not None:
            tools_dir_rel = "tools/"
            dest_tools = out / "tools"
            if dest_tools.exists():
                shutil.rmtree(dest_tools)
            shutil.copytree(tools, dest_tools)

        # 5. requirements
        req_rel = "requirements.txt"
        if requirements is not None:
            shutil.copyfile(requirements, out / req_rel)
        else:
            # base requirements so external tool packages can be listed by the user later;
            # vendored tools need no entry.
            (out / req_rel).write_text("# Add custom-tool dependencies here.\narmature-agents\n",
                                       encoding="utf-8")

        # 6. secrets.yaml (auto-generated from api_key_env scan)
        envs = collect_api_key_envs(loaded)
        sf = SecretsFile(required=[SecretRequirement(name=e) for e in sorted(envs)])
        _Y.dump(sf.model_dump(), out / "secrets.yaml")

        # 7. destinations.yaml
        if destinations is not None:
            shutil.copyfile(destinations, out / "destinations.yaml")
        else:
            dest = self._infer_destinations(loaded)
            _Y.dump(dest.model_dump(), out / "destinations.yaml")

        # manifest (written before verify so the verifier can read runtime_inputs)
        manifest = PackageManifest(
            name=loaded.name, version=str(loaded.version), spec="workflow.yaml",
            inputs="inputs.yaml", requirements=req_rel, requirements_lock=None,
            tools_dir=tools_dir_rel, secrets="secrets.yaml", destinations="destinations.yaml",
            runtime_inputs=runtime_inputs or [], armature_version=">=0.6.0",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        _Y.dump(manifest.model_dump(), out / "package.yaml")

        # 8. verify (aborts on fail)
        report = CompletenessVerifier().verify(out, manifest, profile_env=profile_env)
        if not report.ok:
            details = "; ".join(f"{c.check}: {c.detail}" for c in report.failures)
            raise PackageBuildError(f"package incomplete — {details}")

        # 9. README + final integrity (verifier already wrote manifest.sha256; rewrite to include README)
        (out / "README.md").write_text(
            f"# {loaded.name} v{loaded.version}\n\n{loaded.description or ''}\n\n"
            "Run with: `armature package run . --profile <env>`\n",
            encoding="utf-8",
        )
        write_manifest_sha256(out)

        # 10. archive
        if archive:
            self._archive(out, archive)
        return out

    @staticmethod
    def _bundle_context_layer_srcs(spec, spec_path: Path, out: Path) -> None:
        """Copy each layer's src: file into the package, preserving its
        spec-relative path so the packaged workflow.yaml resolves it.

        Containment guard: a src that escapes the package dir (``../``)
        aborts the build — same posture as the Docker file handlers.
        """
        for layer in getattr(spec, "context_layers", None) or []:
            if layer.src is None:
                continue
            src_file = (spec_path.parent / layer.src).resolve()
            if not src_file.is_file():
                raise PackageBuildError(
                    f"context layer '{layer.name}' src not found: {layer.src}"
                )
            dest = (out / layer.src).resolve()
            if not dest.is_relative_to(out.resolve()):
                raise PackageBuildError(
                    f"context layer '{layer.name}' src escapes the package "
                    f"dir: {layer.src}"
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_file, dest)

    @staticmethod
    def _infer_destinations(spec) -> Destinations:
        depended_on = {dep for s in spec.stages for dep in (s.depends_on or [])}
        leaves = [s for s in spec.stages if s.id not in depended_on and not s.post_run]
        from armature.spec.models import OutputMode
        artifacts = []
        for s in leaves:
            # SKIP gate stages (human gates produce no artifact) and stages with no execution type.
            if s.gate:
                continue
            if not any(getattr(s, attr, None) for attr in ("role", "tool_call", "adapter", "subagent_spec")):
                continue
            fmt = "json" if s.output_mode == OutputMode.GUIDED_JSON else "markdown"
            artifacts.append(ArtifactSpec(stage_id=s.id, name=s.id, format=fmt))
        return Destinations(artifacts=artifacts, include_trace=False)

    @staticmethod
    def _archive(pkg: Path, archive: str) -> Path:
        import tarfile, zipfile
        if archive == "tar":
            target = pkg.with_suffix(".tar")
            with tarfile.open(target, "w") as tf:
                tf.add(pkg, arcname=pkg.name)
            return target
        if archive == "zip":
            target = pkg.with_suffix(".zip")
            with zipfile.ZipFile(target, "w") as zf:
                for f in pkg.rglob("*"):
                    if f.is_file():
                        zf.write(f, arcname=f.relative_to(pkg.parent))
            return target
        raise PackageBuildError(f"unknown archive format: {archive}")
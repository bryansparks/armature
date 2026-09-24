# armature/packaging/builder.py
from __future__ import annotations
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from ruamel.yaml import YAML

from armature.packaging.manifest import (
    PackageManifest, SecretsFile, SecretRequirement, Destinations, ArtifactSpec, PACKAGE_API_VERSION,
)
from armature.packaging.verifier import CompletenessVerifier, collect_api_key_envs
from armature.packaging.integrity import write_manifest_sha256


def _dump_yaml(data, path) -> None:
    """Fresh ruamel instance per dump: an exception mid-dump (e.g. a
    non-representable object) can corrupt a shared YAML() instance's
    representer state, silently mangling every later dump in the process —
    observed as cross-test poisoning when a build aborts mid-destinations.
    """
    YAML().dump(data, path)

_log = logging.getLogger(__name__)


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

        # 2.5 bundle file references (covered by manifest.sha256): context-layer
        # src: files and subagent_spec child workflows, recursively.
        self._bundle_spec_file_refs(loaded, spec, out)

        # 3. inputs
        _dump_yaml(inputs or {}, out / "inputs.yaml")

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
        _dump_yaml(sf.model_dump(), out / "secrets.yaml")

        # 7. destinations.yaml — precedence: explicit --destinations file >
        # spec destinations section > inferred from leaf stages.
        if destinations is not None:
            shutil.copyfile(destinations, out / "destinations.yaml")
        elif loaded.destinations is not None:
            _dump_yaml(self._destinations_from_spec(loaded.destinations).model_dump(),
                    out / "destinations.yaml")
        else:
            dest = self._infer_destinations(loaded)
            _dump_yaml(dest.model_dump(), out / "destinations.yaml")

        # manifest (written before verify so the verifier can read runtime_inputs)
        manifest = PackageManifest(
            name=loaded.name, version=str(loaded.version), spec="workflow.yaml",
            inputs="inputs.yaml", requirements=req_rel, requirements_lock=None,
            tools_dir=tools_dir_rel, secrets="secrets.yaml", destinations="destinations.yaml",
            runtime_inputs=runtime_inputs or [], armature_version=">=0.6.0",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        _dump_yaml(manifest.model_dump(), out / "package.yaml")

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
    def _bundle_spec_file_refs(spec, spec_path: Path, out: Path) -> None:
        """Vendor every file the spec tree references: context-layer src:
        files and subagent_spec child workflows, recursively — children may
        reference grandchildren and their own layer files.

        Each ref is preserved at its as-written package-relative path, so the
        run-time loader (which resolves refs spec-dir first) finds the bundled
        copies: the packaged entry spec sits at the package root, and each
        bundled child sits at the ref its parent used.

        Containment: a ref that escapes the package dir (``../``) aborts the
        build — same posture as the Docker file handlers. Reference cycles
        (a child pointing back at an ancestor) are fine: the visited set stops
        the walk, and every file still ships exactly where resolution expects.

        Absolute subagent_spec refs are rejected: they can't be vendored
        portably. Still-templated refs (``{{ ... }}`` the load render didn't
        substitute) are skipped with a warning — the package must provide the
        file at run time.
        """
        from armature.packaging.refs import walk_spec_file_refs
        from armature.spec.loader import resolve_spec_ref

        out_resolved = out.resolve()
        walked: set[Path] = set()

        def vendor_level(subagent_refs: list[str], layer_srcs: list[str],
                         level_dir: Path, pkg_dir: Path, label: str) -> None:
            for src in layer_srcs:
                src_file = (level_dir / src).resolve()
                if not src_file.is_file():
                    raise PackageBuildError(
                        f"{label} context layer src not found: {src}"
                    )
                dest = (pkg_dir / src).resolve()
                if not dest.is_relative_to(out_resolved):
                    raise PackageBuildError(
                        f"{label} context layer src escapes the package dir: {src}"
                    )
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_file, dest)
            for ref in subagent_refs:
                if "{{" in ref:
                    _log.warning(
                        "subagent_spec '%s' (%s) is templated — not vendored; "
                        "the package must provide it at run time", ref, label,
                    )
                    continue
                if Path(ref).is_absolute():
                    raise PackageBuildError(
                        f"absolute subagent_spec '{ref}' is not portable — "
                        f"rewrite it as a path relative to the spec"
                    )
                src_file = resolve_spec_ref(ref, level_dir)
                if src_file is None:
                    raise PackageBuildError(
                        f"subagent_spec not found (looked in {level_dir} and "
                        f"cwd): {ref}"
                    )
                dest = (pkg_dir / ref).resolve()
                if not dest.is_relative_to(out_resolved):
                    raise PackageBuildError(
                        f"subagent_spec escapes the package dir: {ref}"
                    )
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_file, dest)
                src_file = src_file.resolve()
                if src_file in walked:
                    continue
                walked.add(src_file)
                child_refs, child_srcs = walk_spec_file_refs(src_file)
                vendor_level(child_refs, child_srcs, src_file.parent,
                             dest.parent, f"child spec {ref}")

        vendor_level(
            [s.subagent_spec for s in spec.stages if s.subagent_spec],
            [l.src for l in spec.context_layers if l.src],
            spec_path.parent, out, "spec",
        )

    @staticmethod
    def _destinations_from_spec(spec_dest) -> Destinations:
        """Convert the spec's destinations section into the packaging model.
        ArtifactSpec validates formats (and rejects unknown artifact fields)
        here, at build time rather than at run time."""
        return Destinations(
            artifacts=[ArtifactSpec(**a.model_dump()) for a in spec_dest.artifacts],
            include_trace=spec_dest.include_trace,
        )

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
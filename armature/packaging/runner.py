# armature/packaging/runner.py
from __future__ import annotations
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from ruamel.yaml import YAML

from armature.packaging.manifest import PackageManifest, SecretsFile, Destinations, ResultsManifest
from armature.packaging.verifier import CompletenessVerifier
from armature.packaging.integrity import verify_integrity
from armature.packaging.results import ResultsWriter

_Y = YAML()


class PackageError(Exception):
    pass


class SecretMissingError(PackageError):
    pass


def _default_harness_factory(spec, session_dir, traces_db):
    from armature.runtime.engine import Harness
    return Harness(spec=spec, session_dir=session_dir, traces_db=traces_db)


class PackageRunner:
    def __init__(self, harness_factory=_default_harness_factory, skip_deps_install: bool = False):
        self._harness_factory = harness_factory
        self._skip_deps_install = skip_deps_install

    def run_sync(self, pkg_dir: Path, results_dir: Path, *,
                 profile_path: Path | None = None, inputs_override: dict | None = None,
                 include_trace: bool | None = None, secrets_path: Path | None = None) -> "ResultsManifest":
        return asyncio.run(self.run(pkg_dir, results_dir, profile_path=profile_path,
                                    inputs_override=inputs_override, include_trace=include_trace,
                                    secrets_path=secrets_path))

    async def run(self, pkg_dir: Path, results_dir: Path, *,
                  profile_path: Path | None = None, inputs_override: dict | None = None,
                  include_trace: bool | None = None, secrets_path: Path | None = None):
        from armature import __version__ as armature_version

        started = datetime.now(timezone.utc)
        manifest = PackageManifest.model_validate(_Y.load(pkg_dir / "package.yaml"))
        destinations = Destinations.model_validate(_Y.load(pkg_dir / manifest.destinations))
        if include_trace is not None:
            destinations = destinations.model_copy(update={"include_trace": include_trace})

        try:
            # R1 integrity
            if not verify_integrity(pkg_dir):
                raise PackageError("integrity check failed — package corrupt or tampered")
            # R2 re-verify (read-only integrity: the package mount may be :ro,
            # and R1 already verified integrity above)
            report = CompletenessVerifier().verify(pkg_dir, manifest, write_integrity=False)
            if not report.ok:
                raise PackageError("re-verify failed: " +
                                   "; ".join(f"{c.check}: {c.detail}" for c in report.failures))
            # R3 secrets (fail closed)
            self._inject_secrets(pkg_dir, manifest, profile_path, secrets_path)
            # R4 deps
            # Vendored tools are package data, not pip deps — they must be importable
            # even when dependency installation is skipped (tests + later e2e rely on this).
            self._install_tools_dir(pkg_dir, manifest)
            if not self._skip_deps_install:
                self._install_deps(pkg_dir, manifest)
            # R5 inputs
            inputs = self._load_inputs(pkg_dir, manifest, inputs_override)
            # R6 run
            from armature.spec.loader import load_spec
            spec = load_spec(pkg_dir / manifest.spec)
            session_dir = Path(results_dir) / "_pending" / "session"
            traces_db = Path(results_dir) / "_pending" / "traces.db"
            session_dir.mkdir(parents=True, exist_ok=True)
            harness = self._harness_factory(spec, session_dir, traces_db)
            result = await harness.run(inputs)
            run_id = getattr(harness, "_run_id", "run00000000")
            trace_records = []
            try:
                trace_records = await harness._traces.query_by_run(run_id)
            except Exception:
                pass
            # R7 capture + deliver
            finished = datetime.now(timezone.utc)
            writer = ResultsWriter(results_dir)
            run_dir = writer.write(
                run_id=run_id, package_name=manifest.name, package_version=manifest.version,
                destinations=destinations, result=result, trace_records=trace_records,
                status="complete", started_at=started.isoformat(), finished_at=finished.isoformat(),
                duration_s=(finished - started).total_seconds(), exit_code=0,
                armature_version=armature_version,
            )
            receipt = json.loads((run_dir / "receipt.json").read_text())
            return ResultsManifest.model_validate(receipt)
        except Exception as exc:
            finished = datetime.now(timezone.utc)
            writer = ResultsWriter(results_dir)
            run_id = "failed"
            run_dir = writer.write(
                run_id=run_id, package_name=manifest.name, package_version=manifest.version,
                destinations=destinations, result={}, trace_records=[],
                status="failed", started_at=started.isoformat(), finished_at=finished.isoformat(),
                duration_s=(finished - started).total_seconds(), exit_code=1,
                armature_version=armature_version, error=str(exc),
            )
            receipt = json.loads((run_dir / "receipt.json").read_text())
            r = ResultsManifest.model_validate(receipt)
            if isinstance(exc, (SecretMissingError,)):
                raise
            return r


    # -- helpers -------------------------------------------------------------
    def _inject_secrets(self, pkg_dir, manifest, profile_path, secrets_path):
        sf = SecretsFile.model_validate(_Y.load(pkg_dir / manifest.secrets) or {"required": []})
        env = dict(os.environ)
        src = secrets_path or profile_path
        if src and Path(src).exists():
            env.update(self._parse_env_file(Path(src)))
        missing = [r.name for r in sf.required if not env.get(r.name)]
        if missing:
            raise SecretMissingError(f"missing required secrets: {missing}")
        # inject so litellm/api_key_env resolution sees them
        for r in sf.required:
            os.environ[r.name] = env[r.name]

    @staticmethod
    def _parse_env_file(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
        return out

    def _install_deps(self, pkg_dir, manifest):
        import subprocess
        req = pkg_dir / manifest.requirements if manifest.requirements else None
        if not req or not req.exists():
            return
        venv_dir = Path("/tmp/armature-venv")
        try:
            venv_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req),
                            "--target", str(venv_dir), "--quiet"],
                           check=True, capture_output=True)
            for p in (str(venv_dir),):
                if p not in sys.path:
                    sys.path.insert(0, p)
        except Exception:
            # Non-fatal in environments where deps are preinstalled; the import
            # will fail later with a clearer error if something is truly missing.
            pass

    def _install_tools_dir(self, pkg_dir, manifest):
        if manifest.tools_dir:
            tp = str(pkg_dir / manifest.tools_dir)
            if tp not in sys.path:
                sys.path.insert(0, tp)

    @staticmethod
    def _load_inputs(pkg_dir, manifest, override):
        bundled = _Y.load(pkg_dir / manifest.inputs) or {}
        merged = dict(bundled)
        if override:
            merged.update(override)
        return merged
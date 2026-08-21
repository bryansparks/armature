# armature/packaging/verifier.py
from __future__ import annotations
import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field
from ruamel.yaml import YAML

from armature.packaging.manifest import PackageManifest, SecretsFile, Destinations
from armature.packaging.integrity import write_manifest_sha256

_Y = YAML()


class CheckResult(BaseModel):
    check: str
    status: Literal["pass", "fail", "warn"]
    detail: str = ""


class VerificationReport(BaseModel):
    checks: list[CheckResult] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "fail"]


_TIER_NAMES = ("tiny", "small", "medium", "large", "frontier")


def collect_api_key_envs(spec) -> set[str]:
    """Every api_key_env referenced by any model tier (named or custom)."""
    envs: set[str] = set()
    mt = spec.model_tiers
    for n in _TIER_NAMES:
        cfg = getattr(mt, n, None)
        if cfg and getattr(cfg, "api_key_env", None):
            envs.add(cfg.api_key_env)
    extra = getattr(mt, "__pydantic_extra__", None) or {}
    for cfg in extra.values():
        if getattr(cfg, "api_key_env", None):
            envs.add(cfg.api_key_env)
    return envs


def _has_execution_type(stage) -> bool:
    return any(getattr(stage, attr, None) for attr in ("role", "tool_call", "adapter", "subagent_spec", "gate"))


class CompletenessVerifier:
    def verify(self, pkg_dir: Path, manifest: PackageManifest,
               profile_env: dict[str, str] | None = None) -> VerificationReport:
        report = VerificationReport()
        report.checks.append(self._v1_spec(pkg_dir, manifest))
        report.checks.append(self._v2_inputs(pkg_dir, manifest))
        report.checks.append(self._v3_secrets(pkg_dir, manifest, profile_env))
        report.checks.append(self._v4_tools(pkg_dir, manifest))
        report.checks.append(self._v5_sandbox(pkg_dir, manifest))
        report.checks.append(self._v6_artifacts(pkg_dir, manifest))
        report.checks.append(self._v7_deps(pkg_dir, manifest))
        report.checks.append(self._v8_integrity(pkg_dir))
        return report

    # -- individual checks ---------------------------------------------------
    def _spec(self, pkg_dir, manifest):
        from armature.spec.loader import load_spec
        return load_spec(pkg_dir / manifest.spec)

    def _v1_spec(self, pkg_dir, manifest) -> CheckResult:
        from armature.spec.loader import load_spec
        try:
            load_spec(pkg_dir / manifest.spec)
            return CheckResult(check="SPEC_VALID", status="pass", detail="spec loads")
        except Exception as exc:
            return CheckResult(check="SPEC_VALID", status="fail", detail=f"spec invalid: {exc}")

    def _v2_inputs(self, pkg_dir, manifest) -> CheckResult:
        try:
            spec = self._spec(pkg_dir, manifest)
        except Exception:
            return CheckResult(check="INPUTS_COMPLETE", status="fail", detail="spec did not load")
        declared = set()
        if spec.contracts and spec.contracts.inputs:
            declared = {i.get("name") for i in spec.contracts.inputs if i.get("name")}
        bundled = self._read_yaml(pkg_dir / manifest.inputs) or {}
        bundled_keys = set(bundled.keys())
        runtime = set(manifest.runtime_inputs)
        missing = declared - bundled_keys - runtime
        if missing:
            return CheckResult(check="INPUTS_COMPLETE", status="fail",
                               detail=f"missing inputs: {sorted(missing)}")
        return CheckResult(check="INPUTS_COMPLETE", status="pass", detail="all inputs present")

    def _v3_secrets(self, pkg_dir, manifest, profile_env) -> CheckResult:
        try:
            spec = self._spec(pkg_dir, manifest)
        except Exception:
            return CheckResult(check="SECRETS_DECLARED", status="fail", detail="spec did not load")
        required_envs = collect_api_key_envs(spec)
        sf = SecretsFile.model_validate(self._read_yaml(pkg_dir / manifest.secrets) or {"required": []})
        declared = {r.name for r in sf.required}
        undeclared = required_envs - declared
        if undeclared:
            return CheckResult(check="SECRETS_DECLARED", status="fail",
                               detail=f"undeclared secrets: {sorted(undeclared)}")
        if profile_env is not None:
            env = dict(os.environ)
            env.update(profile_env)
            missing_vals = [n for n in required_envs if not env.get(n)]
            if missing_vals:
                return CheckResult(check="SECRETS_DECLARED", status="fail",
                                   detail=f"unresolvable in profile: {sorted(missing_vals)}")
        return CheckResult(check="SECRETS_DECLARED", status="pass",
                           detail=f"{len(required_envs)} secret(s) declared")

    def _v4_tools(self, pkg_dir, manifest) -> CheckResult:
        try:
            spec = self._spec(pkg_dir, manifest)
        except Exception:
            return CheckResult(check="TOOLS_RESOLVABLE", status="fail", detail="spec did not load")
        req_text = ""
        req_path = pkg_dir / manifest.requirements if manifest.requirements else None
        if req_path and req_path.exists():
            req_text = req_path.read_text(encoding="utf-8")
        req_pkgs = {self._req_top_level(line) for line in req_text.splitlines()
                    if line.strip() and not line.strip().startswith("#")}
        req_pkgs.discard(None)
        tools_root = pkg_dir / manifest.tools_dir if manifest.tools_dir else None
        unresolvable = []
        for t in (spec.tools or []):
            top = t.module.split(".")[0]
            vendored = tools_root is not None and (tools_root / top.replace("-", "_")).exists()
            if not vendored and top.replace("-", "_") not in req_pkgs and top not in req_pkgs:
                unresolvable.append(t.module)
        if unresolvable:
            return CheckResult(check="TOOLS_RESOLVABLE", status="fail",
                               detail=f"unresolvable tools: {unresolvable}")
        return CheckResult(check="TOOLS_RESOLVABLE", status="pass", detail="all tools resolvable")

    def _v5_sandbox(self, pkg_dir, manifest) -> CheckResult:
        try:
            spec = self._spec(pkg_dir, manifest)
        except Exception:
            return CheckResult(check="SANDBOX_IMAGE", status="fail", detail="spec did not load")
        sb = getattr(spec, "sandbox", None)
        mode = getattr(sb, "mode", None)
        image = getattr(sb, "image", None)
        from armature.spec.models import SandboxMode
        if mode is None or mode == SandboxMode.NONE or not image:
            return CheckResult(check="SANDBOX_IMAGE", status="pass", detail="no docker sandbox")
        # Best-effort: warn if not present locally (pulling happens at run).
        import shutil, subprocess
        if not shutil.which("docker"):
            return CheckResult(check="SANDBOX_IMAGE", status="warn",
                               detail=f"docker not on PATH; image '{image}' will pull at run")
        try:
            subprocess.run(["docker", "image", "inspect", image],
                           capture_output=True, timeout=30, check=True)
            return CheckResult(check="SANDBOX_IMAGE", status="pass", detail=f"image '{image}' present")
        except Exception:
            return CheckResult(check="SANDBOX_IMAGE", status="warn",
                               detail=f"image '{image}' not local; will pull at run")

    def _v6_artifacts(self, pkg_dir, manifest) -> CheckResult:
        try:
            spec = self._spec(pkg_dir, manifest)
        except Exception:
            return CheckResult(check="ARTIFACTS_VALID", status="fail", detail="spec did not load")
        dest = Destinations.model_validate(self._read_yaml(pkg_dir / manifest.destinations) or {})
        stage_ids = {s.id for s in spec.stages}
        bad = []
        for a in dest.artifacts:
            if a.stage_id not in stage_ids:
                bad.append(f"{a.name}: stage '{a.stage_id}' not in spec")
            else:
                st = next(s for s in spec.stages if s.id == a.stage_id)
                if not _has_execution_type(st):
                    bad.append(f"{a.name}: stage '{a.stage_id}' produces no output")
        if bad:
            return CheckResult(check="ARTIFACTS_VALID", status="fail", detail="; ".join(bad))
        return CheckResult(check="ARTIFACTS_VALID", status="pass",
                           detail=f"{len(dest.artifacts)} artifact(s) valid")

    def _v7_deps(self, pkg_dir, manifest) -> CheckResult:
        req_path = pkg_dir / manifest.requirements if manifest.requirements else None
        if not req_path or not req_path.exists():
            return CheckResult(check="DEPS_RESOLVE", status="pass", detail="no requirements.txt")
        lines = [l.strip() for l in req_path.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        bad = [l for l in lines if not l.split()[0]]
        if bad:
            return CheckResult(check="DEPS_RESOLVE", status="fail", detail=f"unparseable lines: {bad}")
        return CheckResult(check="DEPS_RESOLVE", status="pass", detail=f"{len(lines)} requirement(s)")

    def _v8_integrity(self, pkg_dir) -> CheckResult:
        write_manifest_sha256(pkg_dir)
        return CheckResult(check="INTEGRITY", status="pass", detail="manifest.sha256 written")

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _read_yaml(path: Path):
        if not path.exists():
            return None
        return _Y.load(path)

    @staticmethod
    def _req_top_level(line: str) -> str | None:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-e ") or line.startswith("git+"):
            return None
        token = line.split(";")[0].split()[0]
        # strip extras/specifiers
        for sep in ("[", "=", "<", ">", "!", "~", " "):
            if sep in token:
                token = token.split(sep)[0]
        return token.lower() or None
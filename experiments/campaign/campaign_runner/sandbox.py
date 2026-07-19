"""Per-campaign isolation: HOME redirect + trace-db path + working-spec copy.

`armature run` has no --traces flag and writes to ~/.armature/traces.db, so the
only way to give a campaign its own trace DB is to point HOME at the sandbox.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from campaign_runner.plan import CampaignPlan


class Sandbox:
    def __init__(self, plan: CampaignPlan, root: Path) -> None:
        self.plan = plan
        self.dir = (Path(root) / plan.name).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / ".armature").mkdir(exist_ok=True)
        self.trace_db = self.dir / ".armature" / "traces.db"
        self.working_spec = self.dir / "spec_work.yml"

    def env(self, extra: dict | None = None) -> dict:
        """A subprocess env with HOME redirected to the sandbox dir."""
        env = dict(os.environ)
        env["HOME"] = str(self.dir)
        # Ensure `python -m armature` (the campaign subprocess form) imports the
        # worktree's armature — the build with the memory pyramid — not the
        # main-checkout install the bare `armature` console script resolves to.
        # `python -m armature` puts cwd on sys.path[0]; if the campaign is
        # launched from outside the worktree root, cwd alone would not find the
        # worktree package and Python would fall back to the editable install
        # (main checkout, no Phase 3). Prepending the worktree root
        # (self.dir.parent, since self.dir = <root>/<plan.name>) to PYTHONPATH
        # makes the worktree build win regardless of the subprocess cwd.
        worktree_root = str(self.dir.parent)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = worktree_root + (os.pathsep + existing if existing else "")
        if extra:
            env.update(extra)
        return env

    def copy_working_spec(self, source: Path) -> Path:
        shutil.copyfile(source, self.working_spec)
        return self.working_spec

    def working_spec_for(self, phase_id: str) -> Path:
        return self.dir / f"spec_work_{phase_id}.yml"

    def copy_working_spec_to(self, source: Path, target: Path) -> Path:
        shutil.copyfile(source, target)
        return target

    def apply_tier_override(self, spec_path: Path, override) -> None:
        """Rewrite the spec's model_tiers block from the override's tiers,
        mapping by tier NAME so a campaign can keep worker tiers cheap while
        giving guided_json/escalation tiers a more capable model. Preserves
        tier names so stage model_tier references still resolve. Idempotent.
        No-op when override is None/disabled/empty or the spec has no model_tiers.

        Name mapping: spec tier `T` -> override.tiers[T] if present, else
        override.tiers['default'], else override.tiers['tiny'], else the first
        override tier. Back-compatible: an override with only a `tiny` tier maps
        every spec tier to tiny (the original flatten-all behavior).

        Surgical: only the top-level `model_tiers:` block is rewritten — the rest
        of the file is left byte-for-byte intact. Armature's spec loader renders
        the whole file as a Jinja2 template, so a full yaml round-trip would
        reformat multi-line block scalars (descriptions) into escaped
        double-quoted strings with literal backslashes that raise
        TemplateSyntaxError. The tier configs are flat scalar dicts with no
        multi-line strings, so serializing just model_tiers is safe."""
        import re
        import yaml
        if override is None or not override.apply or not override.tiers:
            return
        text = spec_path.read_text()
        parsed = yaml.safe_load(text) or {}
        tiers = parsed.get("model_tiers") or {}
        if not tiers:
            return
        default = (override.tiers.get("default")
                   or override.tiers.get("tiny")
                   or next(iter(override.tiers.values())))
        new_tiers = {name: dict(override.tiers.get(name, default)) for name in tiers}
        new_block = yaml.safe_dump({"model_tiers": new_tiers}, sort_keys=False)

        lines = text.splitlines(keepends=True)
        start = None
        for idx, ln in enumerate(lines):
            if ln[:1].isspace():
                continue
            m = re.match(r"^model_tiers:\s*(\S.*)?$", ln.rstrip("\n"))
            if not m:
                continue
            start = idx
            inline = bool(m.group(1))   # `model_tiers: {small: ...}` (one line)
            break
        if start is None:
            return
        if inline:
            end = start + 1
        else:
            # block style: consume indented / blank lines until the next
            # top-level key
            end = len(lines)
            for j in range(start + 1, len(lines)):
                if lines[j].strip() == "":
                    continue
                if not lines[j][:1].isspace():
                    end = j
                    break
        spec_path.write_text("".join(lines[:start]) + new_block + "".join(lines[end:]))

    def write_working_spec(self, text: str) -> Path:
        self.working_spec.write_text(text)
        return self.working_spec

    def reset_trace_db(self) -> None:
        """Delete the trace DB so the next `armature run` starts a fresh history.

        `armature improve` / `armature dashboard` compute HQS across the shared
        DB's last ~200 traces; without a reset, a degradation phase's few
        failure runs are diluted by prior phases' successes and self_improve
        never fires. The recording is untouched (it captured each run's trace
        rows at record time), so replay still reproduces the full campaign.
        """
        try:
            self.trace_db.unlink()
        except FileNotFoundError:
            pass
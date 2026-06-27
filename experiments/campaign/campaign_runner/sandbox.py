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
        if extra:
            env.update(extra)
        return env

    def copy_working_spec(self, source: Path) -> Path:
        shutil.copyfile(source, self.working_spec)
        return self.working_spec

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
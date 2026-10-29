"""Copy the ops prompts, missions, and principles the remaining workflows need into migrate_ops' document files.

Run by hand when ops changes: ``python tests/fixtures/ops/port_documents.py <armature-ops checkout>``.

Prompt files are copied byte for byte. A spec's inline role description,
mission, and context layer become a document of their own. The ten review
roles and the PR team lead are not copied, because S14b seeds them from
``armature/server/starters/roles``. ``MANIFEST.json`` records each
document's ops source and its sha256, so a test can tell an edit from a port.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from ruamel.yaml import YAML

OUT = Path(__file__).parents[3] / "armature/server/migrate_ops/documents"
PROMPTS = ("_shared", "_shared_jira_consult", "_shared_jira_review", "incremental_pr", "pr_fix", "pr_fix_merge",
           "jira_work", "jira_work_backend", "jira_work_aws", "jira_work_aws_backend", "jira_work_aws_ios",
           "ios_port", "ios_port_aws", "localization_translate")
ROLES = {("slack-intake", "classify"): "slack_intake_classify", ("repo-review", "team_lead"): "repo_review_team_lead",
         ("repo-review", "digest"): "repo_review_digest", ("repo-review", "self_analyst"): "repo_review_director",
         ("development", "platform"): "development_platform", ("development", "brief"): "development_brief",
         ("ios-port", "decide"): "ios_port_decide"}
SPECS = ("slack-intake", "repo-review", "code-review", "pr-fix", "development", "ios-port", "localization")
DATA = {"templates/slack_intake_reply.txt": "templates/slack_intake_reply.txt",
        "schemas/incremental.json": "config/incremental-schema.json",
        "schemas/team-lead.json": "config/team-lead-schema.json"}


def _put(manifest: dict, kind: str, key: str, text: str, source: str) -> None:
    path = OUT / kind / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    manifest[f"{kind}/{key}"] = {"source": source, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def main(ops: Path) -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    manifest: dict = {}
    for name in PROMPTS:
        _put(manifest, "prompt", name, (ops / f"prompts/{name}.md").read_text(), f"prompts/{name}.md")
    for spec_name in SPECS:
        spec = YAML(typ="safe").load((ops / f"specs/{spec_name}.yml").read_text())
        where = f"specs/{spec_name}.yml"
        if spec.get("mission"):
            _put(manifest, "mission", spec_name, spec["mission"], f"{where} mission")
        for layer in spec.get("context_layers") or []:
            key = f"{spec_name.replace('-', '_')}_{layer['name']}"
            _put(manifest, "prompt", key, layer["content"], f"{where} context_layers.{layer['name']}")
        for stage in spec["stages"]:
            key = ROLES.get((spec_name, stage["id"]))
            if key:
                _put(manifest, "prompt", key, stage["role"]["description"], f"{where} stages.{stage['id']}.role")
    for target, source in DATA.items():
        path = OUT / target
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (ops / source).read_text()
        path.write_text(text)
        manifest[target] = {"source": source, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Ported {len(manifest)} files to {OUT}.")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

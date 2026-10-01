"""Mission document models + loader (slice 1 of the missions design)."""
import pytest
from pathlib import Path
from armature.spec.models import CLOSURE_REASONS
from armature.spec.mission import MissionSpec, WorkUnit, load_mission


VALID_MISSION = """\
name: campaign-pretzel
version: "1.0"
description: Ad campaign chain for Dangerous Pretzel.
objective: |
  Launch the Dangerous Pretzel ad campaign by Nov 15.
posture: human-led
budget_usd: 200.0
work:
  - id: brand-approval
    title: Human approves brand voice examples
    workflow: ../06_human_in_the_loop.yml
    posture: human-led
  - id: hero-headlines
    title: Write hero headline variants
    objective: 12 variants across 3 tones, each under 40 characters.
    workflow: ../11_iterative_refinement.yml
    inputs:
      repo_path: ~/projects/dangerous-pretzel-ads
    requires: [brand-approval]
    posture: delegated
    max_budget_usd: 1.0
    timeout_hours: 1.0
    max_attempts: 2
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "campaign.mission.yml"
    p.write_text(text)
    return p


def test_closure_reasons_constant():
    assert CLOSURE_REASONS == ("done_no_follow_on", "handed_off", "blocked_on", "escalation")


def test_load_valid_mission(tmp_path):
    mission = load_mission(_write(tmp_path, VALID_MISSION))
    assert isinstance(mission, MissionSpec)
    assert mission.name == "campaign-pretzel"
    assert mission.posture == "human-led"
    assert mission.budget_usd == 200.0
    assert [u.id for u in mission.work] == ["brand-approval", "hero-headlines"]


def test_unit_defaults(tmp_path):
    mission = load_mission(_write(tmp_path, VALID_MISSION))
    unit = mission.work[0]
    assert unit.objective == ""          # optional, defaults empty
    assert unit.inputs == {}
    assert unit.requires == []
    assert unit.posture == "human-led"
    assert unit.max_budget_usd is None
    assert unit.max_attempts == 2


def test_posture_inherits_when_omitted(tmp_path):
    text = VALID_MISSION.replace("    posture: delegated\n", "")
    mission = load_mission(_write(tmp_path, text))
    assert mission.work[1].posture is None   # None = inherit mission default (resolved in Task 2's helper)


def test_workflow_path_stamped_for_path_like(tmp_path):
    mission = load_mission(_write(tmp_path, VALID_MISSION))
    assert mission.work[0].workflow_path == str((tmp_path / "../06_human_in_the_loop.yml").resolve())


def test_workflow_path_none_for_bare_name(tmp_path):
    text = VALID_MISSION.replace("workflow: ../06_human_in_the_loop.yml", "workflow: armature-code-worker")
    mission = load_mission(_write(tmp_path, text))
    assert mission.work[0].workflow_path is None


def test_unknown_field_rejected(tmp_path):
    text = VALID_MISSION.replace("version: \"1.0\"", "versio: \"1.0\"")  # typo'd key
    with pytest.raises(ValueError, match="versio"):
        load_mission(_write(tmp_path, text))


def test_unknown_unit_field_rejected(tmp_path):
    text = VALID_MISSION.replace("    max_attempts: 2", "    max_atempts: 2")
    with pytest.raises(ValueError, match="max_atempts"):
        load_mission(_write(tmp_path, text))


def test_work_as_mapping_fails_cleanly(tmp_path):
    text = VALID_MISSION.split("work:")[0] + """work:
  brand-approval:
    title: broken shape
"""
    with pytest.raises(ValueError, match="work"):
        load_mission(_write(tmp_path, text))


def test_bad_yaml_fails_cleanly(tmp_path):
    p = tmp_path / "bad.mission.yml"
    p.write_text("name: [unclosed\n  - ]bad")
    with pytest.raises(ValueError):
        load_mission(p)


def test_missing_file_fails_cleanly(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_mission(tmp_path / "nope.mission.yml")
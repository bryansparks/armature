import json
from pathlib import Path
from campaign_runner import runner, record
from campaign_runner.plan import load_plan


def test_replay_byte_matches_committed_campaign_jsonl(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[3]
    rec_dir = repo / "experiments/campaign/tests/fixtures/demo_recording"
    plan_path = repo / "experiments/campaign/plans/quick.yml"
    committed = rec_dir / "campaign.expected.jsonl"
    plan = load_plan(plan_path)
    src = repo / "experiments/campaign/tests/fixtures/sample_spec.yml"
    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    # no armature calls in replay
    class FakeDrv:
        def __init__(self, sb, rec): pass
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)
    result = r.replay(rec_dir)
    got = [json.dumps(json.loads(l), sort_keys=True) for l in result.campaign_jsonl.read_text().splitlines() if l.strip()]
    expected = [json.dumps(json.loads(l), sort_keys=True) for l in committed.read_text().splitlines() if l.strip()]
    assert got == expected
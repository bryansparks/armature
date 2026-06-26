from pathlib import Path


def test_cli_replay_demo_recording(tmp_path):
    repo = Path(__file__).resolve().parents[3]   # armature repo root
    camp = repo / "experiments/campaign"
    rec = camp / "tests/fixtures/demo_recording"
    import campaign_runner.cli as cli
    # run the entrypoint in replay mode against the bundled recording,
    # writing artifacts into tmp (not the repo)
    code = cli.main([str(camp / "plans/quick.yml"), "--replay", str(rec),
                     "--out-dir", str(tmp_path / "out")])
    assert code == 0
    assert (tmp_path / "out" / "quick-demo" / "report.html").exists()
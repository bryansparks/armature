import pytest
from pathlib import Path
from datetime import datetime, timezone
import aiosqlite
from armature.state.tracks import TrackStore
from armature.state.profile import ProfileStore
from armature.state.knowledge import KnowledgeStore, KnowledgeRecord


async def _seed_knowledge(path: Path, workflow: str, n: int) -> list[int]:
    """Insert n live knowledge records and return their ids."""
    ks = KnowledgeStore(path)
    await ks.init()
    ids: list[int] = []
    for i in range(n):
        rec = KnowledgeRecord(
            workflow_name=workflow,
            entity=f"e{i}",
            fact=f"fact {i}",
            confidence=0.8,
            source_run_id="r1",
        )
        rid = await ks.record(rec)
        ids.append(rid)
    return ids


async def test_upsert_track_and_get(tmp_path):
    db = tmp_path / "k.db"
    ids = await _seed_knowledge(db, "wf", 1)
    ts = TrackStore(db)
    await ts.init()
    res = await ts.upsert_track("wf", "auth", "Auth patterns", "summary text", None, ids, 2000, 20)
    assert res["track_id"] == "auth"
    assert res["dropped_evidence"] == []
    got = await ts.get_track("wf", "auth")
    assert got is not None
    assert got["track_id"] == "auth"
    assert got["title"] == "Auth patterns"
    assert got["summary"] == "summary text"
    assert got["evidence_links"] == ids


async def test_list_tracks(tmp_path):
    db = tmp_path / "k.db"
    ts = TrackStore(db)
    await ts.init()
    await ts.upsert_track("wf", "a", "A", "s", None, [], 2000, 20)
    await ts.upsert_track("wf", "b", "B", "s", None, [], 2000, 20)
    tracks = await ts.list_tracks("wf")
    assert {t["track_id"] for t in tracks} == {"a", "b"}


async def test_track_budget_rejects_21st(tmp_path):
    db = tmp_path / "k.db"
    ts = TrackStore(db)
    await ts.init()
    for i in range(20):
        r = await ts.upsert_track("wf", f"t{i}", f"T{i}", "s", None, [], 2000, 20)
        assert "error" not in r
    res = await ts.upsert_track("wf", "t20", "T20", "s", None, [], 2000, 20)
    assert "error" in res
    assert "track_budget" in res["error"]


async def test_track_budget_allows_update_existing(tmp_path):
    db = tmp_path / "k.db"
    ts = TrackStore(db)
    await ts.init()
    for i in range(20):
        await ts.upsert_track("wf", f"t{i}", f"T{i}", "s", None, [], 2000, 20)
    # update existing t0 — allowed even at budget cap
    res = await ts.upsert_track("wf", "t0", "T0-updated", "s2", None, [], 2000, 20)
    assert "error" not in res
    got = await ts.get_track("wf", "t0")
    assert got["title"] == "T0-updated"


async def test_track_summary_over_budget_rejected(tmp_path):
    db = tmp_path / "k.db"
    ts = TrackStore(db)
    await ts.init()
    res = await ts.upsert_track("wf", "t", "T", "x" * 100, None, [], 50, 20)
    assert "error" in res
    assert "char_budget" in res["error"]


async def test_track_invalid_evidence_links_dropped(tmp_path):
    db = tmp_path / "k.db"
    ids = await _seed_knowledge(db, "wf", 1)  # only id 1 exists
    ts = TrackStore(db)
    await ts.init()
    res = await ts.upsert_track("wf", "t", "T", "s", None, [ids[0], 999, 1000], 2000, 20)
    assert res["dropped_evidence"] == [999, 1000]
    got = await ts.get_track("wf", "t")
    assert got["evidence_links"] == [ids[0]]


async def test_track_superseded_evidence_treated_invalid(tmp_path):
    db = tmp_path / "k.db"
    ks = KnowledgeStore(db)
    await ks.init()
    rid = await ks.record(KnowledgeRecord(workflow_name="wf", entity="e", fact="f", confidence=0.8, source_run_id="r1"))
    await ks.set_superseded(rid, 999)  # mark superseded
    ts = TrackStore(db)
    await ts.init()
    res = await ts.upsert_track("wf", "t", "T", "s", None, [rid], 2000, 20)
    assert res["dropped_evidence"] == [rid]


async def test_track_last_updated_at_and_count(tmp_path):
    db = tmp_path / "k.db"
    ts = TrackStore(db)
    await ts.init()
    assert await ts.count("wf") == 0
    assert await ts.last_updated_at("wf") is None
    await ts.upsert_track("wf", "a", "A", "s", None, [], 2000, 20)
    assert await ts.count("wf") == 1
    assert await ts.last_updated_at("wf") is not None


async def test_upsert_profile_and_get(tmp_path):
    db = tmp_path / "k.db"
    ps = ProfileStore(db)
    await ps.init()
    res = await ps.upsert_profile("wf", "team profile body", 2000)
    assert "updated_at" in res
    assert await ps.get_profile("wf") == "team profile body"


async def test_profile_over_budget_rejected(tmp_path):
    db = tmp_path / "k.db"
    ps = ProfileStore(db)
    await ps.init()
    res = await ps.upsert_profile("wf", "x" * 100, 50)
    assert "error" in res


async def test_profile_upsert_replaces(tmp_path):
    db = tmp_path / "k.db"
    ps = ProfileStore(db)
    await ps.init()
    await ps.upsert_profile("wf", "first", 2000)
    await ps.upsert_profile("wf", "second", 2000)
    assert await ps.get_profile("wf") == "second"


async def test_init_idempotent(tmp_path):
    db = tmp_path / "k.db"
    ts = TrackStore(db)
    await ts.init()
    await ts.init()  # no error
    ps = ProfileStore(db)
    await ps.init()
    await ps.init()
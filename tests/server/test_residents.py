"""Residents and their event queue: the tables, and the jobs column that links a run to one."""
from sqlalchemy import inspect


def test_resident_tables_exist(engine):
    names = inspect(engine).get_table_names()
    assert "residents" in names and "resident_events" in names
    cols = {c["name"] for c in inspect(engine).get_columns("residents")}
    assert {"workflow_version", "instance_key", "staged", "spent_usd_day", "spent_day",
            "turns_day", "idle_turns_day", "emits_day", "active_job_id"} <= cols
    event_cols = {c["name"] for c in inspect(engine).get_columns("resident_events")}
    assert {"seq", "kind", "reply", "reply_to", "dedupe_key", "dry_run", "handled"} <= event_cols
    jobs_cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert "resident_id" in jobs_cols
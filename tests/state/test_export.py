"""Tests for TraceExporter — SFT/DPO training data export from high-quality traces."""
import json
import pytest
from pathlib import Path
from armature.state.export import TraceExporter, ExportSummary
from armature.state.traces import TraceStore, TraceRecord


def make_trace(**kwargs) -> TraceRecord:
    defaults = dict(
        run_id="run-01",
        workflow_name="wf",
        stage_id="analyst",
        role_type="researcher",
        model="claude-sonnet",
        success=True,
        output_valid=True,
        quorum_score=0.91,
        escalation_count=0,
        error_type=None,
        inputs={"topic": "climate", "depth": "comprehensive"},
        outputs={"brief": "CO2 is rising at record speed.", "confidence": 0.9},
    )
    defaults.update(kwargs)
    return TraceRecord(**defaults)


async def _seed(store: TraceStore, *traces: TraceRecord) -> None:
    await store.init()
    for t in traces:
        await store.record(t)


# ── ExportSummary ─────────────────────────────────────────────────────────────

async def test_export_returns_summary(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace())
    exporter = TraceExporter(store)
    summary = await exporter.export("wf", tmp_path / "out.jsonl")
    assert isinstance(summary, ExportSummary)
    assert summary.total_exported == 1
    assert summary.output_path == tmp_path / "out.jsonl"
    assert summary.format == "chat"
    assert summary.workflow_name == "wf"


async def test_export_creates_file(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace())
    exporter = TraceExporter(store)
    out = tmp_path / "data.jsonl"
    await exporter.export("wf", out)
    assert out.exists()


async def test_export_zero_records_when_no_qualifying_traces(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    low_quality = make_trace(quorum_score=0.40)
    await _seed(store, low_quality)
    exporter = TraceExporter(store)
    summary = await exporter.export("wf", tmp_path / "out.jsonl", min_quorum_score=0.85)
    assert summary.total_exported == 0
    assert (tmp_path / "out.jsonl").read_text() == ""


async def test_export_filters_below_min_quorum_score(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(
        store,
        make_trace(run_id="r1", quorum_score=0.90),
        make_trace(run_id="r2", quorum_score=0.50),
        make_trace(run_id="r3", quorum_score=0.95),
    )
    exporter = TraceExporter(store)
    summary = await exporter.export("wf", tmp_path / "out.jsonl", min_quorum_score=0.85)
    assert summary.total_exported == 2


# ── chat format ───────────────────────────────────────────────────────────────

async def test_chat_format_has_messages_key(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace())
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="chat")
    record = json.loads(out.read_text().strip())
    assert "messages" in record


async def test_chat_format_has_system_user_assistant_roles(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace())
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="chat")
    msgs = json.loads(out.read_text().strip())["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant"]


async def test_chat_format_user_message_includes_inputs(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace(inputs={"topic": "climate", "depth": "deep"}))
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="chat")
    user_content = json.loads(out.read_text().strip())["messages"][1]["content"]
    assert "climate" in user_content
    assert "depth" in user_content


async def test_chat_format_assistant_message_includes_outputs(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace(outputs={"brief": "CO2 is rising."}))
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="chat")
    asst_content = json.loads(out.read_text().strip())["messages"][2]["content"]
    assert "CO2 is rising." in asst_content


async def test_chat_format_infra_keys_excluded_from_user_message(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace(inputs={"topic": "climate", "_memory": "{}", "run_id": "x"}))
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="chat")
    user_content = json.loads(out.read_text().strip())["messages"][1]["content"]
    assert "_memory" not in user_content
    assert "run_id" not in user_content
    assert "climate" in user_content


async def test_chat_format_private_output_keys_excluded(tmp_path):
    """Keys starting with _ in outputs (e.g. _input_tokens) are excluded."""
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace(outputs={"brief": "summary", "_input_tokens": 100}))
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="chat")
    asst_content = json.loads(out.read_text().strip())["messages"][2]["content"]
    assert "_input_tokens" not in asst_content
    assert "summary" in asst_content


async def test_chat_single_content_key_unwrapped(tmp_path):
    """Outputs with only a 'content' key emit the string directly, not JSON-wrapped."""
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace(outputs={"content": "Just the text."}))
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="chat")
    asst_content = json.loads(out.read_text().strip())["messages"][2]["content"]
    assert asst_content == "Just the text."


async def test_custom_system_prompt_overrides_default(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace())
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, system_prompt="Custom instruction here.")
    sys_content = json.loads(out.read_text().strip())["messages"][0]["content"]
    assert sys_content == "Custom instruction here."


# ── alpaca format ─────────────────────────────────────────────────────────────

async def test_alpaca_format_has_correct_keys(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace())
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="alpaca")
    record = json.loads(out.read_text().strip())
    assert set(record.keys()) == {"instruction", "input", "output"}


async def test_alpaca_format_instruction_is_system_prompt(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace(role_type="judge"))
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="alpaca")
    record = json.loads(out.read_text().strip())
    assert "judge" in record["instruction"]


# ── sharegpt format ───────────────────────────────────────────────────────────

async def test_sharegpt_format_has_conversations_key(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace())
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out, format="sharegpt")
    record = json.loads(out.read_text().strip())
    assert "conversations" in record
    convs = record["conversations"]
    assert convs[0]["from"] == "human"
    assert convs[1]["from"] == "gpt"


# ── role_type filter ──────────────────────────────────────────────────────────

async def test_export_filters_by_role_type(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(
        store,
        make_trace(run_id="r1", stage_id="judge", role_type="judge"),
        make_trace(run_id="r2", stage_id="worker", role_type="worker"),
        make_trace(run_id="r3", stage_id="research", role_type="researcher"),
    )
    exporter = TraceExporter(store)
    summary = await exporter.export(
        "wf", tmp_path / "out.jsonl", role_types=["judge", "researcher"]
    )
    assert summary.total_exported == 2


async def test_export_all_role_types_when_filter_not_set(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(
        store,
        make_trace(run_id="r1", stage_id="j", role_type="judge"),
        make_trace(run_id="r2", stage_id="w", role_type="worker"),
    )
    exporter = TraceExporter(store)
    summary = await exporter.export("wf", tmp_path / "out.jsonl")
    assert summary.total_exported == 2


# ── DPO export ────────────────────────────────────────────────────────────────

async def test_export_dpo_produces_chosen_rejected_pairs(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(
        store,
        make_trace(run_id="good", quorum_score=0.92, outputs={"brief": "Excellent analysis."}),
        make_trace(run_id="bad", quorum_score=0.15, outputs={"brief": "Bad output."}),
    )
    exporter = TraceExporter(store)
    out = tmp_path / "dpo.jsonl"
    summary = await exporter.export_dpo("wf", out)
    assert summary.total_exported >= 1
    record = json.loads(out.read_text().strip())
    assert "prompt" in record
    assert "chosen" in record
    assert "rejected" in record


async def test_export_dpo_chosen_contains_high_quality_output(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(
        store,
        make_trace(run_id="good", quorum_score=0.92, outputs={"content": "High quality."}),
        make_trace(run_id="bad", quorum_score=0.10, outputs={"content": "Low quality."}),
    )
    exporter = TraceExporter(store)
    out = tmp_path / "dpo.jsonl"
    await exporter.export_dpo("wf", out)
    record = json.loads(out.read_text().strip())
    assert "High quality." in record["chosen"]
    assert "Low quality." in record["rejected"]


async def test_export_dpo_zero_pairs_when_no_rejected_traces(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(store, make_trace(run_id="good", quorum_score=0.92))
    exporter = TraceExporter(store)
    out = tmp_path / "dpo.jsonl"
    summary = await exporter.export_dpo("wf", out)
    assert summary.total_exported == 0


# ── multiple records produce valid JSONL ──────────────────────────────────────

async def test_multiple_traces_each_on_own_line(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    await _seed(
        store,
        make_trace(run_id="r1"),
        make_trace(run_id="r2"),
        make_trace(run_id="r3"),
    )
    exporter = TraceExporter(store)
    out = tmp_path / "out.jsonl"
    await exporter.export("wf", out)
    lines = [l for l in out.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    for line in lines:
        obj = json.loads(line)
        assert "messages" in obj

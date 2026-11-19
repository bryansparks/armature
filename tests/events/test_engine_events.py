import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

import armature.runtime.engine as engine_module
from armature.events import Emitter, register
from armature.events.env import child_env
from armature.runtime.engine import Harness
from armature.spec.models import (
    Adapter,
    AgentStep,
    HarnessSpec,
    LoopConfig,
    ModelTierConfig,
    ModelTiers,
    OnFailConfig,
    Role,
    RoleType,
    Stage,
)
from tests.events.support import DatagramReader, closed_port


@pytest.fixture
def reader(monkeypatch):
    r = DatagramReader()
    monkeypatch.setenv("ARMATURE_EVENTS", r.endpoint)
    monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "run-token")
    yield r
    r.close()


def _spec() -> HarnessSpec:
    return HarnessSpec(
        name="events-test",
        stages=[
            Stage(id="build", adapter="ok"),
            Stage(id="skipped", adapter="ok", skip_if="true", depends_on=["build"]),
            Stage(id="flaky", adapter="fail", fail_as_value=True, depends_on=["build"]),
            Stage(
                id="lead",
                role=Role(name="Lead", type=RoleType.WORKER, description="decide", model_tier="small"),
                depends_on=["build"],
            ),
        ],
        adapters={
            "ok": Adapter(name="ok", type="script", cmd="echo ok"),
            "fail": Adapter(name="fail", type="script", cmd="exit 3", parse="json"),
        },
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )


async def _llm(context):
    return {"content": "done", "_input_tokens": 120, "_output_tokens": 30, "_cost_usd": 0.002}


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)


def _flush_and_read(harness, reader):
    harness._events.flush(5)
    deadline = time.monotonic() + 5
    while not any(e["kind"] == "run_end" for e in reader.events) and time.monotonic() < deadline:
        time.sleep(0.01)
    return list(reader.events)


async def test_run_emits_lifecycle_and_cost(reader, tmp_path):
    seen = []
    harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db",
                      on_event=lambda name, data: seen.append(name))
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
        await harness.run({})
    events = _flush_and_read(harness, reader)
    kinds = [(e["kind"], e["step_id"]) for e in events]
    assert kinds[0] == ("run_start", None)
    assert kinds[-1] == ("run_end", None)
    assert ("step_start", "build") in kinds and ("step_end", "build") in kinds
    assert ("step_skipped", "skipped") in kinds
    assert ("step_failed", "flaky") in kinds
    assert ("cost", "lead") in kinds
    cost = next(e for e in events if e["kind"] == "cost")["payload"]
    assert cost == {"model": "gpt-4o-mini", "input_tokens": 120, "output_tokens": 30, "cost_usd": 0.002}
    end = events[-1]["payload"]
    assert end["status"] == "succeeded" and end["error"] is None
    assert "cost_usd" not in end
    assert all(e["token"] == "run-token" and "run_id" not in e["payload"] for e in events)
    assert "stage_start" in seen and "run_summary" in seen


async def test_failing_stage_emits_retry_step_failed_and_failed_run(reader, tmp_path):
    spec = HarnessSpec(
        name="events-fail",
        stages=[Stage(id="boom", adapter="fail",
                      on_fail=OnFailConfig(loop=LoopConfig(stage="boom", max=1)))],
        adapters={"fail": Adapter(name="fail", type="script", cmd="exit 3", parse="json")},
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="exited 3"):
        await harness.run({})
    events = _flush_and_read(harness, reader)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "run_start"
    assert "retry" in kinds
    assert kinds[-2:] == ["step_failed", "run_end"]
    assert events[-1]["payload"]["status"] == "failed"
    assert events[-1]["payload"]["error"]


def _response(content, cost):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None
    response.usage.prompt_tokens, response.usage.completion_tokens = 50, 10
    response._hidden_params = {"response_cost": cost}
    return response


async def test_cost_event_names_the_model_that_produced_the_output_after_escalation(reader, tmp_path):
    spec = HarnessSpec(
        name="events-escalate",
        stages=[Stage(id="judge", output_mode="guided_json",
                      role=Role(name="Judge", type=RoleType.WORKER, description="decide", model_tier="small"))],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"),
                               large=ModelTierConfig(provider="anthropic", model="claude-opus-5-5",
                                                     reasoning_effort="high")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db", use_cache=False)
    responses = [_response("not json", 0.001), _response('{"ok": true}', 0.02)]
    with patch("armature.nodes.llm.litellm_completion", side_effect=responses):
        await harness.run({})
    events = _flush_and_read(harness, reader)
    cost = next(e for e in events if e["kind"] == "cost")["payload"]
    assert cost["model"] == "anthropic/claude-opus-5-5"
    assert (cost["tier"], cost["provider"], cost["requested_effort"]) == ("large", "anthropic", "high")
    assert cost["escalation_index"] == 1
    assert cost["cost_usd"] == pytest.approx(0.021)
    assert [(a["tier"], a["outcome"]) for a in cost["attempts"]] == [("small", "parse_error"), ("large", "ok")]


async def test_cost_event_marks_a_cache_hit(reader, tmp_path):
    async def cached(context):
        return {"content": "done", "_input_tokens": 1, "_output_tokens": 1, "_cost_usd": 0.0, "_cached": True}

    harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=cached):
        await harness.run({})
    events = _flush_and_read(harness, reader)
    cost = next(e for e in events if e["kind"] == "cost")["payload"]
    assert cost["cached"] is True and cost["model"] == "gpt-4o-mini"


async def test_an_agent_stage_emits_one_candidate_per_finding_capped(reader, tmp_path):
    findings = [{"path": "a.py", "line": 3, "severity": "high", "blocking": True, "claim": "c", "body": "b",
                 "suggestion": "s", "extra": "dropped"}, "not a finding",
                *({"path": "b.py", "claim": f"c{i}"} for i in range(300))]
    spec = HarnessSpec(name="events-candidates", stages=[
        Stage(id="correctness", agent=AgentStep(model="review", role="correctness", prompt="review")),
        Stage(id="plain", agent=AgentStep(model="review", prompt="p"), depends_on=["correctness"])])
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.agent.AgentNode.execute", side_effect=[{"findings": findings}, {"content": "x"}]):
        await harness.run({})
    events = _flush_and_read(harness, reader)
    candidates = [e for e in events if e["kind"] == "candidate"]
    assert len(candidates) == engine_module.MAX_CANDIDATES - 1
    assert {e["step_id"] for e in candidates} == {"correctness"}
    first = candidates[0]["payload"]
    assert first == {
        "ordinal": 0, "specialist": "correctness", "path": "a.py", "line": 3, "severity": "high", "blocking": True,
        "claim": "c", "body": "b", "suggestion": "s"}
    assert [e["payload"]["ordinal"] for e in candidates[:2]] == [0, 2]
    assert candidates[-1]["payload"]["ordinal"] == engine_module.MAX_CANDIDATES - 1
    kinds = [e["kind"] for e in events if e.get("step_id") == "correctness"]
    assert kinds.index("candidate") < kinds.index("step_end")


async def test_each_fan_out_branch_emits_its_own_candidates_under_its_role(reader, tmp_path):
    spec = HarnessSpec(
        name="events-fanned-candidates",
        stages=[
            Stage(id="source", adapter="roles"),
            Stage(id="specialists", agent=AgentStep(model="review", prompt="review as {{ item.name }}"),
                  partition_source="{{ source.roles }}", partition_key="item", fan_out=2, depends_on=["source"]),
        ],
        adapters={"roles": Adapter(name="roles", type="script", parse="json",
                                   cmd="echo '{\"roles\": [{\"name\": \"correctness\"}, {\"name\": \"security\"}]}'")},
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.agent.AgentNode.execute",
               side_effect=lambda context: {"findings": [{"claim": context["item"]["name"]}]}):
        await harness.run({})
    events = _flush_and_read(harness, reader)
    found = sorted((e["payload"]["branch"], e["payload"]["ordinal"], e["payload"]["specialist"], e["payload"]["claim"])
                   for e in events if e["kind"] == "candidate")
    assert found == [(0, 0, "correctness", "correctness"), (1, 0, "security", "security")]


async def test_findings_that_are_not_a_list_emit_no_candidates(reader, tmp_path):
    spec = HarnessSpec(name="events-no-candidates", stages=[
        Stage(id="correctness", agent=AgentStep(model="review", role="correctness", prompt="review"))])
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.agent.AgentNode.execute", return_value={"findings": {"claim": "c"}}):
        result = await harness.run({})
    events = _flush_and_read(harness, reader)
    assert not [e for e in events if e["kind"] == "candidate"]
    assert result["correctness"] == {"findings": {"claim": "c"}}
    assert [e["payload"].get("status") for e in events if e["kind"] == "run_end"] == ["succeeded"]


async def test_on_event_assigned_after_construction_still_forwards(reader, tmp_path):
    seen = []
    harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
    harness._on_event = lambda name, data: seen.append(name)
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
        await harness.run({})
    events = _flush_and_read(harness, reader)
    assert "stage_complete" in seen
    assert any(e["kind"] == "step_end" for e in events)


def test_unset_env_leaves_on_event_untouched(monkeypatch, tmp_path):
    for name in ("ARMATURE_EVENTS", "ARMATURE_SERVER", "ARMATURE_RUNNER_KEY"):
        monkeypatch.delenv(name, raising=False)

    def callback(name, data):
        return None

    harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db", on_event=callback)
    assert not harness._events.enabled
    assert harness._on_event is callback
    assert Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")._on_event is None


async def _failing_llm(context):
    raise RuntimeError("model down")


@pytest.mark.parametrize("llm", [_llm, _failing_llm])
async def test_a_server_jobs_traces_carry_its_server_run_id(reader, monkeypatch, tmp_path, llm):
    from armature.state.traces import TraceStore
    monkeypatch.delenv("ARMATURE_STEP_ID", raising=False)
    monkeypatch.setenv("ARMATURE_SERVER_RUN_ID", "srv-7")
    harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=llm), contextlib.suppress(Exception):
        await harness.run({})
    traces = await TraceStore(tmp_path / "t.db").query_by_run(harness._run_id)
    assert traces and {t.server_run_id for t in traces} == {"srv-7"}
    assert [t.success for t in traces if t.stage_id == "lead"] == [llm is _llm]


async def test_a_nested_runs_traces_carry_its_parents_server_run_id(reader, monkeypatch, tmp_path):
    from armature.state.traces import TraceStore
    monkeypatch.setenv("ARMATURE_STEP_ID", "outer")
    monkeypatch.setenv("ARMATURE_RUN_ID", "srv-parent")
    monkeypatch.setenv("ARMATURE_SERVER_RUN_ID", "srv-other")
    harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
        await harness.run({})
    traces = await TraceStore(tmp_path / "t.db").query_by_run(harness._run_id)
    assert traces and {t.server_run_id for t in traces} == {"srv-parent"}


async def test_a_subagents_traces_carry_its_parents_server_run_id(reader, monkeypatch, tmp_path):
    from armature.state.traces import TraceStore
    monkeypatch.delenv("ARMATURE_STEP_ID", raising=False)
    monkeypatch.setenv("ARMATURE_SERVER_RUN_ID", "srv-7")
    spec = HarnessSpec(name="parent", stages=[Stage(id="delegate", subagent_spec=_child_spec(tmp_path))])
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
        await harness.run({})
    child = await TraceStore(engine_module._GLOBAL_TRACES_DB.expanduser()).query(workflow_name="child")
    assert {t.stage_id for t in child} == {"inner", "think"}
    assert {t.server_run_id for t in child} == {"srv-7"}


async def test_nested_run_inside_a_step_sends_no_lifecycle(reader, monkeypatch, tmp_path):
    monkeypatch.setenv("ARMATURE_STEP_ID", "outer")
    harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
        await harness.run({})
    events = reader.wait_for(8)
    kinds = [e["kind"] for e in events]
    assert "run_start" not in kinds and "run_end" not in kinds
    assert events and all(e["payload"]["parent_step"] == "outer" for e in events)


def _child_spec(tmp_path) -> str:
    path = tmp_path / "child.yaml"
    path.write_text(
        "name: child\n"
        "adapters:\n  ok:\n    name: ok\n    type: script\n    cmd: echo child\n"
        "model_tiers:\n  small:\n    provider: openai\n    model: gpt-4o-mini\n"
        "stages:\n"
        "  - id: inner\n    adapter: ok\n"
        "  - id: think\n    depends_on: [inner]\n"
        "    role:\n      name: T\n      type: worker\n      description: think\n      model_tier: small\n"
    )
    return str(path)


async def test_subagent_child_shares_emitter_without_lifecycle(reader, tmp_path):
    spec = HarnessSpec(name="parent", stages=[Stage(id="delegate", subagent_spec=_child_spec(tmp_path))])
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
        await harness.run({})
    events = _flush_and_read(harness, reader)
    kinds = [e["kind"] for e in events]
    assert kinds.count("run_start") == 1 and kinds.count("run_end") == 1
    assert "cost_usd" not in events[-1]["payload"]
    child = [e for e in events if e["step_id"] in ("inner", "think")]
    assert {(e["kind"], e["step_id"]) for e in child} >= {
        ("step_start", "inner"), ("step_end", "inner"), ("cost", "think"),
    }
    assert all(e["payload"]["parent_step"] == "delegate" for e in child)
    parent = [e for e in events if e["step_id"] == "delegate"]
    assert parent and all("parent_step" not in e["payload"] for e in parent)
    assert sum(1 for e in events if e["kind"] == "cost") == 1


async def test_fan_out_branches_are_tagged(reader, tmp_path):
    spec = HarnessSpec(
        name="fan",
        stages=[
            Stage(id="source", adapter="items"),
            Stage(id="each", adapter="ok", partition_source="{{ source.paths }}",
                  partition_key="item", fan_out=3, depends_on=["source"]),
        ],
        adapters={
            "items": Adapter(name="items", type="script", cmd="echo '{\"paths\": [1, 2, 3]}'", parse="json"),
            "ok": Adapter(name="ok", type="script", cmd="echo ok"),
        },
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    await harness.run({})
    events = _flush_and_read(harness, reader)
    branch_events = [e for e in events if e["step_id"] == "each"]
    starts = sorted(e["payload"]["branch"] for e in branch_events if e["kind"] == "step_start")
    ends = sorted(e["payload"]["branch"] for e in branch_events if e["kind"] == "step_end")
    assert starts == ends == [0, 1, 2]
    assert all(e["payload"]["branches"] == 3 for e in branch_events)
    assert all("branch" not in e["payload"] for e in events if e["step_id"] == "source")


async def test_scripts_get_this_runs_endpoint_and_no_runner_key(reader, monkeypatch, tmp_path):
    monkeypatch.setenv("ARMATURE_RUNNER_KEY", "runner-secret")
    spec = HarnessSpec(
        name="env",
        stages=[Stage(id="show", adapter="env")],
        adapters={"env": Adapter(name="env", type="script", cmd=(
            "printf '{\"events\":\"%s\",\"token\":\"%s\",\"step\":\"%s\",\"run\":\"%s\",\"key\":\"%s\"}' "
            "\"$ARMATURE_EVENTS\" \"$ARMATURE_EVENTS_TOKEN\" \"$ARMATURE_STEP_ID\" \"$ARMATURE_RUN_ID\" "
            "\"${ARMATURE_RUNNER_KEY:-}\""
        ), parse="json")},
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    result = await harness.run({})
    assert result["show"] == {
        "events": reader.endpoint, "token": "run-token", "step": "show",
        "run": harness._run_id, "key": "",
    }


async def test_given_emitter_is_used_and_left_open(monkeypatch, tmp_path):
    monkeypatch.delenv("ARMATURE_EVENTS", raising=False)
    r = DatagramReader()
    try:
        emitter = Emitter(r.endpoint, "given")
        harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db", emitter=emitter)
        with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
            await harness.run({})
        emitter.emit("log", None, after=True)
        emitter.close()
        _wait_until(lambda: any(e["payload"] == {"after": True} for e in r.events))
        assert r.events[0]["kind"] == "run_start" and r.events[-1]["payload"] == {"after": True}
    finally:
        r.close()


class _RunnerServer:
    def __init__(self, reply):
        server = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.requests.append((self.path, dict(self.headers), body))
                data = json.dumps(reply).encode()
                self.send_response(201)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


async def test_top_level_run_registers_with_the_server(monkeypatch, tmp_path):
    monkeypatch.delenv("ARMATURE_EVENTS", raising=False)
    monkeypatch.delenv("ARMATURE_PROJECT", raising=False)
    r = DatagramReader()
    runner = _RunnerServer({"run_id": "srv-42", "token": "srv-token",
                            "events": {"unix": r.path, "http": "http://127.0.0.1:1/x"}})
    try:
        monkeypatch.setenv("ARMATURE_SERVER", runner.url + "/")
        monkeypatch.setenv("ARMATURE_RUNNER_KEY", "rk")
        monkeypatch.setenv("ARMATURE_ACTOR", "alice")
        monkeypatch.setenv("ARMATURE_TRIGGER_KIND", "schedule")
        monkeypatch.setenv("ARMATURE_LOCATION", '{"kind": "aws-mac", "instance": "i-0mac"}')
        harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
        with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
            await harness.run({"project": "android"})
        [(path, headers, body)] = runner.requests
        assert path == "/api/runner/runs"
        assert headers["Authorization"] == "Bearer rk"
        assert body == {"workflow": "events-test", "project": "android", "actor": "alice",
                        "trigger_kind": "schedule", "trigger_ref": {}, "subject": None, "rerun_of": None,
                        "location": {"kind": "aws-mac", "label": "AWS macOS", "instance": "i-0mac"}}
        events = r.wait_for(9)
        assert events[0]["kind"] == "run_start" and events[0]["token"] == "srv-token"
        assert harness._events._closed
    finally:
        runner.close()
        r.close()


async def test_failed_registration_warns_once_and_runs(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("ARMATURE_EVENTS", raising=False)
    monkeypatch.setenv("ARMATURE_SERVER", f"http://127.0.0.1:{closed_port()}")
    monkeypatch.setenv("ARMATURE_RUNNER_KEY", "rk")
    harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
    with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
        result = await harness.run({})
    assert "lead" in result
    warnings = [rec for rec in caplog.records if "not registered" in rec.getMessage()]
    assert len(warnings) == 1
    assert not harness._events.enabled


@pytest.fixture(autouse=True)
def _fresh_registration_cache():
    register._skip_until.clear()
    yield
    register._skip_until.clear()


async def test_silent_server_hits_the_deadline_then_is_skipped(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("ARMATURE_EVENTS", raising=False)
    monkeypatch.setattr(register, "REGISTER_TIMEOUT_S", 0.5)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)
    try:
        monkeypatch.setenv("ARMATURE_SERVER", f"http://127.0.0.1:{silent.getsockname()[1]}")
        monkeypatch.setenv("ARMATURE_RUNNER_KEY", "rk")
        durations = []
        for _ in range(2):
            harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
            t0 = time.monotonic()
            with patch("armature.nodes.llm.LLMNode.execute", side_effect=_llm):
                await harness.run({})
            durations.append(time.monotonic() - t0)
            assert not harness._events.enabled
        assert 0.4 < durations[0] < 3
        assert durations[1] < durations[0] - 0.3
        assert len([r for r in caplog.records if "not registered" in r.getMessage()]) == 1
    finally:
        silent.close()


def test_registration_refuses_plain_http_to_a_remote_server(monkeypatch):
    called = []
    monkeypatch.setattr(register, "_post_registration", lambda *a: called.append(a))
    assert register.register_run("http://runner.example.com", "rk", {}) is None
    assert called == []


def test_registration_project_precedence(monkeypatch):
    monkeypatch.delenv("ARMATURE_PROJECT", raising=False)
    both = {"project": "mobile-app", "repo": "fs-eng/android-tree"}
    assert register.registration_body("w", both)["project"] == "mobile-app"
    assert register.registration_body("w", {"repo": "fs-eng/android-tree"})["project"] == "fs-eng/android-tree"
    assert register.registration_body("w", {"project": 7, "repo": "fs-eng/android-tree"})["project"] == \
        "fs-eng/android-tree"
    assert register.registration_body("w", {"project": "", "repo": ["x"]})["project"] is None
    assert register.registration_body("w", None)["project"] is None
    monkeypatch.setenv("ARMATURE_PROJECT", "mobile/ios")
    assert register.registration_body("w", both)["project"] == "mobile/ios"


def test_unix_endpoint_only_for_a_loopback_server(tmp_path):
    r = DatagramReader()
    try:
        events = {"unix": r.path, "http": "https://events.example.com/x"}
        assert register._endpoint("http://127.0.0.1:8000", events) == f"unix:{r.path}"
        assert register._endpoint("http://localhost:8000", events) == f"unix:{r.path}"
        assert register._endpoint("https://armature.example.com", events) == "https://events.example.com/x"
        missing = {"unix": str(tmp_path / "gone.sock"), "http": "http://127.0.0.1:8000/x"}
        assert register._endpoint("http://127.0.0.1:8000", missing) == "http://127.0.0.1:8000/x"
        with pytest.raises(ValueError):
            register._endpoint("https://armature.example.com", {"http": "http://10.0.0.5/x"})
    finally:
        r.close()


async def test_running_stage_sends_heartbeats(reader, monkeypatch, tmp_path):
    monkeypatch.setattr(engine_module, "HEARTBEAT_S", 0.05)
    spec = HarnessSpec(
        name="slow",
        stages=[Stage(id="wait", adapter="sleep"), Stage(id="after", adapter="sleep", depends_on=["wait"])],
        adapters={"sleep": Adapter(name="sleep", type="script", cmd="sleep 0.3")},
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    await harness.run({})
    events = _flush_and_read(harness, reader)
    beats = [e for e in events if e["kind"] == "progress"]
    assert {e["step_id"] for e in beats} == {"wait", "after"}
    assert all(e["payload"]["stage"] == e["step_id"] and e["payload"]["elapsed_s"] >= 0 for e in beats)
    wait_end = next(i for i, e in enumerate(events) if e["kind"] == "step_end" and e["step_id"] == "wait")
    assert not [e for e in events[wait_end + 1:] if e["kind"] == "progress" and e["step_id"] == "wait"]


async def test_script_stderr_reaches_the_server_as_log_events(reader, tmp_path):
    spec = HarnessSpec(
        name="tail",
        stages=[Stage(id="noisy", adapter="noisy")],
        adapters={"noisy": Adapter(name="noisy", type="script", cmd="echo first >&2; echo second >&2")},
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    await harness.run({})
    events = _flush_and_read(harness, reader)
    logs = [e for e in events if e["kind"] == "log" and e["payload"].get("stream") == "stderr"]
    assert [(e["step_id"], e["payload"]["message"]) for e in logs] == [("noisy", "first"), ("noisy", "second")]


def test_child_env_never_passes_the_runner_key():
    base = {"PATH": "/bin", "ARMATURE_RUNNER_KEY": "rk", "ARMATURE_EVENTS": "unix:/p",
            "ARMATURE_EVENTS_TOKEN": "parent"}
    assert child_env(base=base) == {"PATH": "/bin"}
    mine = child_env({"ARMATURE_EVENTS": "unix:/mine", "ARMATURE_RUNNER_KEY": "x"}, base=base)
    assert mine == {"PATH": "/bin", "ARMATURE_EVENTS": "unix:/mine"}


async def test_shell_builtin_scrubs_the_runner_key(monkeypatch):
    from armature.registry.builtins import _shell_run

    monkeypatch.setenv("ARMATURE_RUNNER_KEY", "rk-secret")
    monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "parent-token")
    result = await _shell_run({"cmd": "echo \"[${ARMATURE_RUNNER_KEY:-}][${ARMATURE_EVENTS_TOKEN:-}]\""})
    assert result["stdout"].strip() == "[][]"


@pytest.mark.parametrize("name", ["runtime", "nodes", "spec", "state", "service", "events", "server"])
def test_lazy_package_still_exposes_subpackages(name):
    code = f"import armature; assert armature.{name}.__name__ == 'armature.{name}'; print('ok')"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert proc.stdout.strip() == "ok", proc.stderr

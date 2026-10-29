"""call: stages end to end: a stored workflow resolved from a call bundle runs as an isolated child."""
import json
import sys
from pathlib import Path

import pytest

from armature.runtime.engine import Harness
from armature.spec.calls import ResolvedCall, write_call_bundle
from armature.spec.loader import load_spec
from armature.spec.validator import SpecValidationError

# Prints a JSON object built from ARMATURE_CONTEXT, so a callee's stage output
# shows exactly what context it saw.
ECHO = """
import json, os, sys
ctx = json.loads(os.environ["ARMATURE_CONTEXT"])
mode = sys.argv[1]
if mode == "keys":
    print(json.dumps({"keys": sorted(ctx)}))
elif mode in ("count", "flaky", "fail"):
    with open(ctx["log"], "a") as f:
        f.write("x" + chr(10))
    n = len(open(ctx["log"]).read().splitlines())
    if mode == "fail" or (mode == "flaky" and n < 2):
        sys.exit(1)
    print(json.dumps({"n": n, "item": ctx.get("item", "")}))
elif mode == "fix":
    n = ctx["attempt"]
    print(json.dumps({"passed": n >= ctx["pass_at"], "attempt": n, "dry_run": ctx["dry_run"]}))
"""

CALLEE_KEYS = """# armature: stored-workflow
name: inspect
contracts:
  inputs:
    - {{name: topic, type: string, required: true}}
adapters:
  echo: {{name: echo, type: script, cmd: "{py} {script} keys", parse: json}}
stages:
  - id: look
    adapter: echo
"""

CALLEE_FIX = """# armature: stored-workflow
name: fixer
contracts:
  inputs:
    - {{name: attempt, type: integer, required: true}}
    - {{name: pass_at, type: integer, default: 2}}
    - {{name: dry_run, type: boolean, default: false}}
  outputs:
    - {{name: passed, type: boolean, from: "{{{{ run_fix.passed }}}}"}}
    - {{name: attempt, type: integer, from: "{{{{ run_fix.attempt }}}}"}}
    - {{name: dry_run, type: boolean, from: "{{{{ run_fix.dry_run }}}}"}}
adapters:
  echo: {{name: echo, type: script, cmd: "{py} {script} fix", parse: json}}
stages:
  - id: run_fix
    adapter: echo
"""


CALLEE_COUNT = """# armature: stored-workflow
name: {name}
contracts:
  inputs:
    - {{name: log, type: string, required: true}}
    - {{name: item, type: string}}
adapters:
  echo: {{name: echo, type: script, cmd: "{py} {script} {mode}", parse: json}}
stages:
  - id: run
    adapter: echo
"""

CALLEE_SPEND = """# armature: stored-workflow
name: spender
model_tiers:
  small: {{provider: openai, model: gpt-4o-mini}}
role_type_defaults: {{worker: small}}
contracts:
  inputs:
    - {{name: log, type: string, required: true}}
adapters:
  echo: {{name: echo, type: script, cmd: "{py} {script} {mode}", parse: json}}
stages:
  - id: think
    role: {{name: Thinker, type: worker, description: think}}
  - id: run
    adapter: echo
    depends_on: [think]
"""


@pytest.fixture
def bundle(tmp_path):
    script = tmp_path / "echo.py"
    script.write_text(ECHO)
    fmt = {"py": sys.executable, "script": script}
    write_call_bundle(tmp_path / "calls", [
        ResolvedCall("inspect", 1, CALLEE_KEYS.format(**fmt)),
        ResolvedCall("fixer", 3, CALLEE_FIX.format(**fmt)),
        *(ResolvedCall(mode, 1, CALLEE_COUNT.format(name=mode, mode=mode, **fmt))
          for mode in ("count", "flaky", "fail")),
        ResolvedCall("spend-ok", 1, CALLEE_SPEND.format(mode="count", **fmt)),
        ResolvedCall("spend-fail", 1, CALLEE_SPEND.format(mode="fail", **fmt)),
    ], {"inspect": 1, "fixer": 3, "count": 1, "flaky": 1, "fail": 1, "spend-ok": 1, "spend-fail": 1})
    return tmp_path


def _write(dir_: Path, body: str) -> Path:
    path = dir_ / "workflow.yaml"
    path.write_text(body)
    return path


async def test_callee_sees_only_its_with_inputs(bundle):
    spec = load_spec(_write(bundle, """
name: caller
contracts:
  inputs:
    - name: secret
stages:
  - id: ask
    call: {workflow: inspect}
    with: {topic: "about {{ secret }}"}
"""))
    harness = Harness(spec=spec, session_dir=bundle / "session")
    result = await harness.run({"secret": "s3cr3t"})
    seen = result["ask"]["keys"]
    assert "topic" in seen
    assert "secret" not in seen
    assert (bundle / "session" / "stage_ask" / "child_0").is_dir()


async def test_typed_outputs_are_the_call_result(bundle):
    spec = load_spec(_write(bundle, """
name: caller
stages:
  - id: fix
    call: {workflow: fixer, version: 3}
    with: {attempt: "5", dry_run: "true"}
"""))
    result = await Harness(spec=spec, session_dir=bundle / "s").run({})
    assert result["fix"] == {"passed": True, "attempt": 5, "dry_run": True}


async def test_loop_over_call_stops_on_typed_output_and_sees_iteration(bundle):
    events = []
    spec = load_spec(_write(bundle, """
name: caller
stages:
  - id: fix
    call: {workflow: fixer}
    with: {attempt: "{{ _iteration.num }}", pass_at: 3}
    loop:
      max_iterations: 5
      until: "{{ passed }}"
"""))
    harness = Harness(spec=spec, session_dir=bundle / "s", on_event=lambda n, d: events.append((n, d)))
    result = await harness.run({})
    assert result["fix"]["passed"] is True
    assert result["fix"]["attempt"] == 3
    rounds = [d for n, d in events if n == "loop_iteration"]
    assert [r["iteration"] for r in rounds] == [1, 2, 3]
    assert (bundle / "s" / "stage_fix" / "iter_3" / "child_0").is_dir()


async def test_loop_timeout_stops_with_reason(bundle):
    events = []
    spec = load_spec(_write(bundle, """
name: caller
stages:
  - id: fix
    call: {workflow: fixer}
    with: {attempt: "{{ _iteration.num }}", pass_at: 99}
    loop:
      max_iterations: 50
      until: "{{ passed }}"
      timeout_s: 0.001
"""))
    harness = Harness(spec=spec, session_dir=bundle / "s", on_event=lambda n, d: events.append((n, d)))
    result = await harness.run({})
    assert result["fix"]["passed"] is False
    assert result["fix"]["attempt"] == 1
    assert "timeout_s" in result["fix"]["_loop_stopped"]
    stopped = [d for n, d in events if n == "loop_stopped"]
    assert stopped and stopped[0]["iteration"] == 1


async def test_without_typed_outputs_the_final_stage_output_is_returned(bundle):
    spec = load_spec(_write(bundle, """
name: caller
stages:
  - id: ask
    call: {workflow: inspect, version: 1}
    with: {topic: x}
"""))
    result = await Harness(spec=spec, session_dir=bundle / "s").run({})
    assert set(result["ask"]) == {"keys"}


async def test_bad_with_value_fails_clearly(bundle):
    spec = load_spec(_write(bundle, """
name: caller
contracts:
  inputs: [{name: n}]
stages:
  - id: fix
    call: {workflow: fixer}
    with: {attempt: "{{ n }}"}
"""))
    harness = Harness(spec=spec, session_dir=bundle / "s")
    with pytest.raises(ValueError, match="Input 'attempt' must be an integer"):
        await harness.run({"n": "many"})


def test_unknown_with_key_is_rejected_at_construction(bundle):
    spec = load_spec(_write(bundle, """
name: caller
stages:
  - id: fix
    call: {workflow: fixer}
    with: {attempt: 1, colour: red}
"""))
    with pytest.raises(SpecValidationError, match="CALL_UNKNOWN_INPUT"):
        Harness(spec=spec, session_dir=bundle / "s")


def test_missing_bundle_is_rejected_at_construction(tmp_path):
    spec = load_spec(_write(tmp_path, """
name: caller
stages:
  - id: fix
    call: {workflow: fixer}
"""))
    with pytest.raises(SpecValidationError, match="CALL_UNRESOLVED"):
        Harness(spec=spec, session_dir=tmp_path / "s")


async def test_subagent_children_share_the_parent_call_bundle(bundle):
    child = bundle / "children" / "child.yaml"
    child.parent.mkdir()
    child.write_text("""
name: child
stages:
  - id: ask
    call: {workflow: inspect}
    with: {topic: nested}
""")
    spec = load_spec(_write(bundle, """
name: caller
stages:
  - id: sub
    subagent_spec: children/child.yaml
"""))
    result = await Harness(spec=spec, session_dir=bundle / "s").run({})
    assert "topic" in result["sub"]["ask"]["keys"]
    assert json.dumps(result)


def _lines(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


async def test_call_under_fail_as_value_returns_the_failure(bundle):
    spec = load_spec(_write(bundle, f"""
name: caller
stages:
  - id: c
    call: {{workflow: fail}}
    with: {{log: "{bundle / 'log'}"}}
    fail_as_value: true
  - id: after
    call: {{workflow: count}}
    with: {{log: "{bundle / 'log2'}"}}
    depends_on: [c]
"""))
    result = await Harness(spec=spec, session_dir=bundle / "s").run({})
    assert result["c"]["_failed"] is True
    assert "exited 1" in result["c"]["_failed_reason"]
    assert result["after"]["n"] == 1


async def test_retried_call_succeeds_on_second_attempt(bundle):
    spec = load_spec(_write(bundle, f"""
name: caller
stages:
  - id: c
    call: {{workflow: flaky}}
    with: {{log: "{bundle / 'log'}"}}
    on_fail: {{loop: {{stage: c, max: 2}}}}
"""))
    result = await Harness(spec=spec, session_dir=bundle / "s").run({})
    assert result["c"]["n"] == 2
    assert _lines(bundle / "log") == 2


async def test_fanned_out_call_runs_one_child_per_item(bundle):
    spec = load_spec(_write(bundle, f"""
name: caller
contracts:
  inputs: [{{name: items, type: array}}]
stages:
  - id: c
    call: {{workflow: count}}
    with: {{log: "{bundle / 'log'}", item: "{{{{ it }}}}"}}
    fan_out: 3
    partition_source: "{{{{ items }}}}"
    partition_key: it
"""))
    result = await Harness(spec=spec, session_dir=bundle / "s").run({"items": '["a", "b", "c"]'})
    assert sorted(r["item"] for r in result["c"]) == ["a", "b", "c"]
    assert _lines(bundle / "log") == 3
    assert all((bundle / "s" / "stage_c" / f"child_{i}").is_dir() for i in range(3))


async def test_checkpoint_resume_skips_a_completed_call(bundle):
    body = f"""
name: caller
checkpoint: true
stages:
  - id: c
    call: {{workflow: count}}
    with: {{log: "{bundle / 'log'}"}}
"""
    first = await Harness(spec=load_spec(_write(bundle, body)), session_dir=bundle / "s").run({})
    events = []
    second = await Harness(spec=load_spec(_write(bundle, body)), session_dir=bundle / "s",
                           on_event=lambda n, d: events.append(n)).run({})
    assert second["c"] == first["c"]
    assert _lines(bundle / "log") == 1
    assert "stage_resumed" in events


@pytest.fixture
def llm_spends(monkeypatch):
    from armature.nodes.llm import LLMNode

    async def execute(self, context):
        return {"content": "ok", "_cost_usd": 0.25}
    monkeypatch.setattr(LLMNode, "execute", execute)


async def test_callee_spend_rolls_into_the_parent(bundle, llm_spends):
    spec = load_spec(_write(bundle, f"""
name: caller
stages:
  - id: c
    call: {{workflow: spend-ok}}
    with: {{log: "{bundle / 'log'}"}}
"""))
    harness = Harness(spec=spec, session_dir=bundle / "s", use_cache=False)
    await harness.run({})
    assert harness.total_cost_usd == pytest.approx(0.25)


async def test_callee_spend_counts_when_the_callee_fails(bundle, llm_spends):
    spec = load_spec(_write(bundle, f"""
name: caller
stages:
  - id: c
    call: {{workflow: spend-fail}}
    with: {{log: "{bundle / 'log'}"}}
    on_fail: {{loop: {{stage: c, max: 1}}}}
"""))
    harness = Harness(spec=spec, session_dir=bundle / "s", use_cache=False)
    with pytest.raises(RuntimeError, match="exited 1"):
        await harness.run({})
    assert harness.total_cost_usd == pytest.approx(0.5)


ADAPTER_ECHO = """
import json, os
print(json.dumps({"with": json.loads(os.environ["ARMATURE_WITH"])}))
"""


@pytest.mark.parametrize("header", ["# armature: stored-workflow\n", ""])
async def test_call_with_keeps_its_templates_through_load(bundle, header):
    body = header + """name: top
contracts:
  inputs: [{name: topic}, {name: secret}]
stages:
  - id: i
    call: {workflow: inspect}
    with: {topic: "about {{ topic }}"}
"""
    spec = load_spec(_write(bundle, body), vars={"topic": "{{ secret }}", "secret": "TOPSECRET"})
    assert spec.stages[0].with_ == {"topic": "about {{ topic }}"}


@pytest.mark.parametrize("header", ["# armature: stored-workflow\n", ""])
async def test_adapter_with_renders_once_at_run_time_keeping_strings(bundle, header):
    script = bundle / "with_echo.py"
    script.write_text(ADAPTER_ECHO)
    body = header + f"""name: top
contracts:
  inputs: [{{name: count}}, {{name: note}}, {{name: secret}}]
adapters:
  echo: {{name: echo, type: script, cmd: "{sys.executable} {script}", parse: json}}
stages:
  - id: a
    adapter: echo
    with: {{n: "{{{{ count }}}}", note: "{{{{ note }}}}"}}
"""
    inputs = {"count": "5", "note": "{{ secret }}", "secret": "TOPSECRET"}
    spec = load_spec(_write(bundle, body), vars=inputs)
    assert spec.stages[0].with_ == {"n": "{{ count }}", "note": "{{ note }}"}
    result = await Harness(spec=spec, session_dir=bundle / "s").run(inputs)
    assert result["a"]["with"] == {"n": "5", "note": "{{ secret }}"}


def test_unparseable_spec_keeps_the_load_time_with_render(bundle):
    body = """name: top
contracts:
  inputs: [{name: mode}]
stages:
{% if mode == "a" %}
  - id: s
    call: {workflow: inspect}
    with: {topic: from_a}
{% else %}
  - id: s
    call: {workflow: inspect}
    with: {topic: from_b}
{% endif %}
"""
    assert load_spec(_write(bundle, body), vars={"mode": "a"}).stages[0].with_ == {"topic": "from_a"}
    assert load_spec(_write(bundle, body), vars={"mode": "b"}).stages[0].with_ == {"topic": "from_b"}


@pytest.mark.parametrize("value", ["True", "[1, 2]", "007"])
async def test_call_passes_string_values_to_string_inputs_unchanged(bundle, value):
    probe = bundle / "probe.py"
    probe.write_text("import json,os;c=json.loads(os.environ['ARMATURE_CONTEXT']);"
                     "print(json.dumps({'topic': c['topic']}))\n")
    write_call_bundle(bundle / "calls", [ResolvedCall("leaf", 1, f"""# armature: stored-workflow
name: leaf
contracts:
  inputs: [{{name: topic, type: string, required: true}}]
adapters:
  echo: {{name: echo, type: script, cmd: "{sys.executable} {probe}", parse: json}}
stages:
  - id: look
    adapter: echo
""")], {"leaf": 1})
    spec = load_spec(_write(bundle, """name: top
contracts:
  inputs: [{name: v}]
stages:
  - id: c
    call: {workflow: leaf}
    with: {topic: "{{ v }}"}
"""))
    result = await Harness(spec=spec, session_dir=bundle / "s").run({"v": value})
    assert result["c"]["topic"] == value


async def test_callee_receives_template_syntax_literally(bundle):
    probe = bundle / "probe.py"
    probe.write_text("import json,os;c=json.loads(os.environ['ARMATURE_CONTEXT']);"
                     "print(json.dumps({'topic': c['topic']}))\n")
    write_call_bundle(bundle / "calls", [ResolvedCall("leaf", 1, f"""# armature: stored-workflow
name: leaf
contracts:
  inputs: [{{name: topic, type: string, required: true}}]
adapters:
  echo: {{name: echo, type: script, cmd: "{sys.executable} {probe}", parse: json}}
stages:
  - id: look
    adapter: echo
""")], {"leaf": 1})
    inputs = {"topic": "{{ secret }}", "secret": "TOPSECRET"}
    spec = load_spec(_write(bundle, """# armature: stored-workflow
name: top
contracts:
  inputs: [{name: topic}, {name: secret}]
stages:
  - id: c
    call: {workflow: leaf}
    with: {topic: "about {{ topic }}"}
"""), vars=inputs)
    result = await Harness(spec=spec, session_dir=bundle / "s").run(inputs)
    assert result["c"]["topic"] == "about {{ secret }}"


@pytest.mark.parametrize("header", ["# armature: stored-workflow\n", ""])
@pytest.mark.parametrize("call", ["{workflow: '{{ target }}'}", "{workflow: fixer, version: '{{ v }}'}"])
def test_templated_call_target_is_rejected_at_load(bundle, header, call):
    path = _write(bundle, header + f"name: top\nstages:\n  - id: c\n    call: {call}\n")
    with pytest.raises(ValueError, match="CALL_INVALID_WORKFLOW"):
        load_spec(path, vars={"target": "fixer", "v": "3"})


def test_call_target_chosen_by_input_is_rejected_at_load(bundle):
    path = _write(bundle, """name: top
stages:
  - id: c
{% if pick %}
    call: {workflow: fixer}
{% else %}
    call: {workflow: inspect}
{% endif %}
""")
    with pytest.raises(ValueError, match="CALL_INVALID_WORKFLOW"):
        load_spec(path, vars={"pick": "yes"})


def test_in_memory_spec_with_a_call_needs_a_resolver(tmp_path, monkeypatch):
    from armature.spec.models import CallRef, HarnessSpec, Stage
    monkeypatch.chdir(tmp_path)
    write_call_bundle(tmp_path / "calls", [ResolvedCall("fixer", 1, CALLEE_KEYS)], {"fixer": 1})
    spec = HarnessSpec(name="mem", stages=[Stage(id="c", call=CallRef(workflow="fixer"))])
    with pytest.raises(SpecValidationError, match="not loaded from a file"):
        Harness(spec=spec, session_dir=tmp_path / "s")

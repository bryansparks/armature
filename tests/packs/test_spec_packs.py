import json
import textwrap

import pytest
from typer.testing import CliRunner

from armature.cli import app
from armature.packs import PACKS_PATH_ENV
from armature.runtime.engine import Harness
from armature.spec.loader import load_spec
from armature.spec.validator import validate_spec
from tests.packs.support import HELLO, write_pack


@pytest.fixture(autouse=True)
def packs_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(PACKS_PATH_ENV, str(HELLO.parent))


def _spec(tmp_path, body: str):
    path = tmp_path / "wf.yml"
    path.write_text(textwrap.dedent(body))
    return load_spec(path)


def _codes(spec):
    return [(e.code, e.stage_id, e.message) for e in validate_spec(spec, strict=False) if e.severity == "error"]


def test_pack_adapter_resolves_at_load(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        stages:
          - id: greet
            adapter: hello/greet
            with: {name: Ada}
    """)
    adapter = spec.adapters["hello/greet"]
    assert adapter.type == "script" and adapter.parse == "json" and adapter.timeout == 30
    assert adapter.cmd == 'python3 "$PACK_DIR/adapters/greet.py"'
    assert adapter.pack_dir == str(HELLO)
    assert spec.stages[0].with_ == {"name": "Ada"}
    assert _codes(spec) == []


def test_spec_defined_adapter_wins_over_a_pack(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        adapters:
          hello/greet: {name: mine, type: script, cmd: "echo '{}'"}
        stages:
          - id: greet
            adapter: hello/greet
    """)
    assert spec.adapters["hello/greet"].name == "mine" and spec.adapters["hello/greet"].pack_dir is None


def test_unknown_pack_adapter(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        stages:
          - {id: a, adapter: hello/nope}
          - {id: b, adapter: nopack/greet}
          - {id: c, adapter: plain}
    """)
    codes = [(c, s) for c, s, _ in _codes(spec)]
    assert ("UNKNOWN_PACK_ADAPTER", "a") in codes and ("UNKNOWN_PACK_ADAPTER", "b") in codes
    assert ("UNDEFINED_ADAPTER", "c") in codes


def test_with_schema_errors_name_the_field(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        stages:
          - {id: missing, adapter: hello/greet}
          - {id: extra, adapter: hello/greet, with: {name: Ada, shout: true}}
          - {id: wrong, adapter: hello/wait, with: {seconds: "soon"}}
          - {id: big, adapter: hello/wait, with: {seconds: 600}}
    """)
    found = {(s, m) for c, s, m in _codes(spec) if c == "INVALID_WITH"}
    assert ("missing", "with: 'name' is a required property") in found
    assert any(s == "extra" and "shout" in m for s, m in found)
    assert ("wrong", "with.seconds: 'soon' is not of type 'integer'") in found
    assert ("big", "with.seconds: 600 is greater than the maximum of 60") in found


def test_templated_with_value_is_checked_after_rendering(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        stages:
          - {id: pause, adapter: hello/wait, with: {seconds: "{{ delay }}"}}
    """)
    assert _codes(spec) == []


@pytest.mark.parametrize("value", [
    "{{ ''.__class__.__mro__ }}",
    "{{ lipsum() }}",
    "{% for x in y %}{{ x }}{% endfor %}",
    "{{ name | attr('x') }}",
])
def test_lint_unsafe_with_is_refused(tmp_path, value):
    spec = _spec(tmp_path, f"""
        name: t
        stages:
          - id: greet
            adapter: hello/greet
            with:
              name: {json.dumps(value)}
    """)
    assert any(c == "UNSAFE_TEMPLATE" and s == "greet" and m.startswith("with.name:") for c, s, m in _codes(spec))


def test_with_without_adapter_is_invalid(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        stages:
          - id: g
            gate: approve
            with: {x: 1}
    """)
    assert ("INVALID_WITH", "g", "with: needs an adapter") in _codes(spec)


async def test_with_renders_from_step_outputs_at_run_time(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        stages:
          - id: first
            adapter: hello/greet
            with: {name: Ada}
          - id: second
            adapter: hello/greet
            depends_on: [first]
            with: {name: "{{ first.greeting }}"}
    """)
    result = await Harness(spec, session_dir=tmp_path / "s", traces_db=tmp_path / "t.db", use_cache=False).run({})
    assert result["second"]["greeting"] == "Hello, Hello, Ada!!"
    assert result["second"]["pack_dir"] == str(HELLO)


async def test_rendered_with_is_checked_against_the_schema(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        stages:
          - id: pause
            adapter: hello/wait
            with: {seconds: "{{ delay }}"}
            fail_as_value: true
    """)
    result = await Harness(spec, session_dir=tmp_path / "s", traces_db=tmp_path / "t.db", use_cache=False).run(
        {"delay": "later"})
    assert result["pause"]["_failed"] is True
    assert "with.seconds" in json.dumps(result["pause"])


async def test_with_never_reaches_cmd(tmp_path, monkeypatch):
    write_pack(tmp_path / "packs" / "probe", name="probe", show={
        "cmd": "python3 -c 'import json,os; print(json.dumps({\"with\": os.environ[\"ARMATURE_WITH\"], "
               "\"ctx\": json.loads(os.environ[\"ARMATURE_CONTEXT\"])[\"with\"]}))'",
        "params": {"type": "object", "properties": {"x": {"type": "string"}}},
    })
    monkeypatch.setenv(PACKS_PATH_ENV, str(tmp_path / "packs"))
    marker = tmp_path / "pwned"
    spec = _spec(tmp_path, f"""
        name: t
        stages:
          - id: show
            adapter: probe/show
            with: {{x: "$(touch {marker}) {{{{ label }}}}"}}
    """)
    result = await Harness(spec, session_dir=tmp_path / "s", traces_db=tmp_path / "t.db", use_cache=False).run(
        {"label": "{{ 7 * 7 }}"})
    expected = {"x": f"$(touch {marker}) {{{{ 7 * 7 }}}}"}
    assert json.loads(result["show"]["with"]) == expected and result["show"]["ctx"] == expected
    assert not marker.exists()


async def test_plain_script_stage_gets_no_pack_env(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        adapters:
          env:
            name: env
            type: script
            parse: json
            cmd: "python3 -c 'import json,os; print(json.dumps({k: os.environ.get(k) for k in [\\"PACK_DIR\\", \\"ARMATURE_WITH\\"]}))'"
        stages:
          - {id: env, adapter: env}
    """)
    result = await Harness(spec, session_dir=tmp_path / "s", traces_db=tmp_path / "t.db", use_cache=False).run({})
    assert result["env"] == {"PACK_DIR": None, "ARMATURE_WITH": None}


def test_armature_run_of_the_example_workflow(tmp_path):
    out = tmp_path / "result.json"
    result = CliRunner().invoke(app, ["run", str(HELLO / "workflows" / "hello.yml"), "-i", "name=World", "-q",
                                      "--no-cache", "-o", str(out)])
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["greet"] == {"greeting": "Hello, World!", "pack_dir": str(HELLO)}
    assert data["pause"] == {"waited": 1}


def test_armature_run_without_the_pack_fails_validation(tmp_path, monkeypatch):
    monkeypatch.delenv(PACKS_PATH_ENV)
    result = CliRunner().invoke(app, ["run", str(HELLO / "workflows" / "hello.yml"), "-i", "name=World", "-q"])
    assert result.exit_code == 1 and "UNKNOWN_PACK_ADAPTER" in result.output


@pytest.mark.parametrize("field", ["pack_dir: /tmp", "params: {type: object}"])
def test_spec_adapter_may_not_set_loader_fields(tmp_path, field):
    from pydantic import ValidationError

    from armature.spec.models import HarnessSpec
    body = f"""
        name: t
        adapters:
          mine: {{name: mine, type: script, cmd: "echo '{{}}'", {field}}}
        stages:
          - {{id: a, adapter: mine}}
    """
    with pytest.raises(ValidationError, match="RESERVED_FIELD"):
        _spec(tmp_path, body)
    from ruamel.yaml import YAML
    with pytest.raises(ValidationError, match="RESERVED_FIELD"):
        HarnessSpec.model_validate(YAML(typ="safe").load(textwrap.dedent(body)))


def test_pack_resolved_adapter_survives_the_reserved_field_check(tmp_path):
    spec = _spec(tmp_path, """
        name: t
        stages:
          - {id: greet, adapter: hello/greet, with: {name: Ada}}
    """)
    assert spec.adapters["hello/greet"].pack_dir == str(HELLO)
    assert spec.adapters["hello/greet"].params["required"] == ["name"]

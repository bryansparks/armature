"""Typed contracts.inputs coercion and contracts.outputs projection."""
import pytest

from armature.spec.contract_io import (
    InputError, coerce, declaration_errors, project_outputs, resolve_inputs,
)


@pytest.mark.parametrize("type_,raw,want", [
    ("boolean", "true", True),
    ("boolean", "False", False),
    ("boolean", "yes", True),
    ("boolean", "0", False),
    ("integer", "42", 42),
    ("integer", " -3 ", -3),
    ("integer", 4.0, 4),
    ("number", "2.5", 2.5),
    ("number", "7", 7),
    ("string", 12, "12"),
    ("string", True, "true"),
    ("object", '{"a": 1}', {"a": 1}),
    ("array", "[1, 2]", [1, 2]),
    (None, "anything", "anything"),
])
def test_coerce_converts_cli_strings(type_, raw, want):
    assert coerce(type_, raw, "x") == want


@pytest.mark.parametrize("type_,raw", [
    ("boolean", "maybe"),
    ("integer", "4.5"),
    ("integer", True),
    ("number", "lots"),
    ("object", "[1]"),
    ("array", "not json"),
    ("string", {"a": 1}),
])
def test_coerce_rejects_bad_values(type_, raw):
    with pytest.raises(InputError, match="x must be"):
        coerce(type_, raw, "x")


def test_resolve_inputs_applies_defaults_and_coerces():
    decls = [
        {"name": "auto_fix", "type": "boolean", "default": False},
        {"name": "limit", "type": "integer", "default": 10},
        {"name": "topic"},
    ]
    out = resolve_inputs(decls, {"auto_fix": "true", "topic": "t", "extra": "kept"})
    assert out == {"auto_fix": True, "limit": 10, "topic": "t", "extra": "kept"}


def test_resolve_inputs_enforces_enum_and_required():
    decls = [
        {"name": "mode", "type": "string", "enum": ["fast", "slow"]},
        {"name": "repo", "type": "string", "required": True},
    ]
    with pytest.raises(InputError) as exc:
        resolve_inputs(decls, {"mode": "medium"})
    assert "must be one of ['fast', 'slow']" in str(exc.value)
    assert "Required input 'repo'" in str(exc.value)


def test_resolve_inputs_without_required_enforcement_skips_missing():
    decls = [{"name": "repo", "type": "string", "required": True}]
    assert resolve_inputs(decls, {}, enforce_required=False) == {}


def test_resolve_inputs_scalars_only_leaves_structured_values():
    decls = [{"name": "items", "type": "array"}, {"name": "n", "type": "integer"}]
    out = resolve_inputs(decls, {"items": "[1]", "n": "2"}, scalars_only=True)
    assert out == {"items": "[1]", "n": 2}


def test_declaration_errors_report_type_default_and_enum():
    problems = declaration_errors([
        {"name": "a", "type": "bool"},
        {"name": "b", "type": "integer", "default": "3"},
        {"name": "c", "type": "string", "enum": ["x", 1]},
        {"name": "d", "type": "string", "enum": ["x"], "default": "y"},
        {"name": "e", "enum": []},
        {"name": "ok", "type": "boolean", "default": True},
    ], "contracts.inputs")
    codes = [code for code, _ in problems]
    assert codes == [
        "CONTRACT_INVALID_TYPE", "CONTRACT_INVALID_DEFAULT", "CONTRACT_INVALID_ENUM",
        "CONTRACT_INVALID_DEFAULT", "CONTRACT_INVALID_ENUM",
    ]


def test_project_outputs_renders_from_and_coerces():
    outputs = [
        {"name": "passed", "type": "boolean", "from": "{{ check.status == 'green' }}"},
        {"name": "count", "type": "integer", "from": "{{ check.count }}"},
        {"name": "missing", "from": "{{ nope.value }}"},
        {"stage": "check", "key": "status"},
    ]
    results = {"check": {"status": "green", "count": "3"}}
    assert project_outputs(outputs, results) == {"passed": True, "count": 3, "missing": None}


@pytest.mark.parametrize("type_,default", [("boolean", False), ("integer", 3), ("number", 1.5),
                                           ("array", [1]), ("object", {"a": 1})])
def test_empty_string_counts_as_missing_for_non_string_types(type_, default):
    decls = [{"name": "x", "type": type_, "default": default}]
    assert resolve_inputs(decls, {"x": "  "}) == {"x": default}
    with pytest.raises(InputError, match="Required input 'x'"):
        resolve_inputs([{"name": "x", "type": type_, "required": True}], {"x": ""})


def test_empty_string_stays_for_string_and_untyped_inputs():
    decls = [{"name": "s", "type": "string", "default": "d"}, {"name": "u", "default": "d"}]
    assert resolve_inputs(decls, {"s": "", "u": ""}) == {"s": "", "u": ""}


def test_scalars_only_applies_untyped_defaults_and_skips_structured():
    decls = [{"name": "u", "default": "d"}, {"name": "items", "type": "array", "default": [1]}]
    assert resolve_inputs(decls, {}, scalars_only=True) == {"u": "d"}

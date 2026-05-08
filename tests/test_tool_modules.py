"""Tests for the tools: spec section — declarative tool module loading."""
import sys
import types
import pytest
from armature.spec.models import HarnessSpec, ToolModule
from armature.registry.registry import ToolRegistry, ToolDescriptor
from armature.permissions.permissions import PermissionLevel


# ── helpers ────────────────────────────────────────────────────────────────

def _make_spec(tool_modules: list[str]) -> HarnessSpec:
    return HarnessSpec.model_validate({
        "name": "test_wf",
        "stages": [{"id": "s1", "tool_call": {"name": "noop"}, "depends_on": []}],
        "tools": [{"module": m} for m in tool_modules],
    })


def _inject_module(name: str, has_register: bool = True) -> None:
    """Inject a fake module into sys.modules for import testing."""
    mod = types.ModuleType(name)
    if has_register:
        def register(registry: ToolRegistry) -> None:
            registry.register(ToolDescriptor(
                name=f"{name}.tool",
                description=f"Tool from {name}",
                permission=PermissionLevel.READ_ONLY,
                handler=lambda args: {},
            ))
        mod.register = register
    sys.modules[name] = mod


def _remove_module(name: str) -> None:
    sys.modules.pop(name, None)


# ── ToolModule model ─────────────────────────────────────────────────────

def test_tool_module_model_parses():
    tm = ToolModule(module="myapp.tools.search")
    assert tm.module == "myapp.tools.search"


def test_harness_spec_tools_field_defaults_empty():
    spec = HarnessSpec.model_validate({"name": "w", "stages": [{"id": "s", "depends_on": []}]})
    assert spec.tools == []


def test_harness_spec_tools_field_parsed():
    spec = _make_spec(["myapp.tools.a", "myapp.tools.b"])
    assert len(spec.tools) == 2
    assert spec.tools[0].module == "myapp.tools.a"
    assert spec.tools[1].module == "myapp.tools.b"


# ── Harness._load_tool_modules ───────────────────────────────────────────

def test_load_tool_modules_registers_tools(tmp_path):
    _inject_module("fake_tool_a")
    try:
        from armature.runtime.engine import Harness
        spec = _make_spec(["fake_tool_a"])
        harness = Harness(spec=spec, session_dir=tmp_path)
        assert harness._registry.get("fake_tool_a.tool") is not None
    finally:
        _remove_module("fake_tool_a")


def test_load_tool_modules_registers_multiple(tmp_path):
    _inject_module("fake_tool_b")
    _inject_module("fake_tool_c")
    try:
        from armature.runtime.engine import Harness
        spec = _make_spec(["fake_tool_b", "fake_tool_c"])
        harness = Harness(spec=spec, session_dir=tmp_path)
        assert harness._registry.get("fake_tool_b.tool") is not None
        assert harness._registry.get("fake_tool_c.tool") is not None
    finally:
        _remove_module("fake_tool_b")
        _remove_module("fake_tool_c")


def test_load_tool_modules_raises_on_missing_register(tmp_path):
    _inject_module("bad_tool_mod", has_register=False)
    try:
        from armature.runtime.engine import Harness
        spec = _make_spec(["bad_tool_mod"])
        with pytest.raises(AttributeError, match="register"):
            Harness(spec=spec, session_dir=tmp_path)
    finally:
        _remove_module("bad_tool_mod")


def test_load_tool_modules_raises_on_missing_module(tmp_path):
    from armature.runtime.engine import Harness
    spec = _make_spec(["definitely.does.not.exist.xyz"])
    with pytest.raises(ModuleNotFoundError):
        Harness(spec=spec, session_dir=tmp_path)


def test_no_tools_section_loads_cleanly(tmp_path):
    from armature.runtime.engine import Harness
    spec = _make_spec([])
    harness = Harness(spec=spec, session_dir=tmp_path)
    # builtins still present
    assert harness._registry.get("file_read") is not None


def test_tool_module_does_not_override_builtins(tmp_path):
    """A user module that re-registers 'file_read' replaces the builtin — expected behavior."""
    mod = types.ModuleType("override_tool")
    def register(registry: ToolRegistry) -> None:
        registry.register(ToolDescriptor(
            name="file_read",
            description="Custom file_read",
            permission=PermissionLevel.READ_ONLY,
            handler=lambda args: {"content": "custom"},
        ))
    mod.register = register
    sys.modules["override_tool"] = mod
    try:
        from armature.runtime.engine import Harness
        spec = _make_spec(["override_tool"])
        harness = Harness(spec=spec, session_dir=tmp_path)
        assert harness._registry.get("file_read").description == "Custom file_read"
    finally:
        _remove_module("override_tool")

"""Tests for Stage.inject_file_as — file-content injection in fan-out stages.

inject_file_as adds file content to the per-item context under the given key.
For ToolCallNode stages, the injected value must be forwarded via args templates
(e.g., args={"body": "{{ body }}"}) since handlers only receive rendered cfg.args.
For LLM stages the full context (including injected content) is available in prompts.
"""
import pytest
from pathlib import Path
from armature.spec.models import (
    Stage, HarnessSpec, ModelTiers, ModelTierConfig, ToolCallConfig,
)
from armature.runtime.engine import Harness
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel


def _make_harness(stages, tmp_path) -> Harness:
    spec = HarnessSpec(
        name="wf",
        stages=stages,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=tmp_path)


def _register(harness, name, fn):
    harness._registry.register(ToolDescriptor(
        name=name, description=name, permission=PermissionLevel.READ_ONLY,
        handler=fn, parameters={},
    ))


# ── inject_file_as with tool_call (args must template the injected key) ────────

async def test_inject_file_as_injects_file_content_via_args_template(tmp_path):
    """File content injected into context; tool receives it via args template."""
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("content-a")
    f2.write_text("content-b")

    captured = []

    async def reader(args):
        captured.append(args.get("body"))
        return {"read": args.get("body", "")}

    stage = Stage(
        id="s",
        tool_call=ToolCallConfig(name="reader", args={"body": "{{ body }}"}),
        fan_out=2,
        partition_source="{{ files }}",
        partition_key="item",
        inject_file_as="body",
        depends_on=[],
    )
    harness = _make_harness([stage], tmp_path)
    _register(harness, "reader", reader)

    result = await harness.run({"files": [str(f1), str(f2)]})

    assert isinstance(result["s"], list)
    assert len(result["s"]) == 2
    contents = {r["read"] for r in result["s"]}
    assert "content-a" in contents
    assert "content-b" in contents


async def test_inject_file_as_missing_file_injects_empty_string(tmp_path):
    """Non-existent file path injects '' without raising; tool receives ""."""
    received = []

    async def reader(args):
        received.append(args.get("body"))
        return {"ok": True}

    stage = Stage(
        id="s",
        tool_call=ToolCallConfig(name="reader", args={"body": "{{ body }}"}),
        fan_out=2,
        partition_source="{{ files }}",
        partition_key="item",
        inject_file_as="body",
        depends_on=[],
    )
    harness = _make_harness([stage], tmp_path)
    _register(harness, "reader", reader)

    result = await harness.run({"files": ["/nonexistent/path.txt", "/also/missing.txt"]})

    assert len(result["s"]) == 2
    assert all(r["ok"] is True for r in result["s"])
    assert all(v == "" for v in received)


async def test_inject_file_as_partition_key_available_alongside_body(tmp_path):
    """Both partition_key (the file path) and inject_file_as (content) are in context."""
    f = tmp_path / "f.txt"
    f.write_text("hello")

    captured = []

    async def reader(args):
        captured.append({"path": args.get("path"), "body": args.get("body")})
        return {"ok": True}

    stage = Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="reader",
            args={"path": "{{ item }}", "body": "{{ body }}"},
        ),
        fan_out=1,
        partition_source="{{ files }}",
        partition_key="item",
        inject_file_as="body",
        depends_on=[],
    )
    harness = _make_harness([stage], tmp_path)
    _register(harness, "reader", reader)

    await harness.run({"files": [str(f)]})

    assert len(captured) == 1
    assert captured[0]["path"] == str(f)
    assert captured[0]["body"] == "hello"


async def test_inject_file_as_with_fan_in_merge(tmp_path):
    """inject_file_as works with fan_in=merge."""
    f1 = tmp_path / "p1.txt"
    f2 = tmp_path / "p2.txt"
    f1.write_text("alpha")
    f2.write_text("beta")

    async def reader(args):
        return {args["path"]: args.get("body", "")}

    stage = Stage(
        id="s",
        tool_call=ToolCallConfig(
            name="reader",
            args={"path": "{{ item }}", "body": "{{ body }}"},
        ),
        fan_out=2,
        fan_in="merge",
        partition_source="{{ files }}",
        partition_key="item",
        inject_file_as="body",
        depends_on=[],
    )
    harness = _make_harness([stage], tmp_path)
    _register(harness, "reader", reader)

    result = await harness.run({"files": [str(f1), str(f2)]})

    assert isinstance(result["s"], dict)
    assert result["s"][str(f1)] == "alpha"
    assert result["s"][str(f2)] == "beta"


async def test_inject_file_as_without_template_in_args_tool_does_not_see_content(tmp_path):
    """Without an args template, handler does not receive the injected content.

    This documents the expected behavior: inject_file_as adds to context but
    ToolCallNode only dispatches cfg.args, so the handler only sees what args templates
    explicitly forward.
    """
    f = tmp_path / "x.txt"
    f.write_text("should-not-appear")
    received = []

    async def reader(args):
        received.append(args.get("body"))
        return {"ok": True}

    # No args → handler gets {} → body is None
    stage = Stage(
        id="s",
        tool_call=ToolCallConfig(name="reader"),  # no args configured
        fan_out=1,
        partition_source="{{ files }}",
        partition_key="item",
        inject_file_as="body",
        depends_on=[],
    )
    harness = _make_harness([stage], tmp_path)
    _register(harness, "reader", reader)

    await harness.run({"files": [str(f)]})

    assert received == [None]  # content not forwarded without args template


async def test_no_inject_file_as_partition_key_in_context_only(tmp_path):
    """Without inject_file_as, partition item is in context but not auto-forwarded to args."""
    seen = []

    async def processor(args):
        seen.append(args.get("val"))
        return {"val": args.get("val")}

    # Template explicitly passes partition_key through
    stage = Stage(
        id="s",
        tool_call=ToolCallConfig(name="processor", args={"val": "{{ chunk }}"}),
        fan_out=3,
        partition_source="{{ chunks }}",
        partition_key="chunk",
        depends_on=[],
    )
    harness = _make_harness([stage], tmp_path)
    _register(harness, "processor", processor)

    result = await harness.run({"chunks": ["x", "y", "z"]})

    assert len(result["s"]) == 3
    assert set(r["val"] for r in result["s"]) == {"x", "y", "z"}

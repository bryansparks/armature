"""Tests for AutoHarness — iterative harness spec synthesis.

SpecDrafter calls a frontier LLM to generate YAML from a task description.
AutoHarness orchestrates draft → validate → constraint-check → refine cycles.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── SpecDrafter ───────────────────────────────────────────────────────────────

_MINIMAL_VALID_YAML = """
name: simple_workflow
stages:
  - id: worker
    role:
      name: worker
      type: worker
      description: Do the task.
""".strip()

_INVALID_YAML = "this: is: not: valid: yaml: ::::"

_MISSING_STAGES_YAML = """
name: broken_workflow
""".strip()


async def test_spec_drafter_draft_returns_string():
    """draft() returns a string (the raw LLM output)."""
    from armature.synthesis.autoharness import SpecDrafter

    drafter = SpecDrafter(model="claude-haiku-4-5-20251001")

    async def mock_completion(**kwargs):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = _MINIMAL_VALID_YAML
        return resp

    with patch("armature.synthesis.autoharness.litellm_completion", side_effect=mock_completion):
        result = await drafter.draft("Build a simple worker workflow")

    assert isinstance(result, str)
    assert "name:" in result


async def test_spec_drafter_draft_includes_task_in_prompt():
    """The task description appears in the LLM call."""
    from armature.synthesis.autoharness import SpecDrafter

    drafter = SpecDrafter(model="claude-haiku-4-5-20251001")
    captured = {}

    async def mock_completion(**kwargs):
        captured["messages"] = kwargs.get("messages", [])
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = _MINIMAL_VALID_YAML
        return resp

    with patch("armature.synthesis.autoharness.litellm_completion", side_effect=mock_completion):
        await drafter.draft("analyze sentiment of customer reviews")

    prompt_text = " ".join(m["content"] for m in captured["messages"])
    assert "sentiment" in prompt_text or "analyze" in prompt_text


async def test_spec_drafter_draft_includes_feedback_in_prompt():
    """When feedback is provided, it appears in the LLM call."""
    from armature.synthesis.autoharness import SpecDrafter

    drafter = SpecDrafter(model="claude-haiku-4-5-20251001")
    captured = {}

    async def mock_completion(**kwargs):
        captured["messages"] = kwargs.get("messages", [])
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = _MINIMAL_VALID_YAML
        return resp

    with patch("armature.synthesis.autoharness.litellm_completion", side_effect=mock_completion):
        await drafter.draft("do something", feedback="DUPLICATE_STAGE_ID error on stage 'worker'")

    prompt_text = " ".join(m["content"] for m in captured["messages"])
    assert "DUPLICATE_STAGE_ID" in prompt_text or "duplicate" in prompt_text.lower()


def test_spec_drafter_parse_valid_yaml_returns_spec():
    """parse() returns a HarnessSpec for valid YAML."""
    from armature.synthesis.autoharness import SpecDrafter
    from armature.spec.models import HarnessSpec

    drafter = SpecDrafter(model="claude-haiku-4-5-20251001")
    spec = drafter.parse(_MINIMAL_VALID_YAML)

    assert spec is not None
    assert isinstance(spec, HarnessSpec)
    assert spec.name == "simple_workflow"


def test_spec_drafter_parse_invalid_yaml_returns_none():
    """parse() returns None for garbage YAML."""
    from armature.synthesis.autoharness import SpecDrafter

    drafter = SpecDrafter(model="claude-haiku-4-5-20251001")
    assert drafter.parse(_INVALID_YAML) is None


def test_spec_drafter_parse_invalid_spec_returns_none():
    """parse() returns None when YAML parses but spec is structurally invalid."""
    from armature.synthesis.autoharness import SpecDrafter

    drafter = SpecDrafter(model="claude-haiku-4-5-20251001")
    assert drafter.parse(_MISSING_STAGES_YAML) is None


def test_spec_drafter_parse_strips_markdown_fences():
    """parse() handles LLM output wrapped in ```yaml fences."""
    from armature.synthesis.autoharness import SpecDrafter

    drafter = SpecDrafter(model="claude-haiku-4-5-20251001")
    fenced = f"```yaml\n{_MINIMAL_VALID_YAML}\n```"
    spec = drafter.parse(fenced)
    assert spec is not None
    assert spec.name == "simple_workflow"


# ── AutoHarness synthesis loop ────────────────────────────────────────────────

def _make_draft_fn(yaml_sequence: list[str]):
    """Returns an async mock that yields successive YAML strings."""
    call_idx = [0]

    async def _draft(task_description, feedback=None):
        idx = min(call_idx[0], len(yaml_sequence) - 1)
        call_idx[0] += 1
        return yaml_sequence[idx]

    return _draft


async def test_autoharness_returns_spec_on_first_valid_draft():
    """If first draft is valid, synthesize() returns it without retrying."""
    from armature.synthesis.autoharness import AutoHarness, SpecDrafter

    drafter = SpecDrafter(model="m")
    drafter.draft = _make_draft_fn([_MINIMAL_VALID_YAML])

    harness = AutoHarness(drafter=drafter, max_iterations=5)
    spec, history = await harness.synthesize("build a worker")

    assert spec is not None
    assert spec.name == "simple_workflow"
    assert len(history) == 0  # no failures, no feedback needed


async def test_autoharness_retries_on_invalid_yaml():
    """If first draft is invalid YAML, retries with feedback."""
    from armature.synthesis.autoharness import AutoHarness, SpecDrafter

    drafter = SpecDrafter(model="m")
    drafter.draft = _make_draft_fn([_INVALID_YAML, _MINIMAL_VALID_YAML])

    harness = AutoHarness(drafter=drafter, max_iterations=5)
    spec, history = await harness.synthesize("build a worker")

    assert spec is not None
    assert len(history) == 1  # one failure round before success


async def test_autoharness_retries_on_validation_failure():
    """If first draft fails spec validation, retries with error feedback."""
    _bad_stage_yaml = """
name: bad_wf
stages:
  - id: a
    role:
      name: r
      type: worker
      description: do it.
    depends_on: [missing_stage]
""".strip()

    from armature.synthesis.autoharness import AutoHarness, SpecDrafter

    drafter = SpecDrafter(model="m")
    drafter.draft = _make_draft_fn([_bad_stage_yaml, _MINIMAL_VALID_YAML])

    harness = AutoHarness(drafter=drafter, max_iterations=5)
    spec, history = await harness.synthesize("build a worker")

    assert spec is not None
    assert len(history) >= 1
    assert any("UNDEFINED_DEPENDENCY" in h or "missing_stage" in h for h in history)


async def test_autoharness_stops_at_max_iterations():
    """Returns (None, history) when all iterations produce invalid specs."""
    from armature.synthesis.autoharness import AutoHarness, SpecDrafter

    drafter = SpecDrafter(model="m")
    drafter.draft = _make_draft_fn([_INVALID_YAML] * 10)

    harness = AutoHarness(drafter=drafter, max_iterations=3)
    spec, history = await harness.synthesize("build a worker")

    assert spec is None
    assert len(history) == 3


async def test_autoharness_feedback_contains_error_details():
    """Feedback passed after failure includes specific error information."""
    from armature.synthesis.autoharness import AutoHarness, SpecDrafter

    feedback_seen = []

    async def capture_draft(task_description, feedback=None):
        if feedback is not None:
            feedback_seen.append(feedback)
        return _MINIMAL_VALID_YAML

    drafter = SpecDrafter(model="m")
    drafter.draft = AsyncMock(side_effect=capture_draft)

    # Override parse to fail once, then succeed
    call_count = [0]
    original_parse = drafter.parse

    def selective_parse(yaml_text):
        call_count[0] += 1
        if call_count[0] == 1:
            return None  # simulate parse failure on first attempt
        return original_parse(yaml_text)

    drafter.parse = selective_parse

    harness = AutoHarness(drafter=drafter, max_iterations=5)
    await harness.synthesize("build a worker")

    assert len(feedback_seen) >= 1
    assert any(len(f) > 0 for f in feedback_seen)


async def test_autoharness_constraint_fn_passes_on_valid_spec():
    """When constraint_fn returns True, spec is accepted."""
    from armature.synthesis.autoharness import AutoHarness, SpecDrafter

    drafter = SpecDrafter(model="m")
    drafter.draft = _make_draft_fn([_MINIMAL_VALID_YAML])

    harness = AutoHarness(drafter=drafter, max_iterations=5)
    spec, history = await harness.synthesize(
        "build a worker",
        constraint_fn=lambda s: True,
    )

    assert spec is not None


async def test_autoharness_constraint_fn_triggers_retry():
    """When constraint_fn returns False, the loop retries with constraint feedback."""
    from armature.synthesis.autoharness import AutoHarness, SpecDrafter

    constraint_calls = [0]

    def flaky_constraint(spec):
        constraint_calls[0] += 1
        return constraint_calls[0] >= 2  # fail first, pass second

    drafter = SpecDrafter(model="m")
    drafter.draft = _make_draft_fn([_MINIMAL_VALID_YAML, _MINIMAL_VALID_YAML])

    harness = AutoHarness(drafter=drafter, max_iterations=5)
    spec, history = await harness.synthesize("build a worker", constraint_fn=flaky_constraint)

    assert spec is not None
    assert constraint_calls[0] == 2
    assert len(history) == 1  # one failure before passing


async def test_autoharness_constraint_failure_in_feedback():
    """Feedback after constraint failure mentions the constraint check failed."""
    from armature.synthesis.autoharness import AutoHarness, SpecDrafter

    feedback_seen = []

    async def capture_draft(task_description, feedback=None):
        if feedback is not None:
            feedback_seen.append(feedback)
        return _MINIMAL_VALID_YAML

    drafter = SpecDrafter(model="m")
    drafter.draft = AsyncMock(side_effect=capture_draft)

    call_count = [0]

    def flaky_constraint(spec):
        call_count[0] += 1
        return call_count[0] >= 2

    harness = AutoHarness(drafter=drafter, max_iterations=5)
    await harness.synthesize("build a worker", constraint_fn=flaky_constraint)

    assert any("constraint" in f.lower() for f in feedback_seen)


async def test_autoharness_returns_iteration_count_in_history():
    """History length equals the number of failed iterations."""
    from armature.synthesis.autoharness import AutoHarness, SpecDrafter

    drafter = SpecDrafter(model="m")
    # 2 bad, then 1 good
    drafter.draft = _make_draft_fn([_INVALID_YAML, _INVALID_YAML, _MINIMAL_VALID_YAML])

    harness = AutoHarness(drafter=drafter, max_iterations=10)
    spec, history = await harness.synthesize("build a worker")

    assert spec is not None
    assert len(history) == 2

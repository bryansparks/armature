"""Levers that vary conditions between runs.

- input_difficulty_ramp: walks a CSV corpus in order, one row per phase index,
  injecting row fields as --input values. Tests H1 (HQS tracks difficulty).
- spec_corruption: deterministically corrupts one prompt in the working spec,
  then lets self-improve try to recover. Tests H2 (fires + recovers).
- model_tier_degradation: downgrades the judge's model_tier to a deliberately
  broken tier (invalid model id) so its LLM call reliably errors -> a failure
  trace row is written -> HQS drops below target -> self_improve fires. Maps to
  improve's STAGE_FAILED (model_problem) -> upgrade model_tier rule, giving a
  plausible fire->edit->recover path. Use with fresh_db:true so the failures
  are not diluted by prior phases' successes. Tests H2 (fires + recovers).
- memory_cold_warm: toggles the working spec's `memory.fresh` field from the
  phase's `memory_fresh` input ("true" -> cold, "false" -> warm). The memory
  block itself (enabled/capture/inject_as) lives in the workflow spec; this
  lever only flips the one field that distinguishes a cold run (ignore prior
  memories) from a warm run (inject them) against the shared memory DB.
  memory_mode() reads the field back to label the row for the H4 verdict.
- none: pass the phase's literal inputs through.
"""
from __future__ import annotations

import csv
import random
import re
from pathlib import Path

from campaign_runner.plan import Phase


def load_corpus(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


_VAR_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _render(template: str, ctx: dict) -> str:
    def sub(m: re.Match) -> str:
        val: object = ctx
        for part in m.group(1).split("."):
            if isinstance(val, dict) and part in val:
                val = val[part]
            else:
                return ""
        return str(val)
    return _VAR_RE.sub(sub, template)


def apply_lever(phase: Phase, *, phase_index: int, rep: int,
                corpus: list[dict], working_spec: Path, rng_seed: int) -> dict:
    """Return the --input dict for this run; mutate working_spec if needed."""
    if phase.lever == "input_difficulty_ramp":
        row = corpus[phase_index % len(corpus)] if corpus else {}
        ctx = {"corpus_row": row, "phase_index": phase_index, "rep": rep}
    elif phase.lever == "spec_corruption":
        rng = random.Random(rng_seed + phase_index * 100 + rep)
        _corrupt_spec(working_spec, rng)
        ctx = {"phase_index": phase_index, "rep": rep, "seed": rng_seed}
    elif phase.lever == "model_tier_degradation":
        _break_judge_tier(working_spec)
        ctx = {"phase_index": phase_index, "rep": rep, "seed": rng_seed}
    elif phase.lever == "memory_cold_warm":
        # interleave cold/warm by rep parity so the H4 verdict is not
        # confounded by phase ordering: even rep = cold (fresh=true, ignore
        # prior memory but still capture), odd rep = warm (fresh=false, inject).
        fresh = _fresh_for_memory(phase.inputs, rep)
        _set_memory_fresh(working_spec, fresh)
        ctx = {"phase_index": phase_index, "rep": rep, "seed": rng_seed}
    else:  # "none"
        ctx = {"phase_index": phase_index, "rep": rep}

    return {k: _render(v, ctx) for k, v in phase.inputs.items()}


def _fresh_for_memory(inputs: dict, rep: int) -> bool:
    """The memory.fresh value the memory_cold_warm lever sets for a given rep.
    Single source of truth for the cold/warm alternation convention so the live
    apply_lever and zero-cost replay derivation (memory_mode_for) cannot drift:
    'alternate' -> cold on even rep, warm on odd; otherwise fresh iff the
    memory_fresh input is 'true'. A static string, so the rendered inputs the
    recording carries equal the declared phase.inputs."""
    mf = str((inputs or {}).get("memory_fresh", "false")).strip().lower()
    if mf == "alternate":
        return (rep % 2 == 0)
    return (mf == "true")


def memory_mode_for(lever: str | None, inputs: dict, rep: int) -> str | None:
    """Derive the memory_mode label ('cold'/'warm'/'nav'/None) for a run from the
    phase lever + inputs + rep index WITHOUT a spec — the replay-path mirror of
    what apply_lever + memory_mode compute live. Used by replay() to reconstruct
    the H4 cold/warm split from an OLD recording whose meta predates
    forward-captured memory_mode. Returns None for any lever that does not
    exercise memory, matching fault.memory_mode's None for a spec with no
    memory block.

    A forward-captured `memory_mode` in inputs/meta takes precedence: the nav
    phase uses lever='none' (no cold/warm signal), so its label must be carried
    forward from the live recording rather than re-derived from the lever."""
    # Forward-captured label takes precedence (replay of nav-phase rows whose
    # lever is "none" and so carry no cold/warm signal).
    captured = (inputs or {}).get("memory_mode")
    if captured in ("cold", "warm", "nav"):
        return captured
    if lever != "memory_cold_warm":
        return None
    return "cold" if _fresh_for_memory(inputs, rep) else "warm"


def memory_mode(path: Path) -> str | None:
    """Label a working spec's run 'cold', 'warm', or 'nav' from its `memory` block.

    'nav'   -> memory.navigation_tools is truthy (the run uses memory navigation
               tools — the third arm of the H4 campaign). Checked first because
               a nav spec also carries fresh:false, which would otherwise label
               it 'warm'.
    'cold'  -> memory.fresh is true  (prior memories ignored this run; a clean
               baseline that still captures, so it populates the shared DB).
    'warm'  -> memory.fresh is false (prior memories injected at run start).
    None    -> the spec has no `memory` block (memory is not exercised).

    Drives the H4 verdict's cold/warm/nav split. Pure reader — never mutates.
    """
    import yaml
    try:
        spec = yaml.safe_load(path.read_text())
    except Exception:
        return None
    mem = (spec or {}).get("memory")
    if isinstance(mem, dict):
        if mem.get("navigation_tools"):
            return "nav"
        return "cold" if mem.get("fresh") else "warm"
    return None


_MEMORY_BLOCK_RE = re.compile(r'^memory:[ \t]*(?:#.*)?$', re.MULTILINE)
# matches an indented `fresh:` key with an optional value (true/false/<empty>).
# `(?P<val>...)` lets us rewrite only the value, keeping the key+indent.
_FRESH_LINE_RE = re.compile(r'^([ \t]*fresh:[ \t]*)(?P<val>\S.*?)?\s*$', re.MULTILINE)


def _set_memory_fresh(path: Path, fresh: bool) -> None:
    """Set spec['memory']['fresh'] to toggle cold (fresh=True) vs warm (fresh=False).

    Only this one field is flipped; every other line of the spec — including
    block-scalar Jinja `description:|` prompts with `{% if %}` / `{{ }}`
    control blocks — is preserved byte-for-byte. This is a SURGICAL line edit,
    NOT a yaml round-trip: yaml.safe_dump rewrites block scalars into
    double-quoted folded form and injects stray backslashes into multi-line
    Jinja, which then raises TemplateSyntaxError at run time (validation does
    not render templates, so the bug is invisible until `armature run`).

    A no-op when the spec has no top-level `memory:` mapping (memory_mode()
    then returns None for the row). If the block exists but has no `fresh:`
    line, one is inserted immediately under `memory:`.
    """
    text = path.read_text()
    m = _MEMORY_BLOCK_RE.search(text)
    if m is None:
        return  # no memory block — nothing to toggle
    new_val = "true" if fresh else "false"
    fm = _FRESH_LINE_RE.search(text, m.end())
    if fm is not None:
        # rewrite only the value, keep indent + key + trailing newline
        line_end = text.find("\n", fm.start())
        replacement = f"{fm.group(1)}{new_val}"
        new_text = text[:fm.start()] + replacement + text[line_end:]
    else:
        # no fresh: line — insert one (two-space indent) right after `memory:`
        insert_at = text.find("\n", m.end())  # end of the `memory:` line
        if insert_at == -1:
            insert_at = len(text)
        new_text = text[:insert_at] + f"\n  fresh: {new_val}" + text[insert_at:]
    path.write_text(new_text)


# Match `description:` anywhere (block key or inline in a flow mapping) followed
# by the rest of that line — the value may be quoted ("..."), a block-scalar
# indicator (| or >), or bare unquoted text.
_DESC_RE = re.compile(r'description:[ \t]*(?P<rest>[^\n]*)')


def _corrupt_spec(path: Path, rng: random.Random) -> None:
    """Garble one stage's description text so HQS degrades, deterministically.

    Handles three real-world description forms so the corruption actually
    fires on real Armature specs (not just quoted single-line ones):
      1. quoted single-line:   description: "Research X"
      2. unquoted single-line: description: Research X
      3. block-scalar:         description: |\n  Research X\n  ...
    The garble style (truncate to ~half + " ZZZCORRUPT zzz ") is preserved so
    downstream tests' expectations of degraded text still hold.
    """
    text = path.read_text()
    matches = list(_DESC_RE.finditer(text))
    if not matches:
        return
    m = rng.choice(matches)
    rest = m.group("rest").rstrip()
    garble_token = " ZZZCORRUPT zzz "

    # indentation of the line holding `description:` — needed to locate
    # block-scalar content lines (which must be indented deeper than this).
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_indent = ""
    for ch in text[line_start:m.start()]:
        if ch in " \t":
            line_indent += ch
        else:
            break

    if rest.startswith('"'):
        # quoted single-line: description: "Research X"
        inner = rest[1:]
        if inner.endswith('"'):
            inner = inner[:-1]
        garbled = inner[: max(0, len(inner) // 2)] + garble_token
        replacement = f'description: "{garbled}"'
        new_text = text[:m.start()] + replacement + text[m.end():]
    elif rest[:1] in ("|", ">"):
        # block-scalar: description: |  (or >, |-, >-, etc.)
        # inject the garble token into the first indented content line below,
        # preserving indentation so the YAML stays valid.
        after = m.end()
        line_re = re.compile(r'^(?P<lindent>[ \t]*)(?P<content>\S.*)$', re.MULTILINE)
        new_text = None
        for lm in line_re.finditer(text, after):
            if lm.group("lindent") <= line_indent:
                break  # dedented past the key — block ended, no content found
            content = lm.group("content")
            lindent = lm.group("lindent")
            garbled = content[: max(0, len(content) // 2)] + garble_token
            new_text = text[:lm.start()] + lindent + garbled + text[lm.end():]
            break
        if new_text is None:
            return
    else:
        # unquoted single-line: description: Research X
        original = rest
        garbled = original[: max(0, len(original) // 2)] + garble_token
        replacement = f'description: {garbled}'
        new_text = text[:m.start()] + replacement + text[m.end():]

    path.write_text(new_text)


# An OpenRouter model id that does not exist. litellm raises a BadRequestError
# immediately (client error, not retried), so the judge's LLM call fails fast
# and deterministically without burning tokens or wall-clock.
_BROKEN_MODEL = "z-ai/nonexistent-degradation-model"


def _break_judge_tier(path: Path) -> None:
    """Degrade the judge's model tier to a deliberately-broken tier.

    Adds a `broken` tier pointing at an invalid model id and sets the judge
    stage's `model_tier: broken`. The judge's LLM call then errors every time
    (BadRequestError), producing a failure trace row (success=0, output_valid=0)
    so the run's HQS drops below target and self_improve fires. improve's
    diagnostic for this is STAGE_FAILED (model_problem) -> "upgrade model_tier",
    so the SpecRefiner plausibly upgrades the judge back to a valid tier and the
    recovery probe succeeds: a real fire->edit->recover.

    The working spec is a per-campaign sandbox copy, so the yaml round-trip
    (which drops source comments) is fine — the source spec is untouched.
    Re-applied each rep so a prior improve fix is re-broken deterministically.
    """
    import yaml
    spec = yaml.safe_load(path.read_text()) or {}
    tiers = spec.setdefault("model_tiers", {})
    tiers["broken"] = {
        "provider": "openrouter",
        "model": _BROKEN_MODEL,
        "api_key_env": "OPENROUTER_API_KEY",
        "temperature": 0.2,
        "max_tokens": 64,
    }
    for stage in spec.get("stages", []):
        if stage.get("id") == "judge":
            stage.setdefault("role", {})["model_tier"] = "broken"
            break
    path.write_text(yaml.safe_dump(spec, sort_keys=False))
"""Levers that vary conditions between runs.

- input_difficulty_ramp: walks a CSV corpus in order, one row per phase index,
  injecting row fields as --input values. Tests H1 (HQS tracks difficulty).
- spec_corruption: deterministically corrupts one prompt in the working spec,
  then lets self-improve try to recover. Tests H2 (fires + recovers).
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
    else:  # "none"
        ctx = {"phase_index": phase_index, "rep": rep}

    return {k: _render(v, ctx) for k, v in phase.inputs.items()}


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
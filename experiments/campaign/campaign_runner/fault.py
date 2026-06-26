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


_DESC_RE = re.compile(r'(description:\s*"?)([^\n]*)(\??")', re.MULTILINE)


def _corrupt_spec(path: Path, rng: random.Random) -> None:
    """Garble one stage's description text so HQS degrades, deterministically."""
    text = path.read_text()
    # find all description lines
    matches = list(_DESC_RE.finditer(text))
    if not matches:
        return
    m = rng.choice(matches)
    original = m.group(2)
    # drop a few characters and inject a confusing token — degrades prompt quality
    garbled = original[: max(0, len(original) // 2)] + " ZZZCORRUPT zzz "
    new_text = text[:m.start()] + m.group(1) + garbled + m.group(3) + text[m.end():]
    path.write_text(new_text)
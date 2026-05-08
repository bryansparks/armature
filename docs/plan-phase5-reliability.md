# Phase 5: Reliability Improvements

Three targeted improvements in recommended execution order.

---

## Item 1 — Contract.inputs Enforcement

**Goal:** Validate required inputs at `harness.run()` boundary; fail fast with a clear error before any stage executes.

**Motivation:** `HarnessSpec.contracts.inputs` exists in the model but is never checked at runtime. A missing input currently causes a cryptic Jinja2 render failure deep inside a stage — or silently produces wrong output.

### Steps

1. **Define the input schema format** (`armature/spec/models.py`)
   - `Contract.inputs` is currently `list[dict[str, Any]]`. Agree on the shape: `{"name": "repo_path", "required": true, "description": "...", "type": "string"}`.
   - No model change needed if we just pick the keys we'll read.

2. **Write the validator** (`armature/runtime/engine.py`)
   - New method `_validate_inputs(self, context: dict) -> None`
   - Iterate `self._spec.contracts.inputs`; for each entry with `required: True`, assert the key is present in `context` and is not `None`.
   - Raise `ValueError(f"Required input '{name}' missing from context")` on first violation (fail fast).

3. **Wire into `run()`**
   - Call `self._validate_inputs(context)` as the first line of `Harness.run()` before any stage dispatch.

4. **Tests** (`tests/runtime/test_contract_inputs.py`)
   - Missing required input → `ValueError` with key name in message.
   - Optional input absent → runs normally.
   - All required inputs present → runs normally.
   - Empty `contracts.inputs` list → always passes.
   - Multiple missing inputs → error mentions first missing key.

**Estimated effort:** ~1 hour. No external deps.

---

## Item 2 — Context Window Management

**Goal:** Prevent large stage outputs from bloating the context dict passed to downstream LLM system prompts; truncate or summarise over-sized values.

**Motivation:** A stage that returns a 50 KB file-scan result will be Jinja2-rendered into every downstream role description and user message, blowing past LLM context windows and costing unnecessary tokens.

### Steps

1. **Add per-stage output size limit** (`armature/spec/models.py`)
   - New field on `Stage`: `output_max_chars: int | None = None`
   - If set, the stored result is truncated to that many characters (for string values) or serialised-then-truncated (for dicts/lists).
   - A global default can live in `Contract`: `output_max_chars: int | None = None` (e.g., 8000).

2. **Implement truncation** (`armature/runtime/engine.py`)
   - After a stage result is computed, call `_truncate_result(result, limit)` before storing in `context`.
   - `_truncate_result`: if value is a `dict`, truncate each string leaf; if root is a string, truncate and append `" ...[truncated]"`.
   - Use the stage-level limit if set, otherwise fall back to the spec-level `contracts.output_max_chars`.

3. **Add context-injection filter for Jinja2** (`armature/runtime/prompt.py`)
   - `PromptAssembler` currently renders the full `context` dict into role descriptions via Jinja2.
   - Apply the same truncation before passing `context` to `env.from_string().render()` so oversized values don't reach the LLM even if they weren't truncated at storage time (belt and suspenders).

4. **Tests** (`tests/runtime/test_output_truncation.py`)
   - Stage result string > limit is truncated and contains `[truncated]` marker.
   - Stage result within limit is unchanged.
   - `output_max_chars` on stage overrides spec-level default.
   - Downstream stage can still read `result["_truncated"] == True` flag (optional sentinel key).
   - Truncation doesn't break `fail_as_value` dict or `_skipped` dict.

**Estimated effort:** ~2–3 hours. Pure Python, no new deps.

---

## Item 3 — Checkpoint/Resume

**Goal:** Persist completed stage results to disk; on re-run with the same `session_dir`, skip already-completed stages and resume from the last checkpoint.

**Motivation:** Long-running workflows (hour-scale security scans, multi-stage research) fail partway through and must restart from scratch. Checkpointing lets the user re-run and pick up where they left off.

### Steps

1. **Checkpoint file format**
   - One JSON file per run: `{session_dir}/checkpoint.json`
   - Schema: `{"stage_id": <result_dict>, ...}` — one key per completed stage.
   - Written after each stage completes (append semantics: read, update key, write).

2. **Add checkpoint enable flag** (`armature/spec/models.py`)
   - New field on `HarnessSpec` (or `Contract`): `checkpoint: bool = False`
   - Default off so existing behavior is unchanged.

3. **Checkpoint writer** (`armature/runtime/engine.py`)
   - New method `_checkpoint_write(self, stage_id: str, result: Any) -> None`
   - Reads existing file (or `{}`), adds/overwrites the key, writes back atomically (write to `.tmp`, rename).

4. **Checkpoint reader at startup**
   - In `Harness.run()`, if `checkpoint == True`, read `checkpoint.json` into `prior: dict`.
   - Pre-populate `context` with all `prior` results.
   - In `_execute_stage_with_recovery`, if the stage id is in `prior`, skip execution and return `prior[stage_id]` immediately (log a `[checkpoint] skipping stage_id` message).

5. **Invalidation** (simple strategy for now)
   - No automatic invalidation — if the user wants to re-run from scratch they delete `checkpoint.json` or pass `force=True` to `harness.run()`.
   - Document this clearly.

6. **Tests** (`tests/runtime/test_checkpoint.py`)
   - First run with `checkpoint=True` creates `checkpoint.json` with completed stages.
   - Second run loads checkpoint, skips completed stages (verify tool not called again).
   - Failed stage is NOT written to checkpoint; next run re-executes it.
   - `fail_as_value` failure result IS written (it's a valid result, user decides to retry by deleting checkpoint).
   - `force=True` ignores checkpoint and reruns all stages.
   - `checkpoint=False` (default) writes no file.

**Estimated effort:** ~3–4 hours. Requires atomic file I/O, careful interaction with `fail_as_value` and `skip_if`.

---

## Execution Order Summary

| # | Item | Effort | Dependency |
|---|------|--------|------------|
| 1 | Contract.inputs enforcement | 1h | None |
| 2 | Context window management | 2–3h | None |
| 3 | Checkpoint/resume | 3–4h | Builds naturally on engine structure established in #1 and #2 |

Each item is independently mergeable. Start with #1 (fastest, zero risk), then #2, then #3.

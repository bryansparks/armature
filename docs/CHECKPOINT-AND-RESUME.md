# Checkpoint and Resume in Armature

Fault-tolerant workflow execution — persist completed stage results, pick up where you left off.

---

A workflow is not a transaction. Stage 9 of a 12-stage run does not roll back because stage 10 hit a rate limit. Without checkpointing, it restarts from zero — every LLM call, every minute of wall time, repeated. With checkpointing, the next run finds stage 9's result on disk and skips straight past it.

That is the entire idea. The rest is implementation details.

---

## Enabling checkpoint

One field in the spec:

```yaml
name: compliance-audit
checkpoint: true   # that's it

stages:
  - id: list_documents
    ...
  - id: review_each
    fan_out: 10
    ...
  - id: escalation_check
    ...
  - id: final_report
    ...
```

When `checkpoint: true`, the harness writes completed stage results to `checkpoint.json` in the session directory after every successfully completed stage. On the next run with the same session directory, completed stages are skipped and their results injected directly into context.

---

## Why it matters: the cost math

A 12-stage compliance workflow runs 100 documents through a fan-out review, costs $40 in LLM calls, and takes 20 minutes.

Stage 10 fails — network timeout, API rate limit, process killed by the OS, terminal closed mid-run.

| Without checkpoint | With checkpoint |
|-------------------|-----------------|
| Restart from stage 1 | Resume from stage 10 |
| $40, 20 minutes | < $1, < 1 minute |
| All prior work discarded | All prior work preserved |

The value scales with workflow cost. A $0.05, 30-second workflow does not need checkpointing. A $40, 20-minute workflow absolutely does.

---

## What gets checkpointed, what doesn't

**Checkpointed after completion:**
- Every successfully completed stage result
- Skipped stages — a stage that returned `{"_skipped": True}` is checkpointed so it stays skipped on resume
- Failed stages when `fail_as_value: true` — the `{"_failed": True, "_failed_reason": "..."}` value is written, so that stage is not re-run
- Fan-out stages — the entire collected result (after fan-in) is written as a unit once the whole fan-out completes

**Not checkpointed:**
- Stages that raised an exception and did not have `fail_as_value: true` — they re-run on resume
- `skip_if` and `condition` evaluations — these are always re-evaluated against live context, because skip conditions can depend on values that may have changed between runs

---

## ASCII timeline: run → crash → resume

```
First run (crashes at stage 3):

  stage 1 ──► [execute] ──► write checkpoint ──► done
  stage 2 ──► [execute] ──► write checkpoint ──► done
  stage 3 ──► [execute] ──► CRASH (timeout / rate limit / kill)
  stage 4     never reached
  stage 5     never reached

  checkpoint.json: {"stage_1": {...}, "stage_2": {...}}

─────────────────────────────────────────────────────────

Resume run (same session directory):

  stage 1 ──► [in checkpoint] ──► skip, inject result ──► done
  stage 2 ──► [in checkpoint] ──► skip, inject result ──► done
  stage 3 ──► [execute] ──► write checkpoint ──► done   ← picks up here
  stage 4 ──► [execute] ──► write checkpoint ──► done
  stage 5 ──► [execute] ──► write checkpoint ──► done
```

The resume run is identical to the first run from the spec's perspective. Context is rebuilt from checkpoint data before any stage executes, so stage 3 has full access to stage 1 and stage 2's results exactly as if they had just run.

---

## Atomic writes

`CheckpointStore` writes atomically:

1. Serialize the updated checkpoint dict to `checkpoint.json.tmp`
2. `rename()` to `checkpoint.json`

A crash mid-write leaves the `.tmp` file behind and the previous `checkpoint.json` intact. The next run loads the last complete checkpoint, not a corrupt partial write. This is the same fsync-and-rename pattern used by databases and log systems for crash safety.

---

## Session directory

The checkpoint file lives at `checkpoint.json` inside the session directory. The session directory is fixed at run start:

- **Default:** `~/.armature/runs/{run_id}/checkpoint.json` — a UUID-based path generated fresh each run
- **Explicit:** pass `session_dir` to the `Harness` constructor, or `session_dir` in the service API request body

The default path is **not resumable** because each new run generates a new UUID. For a workflow you intend to resume, set an explicit session directory before the first run and keep it stable:

```python
# Python API
harness = Harness(spec=spec, session_dir=Path("./my-run"))
result = await harness.run(inputs)
```

```json
// Service API (POST /run)
{
  "spec": "...",
  "session_dir": "/path/to/my-run"
}
```

The session directory also holds the session log (`session.jsonl`) and any artifacts the workflow produces. It is safe to inspect or archive after a run.

---

## Force-restart: ignoring the checkpoint

To discard the checkpoint and run all stages from scratch, pass `force=True`:

```bash
armature run workflow.yaml --force
```

```python
result = await harness.run(inputs, force=True)
```

`--force` calls `CheckpointStore.clear()`, which deletes `checkpoint.json`, then proceeds with a fresh run. This is the escape hatch when you want to re-run a completed workflow without creating a new session directory.

---

## Composing with fan-out

Fan-out stages are checkpointed **as a unit**. The entire collected result — the list of all per-item outputs after fan-in — is written to the checkpoint once the fan-out completes. Individual item results are not written incrementally.

This means: if a 100-item fan-out completes 87 items and crashes, those 87 results are not preserved. The whole fan-out re-runs on resume.

```
fan-out (100 items, fan_out: 10):

  items 1–10   ──► batch completes
  items 11–20  ──► batch completes
  items 21–30  ──► batch completes
  ...
  items 81–87  ──► completes
  item 88      ──► CRASH

  checkpoint.json: {} ← fan-out result not written yet
  On resume: entire 100-item fan-out re-runs from item 1
```

This is intentional. Partial fan-out results are harder to reason about — the fan-in strategy (list, merge, consensus) is designed to operate on a complete result set. Checkpointing 87 of 100 items and running the fan-in on a partial list would produce a different answer than running all 100.

**The right tool for long expensive fan-outs is `fail_as_value: true`, not partial checkpointing:**

```yaml
  - id: review_each
    fan_out: 10
    fail_as_value: true   # per-item failures return {"_failed": true} rather than crashing
    partition_source: "{{ documents }}"
    ...
```

With `fail_as_value: true`, a single item failure does not abort the batch. All 100 items complete — some with real results, some with `{"_failed": true, "_failed_reason": "..."}`. The fan-out stage result is written to the checkpoint as a unit. On a re-run with `--force` or a new session, you can filter failed items and re-process them explicitly.

For truly long fan-outs on expensive items (hundreds of API calls, multi-minute processing per item), structure the workflow so the fan-out is preceded by a stage that checkpoints the item list, and follow it with a stage that handles failures in the collected results. The fan-out itself re-runs as a unit if interrupted.

---

## Composing with `skip_if` and `condition`

Skip conditions are not cached. Every run evaluates `skip_if` and `condition` against live context, even if the stage result was loaded from checkpoint.

Wait — if the stage result is in the checkpoint, it's returned immediately before `skip_if` is evaluated. The comment in the engine:

```
# Return cached result from a prior run (checkpoint resume).
# Skip conditions are not checkpointed — they are re-evaluated each run.
```

means: if a stage is *not* in the checkpoint (i.e., it needs to run this time), its `skip_if`/`condition` are evaluated fresh. This matters for stages that were skipped on the first run. A skipped stage writes `{"_skipped": True}` to the checkpoint — so on resume, it is still reported as skipped without re-evaluating the condition. The checkpoint preserves the outcome, not the decision logic.

---

## A complete example: checkpointed compliance pipeline

```yaml
name: compliance-audit
checkpoint: true
version: "1.0"
mission: "Review all incoming documents for regulatory compliance issues."

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: list_documents
    role:
      name: Collector
      type: researcher
      model_tier: small
      description: |
        List all PDF paths in /incoming that arrived today.
        Return {"documents": ["/incoming/doc1.pdf", ...]}.

  - id: review_each
    fan_out: 10
    fan_in: list
    fail_as_value: true
    partition_source: "{{ list_documents.documents }}"
    partition_key: doc_path
    inject_file_as: doc_content
    role:
      name: Reviewer
      type: worker
      model_tier: small
      description: |
        Review the following document for compliance issues.
        Document: {{ doc_path }}
        Content: {{ doc_content }}
        Return:
          {"issues": [{"clause": "...", "severity": "low|medium|high"}],
           "risk_level": "low|medium|high",
           "requires_escalation": true|false}
    depends_on: [list_documents]

  - id: escalation_check
    skip_if: "{{ review_each | rejectattr('_failed') | selectattr('requires_escalation') | list | length == 0 }}"
    role:
      name: EscalationJudge
      type: judge
      model_tier: frontier
      description: |
        {{ review_each | rejectattr('_failed') | selectattr('requires_escalation') | list | length }}
        documents require escalation. Review and produce an escalation report.
        Flagged reviews: {{ review_each | rejectattr('_failed') | selectattr('requires_escalation') | list }}
    depends_on: [review_each]

  - id: final_report
    role:
      name: ReportWriter
      type: orchestrator
      model_tier: frontier
      description: |
        Produce a final compliance summary.
        Successful reviews: {{ review_each | rejectattr('_failed') | list | length }}
        Failed items: {{ review_each | selectattr('_failed') | list | length }}
        All reviews: {{ review_each | rejectattr('_failed') | list }}
        Return a structured compliance report.
    depends_on: [review_each]
```

Run it:

```bash
# First run — crashes at review_each (rate limit on item 47)
armature run compliance-audit.yaml

# Resume — list_documents is skipped, review_each re-runs from item 1
armature run compliance-audit.yaml

# Force fresh start — ignore checkpoint entirely
armature run compliance-audit.yaml --force
```

---

## When not to use checkpoint

Checkpoint adds a file write after every stage. For most workflows this overhead is negligible, but there are cases where it is unnecessary:

**Short, cheap workflows.** A three-stage workflow that costs $0.02 and runs in 8 seconds does not need fault tolerance. Restarting is cheaper than managing session directories.

**Idempotent workflows with external state.** If the workflow writes to a database or sends emails, checkpointing the stage result does not undo the external side effect. A resumed run skips the stage but the effect already happened. Design the workflow to handle this (check before acting, use idempotency keys) rather than relying on checkpoint to make it safe.

**Workflows run as one-off jobs in CI.** CI jobs have ephemeral filesystems. The session directory does not survive between pipeline runs, so checkpoint data is lost anyway. Use checkpoint when the workflow runs on a persistent host with a stable filesystem.

**Experimental or development runs.** When iterating on a spec, `--force` or a fresh session directory is usually correct. Resuming from a stale checkpoint during development adds confusion — you may not realize a stage was skipped from a previous version of the spec.

The signal is straightforward: if a workflow costs more to repeat than to checkpoint, turn it on. If it doesn't, leave it off.

---

*Checkpoint is not a distributed transaction system. It does not guarantee exactly-once execution of external side effects. It guarantees that completed stage results are not discarded on failure, and that resuming a run does not repeat successful LLM calls unnecessarily.*

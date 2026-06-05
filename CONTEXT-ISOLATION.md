# Context Isolation in Armature

Restrict a stage to exactly the context it declared — nothing more.

---

By default, every stage in an Armature workflow inherits the full accumulated context: every input injected at launch, every output produced by prior stages, the `mission`, the `run_id`, and (when continuation is enabled) `prior_run`. For small workflows where every stage genuinely needs global context, this is the right default.

For larger workflows, workflows that handle sensitive data, or fan-out stages where many workers run concurrently, that accumulated context becomes a liability. `isolated: true` cuts it down to exactly the keys the stage declared.

---

## Security — keeping sensitive data out of child workflows

The most important reason to use isolation is access control.

A common workflow pattern injects credentials at the top level — an API key, a database connection string, user PII — and then dispatches subagents to do the actual work. Without isolation, every subagent and fan-out worker inherits those credentials in full, even if they have no use for them. One compromised or misbehaving worker sees everything.

```yaml
inputs:
  api_key: "str"            # injected at launch
  user_credentials: "str"
  internal_config: "str"
  document_text: "str"

stages:
  - id: document_classifier
    subagent_spec: workflows/classifier.yaml
    isolated: true
    signature:
      input:
        document_text: "str"
        classification_labels: "list"
      output:
        category: "str"
        confidence: "float"
```

The classifier receives `document_text` and `classification_labels`. The `api_key`, `user_credentials`, and `internal_config` that exist in the parent context never enter the child workflow — the harness filters them out in `SubagentNode._resolve_child_context()` before dispatch.

```python
def _resolve_child_context(self, context: dict[str, Any]) -> dict[str, Any]:
    if not self._stage.isolated:
        return context          # full parent context passed through
    sig = self._stage.signature
    if sig is None or not sig.input:
        return {}               # no declared inputs → empty context
    return {k: context[k] for k in sig.input if k in context}
```

If no `signature.input` is declared on an isolated stage, the child receives an empty context — a useful default when you want total isolation with no data sharing.

---

## Clean interfaces — making contracts explicit

`signature:` is the agentic equivalent of a typed function signature. Declaring it forces the workflow author to state exactly what the stage needs and what it produces. Any reader of the spec — engineer, non-engineer, another LLM reviewing the workflow — immediately knows the contract.

```yaml
stages:
  - id: sentiment_scorer
    subagent_spec: workflows/sentiment.yaml
    isolated: true
    signature:
      input:
        review_text: "str"
        language: "str"
      output:
        sentiment: "str"      # "positive" | "negative" | "neutral"
        score: "float"        # 0.0–1.0
```

Without isolation, the child workflow might quietly depend on `prior_run` data or a context key set three stages earlier — a hidden coupling that only breaks when the parent workflow changes. With isolation and an explicit signature, that dependency would have to be declared or it does not exist.

---

## Determinism — controlling what the LLM sees

LLM behavior is influenced by context. A model given a 5,000-token context window containing a prior stage's failed attempt, a lengthy `mission:`, and accumulated intermediate outputs may respond differently than the same model given only the two keys it actually needs.

Isolating a stage to its declared inputs removes that variance. The stage sees the same context every time it runs regardless of how the parent workflow evolved, making outputs more predictable, easier to test, and easier to reproduce in isolation.

This matters most in workflows that are evaluated against a benchmark, or where the same subagent spec is reused across many different parent workflows.

---

## Fan-out with isolation

Isolation is especially valuable in fan-out stages, where the context hazard is multiplied by the number of concurrent workers. Without isolation, every worker receives a full copy of the accumulated context — including outputs from prior stages, intermediate data, and sensitive fields that the worker has no use for.

```yaml
stages:
  - id: review_each
    fan_out: 10
    partition_source: "{{ documents }}"
    partition_key: doc_path
    isolated: true
    signature:
      input:
        doc_path: "str"       # each worker sees only its assigned document path
    role:
      type: worker
      description: |
        Review {{ doc_path }} for compliance issues.
        Return {"issues": [...], "risk_level": "low|medium|high"}.
```

Each worker receives `doc_path` — its partition variable — and nothing else. The `partition_key` variable is always injected into the isolated context automatically; it does not need to appear in `signature.input` explicitly. Workers are truly independent: they share no context, carry no history of prior stages, and cannot observe each other's state.

---

## Decision guide

Use `isolated: true` when any of the following apply:

- **Sensitive data is in the context.** If the parent context contains credentials, PII, API keys, or internal configuration that the child has no business seeing, isolation is not optional — it is a security control.

- **The child has well-defined, bounded inputs.** If you can enumerate what the child needs, do so. A stage that can state its inputs explicitly should state them.

- **Workers are parallel and independent.** Fan-out stages running concurrently should each see only their own slice of work. Full-context fan-out is almost never intentional.

Leave `isolated: false` (the default) for stages that genuinely need the full accumulated context — typically orchestrator stages at the top of the DAG that reason over everything prior stages produced.

---

*Context isolation is the boundary between what a stage is allowed to know and what it is not. In any workflow that handles real data, drawing that boundary explicitly is good practice. In any workflow that handles sensitive data, it is mandatory.*

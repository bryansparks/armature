# Mission as Shared Context

One field at the top of the spec. Every agent sees it.

---

In a multi-stage Armature workflow, each stage has its own role and description — a researcher gathering data, a worker processing documents, a judge evaluating output, an orchestrator writing the final report. By default, each of those agents knows only what its own stage description tells it. It has no view of what the workflow is ultimately for.

The `mission:` field fixes this. One string at the spec level, automatically prepended to every agent's system prompt.

---

## The field

```yaml
name: due-diligence
mission: |
  Assess acquisition target ACME Corp for financial, legal, and operational risk.
  Output must be suitable for the board's investment committee by Friday.

stages:
  - id: gather_financials
    role:
      type: researcher
      ...

  - id: legal_review
    role:
      type: worker
      ...

  - id: risk_synthesis
    role:
      type: orchestrator
      ...
```

`mission: str = ""` is defined at the top level of `HarnessSpec`. It defaults to the empty string — leaving it out costs nothing, adding it changes every stage.

---

## The mechanism

Before every LLM stage call, the engine runs `_build_mission_block()`:

```python
def _build_mission_block(
    mission: str,
    context: dict,
    spec_stage_ids: set[str],
    max_preview_chars: int = 200,
) -> str:
    parts = []
    if mission:
        parts.append(f"[Workflow Mission]\n{mission.strip()}")
    prior = []
    for sid in spec_stage_ids:
        if sid in context:
            preview = json.dumps(context[sid], default=str)[:max_preview_chars]
            prior.append(f"• {sid} → {preview}")
    if prior:
        parts.append("[Prior stages]\n" + "\n".join(prior))
    return "\n\n".join(parts)
```

The result becomes the first thing in the system prompt — ahead of the role preamble, ahead of the stage description. Every LLM call in the workflow sees it.

Two things are produced simultaneously, which is worth understanding separately.

---

## Effect 1: Orientation

The mission string gives each agent a frame of reference that its local task description cannot provide.

**Without mission:**

```
[System prompt — researcher stage]

You are gathering and synthesizing information. Search broadly,
filter for credibility, and structure your findings.

## Your Role
Find all SEC filings for ACME Corp from the past three years.
Return {"filings": [...]}.
```

The researcher dutifully finds SEC filings. It returns raw data.

**With mission:**

```
[System prompt — researcher stage]

[Workflow Mission]
Assess acquisition target ACME Corp for financial, legal, and
operational risk. Output must be suitable for the board's
investment committee by Friday.

You are gathering and synthesizing information. Search broadly,
filter for credibility, and structure your findings.

## Your Role
Find all SEC filings for ACME Corp from the past three years.
Return {"filings": [...]}.
```

The researcher now knows this is acquisition due diligence destined for a board investment committee. It will prioritize material disclosures, flag unusual items that might concern board members, and structure its output at the right level of abstraction — not because the task description said to, but because the mission frames what "useful" means.

This is the core effect: **the mission shapes how the model interprets ambiguous instructions.** A stage description is necessarily brief. The mission fills in the interpretive context that makes brevity safe.

The same applies to the orchestrator writing the final report. It knows the output is board-suitable by Friday. It will use formal language, executive summary structure, and appropriate hedging — without any of that being spelled out in its stage description.

---

## Effect 2: Prior stages breadcrumb

The second section of the block — `[Prior stages]` — is equally important and is easy to overlook because it happens automatically.

For every stage in the spec that has already completed, the block includes a 200-character preview of its output:

```
[Prior stages]
• gather_financials → {"revenue_cagr": 0.12, "ebitda_margin": 0.18, "debt_ratio": 2.3, ...}
• legal_review → {"open_litigation": 2, "regulatory_flags": ["FCPA disclosure gap", "patent...}
```

This means:

- The analyst knows what the researcher found before it starts analyzing.
- The judge knows what the worker produced before it evaluates.
- The orchestrator has a memory trace of the entire pipeline before it writes the report.

Without this, each agent is blind to what came before. It sees only its own stage's inputs — whatever was explicitly threaded through `depends_on` references in the description. The prior stages breadcrumb provides passive awareness of the whole pipeline's state, regardless of whether the stage description references earlier outputs explicitly.

This is not a substitute for structured data passing — a downstream stage that needs specific fields from an upstream result should still reference them via Jinja2 expressions in its description. The breadcrumb is for orientation, not data retrieval. But it means agents can notice context they weren't explicitly handed, which produces more coherent outputs in practice.

---

## Why this produces coherent pipelines

In a multi-stage pipeline, agents can produce outputs that are locally correct but globally incoherent: different terminology for the same concept, incompatible risk scales, conflicting assumptions about the audience. Each stage optimized for its own task without awareness of the larger structure.

The shared mission reduces this by giving every agent the same frame of reference. The researcher, the analyst, the judge, and the orchestrator all share the same stated goal and the same understanding of what "done" looks like. The prior stages breadcrumb adds a shared memory of progress. Together, they create emergent coherence across the pipeline without requiring the workflow author to manually thread context through every stage.

---

## Compared to alternatives

**Repeating context in every stage description** works, but requires manually updating every stage when the goal changes. It drifts. The mission field is a single source of truth.

**No shared context** means each agent is isolated. Coherence depends entirely on well-structured data outputs and carefully written stage descriptions. This is fragile at scale — the larger the pipeline, the more likely stages drift apart.

**Shared memory systems** (RAG, vector stores, memory databases) are more powerful and appropriate when agents need to retrieve arbitrary prior information or persist state across workflow runs. For a single workflow run where the goal is stable and prior stage outputs are bounded in size, they are overkill. The mission field and prior stages breadcrumb cover the vast majority of production cases with zero infrastructure.

---

## The simplicity is the point

One YAML field. No middleware. No vector store. No embeddings. No explicit wiring between stages. The harness reads the mission string, builds the block before each LLM call, and prepends it to the system prompt.

This is the simplest possible implementation of shared agent context. It is not the most powerful — if your pipeline needs agents to retrieve specific facts from a large external corpus, you need RAG. But for the common case — a defined goal, a bounded set of stages, outputs that fit in a context window — it is sufficient, and its simplicity makes it easy to reason about, easy to debug, and impossible to misconfigure.

Write the mission once. Every agent in the workflow inherits it.

---

*`mission:` is a single string at the top of the spec. The harness takes care of the rest.*

---
name: potential-harness-improvement
description: MemoHarness paper analysis and its six harness dimensions mapped to explicit Armature spec levers for improving the harness itself.
metadata:
  node_type: memory
  type: project
  originSessionId: current
---

# POTENTIAL_HARNESS_IMPROVEMENT.md

**Source paper:** [MemoHarness: Learning Adaptive Agent Harnesses from Execution Experience](https://arxiv.org/html/2607.14159v1), arXiv:2607.14159v1.

**Purpose of this note:** distinguish the two kinds of "memory" in play and identify concrete Armature spec levers that could improve the *harness* (the scaffold around the workflow) without conflicting with the Memory Pyramid work that improves *agents inside the workflow*.

---

## 1. Two kinds of memory are orthogonal

| Layer | Owner | What it stores | Why it exists |
|---|---|---|---|
| **Agent memory** (Memory Pyramid) | Stages/agents inside a workflow | Domain facts, tracks, profiles, raw captures | Agents navigate it to make better decisions |
| **Harness memory** (MemoHarness) | The campaign/runner *around* workflows | Which spec configurations worked for which inputs | The harness adapts itself to each input |

The Memory Pyramid is the right answer for *agent-facing* memory. MemoHarness is the right answer for *harness-facing* memory. They should not be merged into one bank.

---

## 2. MemoHarness's six harness dimensions

The paper decomposes any agent harness into six editable surfaces. Below is how each surface maps to concrete Armature YAML fields and campaign levers.

### 2.1 Context — how input is assembled

**MemoHarness meaning:** pre-call input construction; prompt structure, demos, context compression.

**Armature primitives:** `mission`, per-stage `role.description`, `signature.input`, Jinja2 conditionals, `_memory` / `_knowledge` / `_memory_index` injection.

**Potential levers (explicit spec patches):**
- Compress the mission or stage description when input context grows.
- Add/remove few-shot examples via a static adapter or a prepended `examples` stage.
- Change `signature.input` to expose fewer upstream keys (context window control).
- Toggle between passive `_memory` injection and active `_memory_index` navigation.
- Rewrite the mission to emphasize a different constraint (brevity vs. exhaustiveness).

```yaml
# example lever: add a compression stage
stages:
  - id: compressor
    adapter:
      command: python -m armature.tools.compress_prompt {{ long_context }}
    depends_on: []
  - id: worker
    role: { ... }
    signature:
      input:
        compressed_context: Compressed context
```

### 2.2 Tool — external tool and retrieval use

**MemoHarness meaning:** when and how the harness calls tools or retrievers.

**Armature primitives:** `role.tools`, `tools:` module registrations, Memory Pyramid navigation tools (`memory.search_records`, `memory.read_track`, `memory.write_track`), `fan_out` + `partition_source`.

**Potential levers:**
- Enable/disable navigation tools (`navigation_tools: true/false`).
- Change retrieval depth (`top_k`) passed to search handlers.
- Add a rerank stage after tool results return.
- Replace a single tool call with a fan-out of multiple searches + synthesis.
- Swap tool providers (e.g., web search vs. memory search).

```yaml
# example lever: researcher gets active memory navigation
stages:
  - id: researcher
    role:
      type: researcher
      tools: [memory.search_records, memory.read_track]
      description: "Search memory; return top 5 relevant records."
```

### 2.3 Generation — decoding configuration

**MemoHarness meaning:** sampling parameters, max tokens, candidate sampling.

**Armature primitives:** `model_tiers`, per-stage `role.model_tier`, `temperature`, `max_tokens`, `output_mode`.

**Potential levers:**
- Swap the tier for a stage (e.g., judge from `large` to a stricter custom tier).
- Raise/lower `temperature` per stage.
- Raise/lower `max_tokens` to control verbosity/latency.
- Switch `output_mode` between `text` and `guided_json` based on whether structure is load-bearing.

```yaml
# example lever: a stricter judge tier
model_tiers:
  judge_rigorous:
    provider: openrouter
    model: moonshotai/kimi-k2.6
    temperature: 0.0
    max_tokens: 4096

stages:
  - id: judge
    role:
      type: judge
      model_tier: judge_rigorous
```

### 2.4 Orchestration — workflow topology

**MemoHarness meaning:** sequence of model calls and intermediate reasoning steps.

**Armature primitives:** `stages` + `depends_on`, `fan_out` / `fan_in`, `subagent_spec`, `gate: human`.

**Potential levers:**
- Replace one monolithic stage with `planner → executor → critic`.
- Insert a critic/judge stage before final output.
- Change `fan_out` breadth for research queries.
- Move a complex sub-task into a `subagent_spec`.
- Add or remove human gates.

```yaml
# example lever: add a critic after workers
stages:
  - id: workers
    fan_out: 10
    partition_source: "{{ planner.queries }}"
    role: { type: worker, ... }
  - id: critic
    role:
      type: judge
      description: "Identify gaps in the worker outputs."
    depends_on: [workers]
```

### 2.5 Memory — cross-call state persistence

**MemoHarness meaning:** what state persists across calls and what stale context is removed.

**Armature primitives:** `memory.enabled`, `memory.fresh`, `memory.inject_as`, `memory.capture`, `MemoryConfig.navigation_tools`, `MemoryConfig.curator_stage`, `track_budget`, `profile_budget`.

**Potential levers:**
- Toggle `fresh: true/false` (cold vs. warm runs — already used in H4).
- Change `max_entries` to control how much prior context is injected.
- Capture different stage outputs (e.g., also capture judge feedback).
- Toggle `navigation_tools` and the curator stage.
- Shrink/expand `track_budget` and `profile_budget`.
- Adjust supersession / reconciliation thresholds in `KnowledgeStore`.

```yaml
# example lever: tighter capture + active navigation
memory:
  enabled: true
  fresh: false
  inject_as: _memory
  capture:
    - stage: researcher
      key: content
      max_entries: 5
  navigation_tools: true
  curator_stage: curator
```

### 2.6 Output — post-call handling

**MemoHarness meaning:** extraction, validation, fallback.

**Armature primitives:** `output_schema`, `on_fail.loop`, `adapter`, `tool_call`.

**Potential levers:**
- Tighten/loosen `output_schema` (e.g., `additionalProperties: false`).
- Change `on_fail.loop.max` from 0 to 3.
- Add an extraction adapter after a verbose LLM stage.
- Add a deterministic fallback adapter when parsing fails.

```yaml
# example lever: extract structured answer from freeform text
stages:
  - id: raw
    role: { type: worker, output_mode: text, ... }
  - id: formatter
    adapter:
      command: python -m armature.tools.extract_json {{ raw.content }}
    depends_on: [raw]
```

---

## 3. The MemoHarness experience bank as a separate component

The paper stores:

- **Per-case entries:** `(case_id, iteration, features, harness_used, harness_delta, trajectory, reward, cost, diagnosis)`
- **Global patterns:** distilled cross-case regularities with recommended edits.

For Armature, this would be a *separate* bank from the Memory Pyramid. It stores meta-learning data, not domain knowledge.

### Proposed schema sketch

```yaml
harness_experience:
  entries:
    - case_id: <hash of input>
      workflow: campaign-research-brief-memory
      inputs:
        topic: "the core design problems of distributed systems"
      harness_delta:
        # what changed from the previous run for this case
        - dimension: generation
          field: model_tiers.large.model
          old: z-ai/glm-5.2
          new: moonshotai/kimi-k2.6
      reward: 0.92
      cost_tokens: 12345
      diagnosis:
        success: true
        primary_dimension: null
        secondary_dimensions: []
        analysis: "Judge tier swap fixed self-contradictory scores."
  patterns:
    - id: p1
      dimension: generation
      phenomenon: "glm-5.2 emits accept=True, confidence=0 on broad-topic judges"
      evidence_count: 5
      recommended_edit:
        field: model_tiers.large.model
        value: moonshotai/kimi-k2.6
```

### Why this should stay separate

- The Memory Pyramid's provenance unit is `(run_id, stage_id, capture_key)` — it answers "where did this fact come from?"
- The harness experience bank's provenance unit is `(case_id, harness_delta, reward)` — it answers "what spec change worked?"
- Mixing them would pollute both.

---

## 4. How this improves the campaign runner

Current `self_improve` in the campaign runner is narrow: it corrupts the spec or degrades the tier, then recovers. A MemoHarness-style diagnostic would generalize this.

### Richer gap vocabulary

Instead of logging only "HQS low" or "model_failed", the runner could diagnose failures into the six dimensions:

| Observed failure | Primary dimension | Candidate lever |
|---|---|---|
| Judge emits `accept=True, confidence=0` | generation / context | swap judge model; tighten prompt |
| Researcher produces empty briefing | generation | lower temperature; add output adapter |
| Warm run latency cancels coverage gain | memory / context | reduce `max_entries`; add compression stage |
| Briefing misses distinct sub-problems | context | rewrite mission; add sub-problem listing requirement |
| Fan-out synthesis is incoherent | orchestration | add critic stage; reduce fan_out |
| Output JSON parse fails | output | tighten schema; add extraction adapter |

### Search over spec patches

A campaign could declare a harness search space:

```yaml
harness_search:
  dimensions: [context, generation, memory]
  candidates:
    - name: strict_judge
      patch:
        - path: model_tiers.large.model
          value: moonshotai/kimi-k2.6
    - name: compress_memory
      patch:
        - path: memory.capture.0.max_entries
          value: 5
    - name: sub_problem_prompt
      patch:
        - path: stages.judge.role.description
          value: "..."
```

The runner tries each candidate on a held-out subset, banks the reward/cost, and selects the best. Every candidate produces an explicit Armature YAML diff — fully auditable and replayable.

---

## 5. What NOT to do

- **Do not let a controller mutate Armature specs opaquely.** Armature's value is explicit, validatable DAGs. Any harness adaptation must produce a concrete spec diff.
- **Do not replace the Memory Pyramid with the experience bank.** They serve different consumers (agents vs. harness) and have different provenance models.
- **Do not treat MemoHarness's shallow Memory dimension as a substitute for L0–L3.** Their Memory dimension is a policy knob; your pyramid is an ontology.

---

## 6. Next steps (optional)

1. Prototype a `harness_experience` table alongside the Memory Pyramid stores.
2. Add a `diagnosis` field to campaign rows with one of the six MemoHarness dimensions.
3. Design a small `harness_search` block for campaign plans so the runner can try candidate spec patches on a subset.
4. Validate that every candidate patch still passes `armature validate` before it is run.

---

**Related:** [[project-memory-pyramid-design]]

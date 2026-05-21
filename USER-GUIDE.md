# Armature User Guide

This guide covers everything you need to build agentic workflows with Armature: installation, spec structure, stage types, role configuration, context flow, templates, self-improvement, and advanced patterns.

---

## Table of Contents

0. [Installation & quickstart](#0-installation--quickstart)
1. [Core concepts](#1-core-concepts)
2. [Spec structure](#2-spec-structure)
3. [Model tiers](#3-model-tiers)
   - [Tier configuration fields](#tier-configuration-fields)
   - [Role type defaults](#role-type-defaults)
4. [Stage types](#4-stage-types)
   - [LLM stage](#41-llm-stage)
   - [Script/adapter stage](#42-scriptadapter-stage)
   - [Human gate](#43-human-gate)
   - [Direct tool call stage](#44-direct-tool-call-stage)
   - [Subagent stage](#45-subagent-stage)
   - [Conditional execution (skip_if)](#46-conditional-stage-execution-skip_if)
5. [Role types](#5-role-types)
6. [Output modes](#6-output-modes)
7. [Context and data flow](#7-context-and-data-flow)
   - [Context filtering (signature.input)](#context-filtering-signatureinput)
8. [Cross-run memory](#8-cross-run-memory)
9. [Jinja2 variables in specs](#9-jinja2-variables-in-specs)
10. [Retry and recovery](#10-retry-and-recovery)
11. [Safety rules](#11-safety-rules)
12. [Lifecycle hooks](#12-lifecycle-hooks)
    - [Hook phases](#hook-phases)
    - [Registering hooks](#registering-hooks)
    - [Hook decisions](#hook-decisions)
    - [Permission levels](#permission-levels)
    - [What guardrails cover — and what they don't](#what-armature-guardrails-cover--and-what-they-dont)
13. [Fan-out / Fan-in](#13-fan-out--fan-in)
14. [Human gates in detail](#14-human-gates-in-detail)
15. [Deliberative teams](#15-deliberative-teams)
    - [The three-round topology](#the-three-round-topology)
    - [Specifying the objective](#specifying-the-objective)
    - [The judge role type](#the-type-judge-role)
    - [Reference implementations](#reference-implementations)
16. [Templates](#16-templates)
17. [The optimizer](#17-the-optimizer)
18. [Running workflows](#18-running-workflows)
    - [CLI reference](#cli-reference)
19. [Integrating with a host application](#19-integrating-with-a-host-application)
    - [Declaring tool modules in the spec](#declaring-tool-modules-in-the-spec)
    - [Pattern A — embedded library](#pattern-a--embedded-library)
    - [Pattern B — HTTP sidecar](#pattern-b--http-sidecar)
    - [Pattern C — subprocess](#pattern-c--subprocess)
    - [Input shaping](#input-shaping)
    - [Output extraction](#output-extraction)
    - [Async lifecycle](#async-lifecycle)
    - [Error contracts](#error-contracts)
20. [Self-improvement](#20-self-improvement)
21. [Trace export for fine-tuning](#21-trace-export-for-fine-tuning)

---

## 0. Installation & quickstart

### Install

```bash
pip install armature
```

Armature requires Python 3.11+. The core install includes the CLI, runtime engine, spec loader, and all built-in tools.

**Optional extras:**

| Extra | Adds |
|-------|------|
| `pip install 'armature[service]'` | FastAPI HTTP service (`armature serve`) |
| `pip install 'armature[wizard]'` | Interactive spec wizard (`armature new`) |
| `pip install 'armature[telemetry]'` | OpenTelemetry tracing export |

**Install from source:**

```bash
git clone <repo>
cd armature
pip install -e .
```

### Environment variables

Armature uses [litellm](https://github.com/BerriAI/litellm) for LLM calls. Set the API key for the provider(s) you use:

```bash
export ANTHROPIC_API_KEY=sk-ant-...    # for Anthropic (Claude) models
export OPENAI_API_KEY=sk-...           # for OpenAI models
export OPENROUTER_API_KEY=sk-or-...    # for OpenRouter (multi-provider routing)
```

Ollama and other local providers do not require an API key — set `api_base` in the tier config instead.

### Quickstart: your first workflow

**1. Write a spec (`my_workflow.yml`):**

```yaml
name: research-summarizer
version: "1.0"

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  large:
    provider: anthropic
    model: claude-sonnet-4-6

stages:
  - id: researcher
    role:
      name: Researcher
      type: researcher
      model_tier: large
      description: |
        Research the following topic and produce a structured summary
        covering key facts, open questions, and practical implications.
        Topic: {{ topic }}
    output_mode: text
    depends_on: []

  - id: editor
    role:
      name: Editor
      type: worker
      model_tier: small
      description: |
        Tighten the researcher's draft into a crisp 3-paragraph summary.
        Eliminate repetition. Preserve all concrete facts.
    output_mode: text
    depends_on: [researcher]
```

**2. Run it:**

```bash
armature run my_workflow.yml --input topic="quantum error correction"
```

**3. Or from Python:**

```python
import asyncio
from armature import Harness

harness = Harness.from_spec("my_workflow.yml")
result = asyncio.run(harness.run({"topic": "quantum error correction"}))
print(result["editor"]["content"])
```

That's it. The engine resolves the DAG, calls the models in order, threads context through, and returns a dict of all stage outputs.

---

## 1. Core concepts

### The DAG

A workflow is a **directed acyclic graph (DAG)** of stages. Each stage declares which stages it `depends_on`. The engine resolves execution order automatically using topological sort — you never specify an order explicitly.

```
researcher ──────┐
                 ▼
strategist ──► synthesizer ──► human_gate
```

### Context

There is one shared **context dict** per run. It starts with the inputs you pass to `harness.run()`. As each stage completes, its result is stored in the context under the stage's `id`. Every downstream stage sees the full accumulated context — including all upstream stage outputs — in its system prompt.

This means you do not need to wire data manually between stages. A stage that `depends_on: [researcher]` will automatically have `researcher`'s output available in its context.

### Stages vs. roles

A **stage** is a node in the graph — it has an id, dependencies, and configuration.

A **role** is the LLM identity within a stage — it has a name, type, description (system prompt body), and model tier. Not every stage has a role; script stages and gates do not.

---

## 2. Spec structure

A complete spec file at a glance:

```yaml
name: my_workflow
version: "1.0"
description: "Optional description"

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-sonnet-4-6

# role_type_defaults lets stages omit model_tier — type drives tier assignment
role_type_defaults:
  worker: small
  researcher: small
  judge: frontier
  orchestrator: frontier

adapters:
  run_script:
    name: run_script
    type: script
    cmd: "python scripts/process.py"

safety_rules:
  - tool: run_script
    condition:
      field: cmd
      op: contains
      value: "rm -rf"
    action: block
    message: "Destructive commands are not permitted."

stages:
  - id: researcher
    role:
      name: Researcher
      type: researcher        # picks up "small" from role_type_defaults
      description: |
        Gather relevant information about: {{ topic }}
    output_mode: text
    depends_on: []

  - id: writer
    role:
      name: Writer
      type: worker            # picks up "small" from role_type_defaults
      description: |
        Write a clear summary based on the researcher's findings.
    output_mode: text
    depends_on: [researcher]

  - id: review_gate
    gate: human
    present: "Review the writer's output before continuing.\n\n{{ writer.content }}"
    depends_on: [writer]
```

### Top-level fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Workflow name (used in traces and logs) |
| `version` | string | no | Spec version string |
| `description` | string | no | Human-readable description |
| `model_tiers` | object | yes | Named model configurations (see §3) |
| `role_type_defaults` | object | no | Default tier per role type — workers, judges, etc. (see §3) |
| `contracts` | object | no | Input/output declarations and run-level limits (see below) |
| `adapters` | object | no | Script/command adapters (see §4.2) |
| `safety_rules` | list | no | Declarative tool safety rules (see §11) |
| `memory` | object | no | Cross-run memory capture and injection (see §8) |
| `tools` | list | no | External tool modules to load into the registry (see §17) |
| `checkpoint` | bool | no | Persist completed stage results to disk so a run can resume after a crash (default: false) |
| `stages` | list | yes | The workflow stages (see §4) |

### Contracts

`contracts` declares the workflow's inputs and run-level resource limits:

```yaml
contracts:
  inputs:
    - name: topic
      type: string
      description: "The research topic"
    - name: max_words
      type: integer
      description: "Target word count for the summary"
  max_iterations: 20      # total LLM dispatch loop iterations across the run
  max_llm_calls: 100      # hard cap on LLM API calls
  timeout_hours: 4.0      # wall-clock timeout for the entire run
  output_max_chars: 8000  # truncate each stage's stored output to this length
```

| Field | Default | Description |
|-------|---------|-------------|
| `inputs` | `[]` | Named inputs the workflow expects — documents the public API |
| `outputs` | `[]` | Named outputs the workflow produces — documents the result shape |
| `max_iterations` | `20` | Maximum total dispatch loop iterations |
| `max_llm_calls` | `100` | Maximum LLM API calls before the run aborts |
| `timeout_hours` | `8.0` | Wall-clock run timeout in hours |
| `output_max_chars` | `null` | Per-stage output truncation limit; individual stages can override with their own `output_max_chars` |

### Checkpoint and resume

Set `checkpoint: true` to persist each completed stage's result to disk. If a run fails mid-way, re-running the same workflow with the same run ID picks up from the last completed stage rather than restarting from scratch:

```yaml
name: long-running-pipeline
checkpoint: true

stages:
  - id: slow_researcher
    # ...
  - id: expensive_synthesizer
    depends_on: [slow_researcher]
    # ...
```

```bash
# If this run crashes after slow_researcher completes...
armature run pipeline.yml --input topic=X

# ...re-run with --force to override checkpoint and restart from scratch:
armature run pipeline.yml --input topic=X --force
```

Without `--force`, a re-run skips stages that already have a persisted result and emits `stage_resumed` events for them.

---

## 3. Model tiers

Model tiers decouple your workflow logic from specific model choices. Stages reference a tier name; the tier maps to a provider and model. This lets you swap models globally without editing every stage.

The tier names (`tiny`, `small`, `medium`, `large`, `frontier`) are fixed schema — you define what each one means for your application. You do not need to define all five — only the ones your workflow uses.

```yaml
model_tiers:
  tiny:
    provider: ollama
    model: qwen2.5:7b
    api_base: http://localhost:11434
    temperature: 0.3
    max_tokens: 1024

  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001

  medium:
    provider: openai
    model: gpt-4o-mini

  large:
    provider: anthropic
    model: claude-sonnet-4-6

  frontier:
    provider: openrouter
    model: anthropic/claude-opus-4-7
    api_key_env: OPENROUTER_API_KEY
```

### Tier configuration fields

| Field | Type | Description |
|---|---|---|
| `provider` | string | `anthropic`, `openai`, `openrouter`, `ollama` — any litellm-supported provider |
| `model` | string | Model identifier as accepted by the provider |
| `api_base` | string | Endpoint URL — required for Ollama and self-hosted models |
| `api_key_env` | string | Name of the env var holding this tier's API key (e.g. `OPENROUTER_API_KEY`). Overrides the provider default. |
| `temperature` | float | Default sampling temperature for this tier. Can be overridden at the role level. |
| `max_tokens` | int | Default max output tokens for this tier. Can be overridden at the role level. |
| `tool_calling` | bool | `true` to force native tool injection; `false` to disable it; omit to auto-detect by provider. |

**Providers:** Any provider supported by [litellm](https://github.com/BerriAI/litellm) works — Anthropic, OpenAI, OpenRouter, Ollama, Azure, Bedrock, and more.

**Credentials:** API keys are read from environment variables. For most providers litellm finds the key automatically (e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). Use `api_key_env` to name a different variable for a specific tier.

**Escalation:** If a stage's assigned tier produces an unparseable JSON response (when `output_mode: guided_json`), the engine automatically escalates to the next defined tier and retries. Only tiers you have actually configured participate — the escalation order is always `tiny → small → medium → large → frontier`.

**Provider-aware structured output:** Providers that support native structured output (OpenAI, Anthropic) receive a `response_format` kwarg enforcing the output schema. Providers that do not (Ollama) fall back to prompt-guided JSON with automatic extraction. This is re-evaluated per escalation tier, so switching providers mid-escalation uses the right strategy automatically.

**Per-tier tool calling override:** By default the engine injects native tool specs for OpenAI/Anthropic providers and uses prompt-based tool descriptions for Ollama. Set `tool_calling: true` on any Ollama tier running a model that supports tool calling (e.g. Llama 3.1+, Qwen 2.5) to enable native dispatch. Set `tool_calling: false` to disable it for any provider.

### Role type defaults

Instead of setting `model_tier` on every role individually, use `role_type_defaults` to establish a mapping at the spec level:

```yaml
role_type_defaults:
  worker: small        # cost-optimized task executors
  orchestrator: frontier
  judge: frontier      # needs the best reasoning for adjudication
  researcher: large
```

Built-in defaults (applied if you omit this section):

| Role type | Default tier |
|---|---|
| `worker` | `small` |
| `orchestrator` | `frontier` |
| `judge` | `frontier` |
| `researcher` | `large` |

A role that sets `model_tier` explicitly always overrides the type default.

---

## 4. Stage types

### 4.1 LLM stage

An LLM stage calls a language model. It requires a `role`.

```yaml
- id: analyst
  role:
    name: Analyst
    type: researcher
    model_tier: small
    description: |
      Analyze the provided data and identify the top 3 trends.
      Be specific and cite evidence from the context.
  output_mode: guided_json
  output_schema:
    type: object
    required: [trends]
    properties:
      trends:
        type: array
        items:
          type: object
          properties:
            title:
              type: string
            evidence:
              type: string
  depends_on: []
```

**Role fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Display name for this agent |
| `type` | string | yes | Role type — see §5 |
| `description` | string | yes | The body of the system prompt |
| `model_tier` | string | no | Which tier to use (`small`, `frontier`, etc.). Omit to inherit from `role_type_defaults`. |
| `temperature` | float | no | Sampling temperature — overrides the tier-level default |
| `max_tokens` | int | no | Max output tokens — overrides the tier-level default |
| `tools` | list | no | Tool names this stage may call — filters the registry to only these names; empty means no tool access |
| `skills` | list | no | Skill names |

**Tool calling**

When a stage declares `tools`, the LLM can invoke those tools natively during its response. The harness runs a ReAct-style dispatch loop: if the model returns tool calls, each is executed via the tool registry, results are fed back, and the model is called again — repeating until the model gives a final text/JSON response or the iteration limit (default 10) is reached.

```yaml
- id: researcher
  role:
    name: Researcher
    type: researcher
    description: |
      Research the given topic. Use the search tool to find relevant information
      before writing your summary.
    tools: [search, http_get]   # only these tools are visible and callable
  output_mode: text
  depends_on: []
```

Rules:
- Only tool names listed in `role.tools` are exposed — other registered tools are invisible to this stage
- Tool names not present in the registry are silently skipped (a typo won't crash the run)
- Providers in `_NO_STRUCTURED_OUTPUT` (Ollama) do not receive native tool specs in the API call; they still see a `## Available Tools` section in the system prompt but cannot make structured tool calls
- The dispatch loop resets its iteration counter on tier escalation

**Stage fields:**

| Field | Type | Description |
|---|---|---|
| `output_mode` | string | `text`, `json`, or `guided_json` (see §6) |
| `output_schema` | object | JSON Schema — required for `guided_json` |
| `depends_on` | list | Stage IDs this stage waits for |
| `on_fail` | object | Retry configuration (see §10) |
| `skip_if` | string | Jinja2 expression — skip this stage when it renders truthy (see §4.6) |
| `timeout_s` | float | Wall-clock timeout for the whole stage including retries. Raises on expiry. |
| `fail_as_value` | bool | When `true`, stage failure returns `{"_failed": true, "error": "..."}` instead of raising. Downstream stages can check `{{ stage_id._failed }}`. |
| `evaluate` | list[string] | Declarative quality criteria assessed post-run (e.g. `["response is factual", "no hallucinated citations"]`). Results appear in the run report. |
| `post_run` | bool | When `true`, this stage runs **after** all normal stages complete, with the full run transcript and diagnostics injected into its context. Used for self-analysis and improvement suggestions. |
| `output_max_chars` | int | Per-stage output truncation limit — overrides the spec-level `contracts.output_max_chars`. |

**`fail_as_value` example:**

```yaml
- id: optional_enrichment
  role:
    name: Enricher
    type: researcher
    description: "Try to fetch supplementary context. If unavailable, return empty."
  fail_as_value: true
  depends_on: []

- id: synthesizer
  role:
    name: Synthesizer
    type: judge
    description: |
      Synthesize findings.
      {% if optional_enrichment._failed %}
      (Note: supplementary context was unavailable — proceed without it.)
      {% endif %}
  depends_on: [optional_enrichment]
```

**`post_run` example:**

```yaml
stages:
  - id: researcher
    role:
      name: Researcher
      type: researcher
      description: "Research the topic."
    depends_on: []

  - id: synthesizer
    role:
      name: Synthesizer
      type: judge
      description: "Synthesize research into a recommendation."
    depends_on: [researcher]

  - id: self_analyst
    post_run: true
    role:
      name: Self-Analyst
      type: judge
      description: |
        Review the completed run transcript and diagnostics.
        Identify which stages had weak output quality and suggest spec improvements
        for the next run. Output: {"issues": [...], "suggestions": [...]}.
    output_mode: guided_json
    output_schema:
      type: object
      required: [issues, suggestions]
      properties:
        issues:
          type: array
          items: {type: string}
        suggestions:
          type: array
          items: {type: string}
    depends_on: []
```

Post-run stages see `_transcript` (full conversation log) and `_diagnostics` (failure signatures from the run) in their context.

---

### 4.2 Script/adapter stage

A script stage runs a Python function or shell command. It requires an `adapter` defined at the top level and referenced by name.

```yaml
adapters:
  fetch_data:
    name: fetch_data
    type: script
    cmd: "python scripts/fetch.py --source {{ source_url }}"

stages:
  - id: fetch
    adapter: fetch_data
    depends_on: []
```

**Adapter fields:**

| Field | Type | Description |
|---|---|---|
| `name` | string | Adapter identifier |
| `type` | string | `script` or `python` |
| `cmd` | string | Shell command to run |
| `fn` | string | Python callable path (for `type: python`) |
| `args` | object | Static arguments merged into context |

Script stage output is passed downstream as a dict under the stage's id.

---

### 4.3 Human gate

A human gate pauses execution for human review. The run blocks until the operator responds.

```yaml
- id: approval
  gate: human
  present: |
    Please review the proposed plan before we proceed.

    PLAN:
    {{ planner.content }}

    Approve to continue, or provide feedback to revise.
  depends_on: [planner]
```

The `present` field supports Jinja2 template syntax — you can embed upstream stage outputs directly in the approval prompt using `{{ stage_id.field }}`.

**Output:** `{"approved": true, "feedback": null}` or `{"approved": false, "feedback": "the operator's text"}`. Downstream stages can branch on `approval.approved`.

---

### 4.4 Direct tool call stage

A `tool_call` stage invokes a registered tool directly — no LLM involved. This is ideal for deterministic steps where you know exactly which tool to call and with what arguments.

```yaml
- id: scan
  tool_call:
    name: my_scanner          # registered tool name
    args:
      dir: "{{ workspace }}"  # Jinja2-rendered against context
      timeout: 30             # non-string values pass through unchanged
  depends_on: []
```

The tool's return value is stored in context under the stage id, exactly like an LLM stage result. Downstream stages can reference it with `{{ scan.some_field }}`.

**When to use over an LLM stage:**
- You already know which tool to call (no reasoning needed)
- The step is deterministic and must not hallucinate arguments
- You want to avoid an LLM call and its latency/cost

The tool must be registered with the harness — either via the `tools:` spec section (see §3) or by calling `harness._registry.register(...)` after construction.

---

### 4.5 Subagent stage

A subagent stage spawns a child workflow (another spec file) as a nested execution. The child receives the current context as its inputs.

```yaml
- id: deep_analysis
  subagent_spec: workflows/deep_analysis.yml
  depends_on: [initial_scan]
```

The child spec is itself a full `HarnessSpec` — it can have its own model tiers, stages, and safety rules. Its final context dict is returned as the subagent stage's result.

**Variables:** The child spec is loaded with the current context as Jinja2 variables, so any `{{ variable }}` in the child spec resolves from the parent's context.

#### Fan-out / Fan-in

Subagent stages support parallel fan-out — spawn N copies of the child workflow simultaneously and merge the results.

```yaml
- id: parallel_reviews
  subagent_spec: workflows/reviewer.yml
  fan_out: 4                 # spawn 4 parallel child runs
  fan_in: list               # collect results as a list
  partition_key: documents   # split context["documents"] evenly across the 4 children
  depends_on: [loader]
```

See §13 for full fan-out/fan-in documentation.

---

### 4.6 Conditional stage execution (`skip_if`)

Any stage type can declare `skip_if` — a Jinja2 expression evaluated against the current context before the stage runs. If the expression renders truthy, the stage is bypassed entirely and returns `{"_skipped": True}`.

```yaml
- id: escalate
  role:
    name: Escalator
    type: orchestrator
    description: "Handle edge cases that the evaluator flagged."
    model_tier: frontier
  skip_if: "{{ evaluator.quality_score >= 0.9 }}"
  depends_on: [evaluator]
```

**Expression rules:**
- The expression is a full Jinja2 template; any Python expression Jinja2 supports is valid
- Truthy result: the rendered string equals `true`, `1`, or `yes` (case-insensitive) — the stage is skipped
- All other outputs (including undefined variables, which render as empty string) are falsy — the stage runs normally
- Missing or undefined variables never cause a skip (they evaluate to empty string)

**What gets skipped:**
- The stage execution (no LLM call, no tool dispatch, no script run)
- The retry loop (`on_fail.loop`) — skipped stages do not retry
- Session events `stage_start` and `stage_complete` (a `stage_skipped` event is emitted instead)

**Downstream access:** The skipped stage's context entry is `{"_skipped": True}`. Downstream stages that reference `{{ skipped_stage.some_field }}` receive an empty string (ChainableUndefined). Check `{{ skipped_stage._skipped }}` explicitly if downstream logic needs to branch on whether the stage ran.

**Common patterns:**

```yaml
# Skip expensive analysis when a judge already gave a high score
- id: deep_analysis
  skip_if: "{{ judge.confidence >= 0.95 }}"

# Skip a retry stage when the first attempt succeeded
- id: retry_fetch
  skip_if: "{{ not fetch._skipped and fetch.exit_code == 0 }}"

# Skip human review in automated runs
- id: approval_gate
  gate: human
  skip_if: "{{ auto_mode }}"   # auto_mode passed as input to harness.run()
```

---

## 5. Role types

Role type affects the preamble prepended to the system prompt by the engine. It also communicates intent to anyone reading the spec.

| Type | Preamble | Use for |
|---|---|---|
| `worker` | "You are a focused task executor. Produce structured output that matches the required schema exactly." | Executing a bounded, well-defined task |
| `orchestrator` | "You are coordinating a multi-step workflow. Plan carefully, delegate to appropriate tools, and track progress." | Planning and routing across sub-tasks |
| `judge` | "You are evaluating output quality. Assess carefully, score objectively, and identify specific issues." | Critique, scoring, synthesis, adjudication |
| `researcher` | "You are gathering and synthesizing information. Search broadly, filter for credibility, and structure your findings." | Information gathering, analysis, perspective-taking |

The preamble is prepended to the role's `description`. Write `description` as if the preamble is already there — you do not need to repeat it.

---

## 6. Output modes

| Mode | Description | When to use |
|---|---|---|
| `text` | Raw text response stored under `{"content": "..."}` | Freeform responses, intermediate analysis |
| `json` | Model asked to return valid JSON; parsed automatically | Structured output without a strict schema |
| `guided_json` | Model asked to return JSON matching `output_schema`; strictly validated | When downstream stages depend on specific fields |

For `guided_json`, provide an `output_schema` as a JSON Schema object. Providers that support structured output (OpenAI, Anthropic) will use native schema enforcement. Ollama and other providers fall back to prompt-guided JSON with automatic extraction.

If a `guided_json` response fails to parse, the engine escalates to the next model tier automatically.

---

## 7. Context and data flow

The context dict is the single shared data bus for a run. Understanding how it flows is the key to writing effective specs.

### Initial context

```python
result = await harness.run({
    "topic": "renewable energy",
    "documents": "...",
    "user_id": "abc123",
})
```

All keys you pass become immediately available in every stage's context.

### Stage output accumulation

When a stage completes, its result is stored as `context[stage_id]`. For LLM stages:

- `text` mode: `context["my_stage"] = {"content": "the model's response"}`
- `json` / `guided_json`: `context["my_stage"] = {"field1": ..., "field2": ...}`

For subagent stages, the child's full result dict is stored. For `tool_call` stages, the tool's return value is stored directly.

### How a stage sees context

The `PromptAssembler` builds each stage's system prompt as:

```
[role type preamble]

## Your Role
[role.description]

## Current Context
- run_id: abc123
- topic: renewable energy
- researcher: {"findings": [...], "gaps": [...]}
- strategist: {"opportunities": [...]}
```

Every stage sees everything accumulated before it. You do not need to explicitly pass data between stages — `depends_on` ensures ordering, and the context carries the content.

### Referencing upstream output in `present` (human gates)

Use Jinja2 syntax in the `present` field:

```yaml
present: "Review this plan:\n\n{{ planner.content }}"
```

### Context filtering (signature.input)

By default every stage sees the entire accumulated context. For complex workflows this can include dozens of keys — some irrelevant to a specific stage, some potentially leaking internal details.

Add a `signature.input` block to a stage to restrict which context keys appear in that stage's system prompt:

```yaml
- id: analyst
  role:
    name: Analyst
    type: worker
    description: |
      Analyze the research findings and produce a structured assessment.
  signature:
    input:
      topic: The research topic
      research: The researcher's findings
  depends_on: [research]
```

When `signature.input` is non-empty, the `PromptAssembler` strips every key not listed before building the `## Current Context` section. The values in the dict are descriptions — they document intent, not constraints.

Benefits:

- **Focused prompts:** The model only sees what it needs, reducing noise and token cost.
- **Information hiding:** Internal keys (`run_id`, raw fetch output, temporary state) are not exposed.
- **Self-documenting:** The input signature documents exactly what a stage depends on beyond `depends_on`.

When `signature.input` is empty or the `signature` block is absent, all context keys are passed through (backward compatible default).

---

## 8. Cross-run memory

Cross-run memory lets stages accumulate knowledge across multiple workflow runs. Unlike context (which resets every run), memory persists to SQLite and is injected at the start of each new run.

### How it works

1. **Inject:** At run start, the engine loads prior captured values and adds them to context under `inject_as` (default: `_memory`).
2. **Use:** Stages that declare `_memory` in their `signature.input` receive the prior values in their prompt.
3. **Capture:** After each configured stage completes, the specified output key is appended to the rolling store.
4. **Trim:** The store keeps only the newest `max_entries` values per stage/key pair — oldest are evicted automatically.

### Configuration

```yaml
memory:
  enabled: true
  fresh: false              # set true to skip loading prior memories for this run
  capture:
    - stage: synthesizer      # stage id whose output to capture
      key: recommendation     # output key to persist
      max_entries: 5          # rolling window — oldest evicted first
    - stage: evaluator
      key: quality_score
      max_entries: 10
  inject_as: _memory          # context key injected at run start
  extract_knowledge: false    # set true to run KnowledgeExtractor post-run
  inject_knowledge_as: _knowledge  # context key for injected knowledge facts
  # db: /custom/path/mem.db  # override default (~/.armature/memory/<name>.db)
```

### Memory fields

| Field | Type | Description |
|---|---|---|
| `enabled` | bool | Set to `false` to disable entirely without removing the config |
| `fresh` | bool | When `true`, skip loading prior memories at run start — each run begins clean. Useful for workflows where each run is independent and past context would be confusing. |
| `capture` | list | Stages and output keys to persist |
| `inject_as` | string | Context key under which memories are injected (default: `_memory`) |
| `extract_knowledge` | bool | Run a post-run knowledge extractor that synthesizes captured memories into structured long-term facts. Injected as `_knowledge` on subsequent runs. |
| `inject_knowledge_as` | string | Context key for injected knowledge facts (default: `_knowledge`) |
| `db` | string | Override the default DB path (`~/.armature/memory/<workflow_name>.db`) |

Each `capture` entry:

| Field | Type | Description |
|---|---|---|
| `stage` | string | Stage id to capture from |
| `key` | string | Output key from the stage's result dict |
| `max_entries` | int | Max entries to keep (default: 5) |

### Injected structure

The context key (`_memory`) is a nested dict: `{stage_id: {capture_key: [newest, ..., oldest]}}`.

For example, after five runs capturing `synthesizer.recommendation`:

```python
context["_memory"] == {
    "synthesizer": {
        "recommendation": [
            "Run 5: adopt the proposal with modification X",
            "Run 4: reject until compliance review completes",
            "Run 3: adopt — risk is acceptable",
            "Run 2: defer for more data",
            "Run 1: insufficient information to decide",
        ]
    }
}
```

### Making memory visible to a stage

Declare `_memory` (or whatever `inject_as` is set to) in `signature.input` so the stage's system prompt includes it:

```yaml
- id: synthesizer
  role:
    name: Synthesizer
    type: worker
    description: |
      Produce a final recommendation. If prior recommendations
      exist in _memory, consider whether this run's findings
      reinforce, contradict, or refine them.
  signature:
    input:
      topic: The original topic
      analyst: The structured analysis
      evaluator: The quality evaluation
      _memory: Prior run recommendations from memory
  depends_on: [evaluator]
```

### Default storage location

Memory is stored globally per workflow name — it survives session restarts and accumulates across all runs:

```
~/.armature/memory/<workflow_name>.db
```

To use a shared memory DB across machines or reset the rolling window, use the `db` field to point to a specific path.

---

## 9. Jinja2 variables in specs

Spec files support Jinja2 variable substitution, applied at load time. This lets you write reusable specs that are parameterized at call time.

### In Python:

```python
harness = Harness.from_spec(
    "workflows/my_workflow.yml",
    vars={
        "objective": "Evaluate the vendor contract",
        "provider": "anthropic",
        "specialist_model": "claude-haiku-4-5-20251001",
    }
)
```

### In the spec:

```yaml
name: "{{ name | default('my_workflow') }}"

model_tiers:
  small:
    provider: "{{ provider | default('anthropic') }}"
    model: "{{ specialist_model | default('claude-haiku-4-5-20251001') }}"

stages:
  - id: analyst
    role:
      name: Analyst
      type: researcher
      model_tier: small
      description: |
        Analyze the following objective and provide your findings.

        OBJECTIVE: {{ objective }}
    depends_on: []
```

### In the CLI:

```bash
armature run workflows/my_workflow.yml \
  --input objective="Evaluate the vendor contract" \
  --input provider=anthropic
```

**Note:** Jinja2 substitution happens at spec load time, before the Pydantic models are built. Runtime context (from `harness.run(inputs)`) is different — it flows through the context dict, not Jinja2.

Use Jinja2 vars for **structural** parameterization (model names, role descriptions, workflow names). Use runtime inputs for **data** (documents, user content, query strings).

---

## 10. Retry and recovery

Stages can be configured to retry on failure using `on_fail.loop`.

```yaml
- id: extractor
  role:
    name: Extractor
    type: worker
    model_tier: small
    description: |
      Extract structured data from the provided text.
      Return valid JSON only.
  output_mode: guided_json
  output_schema:
    type: object
    required: [items]
    properties:
      items:
        type: array
        items:
          type: string
  on_fail:
    loop:
      stage: extractor      # which stage to retry (usually itself)
      max: 2                # retry up to 2 times (3 total attempts)
      context: retry        # context mode
      backoff_s: 1.0        # initial wait before retry 1; doubles each attempt
      backoff_max_s: 30.0   # cap on per-attempt wait (default: 60s)
  depends_on: []
```

**Loop fields:**

| Field | Default | Description |
|-------|---------|-------------|
| `stage` | — | Stage ID to retry (usually the stage itself) |
| `max` | `3` | Maximum number of retries (attempts = max + 1) |
| `context` | `"retry"` | Context mode on retry |
| `backoff_s` | `null` | Initial wait before retry 1 in seconds; doubles each subsequent attempt |
| `backoff_max_s` | `60.0` | Cap on the per-attempt wait time in seconds |

On each retry, the context is augmented with:
- `_retry_attempt`: current attempt number (1-based)
- `_last_error`: the error message from the previous failure

The stage's role description can reference these to improve the retry:

```yaml
description: |
  Extract structured data from the provided text.

  {% if _retry_attempt %}
  PREVIOUS ATTEMPT FAILED: {{ _last_error }}
  Please correct the issue and try again.
  {% endif %}
```

**Safety note:** `ToolBlocked` exceptions from safety rules are never retried — a policy violation will not succeed on the next attempt.

---

## 11. Safety rules

Safety rules declaratively block, warn, or log when a tool is called with specific argument values. They are evaluated before any script/adapter stage runs.

```yaml
safety_rules:
  - tool: shell_runner
    condition:
      field: cmd
      op: contains
      value: "rm -rf"
    action: block
    message: "Destructive filesystem operations are not permitted."

  - tool: api_caller
    condition:
      field: endpoint
      op: matches_regex
      value: ".*/admin/.*"
    action: warn
    message: "Admin endpoint access — verify this is intentional."
```

### Condition operators

| Operator | Description |
|---|---|
| `contains` | Field value contains the string |
| `not_contains` | Field value does not contain the string |
| `equals` | Field value exactly equals the string |
| `not_equals` | Field value does not equal the string |
| `matches_regex` | Field value matches the regex pattern |
| `truthy` | Field value is truthy (non-empty, non-zero, non-null) |

### Actions

| Action | Behavior |
|---|---|
| `block` | Raise `ToolBlocked` — halts the stage, no retry |
| `warn` | Log a warning, continue execution |
| `log` | Log at info level, continue execution |

---

## 12. Lifecycle hooks

Lifecycle hooks let you attach custom logic at four points in the execution pipeline. They are Armature's extensible guardrail layer — safety rules (§11) handle the declarative case; hooks handle everything that requires custom code.

### Hook phases

| Phase | When | Return value |
|---|---|---|
| `PRE_TOOL` | Before any tool call executes | `HookDecision.ALLOW` or `HookDecision.BLOCK` |
| `POST_TOOL` | After any tool call completes | None (observation only) |
| `PRE_STAGE` | Before a stage begins executing | `HookDecision.ALLOW` or `HookDecision.BLOCK` |
| `POST_STAGE` | After a stage completes | None (observation only) |

### Registering hooks

Hooks are registered on the `Harness` object after construction, before calling `run()`:

```python
from armature import Harness
from armature.hooks.lifecycle import HookPhase, HookDecision

harness = Harness.from_spec("my_workflow.yml")

async def my_pre_tool_hook(phase, tool_name, args, ctx):
    if tool_name == "shell" and "sudo" in args.get("cmd", ""):
        return HookDecision.BLOCK
    return HookDecision.ALLOW

async def my_post_stage_hook(phase, stage_id, result, ctx):
    metrics.record(stage=stage_id, tokens=result.get("_output_tokens", 0))

harness._hooks.register(HookPhase.PRE_TOOL, my_pre_tool_hook)
harness._hooks.register(HookPhase.POST_STAGE, my_post_stage_hook)

result = await harness.run(inputs)
```

Multiple hooks for the same phase run in registration order. The first `BLOCK` short-circuits the rest.

### Hook decisions

`PRE_TOOL` and `PRE_STAGE` hooks return a `HookDecision`. `POST_TOOL` and `POST_STAGE` hooks return nothing.

| Decision | Effect |
|---|---|
| `HookDecision.ALLOW` | Execution proceeds normally |
| `HookDecision.BLOCK` | The tool call or stage is cancelled; a `ToolBlocked` exception is raised |

### Common patterns

**Audit logging** — record every tool invocation for compliance:
```python
async def audit_hook(phase, tool_name, args, ctx):
    audit_log.write({"tool": tool_name, "args": args, "run_id": ctx.get("run_id")})
    return HookDecision.ALLOW

harness._hooks.register(HookPhase.PRE_TOOL, audit_hook)
```

**Custom policy enforcement** — block on any condition expressible in Python, beyond what declarative safety rules support:
```python
async def policy_hook(phase, tool_name, args, ctx):
    if not is_allowed_domain(args.get("url", "")):
        return HookDecision.BLOCK
    return HookDecision.ALLOW

harness._hooks.register(HookPhase.PRE_TOOL, policy_hook)
```

**Progress reporting** — push stage events to a queue or WebSocket (this is how the async HTTP service endpoints work internally):
```python
async def progress_hook(phase, stage_id, result, ctx):
    await event_queue.put({"type": "stage_complete", "stage_id": stage_id})

harness._hooks.register(HookPhase.POST_STAGE, progress_hook)
```

**Token budget enforcement** — abort a run if cumulative token consumption exceeds a threshold:
```python
async def budget_hook(phase, stage_id, result, ctx):
    tokens = result.get("_output_tokens", 0) + result.get("_input_tokens", 0)
    budget.consume(tokens)
    if budget.exceeded():
        raise RuntimeError("Token budget exceeded")

harness._hooks.register(HookPhase.POST_STAGE, budget_hook)
```

### Permission levels

Every built-in tool and every custom tool registered via `ToolDescriptor` declares a permission level. The level documents the tool's access scope and can be inspected by hooks and safety rules:

| Level | Meaning |
|---|---|
| `READ_ONLY` | Reads files or data; no writes, no network |
| `WORKSPACE` | Reads and writes the local session directory |
| `NETWORK` | Makes outbound HTTP calls |
| `EXECUTE` | Runs arbitrary shell commands |

Permission levels are declared at registration time — they are metadata, not runtime enforcement. Your hooks and safety rules use them to implement policy.

### What Armature guardrails cover — and what they don't

Armature enforces guardrails at **tool-call boundaries and stage boundaries** — the points where work is delegated to an external system or where an agent's output is committed to context. Between safety rules (declarative), lifecycle hooks (programmable), and permission levels (metadata), every potentially risky action passes through an inspectable, blockable checkpoint.

What Armature does **not** do: guardrails on the **token stream** — inspecting or filtering individual tokens as they arrive from the LLM mid-generation. That is the right model for real-time content moderation in user-facing chat systems. It is not the right model for batch workflows: Armature's execution model completes a stage and stores its output as a whole artifact before that output influences anything downstream. A stage's full output is inspectable via a `POST_STAGE` hook before any subsequent stage sees it — which is a stronger safety boundary than per-token filtering for the workloads Armature is designed for.

---

## 13. Fan-out / Fan-in

Fan-out spawns multiple parallel copies of a subagent workflow and merges the results. This is useful for parallel analysis, ensemble evaluation, or processing a partitioned dataset.

```yaml
- id: ensemble_reviewers
  subagent_spec: workflows/reviewer.yml
  fan_out: 3
  fan_in: list
  depends_on: [loader]
```

### `fan_out`

Integer ≥ 1. Number of parallel child workflow instances to spawn.

### `fan_in`

How to merge the list of child results:

| Value | Behavior |
|---|---|
| `list` | (default) Returns `{"results": [result0, result1, ...]}` |
| `merge` | Shallow-merges all child result dicts; later children overwrite earlier on key conflicts |
| `first` | Returns only the first child's result |

### `partition_key`

When set, the named context key (which must be a list) is split evenly across the child instances. Each child receives a slice of the original list rather than the full list.

```yaml
- id: batch_processor
  subagent_spec: workflows/processor.yml
  fan_out: 4
  fan_in: list
  partition_key: documents    # context["documents"] is split into 4 chunks
  depends_on: [loader]
```

If `partition_key` is not set, all children receive identical context.

---

## 14. Human gates in detail

Human gates block execution until a person approves or provides feedback. They are appropriate at decision checkpoints, before irreversible actions, or for quality review.

```yaml
- id: final_approval
  gate: human
  present: |
    ## Workflow Complete — Final Review

    The workflow has produced the following recommendation:

    **Decision:** {{ synthesizer.decision }}
    **Confidence:** {{ synthesizer.confidence }}
    **Reasoning:** {{ synthesizer.reasoning }}

    Do you approve this recommendation?
  depends_on: [synthesizer]
```

The gate prints the `present` message to stdout and prompts:

```
Approve? [yes/no/feedback]:
```

- `yes` / `y` / `approve` → `{"approved": true, "feedback": null}`
- anything else → prompts for feedback text → `{"approved": false, "feedback": "..."}`

Downstream stages can branch on the gate result using the `condition` field (not yet implemented — check the stage based on `gate_id.approved` in your application code for now).

---

## 15. Deliberative teams

A deliberative team is a multi-agent workflow where specialist agents argue, challenge, and synthesize — producing a structured decision with an explicit confidence score and recorded dissents. This is Armature's native pattern for decisions that require more than a single LLM call.

### The three-round topology

Most deliberative workflows follow the same DAG shape regardless of domain:

```
R1: Specialists ──► R2: Challenger ──► R3: Synthesizer
 (researcher)         (researcher)        (judge)
```

- **R1 — Specialists:** Multiple analyst roles, typically chained sequentially so each sees prior analyses in context. Each specialist examines the question from a different domain angle and declares a position.
- **R2 — Challenger:** Sees all R1 outputs. Its sole job is constructive skepticism — attack unsupported assumptions, surface overlooked risks, prevent groupthink. It does not advocate a position.
- **R3 — Synthesizer:** A `type: judge` stage with `depends_on` pointing to every upstream stage. It reads the full deliberation transcript (accumulated in context automatically), resolves conflicts between specialists, responds to the Challenger's concerns, and emits a structured JSON decision.

### Specifying the objective

The debate topic is a Jinja2 variable embedded in each role's `description` field. Pass it at run time — there is no special YAML key:

```yaml
- id: analyst
  role:
    name: Analyst
    type: researcher
    model_tier: small
    description: |
      You are an objective analyst deliberating on the following:

      OBJECTIVE: {{ objective }}

      Analyze the evidence and end with a position:
      My position: PROCEED | BLOCK | MODIFY
  output_mode: text
  depends_on: []
```

From the CLI:
```bash
armature run my_deliberation.yml --input objective="Should we migrate auth to OAuth 2.0?"
```

From Python:
```python
result = await harness.run({
    "objective": "Should we migrate auth to OAuth 2.0?",
    "documents": "Current system context...",
})
```

The `objective` variable is available in every stage because all context is accumulated forward through the DAG — each stage automatically receives all prior stage outputs plus the initial inputs.

### The `type: judge` role

The Synthesizer is a `type: judge` stage. This does two things:

1. **Model tier:** Judge stages default to the `frontier` tier (see [Role type defaults](#role-type-defaults)). The decision-maker always gets your strongest model unless overridden.
2. **Quorum score:** The engine auto-extracts a `confidence`, `score`, or `quality_score` field from the judge's output and records it as the `quorum_score` on the trace. This feeds the IHR metric, the self-improvement loop, and the bootstrap few-shot selector.

```yaml
- id: synthesizer
  role:
    name: Synthesizer
    type: judge              # ← frontier model by default; confidence auto-extracted
    description: |
      You are the sole decision authority.
      OBJECTIVE: {{ objective }}
      ...
      You MUST end with a JSON block:
      ```json
      { "decision": "...", "reasoning": "...", "confidence": 0.85, ... }
      ```
  output_mode: guided_json
  output_schema:
    type: object
    required: [decision, reasoning, confidence]
    properties:
      decision:    { type: string }
      reasoning:   { type: string }
      confidence:  { type: number, minimum: 0.0, maximum: 1.0 }
      dissenting_opinions:
        type: array
        items: { type: object }
  depends_on: [analyst, strategist, risk_assessor, challenger]
```

### Minimal working example

A two-analyst deliberation (no challenger):

```yaml
name: simple_deliberation
version: "1.0"

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-sonnet-4-6

stages:
  - id: proponent
    role:
      name: Proponent
      type: researcher
      model_tier: small
      description: |
        OBJECTIVE: {{ objective }}
        Make the strongest case FOR this decision. Be evidence-based.
        End with: My position: PROCEED
    output_mode: text
    depends_on: []

  - id: critic
    role:
      name: Critic
      type: researcher
      model_tier: small
      description: |
        OBJECTIVE: {{ objective }}
        You have read the Proponent's case. Challenge it rigorously.
        Identify the weakest arguments and strongest counterpoints.
        End with: My position: BLOCK | MODIFY
    output_mode: text
    depends_on: [proponent]

  - id: judge
    role:
      name: Judge
      type: judge              # frontier model, confidence auto-tracked
      description: |
        OBJECTIVE: {{ objective }}
        You have read the full debate. Adjudicate.
        Weigh the Proponent's case against the Critic's challenges.
        End with a JSON block: { "decision": "...", "reasoning": "...", "confidence": 0.0 }
    output_mode: guided_json
    output_schema:
      type: object
      required: [decision, reasoning, confidence]
      properties:
        decision:   { type: string }
        reasoning:  { type: string }
        confidence: { type: number, minimum: 0.0, maximum: 1.0 }
    depends_on: [proponent, critic]
```

### How context accumulation creates the debate

Armature's context manager passes all prior stage outputs forward automatically. The judge in the example above receives a prompt that includes:

- The initial `objective` input
- The full `proponent` output (under key `proponent`)
- The full `critic` output (under key `critic`)

No explicit wiring is needed — `depends_on` controls ordering and the engine handles injection. This is what allows the Critic to reference the Proponent's arguments and the Judge to see the complete exchange.

### Reference implementations

Two complete deliberative specs are included:

| Spec | Pattern | Rounds |
|------|---------|--------|
| `examples/03_deliberation_standard.yml` | Analyst + Strategist + Risk Assessor → Challenger → Synthesizer | 3 |
| `armature/templates/six_thinking_hats.yml` | Six perspective hats → Blue Hat facilitator → Synthesizer | 3 |

Both accept `objective` as a runtime input. See section [15. Templates](#15-templates) for the Six Thinking Hats template in detail.

### Adding a human escalation gate

A common pattern: escalate to a human when the Synthesizer's confidence falls below a threshold. Add a gate stage downstream and inspect the judge's output in your application:

```yaml
- id: human_review
  gate: human
  present: |
    DECISION: {{ synthesizer.decision }}
    Confidence: {{ synthesizer.confidence }}
    Reasoning: {{ synthesizer.reasoning }}

    Do you approve?
  depends_on: [synthesizer]
```

Or conditionally skip the gate in code:
```python
result = await harness.run({"objective": "..."})
if result["synthesizer"]["confidence"] >= 0.80:
    # auto-proceed
else:
    # route to human review
```

---

## 16. Templates

Templates are pre-built, parameterized spec files that implement proven agentic patterns. They live in `armature/templates/` and are loaded like any other spec.

### Six Thinking Hats

Edward de Bono's Six Thinking Hats methodology — structured multi-perspective deliberation with a mandatory facilitator and synthesizer.

**DAG topology:**

```
white_hat ──► red_hat ──► black_hat ──► yellow_hat ──► green_hat
                                                              │
                                     ┌────────────────────────┘
                                     ▼
                                 blue_hat (facilitator)
                                     │
                                     ▼
                                synthesizer (decision)
```

**R1 (sequential):** The six perspective hats run in a chain so each hat sees prior hats' outputs. Each hat has strict constraints on what it may and may not say:

| Hat | Perspective | Constraint |
|---|---|---|
| White | Facts and data | No interpretation, no opinion |
| Red | Gut feelings and intuition | No justification, no data |
| Black | Risks and failure modes | No benefits, pure pessimist |
| Yellow | Benefits and opportunities | No risks, pure optimist |
| Green | Creative alternatives | No evaluation, generation only |

**R2:** The Blue Hat is a *facilitator*, not an advocate. It maps tensions and alignment between the hats and produces a priority list for the Synthesizer.

**R3:** The Synthesizer adjudicates the full deliberation and produces structured JSON: `{decision, reasoning, confidence, key_evidence, dissenting_opinions, risk_factors, open_questions, green_hat_considered}`.

**Usage:**

```python
harness = Harness.from_spec(
    "armature/templates/six_thinking_hats.yml",
    vars={
        "objective": "Should we adopt this new vendor contract?",
        # Optional overrides:
        "provider": "anthropic",
        "specialist_model": "claude-haiku-4-5-20251001",  # R1 + R2
        "synthesizer_model": "claude-sonnet-4-6",         # R3
    }
)

result = await harness.run({
    "documents": "Contract text and supporting materials...",
    "background": "Additional context...",
})

synthesis = result["synthesizer"]
print(synthesis["decision"])
print(f"Confidence: {synthesis['confidence']}")
```

**Adding domain specialists:** To add a domain-specific analyst alongside the hats (e.g., a legal analyst for contract decisions), add a stage with `depends_on: [green_hat]` and add it to `blue_hat` and `synthesizer`'s `depends_on` lists. Start from the template and extend it.

---

## 17. The optimizer

The optimizer reads trace history for a workflow and uses an LLM to propose improvements to prompts or model tier assignments.

```bash
armature optimize my_workflow.yml --traces ~/.armature/traces.db
```

Every LLM stage run is recorded in the trace database: model, input/output tokens, latency, whether the output parsed correctly, and a snapshot of inputs/outputs. The optimizer analyzes this history and generates a proposed unified diff.

The proposed diff is always printed for review. Add `--apply` to patch the spec file in place — the original is backed up as `<spec>.orig` before patching.

```bash
# Review only (default)
armature optimize my_workflow.yml --traces ~/.armature/traces.db

# Review and apply if accepted
armature optimize my_workflow.yml --traces ~/.armature/traces.db --apply
```

You can also run the optimizer in a loop from Python, with optional auto-apply:

```python
from armature.optimizer.runner import OptimizerRunner

runner = OptimizerRunner(
    target_spec_path="my_workflow.yml",
    trace_db_path="~/.armature/traces.db",
)

# Run 5 optimization iterations, auto-applying accepted proposals
loop_result = await runner.run_loop(n_iterations=5, auto_apply=True)
print(f"Accepted: {loop_result.accepted_count}, Rejected: {loop_result.rejected_count}")
```

The optimizer requires at least 5 traces to produce a proposal. Run your workflow a few times before optimizing.

---

## 18. Running workflows

### CLI reference

Armature ships with a full CLI. All commands:

```
armature new        — interactive wizard to create a new spec file
armature validate   — validate a spec file and report all errors
armature run        — execute a workflow from a YAML spec
armature serve      — start the HTTP service
armature optimize   — analyze traces and propose spec improvements
armature improve    — self-improvement loop: analyze, diagnose, revise, apply
armature report     — print a diagnostic report for a completed run
armature export-traces — export high-quality traces as training data (JSONL)
```

---

#### `armature new`

Interactive wizard that asks a series of questions and writes a starter spec file. Requires the `wizard` extra.

```bash
pip install 'armature[wizard]'
armature new my_workflow.yml
```

---

#### `armature validate`

Validate a spec file and report all errors. Returns exit code 0 on success, 1 on validation errors.

```bash
armature validate my_workflow.yml
```

Use this in CI to catch broken specs before they reach production.

---

#### `armature run`

Run a workflow from a YAML spec file.

```bash
armature run my_workflow.yml \
  --input topic="contract risk" \
  --input doc="$(cat contract.txt)" \
  --output result.json \
  --quiet
```

| Flag | Description |
|------|-------------|
| `--input key=value` | Pass input values (repeatable) |
| `--output path` | Write result JSON to a file instead of stdout |
| `--dry-run` | Validate spec without executing |
| `--quiet` / `-q` | Suppress live progress output |
| `--force` | Ignore checkpoint and rerun all stages from scratch |

---

#### `armature serve`

Start the Armature HTTP service. Requires the `service` extra.

```bash
pip install 'armature[service]'
armature serve --port 8080
```

The service exposes `POST /run` accepting `{"spec": "path/to/spec.yml", "inputs": {...}}`.

---

#### `armature optimize`

Analyze accumulated traces for a workflow and propose targeted spec improvements using an LLM. Requires at least 5 traces.

```bash
armature optimize my_workflow.yml --traces ~/.armature/traces.db
armature optimize my_workflow.yml --traces ~/.armature/traces.db --apply
```

Without `--apply`, the proposed diff is printed for review. With `--apply`, it is patched into the spec file (original backed up as `<spec>.orig`).

---

#### `armature report`

Print a human-readable diagnostic report for a completed run, including stage-by-stage metrics, IHR score, and failure signatures.

```bash
armature report --run-id abc123
armature report --run-id abc123 --traces ~/.armature/runs/abc123/traces.db
```

---

#### `armature improve`

Closed-loop self-improvement: loads traces, computes the Implicit Harness Rating (IHR), diagnoses failure signatures, and calls an LLM to produce a revised spec. If IHR is below the target and enough traces exist, the revised spec is auto-applied.

```bash
armature improve my_workflow.yml
armature improve my_workflow.yml --no-apply     # propose but don't write
armature improve my_workflow.yml --target-ihr 0.85 --min-traces 10
armature improve my_workflow.yml --model claude-opus-4-7
```

| Flag | Default | Description |
|------|---------|-------------|
| `--traces path` | `~/.armature/traces.db` | Path to trace database |
| `--model name` | `claude-sonnet-4-6` | LLM used by the spec refiner |
| `--target-ihr float` | `0.90` | IHR threshold; improvement triggered when below this |
| `--min-traces int` | `3` | Minimum traces required before analysis |
| `--apply / --no-apply` | apply | Auto-apply the proposed spec |
| `--log path` | `<spec>.improve_log.jsonl` | Path to the improvement audit log |

See §20 for the full self-improvement system.

---

#### `armature export-traces`

Export high-quality traces from the trace database as JSONL training data for SFT or DPO fine-tuning.

```bash
armature export-traces \
  --workflow my-workflow \
  --output training_data.jsonl \
  --format chat \
  --min-score 0.85
```

| Flag | Default | Description |
|------|---------|-------------|
| `--workflow` / `-w` | — | Workflow name to export traces for |
| `--output` / `-o` | — | Output JSONL file path |
| `--format` / `-f` | `chat` | `chat` (OpenAI ChatML), `alpaca`, `sharegpt`, or `dpo` |
| `--min-score` | `0.85` | Minimum quorum score for traces to include |
| `--rejected-max-score` | `0.30` | DPO only: max quorum score for rejected traces |
| `--role-types` | all | Comma-separated role types to include (e.g. `judge,researcher`) |
| `--system-prompt` | auto | Override the system field for all records |
| `--limit` | `1000` | Maximum traces to fetch |

See §21 for the full trace export system.

---

### Python API

```python
import asyncio
from armature import Harness

# Load from a spec file
harness = Harness.from_spec("my_workflow.yml")

# Or with Jinja2 vars
harness = Harness.from_spec(
    "armature/templates/six_thinking_hats.yml",
    vars={"objective": "..."}
)

# Run with inputs
result = await harness.run({
    "documents": "...",
    "topic": "...",
})

# Access stage results
print(result["my_stage_id"])
```

### Event callbacks

Subscribe to stage lifecycle events during a run:

```python
def on_event(event_type: str, data: dict):
    if event_type == "stage_start":
        print(f"Starting: {data['stage']} ({data['kind']})")
    elif event_type == "stage_complete":
        print(f"Done: {data['stage']} in {data['elapsed_s']}s")

harness = Harness(spec=spec, on_event=on_event)
```

### Transcript

After a run, `harness.transcript` contains the full conversation log for all LLM stages:

```python
result = await harness.run(inputs)

for entry in harness.transcript:
    print(entry["stage_id"], entry["role_name"], entry["model"])
    print(entry["response"][:200])
```

### HTTP service

```bash
armature serve --port 8080
```

The service exposes a REST API for running workflows over HTTP. Requires `pip install 'armature[service]'`.

### Artifacts and session logs

Each run writes to `~/.armature/runs/<run_id>/`:

```
~/.armature/runs/<run_id>/
├── session.jsonl     # structured event log for the run
├── traces.db         # SQLite trace store (tokens, latency, outputs)
└── artifacts/        # any artifacts written by script stages
```

Override the session directory:

```python
from pathlib import Path
harness = Harness(spec=spec, session_dir=Path("./my_run_output"))
```

---

## 19. Integrating with a host application

An Armature workflow is typically one component inside a larger application — a FastAPI service, a background job, a CLI tool — that handles user sessions, persistence, auth, and other concerns. This section describes how to wire the two together.

There are three integration patterns. Pick the one that matches your deployment and coupling requirements.

---

### Built-in tools reference

Armature pre-registers these tools in every harness. They are available to any stage via `tool_call` or `role.tools` without any additional configuration.

| Tool | Permission | Key args | Returns |
|------|-----------|----------|---------|
| `file_read` | READ_ONLY | `path` | `{ content }` |
| `file_write` | WORKSPACE | `path`, `content` | `{ written }` |
| `shell` | WORKSPACE | `cmd` | `{ stdout, stderr, exit_code }` |
| `http_get` | NETWORK | `url` | `{ status, body }` |
| `http_post` | NETWORK | `url`, `body?`, `headers?`, `timeout?` | `{ status, body }` |
| `tessera.retrieve` | NETWORK | `query`, `top_k?` | `{ chunks }` |
| `quorum.deliberate` | NETWORK | `topic`, `brief?`, `agents?` | `{ verdict, rationale }` |
| `alembic.submit` | NETWORK | `trace`, `score?`, `alembic_url?` | `{ submitted }` |

**`http_post`** is the general bridge to any external API. Pass `headers` for authentication:

```yaml
- id: call_openai
  tool_call:
    name: http_post
    args:
      url: "https://api.openai.com/v1/images/generations"
      headers:
        Authorization: "Bearer {{ env.OPENAI_API_KEY }}"
      body:
        model: "dall-e-3"
        prompt: "{{ image_prompt }}"
        n: 1
```

For more complex integrations (multi-step API flows, stateful calls, response parsing), use a custom tool module instead (see below).

---

### Declaring tool modules in the spec

The `tools:` section lets a spec reference external Python modules by dotted import path. The harness imports each module at startup and calls its `register()` function to add tools to the registry. Tool code lives in your codebase — the spec just says which modules to load.

```yaml
tools:
  - module: myapp.tools.search_tools
  - module: myapp.tools.database_tools
```

Each referenced module must expose a single function:

```python
# myapp/tools/search_tools.py
from armature.registry.registry import ToolRegistry, ToolDescriptor
from armature.permissions.permissions import PermissionLevel

async def _search(args: dict) -> dict:
    query = args["query"]
    # ... your search logic ...
    return {"results": [...]}

def register(registry: ToolRegistry) -> None:
    registry.register(ToolDescriptor(
        name="search",
        description="Search the knowledge base",
        permission=PermissionLevel.NETWORK,
        handler=_search,
        parameters={"query": {"type": "string", "description": "Search query"}},
    ))
```

Once registered, tools are available to any LLM stage whose role lists them:

```yaml
stages:
  - id: researcher
    role:
      name: Researcher
      type: researcher
      description: "Research the given topic using available tools."
      tools: [search, database.lookup]
    depends_on: []
```

**Rules:**
- The module must be importable from wherever the harness runs (i.e. on `PYTHONPATH` or installed in the environment)
- Missing `register` function → `AttributeError` at harness startup with a clear message
- Module not found → `ModuleNotFoundError` at harness startup
- A user module can re-register a builtin name — the last registration wins; this is intentional and lets you replace builtins with project-specific implementations

#### Reasoning Automation example

The ToolModule pattern is how you connect Armature workflows to external systems for end-to-end process automation. A typical project layout:

```
my-automation-app/
├── my_app/
│   └── tools/
│       ├── dalle.py           # dalle.generate_image → OpenAI API
│       ├── meta_publisher.py  # meta.publish_ad → Meta Marketing API
│       └── analytics.py       # analytics.collect_meta → Meta Analytics API
├── workflows/
│   ├── intake.yaml            # Brief intake + platform recommendation
│   ├── concept-gen.yaml       # 3 copy variants + DALL-E images + brand judge
│   └── monitoring.yaml        # Daily metrics collection + verdict
└── corpus/
    └── brand.md               # Brand knowledge loaded into Tessera
```

Each tool module handles one external integration. The YAML spec wires them together with LLM reasoning stages. Armature executes the whole chain — the tool modules are domain-specific; the harness is reusable infrastructure.

See `docs/use-case-ad-campaign.md` in this repo for a complete reference implementation (Dangerous Pretzel Co. social ad campaign automation).

---

### Pattern A — embedded library

Import Armature directly into your application. The workflow runs in the same process as the host.

**Best for:** Python applications where the agentic team is a core feature, not a separate service. Single process, easiest debugging, no network round-trips.

```python
import asyncio
from armature import Harness

# Load once at startup (or per-tenant if specs vary)
harness = Harness.from_spec("workflows/research_pipeline.yml")

async def handle_research_request(topic: str, documents: str) -> dict:
    result = await harness.run({
        "topic": topic,
        "documents": documents,
    })
    # Extract just the keys your app cares about
    return {
        "summary": result["synthesizer"],
        "citations": result.get("analyst", {}).get("citations", []),
    }
```

For sync callers (Django views, Flask routes):

```python
def handle_sync(topic: str) -> dict:
    return asyncio.run(handle_research_request(topic, documents=""))
```

**Tradeoff:** Armature's dependencies (LiteLLM, ruamel.yaml, etc.) become your app's dependencies. Workflow failures raise exceptions in your process.

---

### Pattern B — HTTP sidecar

Run Armature as a separate service and call it over HTTP. Your host application is language-agnostic.

```bash
# Start the sidecar (requires pip install 'armature[service]')
armature serve --port 8080
```

```python
import httpx

async def call_workflow(inputs: dict) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "http://localhost:8080/run",
            json={"spec": "workflows/research_pipeline.yml", "inputs": inputs},
        )
        resp.raise_for_status()
        return resp.json()["result"]
```

**Tradeoff:** Independent scaling and failure isolation, but adds a network hop and requires the sidecar to be running. Long-running workflows may require async polling rather than a synchronous HTTP response.

---

### Pattern C — subprocess

Shell out to the CLI. Works from any language; useful for scripting and one-shot integrations.

```python
import subprocess, json

def run_workflow(spec_path: str, inputs: dict) -> dict:
    args = ["armature", "run", spec_path]
    for k, v in inputs.items():
        args += ["--input", f"{k}={v}"]
    out = subprocess.check_output(args)
    return json.loads(out)
```

**Tradeoff:** Simplest integration, zero shared dependencies. Poor fit for structured inputs with nested data (everything serializes to strings), and a new Python interpreter starts per call. Not suitable for latency-sensitive paths.

---

### Input shaping

The host application maps its domain objects to the flat `vars` dict that `harness.run()` (or `--input`) accepts. The first stage's `signature.input` is the natural contract boundary — it declares exactly which keys the workflow expects.

```yaml
stages:
  - id: analyst
    role: ...
    signature:
      input:
        topic: "The research topic"
        documents: "Source documents to analyze"
```

This both documents the interface and filters irrelevant context from the stage's prompt. Your host application should treat `signature.input` keys as the workflow's public API:

```python
# Explicit mapping from app domain → workflow contract
result = await harness.run({
    "topic": request.query,          # app field → workflow key
    "documents": "\n".join(docs),    # app list → workflow string
})
```

For typed inputs, convert to strings before passing; use `guided_json` output stages and `signature.input` on the consuming stage if the downstream workflow needs structured data.

---

### Output extraction

`harness.run()` returns the full context dict — every stage's output is present under its `id`. Your host application should extract only the keys it needs rather than forwarding the entire dict.

```python
result = await harness.run(inputs)

# Typed extraction — the workflow contract
summary: str    = result["synthesizer"]          # text stage output
analysis: dict  = result["analyst"]              # guided_json stage output
score: float    = result["evaluator"]["score"]   # nested field
```

When using `guided_json` or `text` output modes, outputs are already parsed Python objects (dict or str). `raw` mode returns the LLM's response string unparsed.

If the workflow has a dedicated output stage, document its id and expected fields as the integration's response schema. Avoid reaching into intermediate stage outputs — treat those as implementation details of the workflow.

---

### Async lifecycle

The embedded pattern is fully `async`. Common host patterns:

**FastAPI** (async natively):
```python
@app.post("/research")
async def research(req: ResearchRequest):
    result = await harness.run({"topic": req.topic, "documents": req.documents})
    return {"summary": result["synthesizer"]}
```

**Background task** (Celery / ARQ / asyncio.create_task):
```python
# asyncio task within a running loop
async def background_research(job_id: str, inputs: dict):
    result = await harness.run(inputs)
    await db.store_result(job_id, result["synthesizer"])
```

**Persistent harness** — load the spec once at app startup, reuse the `Harness` object across requests. `harness.run()` is safe to call concurrently; each call maintains its own context and transcript internally.

```python
# At startup
_harness = Harness.from_spec("workflows/research.yml")

# Per request — concurrent calls are safe
result = await _harness.run(request_inputs)
```

---

### Error contracts

When a stage exhausts its retry budget (`on_fail.loop.max`), the engine raises an exception that propagates out of `harness.run()`. Wrap calls in try/except to handle this at the host boundary:

```python
from armature.runtime.engine import StageError  # raised on unrecoverable failure

try:
    result = await harness.run(inputs)
except StageError as e:
    logger.error("Workflow stage failed: %s — %s", e.stage_id, e)
    raise HTTPException(status_code=502, detail="Workflow unavailable")
```

For stages with `on_fail.loop`, the loop exhaustion is the error signal. If you need soft failure (partial results), add a final stage that consolidates whatever context is available rather than relying on exception handling in the host.

---

## 20. Self-improvement

Armature includes a closed-loop self-improvement system that analyzes workflow traces, diagnoses quality problems, and produces a targeted spec revision. Each improvement cycle logs its predictions; the next cycle verifies whether those predictions came true — building an accountability record over time.

### How it works

The `armature improve` command runs one analysis cycle:

1. **Load traces** for the workflow from the trace database (default: `~/.armature/traces.db`)
2. **Compute rolling IHR** (Implicit Harness Rating) across the loaded traces:
   - IHR = `0.40 × output_valid_rate + 0.30 × success_rate + 0.20 × avg_quorum + 0.10 × latency_score`
3. **Run DiagnosticAnalyzer** to identify failure signatures — which stages are failing and how:
   - `stage_failed` — the stage raised an exception
   - `output_invalid` — the stage produced output that didn't match its schema
   - `low_confidence` — the stage's quorum score was consistently low
   - `high_escalation` — the stage frequently escalated to a larger model tier
4. **Verify previous cycle's predictions** — compare the current diagnostic state against what the prior cycle predicted would be fixed
5. **If IHR < target and traces ≥ minimum**, call `SpecRefiner` (a frontier LLM) with the current spec + diagnostics + quality metrics
6. **Apply the revised spec** (unless `--no-apply`)
7. **Write an audit log entry** (JSONL) with all metrics, diagnostics, predictions, and verification results

### The prediction-verification loop

When `SpecRefiner` proposes a revised spec, it must declare a falsifiable contract:
- `predicted_fixes` — which failure signatures it expects to resolve
- `predicted_regressions` — which signatures might temporarily worsen

The next `armature improve` run verifies these against observed changes:
- `verified_fixes` — predictions that came true (the signature disappeared)
- `missed_predictions` — predicted fixes that did not materialize
- `unexpected_regressions` — new failures that weren't predicted

This record accumulates in the JSONL log file alongside each run. Over multiple cycles you get a track record of how accurately your workflow improvements land.

### Usage

```bash
# Analyze and apply if IHR < 0.90 (default)
armature improve my_workflow.yml

# Propose only — do not write the spec
armature improve my_workflow.yml --no-apply

# Stricter threshold, more data required
armature improve my_workflow.yml --target-ihr 0.95 --min-traces 20

# Use a more capable model for refinement
armature improve my_workflow.yml --model claude-opus-4-7
```

The improvement log is written to `<spec_stem>.improve_log.jsonl` by default. Each line is a JSON object:

```json
{
  "timestamp": "2026-05-15T10:00:00Z",
  "workflow_name": "campaign-concept-gen",
  "n_traces": 47,
  "ihr_before": 0.71,
  "needs_improvement": true,
  "applied": true,
  "diagnostics": [
    {"code": "output_invalid", "stage_id": "brand_judge", "details": "4/10 runs failed schema validation"}
  ],
  "predicted_fixes": ["output_invalid:brand_judge"],
  "predicted_regressions": [],
  "verified_fixes": [],
  "missed_predictions": [],
  "unexpected_regressions": []
}
```

### Python API

```python
from armature.synthesis.improve import SelfImproveRunner

runner = SelfImproveRunner(
    "my_workflow.yml",
    "~/.armature/traces.db",
    model="claude-sonnet-4-6",
    target_ihr=0.90,
    min_traces=10,
    auto_apply=True,
)

report = await runner.analyze()

print(f"IHR: {report.ihr_before:.3f}")
print(f"Needs improvement: {report.needs_improvement}")
print(f"Applied: {report.applied}")
print(f"Verified fixes: {report.verified_fixes}")
print(f"Unexpected regressions: {report.unexpected_regressions}")
```

### What SpecRefiner changes

The refiner makes targeted changes only — it does not rewrite stages that are performing well. Its rules:

| Failure signature | What the refiner may do |
|-------------------|------------------------|
| `output_invalid` | Relax or correct the stage's `output_schema` required fields |
| `low_confidence` | Enrich `role.description` with explicit evaluation criteria |
| `high_escalation` | Increase `on_fail.loop.max` or upgrade `model_tier` |
| `stage_failed` | Add `timeout_s` or upgrade `model_tier` |

The refiner is explicitly constrained from adding or removing stages, or changing stage IDs.

### Building traces

The improvement system requires trace data. Run your workflow normally — each run automatically records traces:

```bash
# Run several times to build up data
armature run my_workflow.yml --input topic="quantum computing"
armature run my_workflow.yml --input topic="renewable energy"
armature run my_workflow.yml --input topic="supply chain risk"

# Then improve
armature improve my_workflow.yml
```

Traces accumulate in `~/.armature/traces.db` by default.

---

## 21. Trace export for fine-tuning

Every workflow run records execution traces — the inputs, outputs, and quality scores of every LLM stage. High-quality traces (high quorum score, valid output) are valuable training data for fine-tuning smaller language models to perform the same tasks more cheaply.

`armature export-traces` exports these traces as JSONL in the format expected by common fine-tuning frameworks.

### Formats

| Format | Structure | Compatible with |
|--------|-----------|-----------------|
| `chat` | `{"messages": [{"role": "system"}, {"role": "user"}, {"role": "assistant"}]}` | OpenAI, Anthropic, Qwen, LLaMA |
| `alpaca` | `{"instruction": "...", "input": "...", "output": "..."}` | Stanford Alpaca, Axolotl |
| `sharegpt` | `{"conversations": [{"from": "human"}, {"from": "gpt"}]}` | ShareGPT-format trainers |
| `dpo` | `{"prompt": "...", "chosen": "...", "rejected": "..."}` | DPO / GRPO preference training |

### SFT export

```bash
armature export-traces \
  --workflow campaign-concept-gen \
  --output training/brand_judge_sft.jsonl \
  --format chat \
  --min-score 0.85 \
  --role-types judge
```

This exports all traces from the `campaign-concept-gen` workflow where the stage role type is `judge` and the quorum score is ≥ 0.85. Each trace becomes one JSONL record: the stage's inputs become the user turn, and the stage's output becomes the assistant turn.

### DPO export

DPO training requires pairs of (chosen, rejected) responses for the same prompt:

```bash
armature export-traces \
  --workflow campaign-concept-gen \
  --output training/brand_judge_dpo.jsonl \
  --format dpo \
  --min-score 0.85 \
  --rejected-max-score 0.30
```

For each high-quality trace, the exporter finds a low-quality trace from the same stage and pairs them. The lowest-scoring rejected trace is used when multiple are available.

### System prompt override

By default the system prompt is generated from the stage's role type: `"You are a {role_type} agent. Complete the task described by the user."` Override it to match the system prompt used during your target fine-tuning run:

```bash
armature export-traces \
  --workflow my-workflow \
  --output out.jsonl \
  --system-prompt "You are an expert brand compliance analyst for food companies."
```

### Python API

```python
from pathlib import Path
from armature.state.traces import TraceStore
from armature.state.export import TraceExporter

store = TraceStore(Path("~/.armature/traces.db").expanduser())
exporter = TraceExporter(store)

# SFT
summary = await exporter.export(
    "my-workflow",
    Path("training/sft.jsonl"),
    format="chat",
    min_quorum_score=0.85,
    role_types=["judge"],
    system_prompt="You are an expert analyst.",
    limit=500,
)
print(f"Exported {summary.total_exported} records")

# DPO
summary = await exporter.export_dpo(
    "my-workflow",
    Path("training/dpo.jsonl"),
    chosen_min_score=0.85,
    rejected_max_score=0.30,
    limit=500,
)
```

### Quality filtering

The `min_quorum_score` filter is the primary quality gate. Quorum score is set by multi-agent deliberation stages in your workflow (using the `quorum.deliberate` builtin tool). If your workflow doesn't use quorum scoring, all traces have a `null` quorum score and the filter is effectively disabled — you may want to set a high `min_quorum_score` anyway to filter on other criteria, or post-process the exported JSONL manually.

A practical fine-tuning pipeline:
1. Run the workflow many times with diverse inputs to build a trace corpus
2. Export at `--min-score 0.85` for SFT, `--min-score 0.85 --rejected-max-score 0.30` for DPO
3. Fine-tune a small model (e.g. Qwen 2.5 7B) on the exported data
4. Point the workflow's `small` tier at your fine-tuned model
5. Measure cost and quality — the fine-tuned small model often matches the frontier model on specialized tasks at a fraction of the cost

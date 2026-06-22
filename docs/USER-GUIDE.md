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
   - [LoRA adapter-backed skills](#42-lora-adapter-backed-skills)
   - [Script/adapter stage](#43-scriptadapter-stage)
   - [Human gate](#44-human-gate)
   - [Direct tool call stage](#45-direct-tool-call-stage)
   - [Subagent stage](#46-subagent-stage)
   - [Conditional execution (skip_if)](#47-conditional-stage-execution-skip_if)
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
    - [Consensus fan-in](#consensus-fan-in)
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
    - [Drift score](#drift-score)
    - [Component governance](#component-governance)
21. [Trace export for fine-tuning](#21-trace-export-for-fine-tuning)
22. [Post-condition verification](#22-post-condition-verification)
23. [Context provenance](#23-context-provenance)
24. [Memory staleness](#24-memory-staleness)
25. [Workflow health dashboard](#25-workflow-health-dashboard)
26. [LLM response caching](#26-llm-response-caching)
27. [Audit replay](#27-audit-replay)
28. [Trace-triggered behaviors](#28-trace-triggered-behaviors)
29. [Auto self-improvement](#29-auto-self-improvement)
30. [Spec risk scoring](#30-spec-risk-scoring)
31. [Rogue signal tracking](#31-rogue-signal-tracking)
32. [Safety rule composition](#32-safety-rule-composition)
33. [Mission context for long-horizon workflows](#33-mission-context-for-long-horizon-workflows)
34. [Low-latency / streaming stages](#34-low-latency--streaming-stages)
35. [Continuation — rolling memory across runs](#35-continuation--rolling-memory-across-runs)
36. [Triggers — cron and webhook activation](#36-triggers--cron-and-webhook-activation)
37. [Named workflow registry](#37-named-workflow-registry)

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
    # loop:                    # Deliberate iteration (not retry); see IterationConfig

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
| `skill_library` | object | no | Named skills that can be attached to roles, optionally backed by LoRA adapters |
| `adapter_factory` | object | no | Configuration for the pluggable LoRA adapter factory |
| `safety_rules` | list | no | Declarative tool safety rules (see §11) |
| `safety_mode` | string | no | `"permissive"` (default) or `"strict"` — controls default when no rule matches (see §11) |
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
| `adapter_support` | `dynamic` \| `none` | `dynamic` loads LoRA adapters from the registry and passes them to the provider per request; `none` disables adapter loading (default: `none`) |
| `adapter_path_template` | string | Optional path template for locating LoRA artifacts served by this tier. Supports `{adapter_name}` and `{adapter_version}` placeholders. |

**Providers:** Any provider supported by [litellm](https://github.com/BerriAI/litellm) works — Anthropic, OpenAI, OpenRouter, Ollama, Azure, Bedrock, and more.

**Credentials:** API keys are read from environment variables. For most providers litellm finds the key automatically (e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). Use `api_key_env` to name a different variable for a specific tier.

**Escalation:** If a stage's assigned tier produces an unparseable JSON response (when `output_mode: guided_json`), the engine automatically escalates to the next defined tier and retries. Only tiers you have actually configured participate — the escalation order is always `tiny → small → medium → large → frontier`.

**Provider-aware structured output:** Providers that support native structured output (OpenAI, Anthropic) receive a `response_format` kwarg enforcing the output schema. Providers that do not (Ollama) fall back to prompt-guided JSON with automatic extraction. This is re-evaluated per escalation tier, so switching providers mid-escalation uses the right strategy automatically.

**Per-tier tool calling override:** By default the engine injects native tool specs for OpenAI/Anthropic providers and uses prompt-based tool descriptions for Ollama. Set `tool_calling: true` on any Ollama tier running a model that supports tool calling (e.g. Llama 3.1+, Qwen 2.5) to enable native dispatch. Set `tool_calling: false` to disable it for any provider.

**LoRA adapter support:** Set `adapter_support: dynamic` on a tier to enable skill-backed LoRA adapters. The engine resolves `skill.adapter` references from the local adapter registry and passes the artifact path to the provider via provider-specific kwargs (e.g. `extra_body.lora_request` for vLLM, `options.adapter` for Ollama). When a skill's adapter is active, the original skill text is omitted from the prompt to save context window; when the adapter cannot be loaded, the skill's `fallback` policy controls behavior (`text`, `none`, or `fail`).

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
| `skills` | list | no | Skill names from `skill_library:` |

**Skills**

A role can reference skills by name. Skills are declared in the top-level
`skill_library:` block and contain domain instructions that are injected into
the system prompt. A skill may be:

- **Text-backed** — inline `content:` (or a `path:` to a file) is rendered into
  the prompt under `## Skills`.
- **Adapter-backed** — a `skill.adapter` reference loads a LoRA adapter from the
  local registry. When the tier supports `adapter_support: dynamic`, the adapter
  is passed to the provider and the skill text is omitted to save context.

```yaml
skill_library:
  tdd:
    id: tdd
    description: Test-driven development
    content: |
      Write a failing test first, then the minimal implementation.
    adapter:
      name: tdd
      version: latest
      fallback: text
```

Attach it to a role with `skills: [tdd]`.

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
| `loop` | `IterationConfig` | Deliberate iteration: run this stage up to `max_iterations` times, carrying forward selected state between iterations. Distinct from `on_fail.loop` (retry). |
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

### 4.2 LoRA adapter-backed skills

Armature can replace skill text at runtime with a fine-tuned LoRA adapter.
This is the runtime half of the fine-tuning flywheel: export high-quality
traces, train a small specialist, register it, and reference it from a skill.
The pattern is developed from **Skill-to-LoRA: From Using Skills to Learning
Behaviors for Token-Efficient LLM Agents** (Zhang & Qi, CUHK, June 2026 —
[arXiv:2606.16769](https://arxiv.org/abs/2606.16769)), which shows that
skill-induced behavior can be distilled into LoRA weights and loaded at runtime
to cut prefill tokens while preserving task success.

```yaml
model_tiers:
  small:
    provider: vllm
    model: qwen/qwen2.5-7b
    adapter_support: dynamic   # required for adapter loading
    api_base: http://localhost:8000

skill_library:
  tdd:
    id: tdd
    description: Test-driven development workflow
    content: |
      Follow test-driven development:
      1. Write a failing test.
      2. Write the minimal implementation.
      3. Refactor.
    adapter:
      name: tdd
      version: latest
      fallback: text             # text | none | fail
      inject_metadata: false

stages:
  - id: coder
    role:
      name: TDD Coder
      type: worker
      skills: [tdd]
      description: Implement {{ feature }} using the attached TDD skill.
    depends_on: []
```

**How it works:**

1. At runtime, the engine resolves `skill.adapter.name` from the local adapter
   registry (`~/.armature/adapters`).
2. If the tier has `adapter_support: dynamic`, the adapter artifact path is
   passed to the provider in provider-specific kwargs (vLLM uses
   `extra_body.lora_request`, Ollama uses `options.adapter`).
3. The original skill `content` is omitted from the prompt when the adapter is
   active, freeing context window. Set `inject_metadata: true` to keep a short
   "Active via adapter ..." note.
4. If the adapter cannot be resolved:
   - `fallback: text` keeps the original skill text.
   - `fallback: none` omits the skill.
   - `fallback: fail` raises a runtime error.

**CLI workflow:**

```bash
# 1. Train an adapter from a skill document (mock backend = instant placeholder)
armature adapter create --spec workflow.yml --skill tdd --backend mock

# 2. Train from exported high-quality traces
armature export-traces --workflow my-wf --output training.jsonl --min-score 0.85
armature adapter create --spec workflow.yml --traces training.jsonl --backend trace

# 3. Promote the new adapter to latest
armature adapter promote tdd 2

# 4. Merge two adapters into one artifact
armature adapter merge tdd@2 security@1 --name tdd-security

# 5. Evaluate whether the adapter improves a target stage
armature adapter eval tdd workflow.yml --input feature="login" --stage-id judge
```

**Registry directory:** Adapters are stored under `~/.armature/adapters` by
default. Override with `--registry /path/to/adapters` on any `armature adapter`
subcommand, or pass `--registry` to `armature run` to use a custom registry for a
single run.

See `examples/07_lora_adapter.yml` for a complete runnable spec.

---

### 4.3 Script/adapter stage

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

### Memory staleness

Memory entries older than a configurable threshold are flagged as stale at load time. Stale entries are still injected into context — the harness does not silently drop them — but a `_stale_memory_keys` warning list is also added to context so that downstream stages (and the LLM) are aware.

```python
from armature.state.memory import MemoryStore

# Flag entries older than 14 days as stale (default is 30 days)
store = MemoryStore(db_path, staleness_threshold_days=14)
memories, stale_keys = await store.load("my_workflow")
# stale_keys: set of (stage_id, capture_key) tuples
```

When the engine detects stale keys, it injects a list into context:

```python
context["_stale_memory_keys"] = ["synthesizer.recommendation", "evaluator.quality_score"]
```

A stage that uses memory can surface this warning in its prompt:

```yaml
- id: synthesizer
  role:
    description: |
      Produce a recommendation.
      {% if _stale_memory_keys %}
      WARNING: The following memory entries may be outdated: {{ _stale_memory_keys | join(", ") }}
      Weight them accordingly.
      {% endif %}
  signature:
    input:
      _memory: Prior run recommendations
      _stale_memory_keys: Stale memory warnings
```

See §24 for the full staleness reference.

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
- `_iteration`: dict set when the stage has `loop:` — always defined with keys `num` (1-based), `is_first`, `is_last`, and `carry_forward` (empty on iteration 1, populated from prior iterations onward). See [loop section].

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

Safety rules declaratively control what happens when a tool is called with specific argument values. They are evaluated before any script/adapter stage runs.

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
    action: require_approval
    message: "Admin endpoint access — confirm with operator."

  - tool: file_read
    condition:
      field: path
      op: truthy
      value: ""
    action: allow
    message: ""

  - tool: "*"
    condition:
      field: _tool_reversibility
      op: equals
      value: "none"
    action: block
    message: "Irreversible tool calls are disabled in this workflow."
```

### Top-level safety mode

Add `safety_mode` at the top level of your spec to choose the default policy when no rule matches:

```yaml
safety_mode: strict   # deny any tool call not matched by an explicit "allow" rule
# safety_mode: permissive  # (default) allow any tool call not matched by a rule
```

In **strict mode** you must explicitly whitelist every tool you want to use with `action: allow` rules. In **permissive mode** (the default) unmatched tool calls proceed normally.

### Condition operators

| Operator | Description |
|---|---|
| `contains` | Field value contains the string |
| `not_contains` | Field value does not contain the string |
| `equals` | Field value exactly equals the string |
| `not_equals` | Field value does not equal the string |
| `matches_regex` | Field value matches the regex pattern |
| `truthy` | Field value is truthy (non-empty, non-zero, non-null) |

### Pseudo-fields

In addition to actual tool arguments, conditions can match against metadata injected by Armature:

| Field | Values | Description |
|---|---|---|
| `_tool_reversibility` | `full`, `partial`, `none` | Reversibility class of the called tool |

Built-in tool reversibility values:

| Tool | Reversibility |
|---|---|
| `file_read` | `full` |
| `http_get` | `full` |
| `file_write` | `partial` |
| `shell` | `none` |
| `http_post` | `none` |

Custom tools default to `full` unless you set `reversibility` when constructing their `ToolDescriptor`.

### Actions

| Action | Behavior |
|---|---|
| `block` | Raise `ToolBlocked` — halts the stage, no retry |
| `warn` | Log a Python warning, continue execution |
| `log` | Log at info level, continue execution |
| `require_approval` | Print context to stdout, prompt for `y/N`; allow on `y`, raise `ToolBlocked` on `n` |
| `allow` | Immediately allow the call — useful for whitelisting in strict mode |

Rule evaluation is first-match: the first matching rule wins and subsequent rules are not checked.
An `allow` match returns immediately without inspecting later rules.

### Top-level spec fields summary

| Field | Type | Default | Description |
|---|---|---|---|
| `safety_rules` | list | `[]` | Declarative tool safety rules |
| `safety_mode` | `"permissive"` \| `"strict"` | `"permissive"` | Default when no rule matches |

> **Attribution:** The `require_approval` action, `safety_mode: strict`, tool reversibility
> classification, and trace argument hashing are concepts borrowed from Microsoft's
> [Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit).

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
| `consensus` | Forwards all results to a judge LLM that synthesizes a single best answer |

### Consensus fan-in

When `fan_in: consensus`, the engine collects all parallel child results and calls a judge LLM (`openai/gpt-4o-mini` by default) to synthesize them into a single coherent output. This is useful when parallel agents may produce contradictory or overlapping results and you want an automated arbitrator rather than a raw merge.

```yaml
- id: parallel_analysts
  subagent_spec: workflows/analyst.yml
  fan_out: 4
  fan_in: consensus    # LLM synthesizes the 4 analyst outputs
  partition_key: documents
  depends_on: [loader]
```

The consensus judge receives all child results as JSON and is instructed to produce a synthesized dict. If the judge's response is valid JSON, it is returned directly; otherwise it is wrapped as `{"consensus_output": "...", "source_results": [...]}`.

Use `consensus` when:
- Parallel agents may disagree and you want conflict resolution, not just aggregation
- The results are semantically related and require synthesis rather than concatenation
- You want a single structured output rather than a list to pass downstream

Use `list` or `merge` when the parallel results are independent and the downstream stage should see all of them (e.g., a judge stage that deliberately synthesizes them itself).

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
2. **Quorum score:** The engine auto-extracts a `confidence`, `score`, or `quality_score` field from the judge's output and records it as the `quorum_score` on the trace. This feeds the HQS metric, the self-improvement loop, and the bootstrap few-shot selector.

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
armature new           — interactive wizard to create a new spec file
armature validate      — validate a spec file, report all errors, and show risk score
armature run           — execute a workflow from a YAML spec
armature replay        — display a recorded run stage-by-stage from the TraceStore
armature serve         — start the HTTP service
armature optimize      — analyze traces and propose spec improvements
armature improve       — self-improvement loop: analyze, diagnose, revise, apply
armature report        — print a diagnostic report for a completed run
armature dashboard     — Rich 4-panel aggregate health dashboard
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
| `--no-cache` | Bypass the LLM response cache; every LLM call goes to the provider |
| `--auto-improve` | After the run, if HQS < 0.75 automatically apply spec improvements |

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

Print a human-readable diagnostic report for a completed run, including stage-by-stage metrics, HQS score, and failure signatures.

```bash
armature report --run-id abc123
armature report --run-id abc123 --traces ~/.armature/runs/abc123/traces.db
```

---

#### `armature replay`

Display a recorded run stage-by-stage from the TraceStore. Useful for post-mortem debugging of any historical run without re-executing.

```bash
armature replay abc123-def456-...
```

Output: a Rich-formatted table showing stage id, role, model, latency, success/fail, quorum score, and HQS contribution for every stage in the run, followed by a run summary with the overall HQS and total latency.

See §27 for full details.

---

#### `armature dashboard`

Rich 4-panel aggregate health dashboard. Shows HQS trend, per-stage metrics, improvement cycle history, and safety/governance summary across all runs of a workflow.

```bash
armature dashboard my_workflow.yml           # snapshot
armature dashboard my_workflow.yml --watch   # auto-refresh every 5 seconds
armature dashboard my_workflow.yml --format json  # machine-readable
```

See §25 for full panel descriptions and all flags.

---

#### `armature improve`

Closed-loop self-improvement: loads traces, computes the Harness Quality Score (HQS), diagnoses failure signatures, and calls an LLM to produce a revised spec. If HQS is below the target and enough traces exist, the revised spec is auto-applied.

```bash
armature improve my_workflow.yml
armature improve my_workflow.yml --no-apply     # propose but don't write
armature improve my_workflow.yml --target-hqs 0.85 --min-traces 10
armature improve my_workflow.yml --model claude-opus-4-7
```

| Flag | Default | Description |
|------|---------|-------------|
| `--traces path` | `~/.armature/traces.db` | Path to trace database |
| `--model name` | `claude-sonnet-4-6` | LLM used by the spec refiner |
| `--target-hqs float` | `0.90` | HQS threshold; improvement triggered when below this |
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
```

Each tool module handles one external integration. The YAML spec wires them together with LLM reasoning stages. Armature executes the whole chain — the tool modules are domain-specific; the harness is reusable infrastructure.

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
2. **Compute rolling HQS** (Harness Quality Score) across the loaded traces ([arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1):
   - HQS = `0.35 × output_valid_rate + 0.25 × success_rate + 0.20 × avg_quorum + 0.10 × latency_score + 0.10 × hfr`
   - **HFR** (Harness-Following Rate) = fraction of traces where `escalation_count == 0`; models that consistently need to escalate to a stronger tier are not truly following harness instructions
3. **Run DiagnosticAnalyzer** to identify failure signatures — which stages are failing and how:
   - `stage_failed` — the stage raised an exception
   - `output_invalid` — the stage produced output that didn't match its schema
   - `low_confidence` — the stage's quorum score was consistently low
   - `high_escalation` — the stage frequently escalated to a larger model tier
   - `postcondition_failed` — a tool postcondition check failed after execution
   - `low_skill_activation` — the stage declared tools in `role.tools` but the model never invoked any (low Skill-Load Rate per [arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1)

   Each diagnostic carries a **causal 3-tuple** `(terminal_cause, causal_status, mechanism)` — inspired by Self-Harness ([arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1) — distinguishing *what* broke (execution error, schema validation, low confidence) from *whose fault it is* (spec problem, model problem, or tool problem) and *how* (timeout, underpowered model, missing instruction). This attribution lets the refiner propose structurally different fixes rather than generic prompt rewrites.
4. **Verify previous cycle's predictions** — compare the current diagnostic state against what the prior cycle predicted would be fixed
5. **If HQS < target and traces ≥ minimum**, call `SpecRefiner` (medium-tier LLM — frontier models are not needed for spec evolution, per [arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1) with the current spec + diagnostics + quality metrics. Three additional mechanisms (from [arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1) govern this step:
   - **Editable surfaces**: the refiner is restricted to spec fields declared in `self_improvement.editable_surfaces`; everything else is locked against modification.
   - **K-proposal diversity**: `n_proposals` parallel candidates are generated with rotating diversity hints (minimize changes, fix output format, adjust model tier, tighten schema); the candidate whose `predicted_fixes` best covers the active diagnostic codes is selected.
   - **Regression gating**: proposals that modify stages with no current diagnostics (healthy stages) are filtered as regression risks; if all candidates are risky, the best of the risky set is used as a fallback.
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
# Analyze and apply if HQS < 0.90 (default)
armature improve my_workflow.yml

# Propose only — do not write the spec
armature improve my_workflow.yml --no-apply

# Stricter threshold, more data required
armature improve my_workflow.yml --target-hqs 0.95 --min-traces 20

# Use a more capable model for refinement
armature improve my_workflow.yml --model claude-opus-4-7
```

The improvement log is written to `<spec_stem>.improve_log.jsonl` by default. Each line is a JSON object:

```json
{
  "timestamp": "2026-05-15T10:00:00Z",
  "workflow_name": "campaign-concept-gen",
  "n_traces": 47,
  "hqs_before": 0.71,
  "needs_improvement": true,
  "applied": true,
  "n_proposals_generated": 3,
  "regression_risk_count": 1,
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

`n_proposals_generated` is the number of parallel candidates produced before selection. `regression_risk_count` is the number of candidates that were filtered because they touched healthy stages; if this equals `n_proposals_generated`, the refiner consistently proposed changes to non-failing stages and the fallback selection was used.

### Python API

```python
from armature.synthesis.improve import SelfImproveRunner

runner = SelfImproveRunner(
    "my_workflow.yml",
    "~/.armature/traces.db",
    model="claude-sonnet-4-6",
    target_hqs=0.90,
    min_traces=10,
    auto_apply=True,
    n_proposals=3,       # generate 3 diverse candidates, pick best coverage
)

report = await runner.analyze()

print(f"HQS: {report.hqs_before:.3f}")
print(f"Needs improvement: {report.needs_improvement}")
print(f"Applied: {report.applied}")
print(f"Proposals generated: {report.n_proposals_generated}")
print(f"Regression-risk filtered: {report.regression_risk_count}")
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

### Editable surfaces

You can further restrict what the refiner may touch by declaring `editable_surfaces` in your spec:

```yaml
self_improvement:
  editable_surfaces:
    - descriptions    # role.description on stages
    - retry_counts    # on_fail.loop.max
    - timeouts        # stage.timeout_s
    # schemas        ← not listed → locked; refiner cannot touch output_schema
    # model_tiers    ← not listed → locked; refiner cannot change tier assignments
```

| Surface | What it covers |
|---|---|
| `descriptions` | `role.description` — prompt text for each stage |
| `schemas` | `output_schema` — JSON Schema definitions |
| `model_tiers` | `role.model_tier` — tier assignments on stages |
| `retry_counts` | `on_fail.loop.max` — retry limits |
| `timeouts` | `stage.timeout_s` — per-stage wall-clock limits |

The default is `[descriptions, retry_counts, timeouts]`. `schemas` and `model_tiers` are locked by default because they have cascading effects that require human review.

Locking a surface does not prevent the refiner from *diagnosing* problems there — it only prevents the refiner from *changing* it. A `high_escalation` diagnostic can still fire on a stage whose `model_tier` is locked; the refiner will address other surfaces instead.

This design is adapted from the Self-Harness framework ([arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1), which introduces declared editable sets to bound the harness's ability to make changes.

### Drift score

The drift score measures how many previously-verified fixes have regressed in the current cycle. A score of `0.0` means no regressions among ever-fixed failures. A score above `0.5` means more than half of failures that were once successfully fixed have returned.

```json
{
  "drift_score": 0.33,
  "verified_fixes": ["output_invalid:analyst"],
  "unexpected_regressions": ["output_invalid:summarizer"]
}
```

The score is computed against the complete improvement history — not just the previous cycle — so a failure that was fixed three cycles ago and has since returned still counts. When `drift_score > 0.5`, the refiner's prompt includes an explicit regression warning to focus it on stability rather than novelty.

The drift score appears on `ImprovementReport` and in the JSONL audit log:

```python
report = await runner.analyze()
print(f"Drift: {report.drift_score:.2f}")
```

### Component governance

Not all spec changes are equally safe to auto-apply. Changes to stage descriptions, retry limits, and timeouts are low-risk and reversible — the harness applies them immediately. Changes to stage structure (additions, removals), output schemas, or safety rules are harder to reverse and may have cascading effects — these require human review before deployment.

When the refiner proposes a spec revision that includes review-required changes, the harness does **not** overwrite the live spec. Instead it writes a staging file:

```
my_workflow.pending.yaml   ← proposed revision (not yet live)
my_workflow.yaml           ← unchanged live spec
```

`ImprovementReport` reflects this:

```python
report = await runner.analyze()
if report.requires_review:
    print(f"Review required — changes staged at: {report.pending_path}")
```

The JSONL audit log also records `requires_review: true` and the pending path. When you are ready to apply the staged revision:

```bash
# Apply the pending revision and delete the staging file
armature improve my_workflow.yaml --apply-pending
```

**Classification rules:**

| Change type | Classification |
|---|---|
| `role.description` | Auto-apply |
| `stage.timeout_s` | Auto-apply |
| `on_fail.loop.*` | Auto-apply |
| Stage added or removed | Review required |
| `stage.output_schema` changed | Review required |
| `safety_rules` modified | Review required |

If a proposed revision contains *only* auto-apply changes, the live spec is updated immediately as before. If it contains *any* review-required change, the entire revision goes to `pending.yaml`.

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

The `min_quorum_score` filter is the primary quality gate. Quorum score is auto-extracted from judge stage outputs — any field named `confidence`, `score`, or `quality_score` is recorded as the `quorum_score` on the trace. If your workflow doesn't use judge stages with scored outputs, all traces have a `null` quorum score and the filter is effectively disabled — you may want to post-process the exported JSONL manually.

A practical fine-tuning pipeline:
1. Run the workflow many times with diverse inputs to build a trace corpus
2. Export at `--min-score 0.85` for SFT, `--min-score 0.85 --rejected-max-score 0.30` for DPO
3. Fine-tune a small model (e.g. Qwen 2.5 7B) on the exported data
4. Point the workflow's `small` tier at your fine-tuned model
5. Measure cost and quality — the fine-tuned small model often matches the frontier model on specialized tasks at a fraction of the cost

---

## 22. Post-condition verification

Every tool call that completes without an exception is assumed to have succeeded — but no mechanism verifies the actual side effect. Post-condition verification closes this gap: you attach a callable to a `ToolDescriptor` that receives the tool's arguments and return value, and returns `True` (success) or `False` (failure).

### Registering a post-condition

```python
from armature.registry.registry import ToolDescriptor
from armature.permissions.permissions import PermissionLevel

async def _write_file(args: dict) -> dict:
    path = args["path"]
    content = args["content"]
    Path(path).write_text(content)
    return {"written": True, "path": path}

def _verify_file_written(args: dict, result: dict) -> bool:
    return Path(args["path"]).exists()

registry.register(ToolDescriptor(
    name="file_write",
    description="Write content to a file",
    permission=PermissionLevel.WORKSPACE,
    handler=_write_file,
    parameters={
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    postcondition=_verify_file_written,
))
```

When the engine calls this tool:
1. `_write_file(args)` is called and returns `{"written": True, "path": "..."}`.
2. `_verify_file_written(args, result)` is called. If it returns `False`, a `PostconditionFailed` exception is raised.
3. The engine catches `PostconditionFailed`, marks the trace as `success=False` with `error_type="PostconditionFailed"`, and the `DiagnosticAnalyzer` will surface a `POSTCONDITION_FAILED` failure signature on the next `armature improve` run.

### Exception type

```python
from armature.hooks.lifecycle import PostconditionFailed

try:
    result = await registry.dispatch("file_write", args)
except PostconditionFailed as e:
    print(f"Tool '{e.tool_name}' succeeded but its side effect was not verified")
    print(f"Return value: {e.result}")
```

### When to use

Post-conditions are most valuable for:
- **File writes** — verify the file now exists (or has the expected size/hash)
- **HTTP POSTs** — verify the resource was actually created (status code, response body check)
- **Database writes** — verify the row exists after insert
- **External API calls** — verify the external state changed as expected

For read-only tools (`file_read`, `http_get`), post-conditions are rarely needed.

### Diagnostic integration

A `PostconditionFailed` trace produces a `POSTCONDITION_FAILED` diagnostic code:

```json
{
  "code": "postcondition_failed",
  "stage_id": "file_writer",
  "details": "tool postcondition failed"
}
```

This is surfaced in `armature report` output and drives `armature improve` refinement targeting the affected stage.

---

## 23. Context provenance

Every context key carries a hidden history: where did this value come from? Was it a user input? A prior stage's output? A memory entry? Context provenance makes this visible in the trace record, enabling post-hoc audits of data flow through a workflow.

### How it works

The engine maintains a provenance dict alongside the context dict. Every key is labelled with its origin:

| Label | Meaning |
|---|---|
| `"user_input"` | Passed directly in `harness.run(inputs)` |
| `"stage:{stage_id}"` | Output from a prior stage |
| `"memory"` | Loaded from cross-run memory store |
| `"stale_memory"` | Loaded from memory store but flagged as stale |

### Reading provenance from traces

Provenance is persisted on every `TraceRecord` as `inputs_provenance: dict[str, str]`:

```python
from armature.state.traces import TraceStore
from pathlib import Path

store = TraceStore(Path("~/.armature/traces.db").expanduser())
await store.init()

traces = await store.query(workflow_name="my_workflow")
for trace in traces:
    print(f"Stage {trace.stage_id}:")
    for key, source in trace.inputs_provenance.items():
        print(f"  {key!r} came from {source!r}")
```

Example output:
```
Stage analyst:
  'topic' came from 'user_input'
  'researcher' came from 'stage:researcher'
  '_memory' came from 'memory'
```

### Audit use cases

- **Compliance audits:** Prove that a decision stage only saw inputs from approved sources.
- **Debug data contamination:** Find stages where a memory key unexpectedly overrode a fresher stage output.
- **Staleness tracing:** Identify which stages received stale memory entries by filtering on `"stale_memory"` labels.
- **Information flow analysis:** Reconstruct the complete data lineage of any context key at any stage.

---

## 24. Memory staleness

Memory entries accumulate across many runs. An entry captured six months ago may no longer reflect reality. Staleness detection surfaces this automatically.

### Configuration

Set `staleness_threshold_days` when constructing `MemoryStore` directly, or rely on the engine default of 30 days:

```python
from armature.state.memory import MemoryStore
from pathlib import Path

store = MemoryStore(
    db_path=Path("~/.armature/memory/my_workflow.db"),
    staleness_threshold_days=14.0,  # flag entries older than 2 weeks
)
memories, stale_keys = await store.load("my_workflow")
```

`stale_keys` is a `set[tuple[str, str]]` — each tuple is `(stage_id, capture_key)` for an entry that exceeded the threshold.

### Engine behaviour

When `Harness` loads memory at run start, stale keys are injected into context automatically:

```python
context["_stale_memory_keys"] = [
    "synthesizer.recommendation",  # format: "stage_id.capture_key"
]
```

This key is always present — it is an empty list `[]` when no stale entries exist. You can always reference it in Jinja2 without a guard:

```yaml
description: |
  {% if _stale_memory_keys %}
  Note: the following memory entries may be outdated:
  {{ _stale_memory_keys | join(", ") }}
  Weight prior recommendations accordingly.
  {% endif %}
```

### Relationship to context provenance

Stale memory keys are labelled `"stale_memory"` in `TraceRecord.inputs_provenance`. This means you can query the trace store to find exactly which stages in a run received stale context:

```python
stale_stages = [
    trace.stage_id
    for trace in traces
    if any(v == "stale_memory" for v in trace.inputs_provenance.values())
]
```

### Tuning the threshold

| Workflow type | Recommended threshold |
|---|---|
| Daily batch jobs | 7–14 days |
| Weekly strategic analysis | 30–60 days |
| Ad-hoc one-off runs | Disable memory or set `fresh: true` |
| Regulatory compliance | 0 days (always treat as stale) |

A threshold of `0.0` flags every memory entry as stale — useful for workflows where any cross-run carryover must be disclosed to the LLM explicitly.

---

## 25. Workflow health dashboard

`armature dashboard` renders a Rich terminal dashboard aggregating data across multiple runs of a workflow. It is the primary observability surface for developers running Armature workflows in production.

### Quick start

```bash
# Show dashboard for a spec file (reads workflow name from the spec)
armature dashboard my_workflow.yml

# Or reference by workflow name directly
armature dashboard --workflow my-workflow-name

# Auto-refresh every 5 seconds (Ctrl-C to quit)
armature dashboard my_workflow.yml --watch

# Machine-readable JSON output
armature dashboard my_workflow.yml --format json
```

### Panels

The dashboard renders four panels:

**Health strip** (top, full width) — HQS gauge, delta arrow vs. prior run, and a Unicode sparkline showing HQS trend across the last 50 runs. Color: green ≥ 0.85, yellow 0.70–0.84, red < 0.70.

**Stage breakdown** (left) — Per-stage table showing failure rate, average latency, quorum score, and escalation rate. Rows colored by health: red for ≥ 20% failure or quorum < 0.50, yellow for borderline, green for healthy. Post-run stages are rendered dim.

**Improvement cycles** (right top) — Improvement log history, newest first. Shows per-cycle HQS, drift score, applied/pending status, and prediction verification counts. High drift (> 0.5) and pending reviews are highlighted.

**Safety & governance** (right bottom) — Policy version stability, rule hit counts by action type, post-condition failure count, and stale memory key count.

### Options

| Flag | Default | Description |
|---|---|---|
| `--last N` | `200` | Number of most recent traces to aggregate |
| `--watch` | off | Auto-refresh every `--interval` seconds |
| `--interval` | `5.0` | Refresh interval for `--watch` mode |
| `--format json` | terminal | Output machine-readable JSON instead of Rich panels |
| `--traces` | `~/.armature/traces.db` | Override traces database path |
| `--log` | `{spec}.improve_log.jsonl` | Override improvement log path |

### JSON output

When `--format json`, the dashboard emits a structured dict:

```json
{
  "workflow_name": "research-pipeline",
  "total_runs": 47,
  "current_hqs": 0.82,
  "health_color": "yellow",
  "hqs_delta": 0.03,
  "hqs_trend": [0.70, 0.74, 0.79, 0.82],
  "stage_stats": {
    "researcher": {"run_count": 47, "failure_rate": 0.02, "avg_latency_ms": 1200, ...}
  },
  "improvement_cycles": [...],
  "safety": {
    "warn_hits": 4, "block_hits": 0, "postcondition_failures": 0,
    "stale_memory_count": 2, "current_policy_version": "a3f7b2d1"
  }
}
```

This is suitable for ingestion into monitoring systems, Grafana dashboards, or CI health checks:

```bash
# Fail CI if workflow HQS drops below 0.80
hqs=$(armature dashboard my_workflow.yml --format json | jq '.current_hqs')
python -c "import sys; sys.exit(0 if $hqs >= 0.80 else 1)"
```

---

## 26. LLM response caching

<!-- AI-AGENT-NOTE: This section describes content-addressed caching of LLM API calls. An agent reading this can reduce cost and latency of repeated runs by understanding cache keying and bypass options. -->

Armature caches LLM responses by content hash. On every LLM call, the harness computes a SHA-256 key over the model name, the full messages array, and any response-format kwargs. If an entry exists in the cache for that key, the stored response is returned immediately — no API call, no cost, instant result. If not, the API call proceeds and the response is written to the cache before being returned.

The cache is stored in `{armature_runs_dir}/llm_cache.sqlite`. It persists across runs.

### When to use it

**Default behavior (cache enabled):** Re-running the same workflow with the same inputs is instant after the first run. Useful for:
- Development iteration — tweak one stage and re-run; unchanged stages are instant
- Deterministic audit replay — re-run a historical workflow and get bit-for-bit identical LLM outputs
- Cost control — catch regressions in a CI pipeline without paying for every LLM call

**Bypass the cache:** Pass `--no-cache` to force fresh API calls for every stage.

```bash
armature run my_workflow.yml --no-cache
```

Use `--no-cache` when:
- You explicitly want fresh model responses (e.g., production runs after a model update)
- You are testing that a workflow performs well under current model behavior, not cached behavior
- The inputs have changed enough that cached responses would be misleading

### Cache keying

Two runs produce a cache hit if and only if all of the following are identical:
- The LLM model name (e.g., `claude-sonnet-4-6`)
- The full messages array passed to the API (system prompt + assembled context + user turn)
- Any response-format kwargs (guided JSON schema, if used)

A cache miss occurs if:
- A different model tier is configured
- The context dict changed (e.g., a prior stage produced a different output)
- The system prompt changed (stage role description, skill injections, etc.)
- The guided JSON schema changed

### Programmatic access

```python
from armature.cache.llm_cache import LLMCache

cache = LLMCache(db_path=Path("~/.armature/runs/llm_cache.sqlite").expanduser())
await cache.init()

key = cache._make_key(model="claude-sonnet-4-6", messages=[...], extra_kwargs={})
cached = await cache.get(key)  # returns raw response JSON or None
```

### Disabling cache in the harness

```python
harness = Harness.from_spec("my_workflow.yml", use_cache=False)
```

---

## 27. Audit replay

<!-- AI-AGENT-NOTE: armature replay reads the TraceStore and renders past runs without re-executing. An agent can use this to inspect historical behavior, compare runs, or debug a failure by replaying the exact sequence of stage outputs. -->

`armature replay <run_id>` reads TraceStore records and renders a stage-by-stage execution table for any historical run. No LLM calls are made; this is a pure read from the trace database.

### Usage

```bash
armature replay <run_id>
```

`run_id` is the UUID printed at the start of every `armature run`. You can also retrieve it from the session log (`*.session.jsonl`) or from `armature report --run-id`.

### Output

```
Run: abc123-def456  (my-research-pipeline)
────────────────────────────────────────────────────────────────────────
 Stage           Role        Model                Latency  OK  Quorum
────────────────────────────────────────────────────────────────────────
 researcher      researcher  claude-sonnet-4-6    1,241ms  ✓   0.91
 analyst         worker      claude-haiku-4-5     892ms    ✓   0.87
 judge           judge       claude-sonnet-4-6    1,103ms  ✓   0.94
────────────────────────────────────────────────────────────────────────
 HQS: 0.88   Total latency: 3,236ms   Stages: 3   Failed: 0
```

Each row also shows the truncated inputs and outputs (200 characters) when `--verbose` is passed.

### Use cases

**Post-mortem debugging:** A run failed in production. Replay shows exactly which stage failed, what its inputs were, and what output it produced — without re-executing the expensive upstream stages.

**Audit trail:** Compliance requires a record of what each LLM stage received and produced. `armature replay` makes that record human-readable.

**Regression investigation:** Compare two replays (before/after a spec change) to see which stages changed behavior.

### Relationship to the TraceStore

`armature replay` reads from the same SQLite TraceStore that `armature dashboard` and `armature report` use. The trace database is at `~/.armature/runs/{run_id}/traces.db` by default. Override with `--traces path`.

Every stage in every run is recorded automatically — no configuration required. Traces are immutable once written.

---

## 28. Trace-triggered behaviors

<!-- AI-AGENT-NOTE: BehaviorRule and BehaviorRegistry allow post-run reactive logic tied to trace history patterns. An agent building workflows should know that the hqs_feedback behavior fires automatically after low-quality runs, and that custom rules can trigger any arbitrary handler (alerting, escalation, auto-export, etc.). -->

The `BehaviorRegistry` lets you register rules that fire after a run completes, based on patterns in the recent trace history. This is how Armature reacts to observed quality trends rather than individual run events.

### BehaviorRule

```python
from armature.hooks.lifecycle import BehaviorRule, BehaviorRegistry

rule = BehaviorRule(
    name="my_rule",
    description="Fire when something happens",
    pattern=lambda traces: len(traces) > 0 and traces[-1].hqs < 0.5,
    handler=lambda traces: print("HQS critically low — investigate"),
)
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Unique rule identifier |
| `description` | `str` | Human-readable description |
| `pattern` | `Callable[[list[TraceRecord]], bool]` | Returns True when the rule should fire |
| `handler` | `Callable[[list[TraceRecord]], None]` | Called with the trace list when pattern matches |

### BehaviorRegistry

```python
registry = BehaviorRegistry()
registry.register(rule)

# Evaluate all registered rules against recent traces
registry.evaluate(traces)
```

Rules are evaluated in registration order. All matching rules fire; there is no short-circuit.

### Built-in: `hqs_feedback`

The `hqs_feedback` behavior is registered automatically when the harness initializes its default registry. Pattern: rolling HQS below 0.75 over the last 10 traces (minimum 3 required to avoid false alerts on first run). Handler: prints a Rich-formatted hint suggesting `armature improve <spec>`.

```
HQS hint: quality below 0.75 — consider running `armature improve my_workflow.yml`
```

This fires after the run summary, before the CLI exits.

### Custom behaviors

Register additional behaviors by providing a pre-populated `BehaviorRegistry` to the harness:

```python
from armature.hooks.lifecycle import BehaviorRule, BehaviorRegistry

registry = BehaviorRegistry()

registry.register(BehaviorRule(
    name="alert_on_failure_spike",
    description="Page oncall when failure rate exceeds 30% over last 20 traces",
    pattern=lambda traces: (
        len(traces) >= 20
        and sum(1 for t in traces[-20:] if not t.success) / 20 > 0.30
    ),
    handler=lambda traces: send_alert("Failure spike detected in workflow"),
))

harness = Harness.from_spec("my_workflow.yml", behavior_registry=registry)
```

### What behaviors can do

Handlers receive the full trace list and can:
- Print warnings or hints to the terminal
- Write to an external alerting system (PagerDuty, Slack, etc.)
- Trigger `armature improve` programmatically
- Export traces for fine-tuning when quality is high
- Update a dashboard or monitoring metric

Handlers are synchronous and run after the run summary is emitted. They do not affect the run result or HQS score.

---

## 29. Auto self-improvement

<!-- AI-AGENT-NOTE: --auto-improve on `armature run` connects the execution loop to the self-improvement loop automatically. An agent deploying Armature workflows in production should understand when auto-improve fires, what it does, and how to review pending proposals. -->

`--auto-improve` on `armature run` connects the execution loop to the self-improvement loop automatically. If HQS drops below 0.75 after a run, Armature calls `SelfImproveRunner.analyze()` without requiring a separate `armature improve` invocation.

### Usage

```bash
armature run my_workflow.yml --input topic="AI safety" --auto-improve
```

### What happens

1. The workflow executes normally.
2. After the run summary is printed, Armature checks the HQS.
3. If HQS ≥ 0.75: `Auto-improve: workflow is healthy — no improvement needed.`
4. If HQS < 0.75: `SelfImproveRunner.analyze()` runs.
   - **Safe changes** (prompt wording, temperature, retry count): applied directly to the spec file. `Auto-improve: spec updated → my_workflow.yml`
   - **Structural changes** (adding/removing stages, changing DAG topology): written to `my_workflow.pending.yaml` for human review. `Auto-improve: structural changes require review → my_workflow.pending.yaml`
   - **No valid revision found**: `Auto-improve: refiner could not produce a valid revision.`

### Applying a pending revision

```bash
armature improve my_workflow.yml --apply-pending
```

This promotes `my_workflow.pending.yaml` into `my_workflow.yml` after you have reviewed it.

### What self-improvement changes

Armature's self-improvement refiner targets specific spec components based on the 4-code failure taxonomy:

| Failure code | What the refiner modifies |
|---|---|
| `stage_failed` | Retry count, `on_fail` strategy, `depends_on` ordering |
| `output_invalid` | Guided JSON schema, output mode, judge prompt stringency |
| `low_confidence` | System prompt clarity, context filtering (`signature.input`) |
| `high_escalation` | Model tier assignment, temperature settings |

Cross-run memory, safety rules, and DAG topology are classified as structural changes and always require human review before being applied.

### Relationship to `armature improve`

`--auto-improve` calls the same `SelfImproveRunner` that `armature improve` calls. The difference is timing and trigger:
- `armature improve` is a manual, on-demand command that always runs regardless of HQS
- `--auto-improve` is automatic and only fires when HQS drops below 0.75

For development workflows where you want to catch regressions immediately, `--auto-improve` is the right choice. For production workflows where you want control over when the spec changes, use `armature improve` on a schedule.

---

## 30. Spec risk scoring

<!-- AI-AGENT-NOTE: `armature validate` now outputs a risk tier (LOW/MEDIUM/HIGH/CRITICAL) derived from a static analysis of the spec. An agent reviewing or generating specs should understand the five risk factors and aim for LOW or MEDIUM tier before deploying to production. -->

`armature validate` computes a static risk score for every spec it validates, surfacing potential governance concerns before a workflow is ever run.

### Risk tiers

| Tier | Score range | Meaning |
|------|-------------|---------|
| `LOW` | 0–29 | Well-governed; safe to deploy without additional review |
| `MEDIUM` | 30–59 | Moderate risk; review safety rules and judge coverage before production |
| `HIGH` | 60–84 | Significant governance concerns; human review required |
| `CRITICAL` | 85–100 | Multiple unmitigated risks; do not deploy without remediation |

### Risk factors

Five factors contribute to the score:

| Factor | Delta | Trigger |
|--------|-------|---------|
| Tool-call stage | +4 per stage | Each `tool_call` stage that can invoke external systems |
| No judge | +15 | Workflow has no stage with `type: judge` |
| Require-approval rule | +8 per rule | Each safety rule with `action: require_approval` (human bottleneck risk) |
| Fan-out stage | +6 per stage | Each subagent stage with fan-out — amplifies tool-call risk |
| Strict mode | −10 | `safety_mode: strict` in spec — strong mitigating control |

Scores are clamped to [0, 100].

### Usage

```bash
armature validate my_workflow.yml
```

Output (on success):

```
✓ my_workflow.yml is valid
Risk: MEDIUM (score 42)
  • tool_call stages: 3 (+12)
  • no judge stage: +15
  • fan-out stages: 1 (+6)
  • strict mode: -10
  • require_approval rules: 2 (+16)
```

Validation errors are printed before the risk score. If validation fails (exit code 1), the risk score is not shown.

### Reducing risk

To lower the risk tier:
1. **Add a judge stage** — a `type: judge` role that reviews LLM outputs before they flow downstream removes the largest single penalty (+15)
2. **Enable strict mode** — add `safety_mode: strict` to the spec root for an immediate −10 discount
3. **Minimize fan-out for tool-heavy workflows** — each fan-out stage multiplies the blast radius of any tool-call error

A `LOW` score does not mean the workflow is safe; it means the spec structure has the right governance primitives in place. The actual safety depends on the tool implementations and the quality of the safety rules.

### Programmatic access

```python
from armature.spec.risk import compute_spec_risk

result = compute_spec_risk(spec)
print(result.score)   # int, 0-100
print(result.tier)    # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
for factor in result.factors:
    print(f"{factor.label}: {factor.delta:+d}")
```

---

## 31. Rogue signal tracking

<!-- AI-AGENT-NOTE: The rogue signal counter counts ToolBlocked events during a run. An agent monitoring workflows should watch for non-zero rogue_signals in the run_summary event — it indicates the workflow tried to call tools it wasn't allowed to call, which may indicate prompt injection, spec misconfiguration, or an unexpected model behavior. -->

A **rogue signal** is any tool call that the safety subsystem blocked at runtime. The harness counts these across the entire run and reports the total in the run summary.

### What counts as a rogue signal

- A `tool_call` stage or LLM-initiated tool call that matches a `block` safety rule
- A tool call blocked by `safety_mode: strict` (fail-closed mode — no matching rule means block)
- A `require_approval` gate that was refused by the human approver

Each of these increments the `rogue_signals` counter by 1. Warnings and log-only actions do not increment the counter.

### Where it appears

**CLI run output:**

```
Run complete  HQS: 0.83  stages: 4  time: 3.2s  [2 blocked]
```

The `[N blocked]` suffix appears only when `rogue_signals > 0`.

**`run_summary` event (SessionLog):**

```json
{
  "event": "run_summary",
  "run_id": "abc123",
  "hqs": 0.83,
  "rogue_signals": 2,
  ...
}
```

### Interpreting rogue signals

**Zero blocked:** All tool calls were permitted. Expected behavior.

**1–2 blocked:** Usually a safety rule triggering as designed (e.g., a `block` rule on `shell` preventing risky commands). Verify the rule is firing on the intended input.

**3+ blocked in a single run:** Investigate. Possible causes:
- The model is trying to call tools repeatedly after being blocked (retry loop)
- A prompt injection attack is attempting to expand tool permissions
- The spec has a safety rule that is too broad, blocking legitimate tool use
- A new model version has changed tool-calling behavior

**Rogue signals with failing stages:** If blocked tool calls are causing stage failures, the workflow may need its safety rules relaxed or its prompts clarified to prevent the model from attempting blocked operations.

### Programmatic access

```python
from armature.hooks.lifecycle import RogueSignalCounter

counter = RogueSignalCounter()
# Pass to SafetyHookBuilder.register(counter=counter)
# After the run:
print(counter.count)  # int — total blocked calls
```

---

## 32. Safety rule composition

<!-- AI-AGENT-NOTE: The only-tighten principle prevents an allow rule from undoing a block rule. An agent generating or modifying safety rules must ensure that allow rules only grant permissions not already denied. The CONFLICTING_SAFETY_RULES validation error fires at validate time, before any execution. -->

Safety rules compose under a strict monotonicity constraint: **rules may only tighten constraints, never loosen them.** This is the "only-tighten" principle, adapted from the KYA governance framework.

### The only-tighten principle

A safety rule with `action: allow` is only valid if it does not override or weaken an existing `action: block` rule for the same tool. If you have blocked a tool at the workflow level, no stage-level allow rule can re-enable it.

This prevents a common failure mode in layered safety systems: an inner allow overrides an outer block, quietly creating a permission escalation that the outer rule was designed to prevent.

### Validation

`armature validate` checks for conflicting rules and reports `CONFLICTING_SAFETY_RULES`:

```
ERROR CONFLICTING_SAFETY_RULES: Safety rule with action='allow' for tool 'shell'
may loosen an existing block rule — review rule ordering (only-tighten principle)
```

Exit code 1 is returned; the workflow cannot run until the conflict is resolved.

### Example

**Invalid — allow overrides block:**

```yaml
safety_rules:
  - tool: shell
    action: block
    description: "Block shell access globally"

  - tool: shell          # ERROR: allow on a blocked tool
    action: allow
    conditions:
      - field: command
        operator: contains
        value: "ls"
```

**Valid — no conflicting rules:**

```yaml
safety_rules:
  - tool: shell
    action: block

  - tool: file_read       # allow on a different tool — no conflict
    action: allow
```

**Valid — block with conditions instead of a blanket block + allow:**

```yaml
safety_rules:
  - tool: shell
    action: block
    conditions:
      - field: command
        operator: matches_regex
        value: "rm\\s+-rf"   # block only destructive removes

  # No allow rule needed — the block is already narrowly scoped
```

### Wildcard blocks

A wildcard block (`tool: "*"`) triggers the conflict check for any subsequent allow rule targeting any tool:

```yaml
safety_rules:
  - tool: "*"
    action: block          # block all tools

  - tool: file_read        # ERROR: allow after wildcard block
    action: allow
```

If you need to allow specific tools after a blanket block, restructure as an explicit allowlist: list only the permitted tools with `action: allow` and set `safety_mode: strict` (which denies everything not explicitly allowed) instead of using a wildcard block rule.

### Relationship to `safety_mode: strict`

`safety_mode: strict` is the recommended alternative to a wildcard block rule. It denies tool calls with no matching rule (fail-closed) without requiring an explicit block entry. Specific tools are then permitted with `action: allow` rules — no conflict arises because strict mode is enforced at the engine level, not via a rule entry.

```yaml
safety_mode: strict

safety_rules:
  - tool: file_read
    action: allow
  - tool: http_get
    action: allow
    conditions:
      - field: url
        operator: contains
        value: "api.internal"
```

This pattern is the cleanest way to express "allow only these specific tools" without triggering `CONFLICTING_SAFETY_RULES`.

---

---

## 33. Mission context for long-horizon workflows

**Problem.** In a workflow that runs for hours or days — dozens of stages, hundreds of LLM calls — individual agents drift. A researcher three stages deep into a competitive analysis starts optimizing for thoroughness rather than the original deliverable. A worker on iteration 40 of a document-processing loop stops connecting its output to the business goal stated at the top of the run. This is agent focus drift: the inability of individual stages to stay anchored to the stated objective as execution lengthens.

**Solution.** Declare a `mission:` at the workflow level. Armature injects it into every LLM stage's system prompt automatically — as the very first block, before role instructions and context — along with a compact breadcrumb of what prior stages have produced.

### Declaring a mission

```yaml
name: competitive-analysis
mission: |
  Produce a defensible market positioning report for Acme Corp's Q3 board review.
  Ground every finding in publicly available data. Flag any claim that cannot be
  sourced. The final deliverable is a structured JSON document consumable by the
  reporting pipeline.

stages:
  - id: gather_competitors
    role:
      name: researcher
      type: researcher
      description: Find the top 10 competitors and their key differentiators.
    depends_on: []

  - id: analyze_strengths
    role:
      name: analyst
      type: worker
      description: Synthesize competitor strengths and weaknesses.
    depends_on: [gather_competitors]

  - id: position
    role:
      name: strategist
      type: judge
      description: Score positioning options against the mission criteria.
    depends_on: [analyze_strengths]
```

### What each LLM stage receives

Every LLM stage's system prompt opens with:

```
[Workflow Mission]
Produce a defensible market positioning report for Acme Corp's Q3 board review.
Ground every finding in publicly available data. Flag any claim that cannot be
sourced. The final deliverable is a structured JSON document consumable by the
reporting pipeline.

[Prior stages]
• gather_competitors → {"competitors": ["CompA", "CompB"], "count": 7, ...
• analyze_strengths → {"top_strengths": ["pricing", "support"], "gap": ...

## Your Role
...
```

The `[Prior stages]` breadcrumb is built dynamically — each stage sees only the outputs of stages that completed before it (first 200 characters of JSON per stage). Stage 1 sees no breadcrumb. Stage 10 sees nine entries.

### Properties

| Property | Behaviour |
|---|---|
| **Scope** | Applies to all LLM stages in the workflow |
| **Non-LLM stages** | Script, direct tool call, gate, and subagent stages are unaffected |
| **Ordering** | Mission block is the first thing in the system prompt — before role preambles, skills, and context |
| **Default** | `mission: ""` — omitting it is a no-op; no block is injected |
| **Prior stage breadcrumb** | Built per-call from the accumulated context; only completed stages appear; truncated at 200 chars per stage |

### When to use it

Use `mission:` whenever:

- The workflow runs for more than a handful of stages
- Stages have different roles (researcher → worker → judge) and need to share a common north star
- The end deliverable has specific constraints (format, sourcing rules, audience) that must survive the full pipeline
- You are running the same workflow repeatedly over days and want all future runs to stay aligned

For short, single-purpose workflows (two or three tightly coupled stages), the overhead is minimal but also unnecessary.

### Relationship to Steward (the meta-coordinator)

`mission:` addresses *within-workflow* focus — keeping all stages in a single spec run anchored to the stated goal. For *cross-workflow* orchestration (a plan that spans multiple Armature workflow executions over days), the Steward layer handles plan persistence, context compression at handoff, and re-planning triggers. These are complementary: Steward sets the high-level mission for each Armature run; Armature's `mission:` field propagates that goal into every LLM call within the run.

---

## 34. Low-latency / streaming stages

Standard Armature workflows complete fully before returning a result — fine for batch jobs and background pipelines, but noticeable when a human is waiting on a conversational reply. The `response_stage: true` flag marks a single LLM stage as the streaming response point: its tokens are forwarded to the SSE event stream token-by-token, so the client can start rendering before the stage finishes, let alone before background stages complete.

### The problem: batch latency in interactive workflows

```
gather_context → respond → log_analytics
                   ↑
          user is waiting here
```

Without streaming, the user waits for the entire `respond` stage to finish (plus `log_analytics`). With `response_stage: true`, the user sees the first token within milliseconds of the model starting to generate, and `log_analytics` runs invisibly in parallel.

### Declaring a response stage

```yaml
stages:
  - id: respond
    response_stage: true
    role:
      type: worker
      prompt: "Answer the user's question concisely."
```

That is the only change. The engine detects the flag, enables litellm token streaming for that stage, and hooks up the token → SSE pipeline automatically.

### SSE event sequence

When the HTTP service (`POST /run/async` + `GET /run/{job_id}/events`) executes a workflow with a response stage:

```
data: {"type": "stage_start",             "stage_id": "respond"}
data: {"type": "token",    "content": "Sure"}
data: {"type": "token",    "content": ", here is the answer"}
data: {"type": "token",    "content": "…"}
data: {"type": "response_stage_complete",  "stage_id": "respond", "content": "<full assembled text>"}
data: {"type": "stage_complete",           "stage_id": "respond"}
… background stages continue …
data: {"type": "run_complete"}
```

| Event | When | Purpose |
|---|---|---|
| `token` | Each chunk from the model | Progressive rendering |
| `response_stage_complete` | Stage done; full text available | Signal to display the answer |
| `stage_complete` | After all hooks fire | Standard completion marker |
| `run_complete` | All stages done | Full result available via `GET /run/{job_id}` |

The client should render on `response_stage_complete` — it carries the fully assembled `content` field so there's no need to concatenate individual `token` events.

### Constraints

- **Text mode only.** A stage with `output_mode: json` or `output_mode: guided_json` cannot stream meaningfully — the full JSON payload must be assembled before parsing. If `response_stage: true` is set on a JSON-mode stage it is silently ignored and the normal non-streaming path runs.
- **One response stage per workflow.** Designating multiple stages as `response_stage: true` is technically valid YAML but only the stages that actually run will emit streaming events. For clarity, keep it to one.
- **No tier escalation during streaming.** The streaming path uses the primary tier for the stage (no retry, no escalation). Design the response stage for reliability: short prompt, well-tested model.
- **CLI use.** `armature run` does not stream to the terminal — `response_stage: true` is a no-op for CLI execution. It only takes effect when the workflow runs through the HTTP service.

### Combining with `mission:`

`mission:` and `response_stage: true` are orthogonal and compose naturally. A chat workflow that needs long-horizon focus can use both:

```yaml
mission: "Provide accurate, concise answers about Acme's Q3 financials."

stages:
  - id: retrieve
    role:
      type: researcher
      prompt: "Retrieve relevant financial data."

  - id: respond
    response_stage: true
    depends_on: [retrieve]
    role:
      type: worker
      prompt: "Answer the user's question using the retrieved data."
```

Every stage receives the mission block in its system prompt; `respond` additionally streams its tokens to the client.

---

## 35. Continuation — rolling memory across runs

**Problem.** Every `Harness.run()` starts from a clean slate. A daily monitor workflow, a recurring competitive analysis, or a conversational agent has no memory of what it concluded yesterday unless you build that plumbing yourself.

**Solution.** Declare a `continuation:` block naming which stage outputs to carry forward. On every activation after the first, the Harness queries the prior run's traces, assembles the named values, and injects them into the context as `prior_run` (or any name you choose). Every stage that references `prior_run` in its prompt or `signature.input` can reason about what the workflow concluded last time.

### Spec syntax

```yaml
continuation:
  carry_forward:
    - key: monitor.summary          # stage_id.output_key dotted notation
    - key: analyst.recommendations
  inject_as: prior_run              # default; any valid context key name
```

Each `key` is `stage_id.output_key`. The engine looks up the most recent successful run's traces, extracts those values, and merges them into a single dict that gets injected at the start of every stage's context.

### How a stage uses it

```yaml
stages:
  - id: monitor
    role:
      name: Monitor
      type: worker
      description: |
        Analyse today's signals. Prior run summary: {{ prior_run.summary }}
        Identify what has changed since then and what still holds.
    signature:
      input:
        prior_run: "Prior run context (absent on first activation)"
```

On the **first run**, `prior_run` is absent from context — the stage simply doesn't see it. On **subsequent runs**, `prior_run.summary` contains whatever the `monitor` stage returned in the previous activation.

### Output storage cap

Armature normally truncates stored outputs at 200 characters to keep the trace DB lean. For stages that appear in `carry_forward`, the cap is automatically raised to **2000 characters** so values survive the round-trip through TraceStore. No configuration required.

### Full example: daily monitor

```yaml
name: daily-monitor
version: "1.0"
mission: "Track signal changes across activations and surface meaningful drift."

continuation:
  carry_forward:
    - key: monitor.summary
    - key: analyst.alert_level
  inject_as: prior_run

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001

stages:
  - id: monitor
    role:
      name: Monitor
      type: worker
      description: |
        Compare today's signals against prior context.
        Prior summary: {{ prior_run.summary | default('No prior run.') }}
        Prior alert level: {{ prior_run.alert_level | default('unknown') }}
        Return a new summary and alert_level (low/medium/high).

  - id: analyst
    role:
      name: Analyst
      type: judge
      description: |
        Review the monitor's assessment. Return alert_level and recommendations.
    depends_on: [monitor]
```

Run this with `armature run` or `armature watch` (§36). The second activation automatically sees what the first run concluded.

### Properties

| Property | Behaviour |
|---|---|
| `carry_forward` | List of `stage_id.output_key` dotted strings |
| `inject_as` | Context key where the assembled dict is injected (default: `prior_run`) |
| **First run** | No prior run → `inject_as` key is absent from context |
| **Subsequent runs** | Most recent prior run's outputs are queried from TraceStore |
| **Output cap** | 200 chars for normal stages; 2000 chars for stages in `carry_forward` |
| **Missing keys** | If a carry-forward key wasn't produced in the prior run, it is silently omitted |

---

## 36. Triggers — cron and webhook activation

**Problem.** Long-horizon workflows need to be woken by an event — a scheduled time, an inbound HTTP call, a new file — not a human typing `armature run`. Manually scheduling cron jobs or writing webhook handlers for each workflow is boilerplate that belongs in the framework.

**Solution.** Declare a `triggers:` list in the spec. Then run `armature watch <spec>` — a daemon that starts one listener per trigger and fires `Harness.run()` on each event, injecting the trigger payload into context. Combined with `continuation:` (§35), this turns a one-shot workflow into a persistent, self-aware agentic team.

### Spec syntax

```yaml
triggers:
  - type: cron
    schedule: "0 9 * * *"       # standard 5-field cron expression
  - type: webhook
    path: /webhook/my-workflow   # path the HTTP listener exposes
```

Multiple triggers of different types can coexist. The spec is validated at load time — malformed expressions are caught by `armature validate` before the daemon starts.

#### Cron trigger

```yaml
triggers:
  - type: cron
    schedule: "0 6 * * 1-5"   # 6 AM Mon-Fri
```

Uses standard 5-field cron syntax (`minute hour dom month dow`). The daemon computes the next fire time using `croniter`, sleeps until then, fires `Harness.run({"trigger_payload": {}})`, then loops.

#### Webhook trigger

```yaml
triggers:
  - type: webhook
    path: /webhook/my-workflow
```

The daemon starts a Starlette HTTP server (default port 8081). A `POST` to `/webhook/my-workflow` fires the workflow with the request body passed as `trigger_payload.body`:

```bash
curl -X POST http://localhost:8081/webhook/my-workflow \
     -H "Content-Type: application/json" \
     -d '{"event": "new_data", "source": "pipeline"}'
```

Inside the workflow, the payload is available as `{{ trigger_payload.body.event }}`, `{{ trigger_payload.body.source }}`, etc.

### Running the daemon

```bash
armature watch my_workflow.yml               # default port 8081
armature watch my_workflow.yml --port 9000   # custom webhook port
armature watch my_workflow.yml --quiet       # suppress per-run output
armature watch my_workflow.yml --traces /path/to/traces.db
```

The daemon blocks until `Ctrl-C`. Each trigger fires independently — a cron tick and an inbound webhook can run concurrently.

### CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `<spec>` | required | Path to workflow spec YAML |
| `--host` | `0.0.0.0` | Bind address for the webhook listener |
| `--port`, `-p` | `8081` | Port for webhook triggers |
| `--traces` | `~/.armature/traces.db` | Path to the traces SQLite database |
| `--quiet`, `-q` | off | Suppress per-run completion output |

### Combining with `continuation:`

This is the intended pairing for long-horizon workflows:

```yaml
name: market-monitor
version: "1.0"

continuation:
  carry_forward:
    - key: analyst.summary
    - key: analyst.alert_level
  inject_as: prior_run

triggers:
  - type: cron
    schedule: "0 8 * * 1-5"   # every weekday at 8 AM

stages:
  - id: analyst
    role:
      name: Analyst
      type: worker
      description: |
        Analyse today's market signals.
        Prior summary: {{ prior_run.summary | default('First run.') }}
        Return summary and alert_level.
```

Each weekday morning the daemon fires the workflow. The analyst sees what was concluded the previous trading day and can identify drift, escalation, or resolution without any external state management.

### Properties

| Property | Behaviour |
|---|---|
| **Cron** | `croniter` computes next fire time; sleeps between ticks |
| **Webhook** | Starlette HTTP server; one POST route per `WebhookTrigger.path` |
| **Payload** | Injected as `trigger_payload` in context (`body` + `path` for webhooks) |
| **Concurrency** | Each trigger runs as an independent asyncio task |
| **Validation** | `triggers:` is parsed and validated at spec load; `armature validate` catches errors |
| **No triggers** | `armature watch` exits with an error if the spec declares no triggers |

---

## 37. Named workflow registry

**Problem.** The existing `POST /run` endpoint requires the caller to supply a spec path. That leaks filesystem details to clients, ties API contracts to deployment layout, and makes it impossible to enumerate what workflows are available without inspecting the server's directory tree.

**Solution.** `WorkflowRegistry` holds named specs in memory. Start the service with `--specs-dir` and every YAML spec in that directory is pre-loaded and addressable by name. Callers discover available workflows from the API and invoke them by name — no path knowledge needed.

### WorkflowRegistry

`armature.service.registry.WorkflowRegistry` exposes four methods:

| Method | Description |
|---|---|
| `load_dir(path)` | Scan `path` for `*.yaml`/`*.yml` files; load each as a `HarnessSpec`; key by `spec.name`. Malformed files are skipped silently. |
| `register(spec)` | Add a single `HarnessSpec` to the registry. |
| `get(name)` | Return the named `HarnessSpec`, or `None` if not found. |
| `list_all()` | Return a list of `{name, description, stages}` dicts. |

### Starting the service with --specs-dir

```bash
armature serve --specs-dir ./specs/              # load all YAML specs in ./specs/
armature serve --specs-dir ./specs/ --port 9000  # custom port
```

At startup the service prints how many workflows were registered, e.g. `Registered 4 workflows`. The existing `armature serve` (no flag) continues to work unchanged — `build_app()` accepts an optional registry and falls back to no-registry mode for backward compatibility.

### The /workflows routes

#### List all workflows

```http
GET /workflows
```

Response:

```json
[
  {"name": "summarize", "description": "Summarize input text.", "stages": 2},
  {"name": "market-monitor", "description": "Daily signal monitor.", "stages": 3}
]
```

#### Get workflow metadata

```http
GET /workflows/{name}
```

Response:

```json
{
  "name": "summarize",
  "description": "Summarize input text.",
  "version": "1.0",
  "stages": ["ingest", "summarizer"]
}
```

Returns `404` if `name` is not registered.

#### Synchronous run

```http
POST /workflows/{name}/run
Content-Type: application/json

{"inputs": {"text": "Your content here..."}}
```

Response:

```json
{
  "run_id": "a3f7c21d",
  "status": "complete",
  "result": {"summarizer": {"content": "..."}}
}
```

#### Async run

```http
POST /workflows/{name}/run/async
Content-Type: application/json

{"inputs": {"text": "Your content here..."}}
```

Response:

```json
{"job_id": "b9e1d04a", "status": "queued"}
```

Poll or stream via the existing endpoints:
- `GET /run/{job_id}` — status and result when complete
- `GET /run/{job_id}/events` — SSE stream with `token`, `response_stage_complete`, and `run_complete` events

### Combining with continuation: and triggers:

Named workflows work with every other Armature feature. A common pattern for long-horizon automation:

```yaml
name: market-monitor
version: "1.0"

continuation:
  carry_forward:
    - key: analyst.summary
  inject_as: prior_run

triggers:
  - type: cron
    schedule: "0 8 * * 1-5"

stages:
  - id: analyst
    role:
      type: worker
      description: |
        Analyse today's signals.
        Prior run: {{ prior_run.summary | default('First run.') }}
```

Register this spec once with `--specs-dir`. Trigger it via `armature watch` (§36) for scheduled runs, or call `POST /workflows/market-monitor/run` to fire it on demand. The `continuation:` block ensures each activation sees what the previous one concluded.

### Endpoint summary

| Method | Path | Description |
|---|---|---|
| `GET` | `/workflows` | List all registered workflows |
| `GET` | `/workflows/{name}` | Metadata for a single workflow |
| `POST` | `/workflows/{name}/run` | Synchronous run |
| `POST` | `/workflows/{name}/run/async` | Async run; poll via `/run/{job_id}` |

All existing `/run` and `/run/async` endpoints are preserved.

---

## 38. Docker sandbox isolation

**Problem.** Shell tool calls run with the permissions of the Armature process — access to the full filesystem, the network, all installed software. An LLM generating a shell command could, in principle, access files outside the intended workspace, make outbound HTTP calls, or consume unbounded CPU and memory.

**Solution.** Enable `sandbox.mode: docker` to route all shell, file_write, and file_read tool calls through ephemeral Docker containers. The container sees only the declared workspace directory, network is off by default, and CPU/memory are bounded per call.

### Basic configuration

```yaml
sandbox:
  mode: docker                   # default: none (no sandboxing)
  image: python:3.11-slim        # Docker image for all stages
  timeout_s: 60.0                # max wall-clock time per shell call
  allow_network: false           # --network none (default)
  workspace: /workspace          # container path
  host_workspace: ./scratch      # host directory bind-mounted to workspace
  env:                           # environment variables injected as -e flags
    PYTHONPATH: /workspace/lib
  cpu_limit: "1.0"               # --cpus 1.0 (null = no cap)
  memory_limit: "512m"           # --memory 512m (null = no cap)
```

The resulting docker command for each shell call:

```
docker run --rm \
  --network none \
  --cpus 1.0 \
  --memory 512m \
  -v /abs/path/to/scratch:/workspace \
  -e PYTHONPATH=/workspace/lib \
  python:3.11-slim \
  sh -c "<agent's shell command>"
```

The container is removed (`--rm`) immediately after the call. No state persists between calls at the container level — only files written to the mounted workspace directory.

### `sandbox:` field reference

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | `none` \| `docker` | `none` | `docker` routes tool calls through containers. `none` leaves handlers unchanged. |
| `image` | string | `python:3.11-slim` | Default container image. Overridable per stage with `sandbox_image`. |
| `runtime` | string | `"docker"` | Container CLI binary. `"docker"` works with Docker Desktop, OrbStack, Rancher Desktop, and Podman-with-shim. Set to `"podman"` or `"nerdctl"` for native use of those runtimes. |
| `platform` | string \| null | `null` | Forces a specific image platform, e.g. `"linux/amd64"`. `null` uses the host's native arch. Not needed for public multi-arch images. |
| `timeout_s` | float | `300.0` | Seconds before `subprocess.TimeoutExpired` is raised. |
| `allow_network` | bool | `false` | `false` adds `--network none`. |
| `workspace` | string | `/workspace` | Mount point inside the container. |
| `host_workspace` | string | `.` | Host directory bind-mounted into the container. Resolved to absolute at harness init. |
| `env` | dict | `{}` | Variables passed as `-e KEY=VALUE`. |
| `cpu_limit` | string \| null | `null` | Passed as `--cpus <value>` when set. |
| `memory_limit` | string \| null | `null` | Passed as `--memory <value>` when set. |

### Per-stage image override

Different stages often need different execution environments. Add `sandbox_image` to any stage to override the spec-level `image` for that stage only:

```yaml
sandbox:
  mode: docker
  image: python:3.11-slim    # default

stages:
  - id: extract              # uses python:3.11-slim
    role: ...

  - id: transform
    sandbox_image: ubuntu:22.04    # override for this stage only
    role: ...

  - id: render
    sandbox_image: node:20-slim
    role: ...
```

The override applies only during `transform`'s execution. After the stage completes, subsequent stages return to `sandbox.image`. This removes the need to build monolithic images with all dependencies.

### Image digest tracing

When `mode: docker`, the harness captures the image digest at startup via `docker inspect` and records it on every `TraceRecord` as `sandbox_image_digest`. A Docker image tag is mutable (the same tag can point to different content after a registry push). The digest is the SHA256 content hash — immutable.

Query it from the trace store:

```python
traces = await store.query(workflow_name="my-workflow")
for t in traces:
    print(f"{t.stage_id}: {t.sandbox_image_digest}")
```

`sandbox_image_digest` is `null` when `mode: none`, when Docker is unavailable, or when the image has no local manifest.

### Resource limits

`cpu_limit` and `memory_limit` prevent an LLM-generated shell command from consuming unbounded host resources. Set them to match the expected workload:

```yaml
sandbox:
  cpu_limit: "0.5"    # 50% of one core
  memory_limit: "256m"
```

On a host running multiple concurrent workflows, bounded containers mean one CPU-intensive stage cannot starve another.

### What the sandbox does not cover

- **LLM API calls** — model calls go to the provider API directly; sandbox does not intercept them
- **Inter-stage data** — the context dict is not sandboxed; use `isolated: true` + `signature.input` (§7, `CONTEXT-ISOLATION.md`) to scope stage inputs
- **File encryption** — workspace files are plaintext on the host
- **Subagent specs** — configure `sandbox:` in each child spec independently

### Composing sandbox with safety rules

Safety rules (§11) and the sandbox are complementary controls at different layers:

| Layer | What it controls |
|---|---|
| Safety rules | What the agent is *allowed to request* |
| Sandbox | What the container is *capable of doing* |

Use both in production. A safety rule blocks `rm -rf` before the call dispatches. The sandbox independently prevents the container from accessing anything outside the workspace even if a call runs. Defense in depth: policy at the rule layer, enforcement at the execution layer.

```yaml
safety_mode: strict
safety_rules:
  - tool: shell
    condition: {field: cmd, op: starts_with, value: "python /workspace/"}
    action: allow

sandbox:
  mode: docker
  image: python:3.11-slim
  allow_network: false
  cpu_limit: "1.0"
  memory_limit: "512m"
```

The spec is the complete statement of what this agent can do and what its execution environment is capable of. Both layers are readable by a security reviewer without understanding the LLM.

See `SANDBOX-AND-ISOLATION.md` for the full reference including private registry usage, workspace configuration, and the enterprise security posture.

---

*Armature User Guide — built from nine academic papers, one industry governance framework, and one open-source agent architecture project. 1,388 tests. MIT license.*

*Academic influences: [arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1 (HQS metric, Skill-Load Rate, spec refinement without frontier models); [arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1 (Self-Harness — causal failure attribution, declared editable surfaces, K-proposal diversity with best-coverage selection, held-out trace-split regression gating).*

*For AI agents reading this document: every section above describes a composable capability. A full-featured agentic team uses: model tiers (§3) to route by cost/quality, role types (§5) to assign responsibilities, fan-out/fan-in (§13) for parallelism, safety rules (§11) with strict mode and only-tighten composition (§32), sandbox isolation (§38) for execution-layer security, cross-run memory (§8) for knowledge accumulation, self-improvement (§20, §29) for continuous quality, observability (§25, §27, §31) for production monitoring, mission context (§33) to maintain focus across long-horizon runs, response stage streaming (§34) for low-latency interactive workflows, continuation (§35) for rolling state across activations, and triggers (§36) for event-driven autonomous operation. Start with a single worker stage and the starter template; add governance and observability before deploying to production.*

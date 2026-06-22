# Armature

[![CI](https://github.com/bryansparks/armature/actions/workflows/ci.yml/badge.svg)](https://github.com/bryansparks/armature/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

A lightweight, declarative agent execution harness. Define multi-agent workflows as YAML specs. Run them with a single Python call or from the CLI.

![Armature demo — pip install, a YAML spec, and a real workflow run](demo-hero.gif)

No framework dependency. No prescribed team structure. Just a DAG executor, an LLM adapter, and your workflow spec.

Armature is the execution engine for **Reasoning Automation** — end-to-end business processes where multi-agent deliberation replaces brittle rule-based logic. The harness owns orchestration, retries, safety, telemetry, and human approval gates. You supply the domain logic as YAML workflow specs and Python tool modules. The same engine that runs a code-review pipeline can run a contract risk assessment, a social media creative chain, or a compliance audit — without any changes to Armature itself.

For more information, docs, and examples, visit **[armature.now](https://armature.now)**.

---

## What it does

Armature reads a YAML spec that defines a **workflow** as a directed acyclic graph (DAG) of **stages**. Each stage is one of four things:

- An **LLM call** — a role with a system prompt, model tier, and output format
- A **script/adapter** — a Python function or shell command
- A **human gate** — pauses execution for human approval
- A **direct tool call** — invokes a registered tool deterministically, no LLM involved
- A **subagent** — spawns a child workflow (with optional fan-out/fan-in for parallelism)

Stages declare `depends_on` relationships. The engine resolves execution order automatically, passes accumulated results downstream as context, and handles retries, safety hooks, and telemetry.

---

## Installation

```bash
pip install armature-agents
```

With optional extras:

```bash
pip install "armature-agents[service]"   # FastAPI HTTP service (armature serve)
pip install "armature-agents[telemetry]" # OpenTelemetry export
pip install "armature-agents[wizard]"    # interactive spec wizard (armature new)
```

`[service]` adds FastAPI and uvicorn, needed only if you run `armature serve` to expose workflows as an HTTP API. The core `armature run` command works without it.

`[telemetry]` adds the OpenTelemetry SDK for span export to OTLP backends (Jaeger, Honeycomb, etc.). Without it, `armature.telemetry` degrades silently to no-ops — traces are written to the local SQLite store regardless.

`[wizard]` adds `questionary` for the interactive `armature new` spec-creation wizard. Without it, the other commands work normally and `armature new` will tell you to install the extra.

Verify:

```bash
armature --version
```

Set your LLM provider key:

```bash
export ANTHROPIC_API_KEY=sk-...
# or OPENAI_API_KEY, OPENROUTER_API_KEY, or any litellm-supported provider
```

**No API key?** Armature runs against local models via [Ollama](https://ollama.com) — no key, no cloud, nothing leaves your machine. Pull a model, save the spec below as `hello_ollama.yml`, and run it:

```bash
ollama pull llama3.2        # any model from `ollama list` works
```

```yaml
# hello_ollama.yml
name: hello_world_local
version: "1.0"

model_tiers:
  local:
    provider: ollama
    model: llama3.2

stages:
  - id: explainer
    role:
      name: Explainer
      type: worker
      model_tier: local
      description: |
        Explain the following topic clearly and concisely in 3-5 sentences.
    output_mode: text
    depends_on: []
```

```bash
armature run hello_ollama.yml --input topic="what a DAG is"
```

> This same spec ships as `examples/00_hello_ollama.yml` once you clone the repo.

---

## Quick start

**1. Write a spec** (`my_workflow.yml`):

```yaml
name: summarize
version: "1.0"

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001

# Optional: map role types to tiers so stages don't need explicit model_tier
role_type_defaults:
  worker: small
  judge: small

stages:
  - id: summarizer
    role:
      name: Summarizer
      type: worker        # picks up "small" from role_type_defaults
      description: |
        Summarize the provided text in 3 bullet points.
        Be concise and capture the key ideas.
    output_mode: text
    depends_on: []
```

**2. Run it from Python:**

```python
import asyncio
from armature import Harness

async def main():
    harness = Harness.from_spec("my_workflow.yml")
    result = await harness.run({"text": "Your content here..."})
    print(result["summarizer"]["content"])

asyncio.run(main())
```

**3. Or from the CLI:**

```bash
armature run my_workflow.yml --input text="Your content here..."
```

> This spec uses Anthropic (`ANTHROPIC_API_KEY`). To run on OpenAI or a local model instead, change the `model_tiers` block — see **No API key?** above for the Ollama version.

---

## CLI

```bash
armature run <spec>                           # execute a workflow
armature run <spec> --no-cache               # run without LLM response cache
armature run <spec> --auto-improve           # run then auto-apply spec improvements when HQS < 0.75
armature validate <spec>                      # validate spec + show KYA-inspired risk score (LOW/MEDIUM/HIGH/CRITICAL)
armature new [output]                         # interactive spec creation wizard
armature doctor                               # environment health check
armature serve                                # start HTTP service (requires armature[service])
armature serve --specs-dir ./specs/          # serve with named workflow registry (/workflows API)
armature optimize <spec>                      # single-shot meta-harness optimizer
armature improve <spec>                       # analyze traces, propose + auto-apply a spec improvement
armature improve <spec> --no-apply            # propose only; review the diff before applying
armature report --run-id <id>                 # per-run text report with failure signatures
armature replay <run_id>                      # display a recorded run stage-by-stage
armature dashboard <spec>                     # Rich 4-panel aggregate health dashboard
armature dashboard <spec> --watch             # auto-refresh every 5 seconds
armature dashboard <spec> --format json       # machine-readable JSON output
armature export-traces                        # export traces as SFT/DPO training data
armature channels start                       # messaging channel connectors
armature watch <spec>                         # listen for cron/webhook triggers and fire runs
```

---

## Built-in tools

Armature ships with a tool registry pre-loaded with the following tools. Any stage can invoke them via `tool_call` or by listing them in `role.tools`.

| Tool name | Permission | Description |
|-----------|-----------|-------------|
| `file_read` | READ_ONLY | Read a file from disk |
| `file_write` | WORKSPACE | Write content to a file |
| `shell` | WORKSPACE | Run a shell command; returns stdout, stderr, exit_code |
| `http_get` | NETWORK | HTTP GET request; returns status and body |
| `http_post` | NETWORK | Authenticated HTTP POST with JSON body and custom headers; returns status and body |

`http_post` is the general-purpose adapter for any external API — image generation, ad platforms, analytics services, webhooks, etc. Pass auth credentials in `headers`:

```yaml
- id: generate_image
  tool_call:
    name: http_post
    args:
      url: "https://api.openai.com/v1/images/generations"
      headers:
        Authorization: "Bearer {{ env.OPENAI_API_KEY }}"
        Content-Type: "application/json"
      body:
        model: "dall-e-3"
        prompt: "{{ visual_prompt }}"
        size: "1024x1024"
        n: 1
```

---

## Reasoning Automation

Armature's `tools:` spec section lets any workflow load external Python modules that register additional tools. This is the primary extension point for building **Reasoning Automation** applications — end-to-end processes that connect LLM reasoning to real external systems.

### The pattern

Create a Python package alongside your workflows. Each module exposes a `register(registry)` function:

```python
# myapp/tools/dalle.py
import openai
from armature.registry.registry import ToolRegistry, ToolDescriptor, PermissionLevel

_client = openai.AsyncOpenAI()

async def generate_image(args: dict) -> dict:
    response = await _client.images.generate(
        model="dall-e-3",
        prompt=args["prompt"],
        size=args.get("size", "1024x1024"),
        n=1,
    )
    return {"url": response.data[0].url, "revised_prompt": response.data[0].revised_prompt}

def register(registry: ToolRegistry) -> None:
    registry.register(ToolDescriptor(
        name="dalle.generate_image",
        description="Generate an image using DALL-E 3",
        permission=PermissionLevel.NETWORK,
        handler=generate_image,
        parameters={
            "prompt": {"type": "string"},
            "size":   {"type": "string", "optional": True},
        },
    ))
```

Declare it in your workflow spec:

```yaml
tools:
  - module: myapp.tools.dalle
  - module: myapp.tools.meta_publisher
  - module: myapp.tools.analytics

stages:
  - id: generate_image
    tool_call:
      name: dalle.generate_image
      args:
        prompt: "{{ visual_director.prompt_a }}"
```

The tool modules live entirely in your application project. Armature imports them at startup. No changes to Armature are required.

### What you can build

| Use case | Tool modules needed |
|----------|-------------------|
| Social ad campaign automation | Image gen (DALL-E 3), platform publishers (Meta, TikTok), analytics collectors |
| Contract risk review | Document extractor, clause classifier, risk scorer |
| Vendor assessment | Web search, company lookup, scoring rubric |
| Compliance documentation | Regulatory corpus retrieval, template filler, diff checker |
| Code review pipeline | GitHub API, static analysis runner, security scanner |

Each use case is a YAML workflow spec + a small set of Python tool modules. The Armature engine is the shared execution layer across all of them.

---

## Research foundation

Armature is built from nine academic papers, one industry governance framework, and one open-source agent architecture project — all but one published this year (each paper's date is listed below). Every major design decision traces to an experimentally validated finding: **the harness matters more than the model.**

### The papers

**[NLAH] Natural-Language Agent Harnesses** — Tsinghua University, March 2026 ([arXiv:2603.25723](https://arxiv.org/abs/2603.25723))

Establishes the architectural model. NLAH defines six core harness components (contracts, roles, stage structure, adapters/scripts, state semantics, and a failure taxonomy) and shows that workflows defined in structured natural language outperform code-based equivalents on complex benchmark tasks (47.2 vs. 30.4 on OSWorld). It also specifies parallel fan-out as a core orchestration primitive.

**[Meta-Harness] Automated Optimization End-to-End** — Stanford University, March 2026 ([arXiv:2603.28052](https://arxiv.org/abs/2603.28052))

The paper behind the optimizer. Meta-Harness introduces an outer optimization loop where a frontier model reads execution traces and proposes improvements to the harness spec itself. Key finding: giving the optimizer access to full execution traces — not just pass/fail scores — raises best-run accuracy from 41.3% to 56.7% by enabling causal reasoning about why runs failed. Armature keeps a `ProposalStore` of prior proposals and re-runs the loop via `run_loop()`.

**[AutoHarness] LLM-Synthesized Harnesses** — February 2026 ([arXiv:2603.03329](https://arxiv.org/abs/2603.03329))

Demonstrates that LLMs can iteratively write their own harness code and produce systems that outperform larger models without harnesses. The concept most directly applied: the **harness-as-verifier**, where the harness validates outputs meet domain-specific legality constraints before accepting them — the ancestor of the `judge` role type and `SpecDrafter`.

**[AgentSpec] Runtime Enforcement for Safe Agents** — March 2025 ([arXiv:2503.18666](https://arxiv.org/abs/2503.18666))

Introduces a declarative rule language for constraining agent behavior at runtime. Rules are composable, lightweight (millisecond-scale evaluation), and LLM-generatable. Armature implements the full enforcement architecture: pre/post-tool hooks wired into the engine and a declarative condition DSL (`ToolSafetyRule` + `SafetyCondition`) written directly in YAML.

**[Continual Harness] Reset-Free Self-Improvement** — May 2026 ([arXiv:2605.09998](https://arxiv.org/abs/2605.09998))

Formalizes the two-loop self-improvement design: an inner loop (a `post_run` refiner stage that sees the full transcript after the DAG completes) and an outer loop (`SelfImproveRunner` — load traces → diagnose → propose YAML revision → auto-apply). Its emphasis on recurring failure signatures informs Armature's own diagnostic taxonomy (`stage_failed`, `output_invalid`, `low_confidence`, `high_escalation`, `low_skill_activation`), and its fine-tuning bridge — high-quality judge traces exported as SFT/DPO training data — is implemented directly.

**[AHE] Agentic Harness Engineering** — April 2026 ([arXiv:2604.25850](https://arxiv.org/abs/2604.25850))

The accountability paper. AHE introduces the prediction-verification loop: every proposed spec revision carries a falsifiable contract (`predicted_fixes`, `predicted_regressions`), and the next cycle verifies those predictions against observed diagnostic shift. Implements component-level improvement targeting — long-term memory evolution alone yielded +5.6pp; system prompt evolution *alone* caused -2.3pp regression, validating the "one component at a time" discipline.

**[System Scaling] From Model Scaling to System Scaling** — May 2026 ([arXiv:2605.26112](https://arxiv.org/abs/2605.26112))

Identifies three system-level failure modes — which it terms "exposure without access," "stale-but-confident," and "confident-but-unchecked": memory that is present but unreachable, aging memory trusted without warning, and tool side effects assumed rather than verified. Armature answers with staleness penalties, context provenance tracking, post-condition verification, its own drift score (regression detection across improvement cycles), and component governance (auto-apply vs. human-review classification for spec changes).

**[AGT] Microsoft Agent Governance Toolkit** — 2025

Five governance primitives borrowed directly: reversibility classification for every tool call (`FULL / PARTIAL / NONE`), tamper-evident SHA-256 hashing of trace inputs and the governing policy, a `require_approval` gate wired into the tool-call path, and `safety_mode: strict` (fail-closed — deny on no-match).

**[The Log is the Agent]** — Yohei Nakajima, May 2026 ([arXiv:2605.21997](https://arxiv.org/abs/2605.21997))

Event-sourced, graph-memory agent architecture with content-addressed caching of LLM responses and event-triggered reactive behaviors. Adopted concepts: SHA-256 cache keying by model + messages + kwargs (`LLMCache`), audit replay from the trace store (`armature replay`), and the `BehaviorRule`/`BehaviorRegistry` hook layer for pattern-triggered post-run behaviors.

**[KYA] Know Your Agents** — Veldt Labs, May 2026 ([arXiv:2605.25376](https://arxiv.org/abs/2605.25376))

Governance layer operating at definition-time (static risk scoring), runtime-trust (anomaly counting), and composition (only-tighten). Adopted: five-factor static spec risk score surfaced by `armature validate`, `RogueSignalCounter` wired into safety hooks and the run summary, and `CONFLICTING_SAFETY_RULES` validation enforcing the only-tighten composition principle.

---

### What's implemented

| Source | Concept | Status |
|---|---|---|
| NLAH | Declarative NL spec, four role types, fan-out/fan-in | ✅ |
| Meta-Harness | Single-shot + multi-iteration optimizer, proposal history, prompt bootstrapping | ✅ |
| AutoHarness | Harness-as-verifier, NL-to-spec synthesis (`SpecDrafter`), `AutoHarness` loop | ✅ |
| AgentSpec | Pre/post-tool hooks, declarative safety DSL (6 operators, 5 actions) | ✅ |
| Continual Harness | Diagnostic failure taxonomy, inner refiner loop, `SelfImproveRunner`, `TraceExporter` | ✅ |
| Harness Benefit ([arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1) | Cheap-evolver (medium-tier `SpecRefiner`), HFR as 5th HQS component, SLR `low_skill_activation` diagnostic | ✅ |
| AHE | Falsifiable improvement contract, prediction-verification, `_verify_predictions()` | ✅ |
| System Scaling | Memory staleness, context provenance, drift score, postcondition verification, consensus fan-in, component governance | ✅ |
| AGT | Reversibility classification, trace hashing, policy version, `require_approval`, strict mode | ✅ |
| The Log is the Agent | LLM response caching, audit replay, trace-triggered behaviors (`BehaviorRule`), `--auto-improve` | ✅ |
| KYA | Static spec risk score, rogue signal counter, only-tighten safety rule validation | ✅ |

---

## The self-improvement flywheel

Armature is the **execution layer** — the first component in a larger system designed to improve itself the more it runs. The chart below shows where the current implementation stands and where the flywheel leads aspirationally.

```
  TODAY                         NEAR-TERM                    ASPIRATIONAL
  ─────────────────────────────────────────────────────────────────────────

  ┌──────────────────┐
  │  Armature        │  ─── every run records ──►  ┌─────────────────────┐
  │  Harness         │                              │  TraceStore         │
  │                  │  ◄── optimizer proposes ───  │  (SQLite, per run)  │
  │  • DAG executor  │        spec improvements     └──────────┬──────────┘
  │  • Role routing  │                                         │
  │  • Safety hooks  │                              ┌──────────▼──────────┐
  │  • HQS scoring   │                              │  Loop 1:            │
  │  • Session log   │                              │  Harness Optimizer  │
  └──────────────────┘                              │                     │
                                                    │  Reads traces +     │
                                                    │  proposal history   │
                                                    │  → proposes YAML    │
                                                    │  spec improvements  │
                                                    │  → A/B tests by HQS │
                                                    └──────────┬──────────┘
                                                               │ accepted diffs
                                                    ┌──────────▼──────────┐
                                                    │  Loop 2:            │
                                                    │  SLM Fine-Tuning    │
                                                    │                     │
                                                    │  High-quality       │
                                                    │  traces → LoRA      │
                                                    │  fine-tune workers  │
                                                    │  → register as      │
                                                    │  new model tier     │
                                                    └──────────┬──────────┘
                                                               │ better workers
                                                    ┌──────────▼──────────┐
                                                    │  Loop 3:            │
                                                    │  RAG                │
                                                    │                     │
                                                    │  Trace failures     │
                                                    │  reveal knowledge   │
                                                    │  gaps → improve     │
                                                    │  retrieval index    │
                                                    └──────────┬──────────┘
                                                               │ richer context
                                                    ┌──────────▼──────────┐
                                                    │  Loop 4:            │
                                                    │  Consensus          │
                                                    │  deliberation       │
                                                    │                     │
                                                    │  Calibrate          │
                                                    │  deliberation       │
                                                    │  priors from        │
                                                    │  outcomes →         │
                                                    │  cleaner quality    │
                                                    │  signal back to     │
                                                    │  Loop 1             │
                                                    └─────────────────────┘

  ─────────────────────────────────────────────────────────────────────────
  All four loops are implemented. 1,512 tests passing.
```

**The compounding property:** Each loop feeds the next. Better traces → better optimizer proposals → better specs → better traces. Fine-tuned worker models produce better outputs → fewer judge rejections → cleaner quality signal. The harness measurably improves the more it runs, without engineering effort after initial deployment.

---

## Key concepts

| Concept | Description |
|---|---|
| **Spec** | YAML file defining the complete workflow — model tiers, stages, safety rules, memory |
| **Stage** | One unit of work: an LLM call, script, gate, direct tool call, or subagent |
| **DAG** | Stages declare `depends_on`; the engine resolves execution order |
| **Context** | Shared dict that accumulates stage outputs; every stage sees all upstream results |
| **Model tiers** | Named model slots (`tiny`, `small`, `medium`, `large`, `frontier`); the using app defines what each name maps to (provider, model, temperature, max_tokens) |
| **Role type defaults** | Maps role types to tiers automatically (`worker → small`, `judge → frontier`, etc.); stages can omit `model_tier` and inherit from this mapping |
| **Native tool calling** | Stages declare `role.tools` to scope which registry tools they can call; the engine runs a ReAct dispatch loop — tool calls returned by the model are executed and results fed back until a final response is produced |
| **Direct tool call** | A `tool_call` stage invokes a registered tool without an LLM — deterministic, zero-latency, no JSON hallucination. Args are Jinja2-rendered against context. |
| **Mission context** | A `mission:` field on the spec is automatically injected into every LLM stage's system prompt, anchoring agents to the stated goal across long-running workflows and including a compact prior-stage breadcrumb |
| **Continuation** | A `continuation:` block carries selected stage outputs from a prior run into the next activation via `carry_forward` key references; the merged values arrive under an `inject_as` context key (default: `prior_run`). Enables long-horizon workflows that accumulate state across repeated executions without custom code. |
| **Iteration loops** | `loop` on any stage for deliberate iteration with `until` conditions, selective `carry_forward`, and per-iteration `_iteration` context |
| **Triggers** | A `triggers:` list declares `cron` (schedule expression) and `webhook` (HTTP path) trigger sources. `armature watch <spec>` runs a persistent dispatcher that fires `Harness.run()` on every matching event. |
| **Response stage** | Mark one text-mode LLM stage as `response_stage: true` to enable token streaming; the HTTP service forwards each token to the SSE stream immediately and fires a `response_stage_complete` event so clients can render the answer before background stages finish |
| **Context filtering** | A stage's `signature.input` declares which context keys appear in its prompt — keeps prompts focused, hides internal state from irrelevant stages |
| **Cross-run memory** | The `memory:` spec section captures stage outputs across runs and injects them into subsequent runs — lets workflows accumulate knowledge without code changes |
| **HQS** | Harness Quality Score — Armature's own 5-component quality score: output validity (35%), success rate (25%), quorum score (20%), latency (10%), harness-following rate / HFR (10%). HFR = fraction of stages that succeed without escalation, a metric adapted from [arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1 |
| **Sandbox isolation** | `sandbox.mode: docker` routes shell, file_write, and file_read tool calls through ephemeral Docker containers — network-isolated, CPU/memory bounded, workspace-scoped. Per-stage image overrides with `sandbox_image`. Image content digest recorded on every trace for audit. |
| **Templates** | Pre-built spec files for common patterns (Six Thinking Hats deliberation, etc.) |

---

## Examples

`examples/` — annotated workflows you can copy and modify:

> Most examples run on OpenRouter open models (Qwen / Gemini) — set `OPENROUTER_API_KEY` to run them as-is, or edit the `model_tiers` block to point at any provider you prefer. `00_hello_ollama.yml` needs no key at all (local Ollama); `starter_template.yml` uses Anthropic.

| File | What it demonstrates |
|---|---|
| `00_hello_ollama.yml` | Zero-API-key quickstart — a single stage on a local Ollama model |
| `01_hello_world.yml` | Minimal single-stage LLM workflow |
| `02_research_pipeline.yml` | Sequential pipeline (researcher → writer → critic) with a human approval gate |
| `03_deliberation_standard.yml` | Three-round deliberation — specialist analysts, a challenger, and a synthesizer |
| `04_fan_out.yml` | Parallel fan-out / fan-in to a single synthesizer |
| `05_enterprise_slm_tiers.yml` | Multi-tier cost pattern — local SLM workers with a frontier judge |
| `06_human_in_the_loop.yml` | Confidence-gated human escalation (HITL) |
| `11_iterative_refinement.yml` | Deliberate iteration with `loop:` and an `until:` stop condition |
| `starter_template.yml` | **Full-featured reference** — every section documented inline, showing model tiers, context filtering, cross-run memory, safety rules, guided JSON, and a human gate |

## Templates

Ready-to-use deliberation patterns in `armature/templates/`:

| Template | Pattern |
|---|---|
| `six_thinking_hats.yml` | Edward de Bono's Six Thinking Hats — structured multi-perspective deliberation |

---

## Built with Armature

Open-source applications built on Armature — reference implementations you can clone, run, and adapt:

| Project | What it does | Key Armature features |
|---------|-------------|----------------------|
| [Research](https://github.com/bryansparks/armature-research) | Given any topic, plans search queries, fetches web/Reddit/YouTube sources in parallel via a looping child workflow, and produces a structured Markdown research briefing. Supports iterative deepening across multiple runs. | `subagent_spec` (child workflow loop), multiple fan-out stages, tool call stages, continuation, checkpoint, strict safety mode |
| [Argus](https://github.com/bryansparks/argus) | Scans a code repository for security vulnerabilities and ISO/IEC 25010 quality issues, analyzing up to 40 source files in parallel and producing prioritized hardening and improvement reports. | Fan-out (up to 40 parallel file scans), `skip_if`, continuation, checkpoint, two independent workflow types |
| [Sentinel](https://github.com/bryansparks/armature-sentinel) | Weekly Python dependency digest — scans a project manifest, fetches PyPI data for all dependencies in parallel, classifies each update by severity (security / breaking / feature / patch), and writes a prioritized Markdown report. | Fan-out/fan-in, strict safety mode, continuation, `skip_if`, direct tool call stages |

Each repo is a self-contained reference implementation: a YAML workflow spec, Python tool modules, and a CLI runner. Use them as starting points for your own Armature-based applications.

---

## Project layout

```
armature/
├── nodes/          # Stage executors (LLMNode, ScriptNode, HumanGateNode, SubagentNode)
├── registry/       # Tool registry, built-in tools, ToolDescriptor, reversibility
├── runtime/        # DAG executor, engine, prompt assembler, context manager
├── spec/           # YAML loader, Pydantic models (HarnessSpec, Stage, SandboxConfig, ...)
├── hooks/          # Lifecycle hooks, safety rule evaluation, PostconditionFailed
├── permissions/    # PermissionLevel, PermissionChecker
├── optimizer/      # Meta-Harness: trace-driven spec optimization, ProposalStore
├── synthesis/      # SelfImproveRunner, SpecRefiner, DiagnosticAnalyzer, TraceExporter
├── state/          # TraceStore, MemoryStore, SessionLog, ArtifactStore (SQLite + JSONL)
├── report/         # Rich dashboard, sparkline, aggregator, panels
├── sandbox/        # DockerSandboxProvider — shell/file tool sandboxing
├── emitters/       # HermesEmitter — agent bundle generation
├── adapters/       # Observability adapters (LangFuse, LangSmith)
├── templates/      # Reusable workflow spec templates
├── service/        # FastAPI HTTP service — WorkflowRegistry, build_app(), /workflows API
└── cli.py          # CLI entry point

examples/           # Annotated workflow YAML specs (copy and modify)
docs/               # Full documentation (see index below)
```

## Documentation

### Getting started

| Document | Purpose |
|---|---|
| [BUILD_FIRST_WORKFLOW](docs/BUILD_FIRST_WORKFLOW.md) | Hands-on tutorial — build a working workflow from scratch |
| [USER-GUIDE](docs/USER-GUIDE.md) | Full spec reference — every field, every option, worked examples |
| [ARMATURE-SPEC-REF](docs/ARMATURE-SPEC-REF.md) | All spec fields and valid values on one page |
| [FAQ](docs/FAQ.md) | Common questions — positioning, capabilities, comparisons |

### Design & philosophy

| Document | Purpose |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Design rationale, research foundation, implementation table |
| [ARMATURE-PHILOSOPHY](docs/ARMATURE-PHILOSOPHY.md) | Why a harness — philosophy, research papers, architecture deep-dive |
| [DECLARATIVE-CONTROL-FLOW](docs/DECLARATIVE-CONTROL-FLOW.md) | YAML-first control flow — branching, loops, conditions |
| [DAG-vs-LANGGRAPH](docs/DAG-vs-LANGGRAPH.md) | How Armature's DAG model compares to LangGraph |
| [MISSION-AS-CONTEXT](docs/MISSION-AS-CONTEXT.md) | Mission statements as persistent agent context |
| [ROLE-TAXONOMY](docs/ROLE-TAXONOMY.md) | Agent role definitions and the role system |
| [MODEL-TIERS](docs/MODEL-TIERS.md) | Routing work across SLM workers and frontier orchestrators |

### Patterns & features

| Document | Purpose |
|---|---|
| [JUDGE-PATTERN](docs/JUDGE-PATTERN.md) | Output validation with judge agents |
| [QUORUM-SCORING](docs/QUORUM-SCORING.md) | Deliberative quality scoring across agents |
| [FAN-IN_FAN-OUT](docs/FAN-IN_FAN-OUT.md) | Parallel fan-out and aggregation patterns |
| [SUBAGENT-COMPOSITION](docs/SUBAGENT-COMPOSITION.md) | Composing workflows from subagent stages |
| [CONTEXT-ISOLATION](docs/CONTEXT-ISOLATION.md) | Isolating subagent context for focus and safety |
| [MEMORY-AND-CONTEXT](docs/MEMORY-AND-CONTEXT.md) | Memory persistence and context management |
| [CHECKPOINT-AND-RESUME](docs/CHECKPOINT-AND-RESUME.md) | Execution state persistence and resumption |
| [CHATBOT-AND-STREAMING](docs/CHATBOT-AND-STREAMING.md) | Chat applications and streaming responses |
| [HUMAN-IN-THE-LOOP](docs/HUMAN-IN-THE-LOOP.md) | Approval gates and human decision points |
| [HQS-AND-SELF-IMPROVEMENT](docs/HQS-AND-SELF-IMPROVEMENT.md) | The HQS formula and self-improvement loop |

### Operations & safety

| Document | Purpose |
|---|---|
| [ARMATURE-IN-PRODUCTION](docs/ARMATURE-IN-PRODUCTION.md) | Running Armature in production — patterns and case studies |
| [SAFETY-AND-GOVERNANCE](docs/SAFETY-AND-GOVERNANCE.md) | Safety rules, governance, and guardrails |
| [SANDBOX-AND-ISOLATION](docs/SANDBOX-AND-ISOLATION.md) | Sandboxed tool execution (Docker isolation) |
| [INTEGRATION](docs/INTEGRATION.md) | LangGraph sidecar pattern, HTTP endpoint reference |

### Project

| Document | Purpose |
|---|---|
| [CONTRIBUTING](CONTRIBUTING.md) | How to run tests, PR conventions, adding tools and commands |
| [CHANGELOG](CHANGELOG.md) | Release history |
| [ROADMAP](ROADMAP.md) | Where Armature is headed |
| [SECURITY](SECURITY.md) | Reporting vulnerabilities |

---

**Learn more:** full docs, examples, and the story behind Armature live at **[armature.now](https://armature.now)**.

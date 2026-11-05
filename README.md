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

Armature reads a YAML spec that defines a **workflow** as a directed acyclic graph (DAG) of **stages**. Each stage is one of five things:

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
pip install "armature-agents[server]"    # the platform server + web UI (armature server run)
pip install "armature-agents[service]"   # FastAPI HTTP service (armature serve)
pip install "armature-agents[cloud]"     # remote mission transport (unlocks armature/transport)
pip install "armature-agents[telemetry]" # OpenTelemetry export
pip install "armature-agents[wizard]"    # interactive spec wizard (armature new)
```

`[server]` adds SQLAlchemy, alembic, and auth — everything the platform server needs (stored workflows, teams, runs, webhooks, the web UI). It is the install for quick start step 4 below.

`[service]` adds FastAPI and uvicorn, needed only if you run `armature serve` to expose workflows as an HTTP API. The core `armature run` command works without it.

`[cloud]` makes boto3 an explicit dependency and unlocks `armature/transport` — see [Cloud transport](#cloud-transport). The default install never imports boto3.

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

Armature runs two ways. **As an engine** — write a YAML spec and run it from Python or the CLI (steps 1–3). **As a platform** — run the server and manage workflows through a web UI with teams, runners, and webhooks (step 4). Both use the same engine underneath.

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

**4. Or run the platform server and set it up in the browser:**

```bash
git clone <this repository> && cd armature
pip install -e ".[server]"
armature server run
```

The first run prints a one-time setup link, such as
`http://127.0.0.1:8098/setup#<token>`. Open it on the same computer. The page
sets up the database and your admin account, then signs you in and walks you
through your team's models, AWS, runners, connections, repos, and webhooks. Every step
after the admin can be skipped and finished later from **Setup** in the menu.
To use PostgreSQL or MySQL, paste its URL on the database screen (install the
`[postgres]` or `[mysql]` extra for the driver). That URL is
the only setting you type. See
[docs/platform/DEPLOY.md](docs/platform/DEPLOY.md#first-run).

From the web UI, the **Create a workflow** guide walks new users through what
starts the workflow, its steps, its output, and what it must never do, then
hands the description to the authoring assistant to draft the YAML — and a
**Dry run** button on any workflow replays a past run's recorded step outputs
(stub mode) so a change can be tested safely before it goes live. See
[docs/platform/DEMO.md](docs/platform/DEMO.md) for a full walkthrough.

---

## CLI

```bash
armature run <spec>                           # execute a workflow
armature run <spec> --no-cache               # run without LLM response cache
armature run <spec> --auto-improve           # run then auto-apply spec improvements when HQS < 0.75
armature loop <spec>                         # run a workflow back-to-back under a central budget
armature loop <spec> --until "{{ judge.done }}" --max-llm-calls 500
armature validate <spec>                      # validate spec + show KYA-inspired risk score (LOW/MEDIUM/HIGH/CRITICAL)
armature validate <spec> --strict-templates   # also require every template to be sandbox-safe
armature new [output]                         # interactive spec creation wizard
armature doctor                               # environment health check
armature serve                                # start HTTP service (requires armature[service])
armature serve --specs-dir ./specs/          # serve with named workflow registry (/workflows API)
armature server run                           # start the platform server + web UI (requires armature[server])
armature server init                          # create the master key + database and add the first admin
armature server migrate                       # apply pending database migrations
armature pack list                            # list installed workflow packs
armature pack validate <dir>                  # check a pack's pack.yaml and that adapter cmds are static
armature server pack install <dir>            # install a pack on the server (audited; git installs pinned to a full sha;
                                               # installing over a newer version needs --force)
armature server pack remove <name>            # remove an installed pack (audited)
armature optimize <spec>                      # single-shot meta-harness optimizer
armature improve <spec>                       # analyze traces, propose + auto-apply a spec improvement
armature improve <spec> --no-apply            # propose only; review the diff before applying
armature report --run-id <id>                 # per-run text report with failure signatures
armature replay <run_id>                      # display a recorded run stage-by-stage
armature dashboard <spec>                     # Rich 5-panel aggregate health dashboard (incl. stage leverage heatmap)
armature dashboard <spec> --watch             # auto-refresh every 5 seconds
armature dashboard <spec> --format json       # machine-readable JSON output
armature export-traces                        # export traces as SFT/DPO training data
armature channels start                       # messaging channel connectors
armature watch <spec>                         # listen for cron/webhook triggers and fire runs
armature package build --spec <yaml> --out <pkg>  # build a self-contained, verified workflow package
armature package run <pkg> [--profile <env>]      # run a package (default: in a container)
armature package verify <pkg>                     # re-run completeness checks without executing
armature package inspect <pkg>                    # print the package manifest (read-only)
```

## Workflow packs

A **workflow pack** is a directory of script adapters that a spec can call by name — no `adapters:` block, no bundling. A **pack is not a [package](#workflow-packages)**: a package is a self-contained deployable bundle, while a pack is an installed library of adapters that any spec can reference as `adapter: <pack>/<adapter>`.

```yaml
# A stage calling the installed "hello" pack's "greet" adapter:
stages:
  - id: greet
    adapter: hello/greet
    with:
      name: "{{ name }}"        # checked against the adapter's JSON Schema at validation
    depends_on: []
```

A pack's `pack.yaml` declares each adapter's static `cmd` (may reference `$PACK_DIR`, the pack's own directory), a JSON Schema for its parameters and outputs, and a timeout. Parameters travel to the script as `ARMATURE_WITH` and `ARMATURE_CONTEXT["with"]` — never interpolated into the command line. `with:` is checked against the schema when the workflow is validated (`INVALID_WITH`, with the field path) and again after it renders; a `pack/adapter` reference no installed pack provides is `UNKNOWN_PACK_ADAPTER`.

Packs load from `ARMATURE_PACKS_PATH` and then `~/.armature/packs` — the first pack under a name wins. The server ships built-in packs (`conversation`, `github`, `jira`, `linear`, `resident`, `review`, `slack`) that power its review, integration, and resident flows.

```bash
armature pack list                    # every adapter on the pack paths, with its parameter schema
armature pack validate ./my-pack      # check pack.yaml and that adapter cmds are static
armature server pack install ./my-pack   # audited; git installs pinned to a full commit sha;
                                         #   installing over a newer version needs --force
armature server pack remove my-pack       # audited
```

A minimal worked example lives at **[`examples/packs/hello/`](examples/packs/hello/)**.

## Workflow packages

A **workflow package** is a self-contained, verified, portable bundle — the spec, vendored tools, bundled inputs, a `requirements.txt`, a reference-only secrets manifest, declared output destinations, and a `manifest.sha256` integrity digest — that a single generic `armature-runner` Docker image executes. The same image runs every workflow; there is no per-workflow container build. This is the deployment shape for a **pool of worker containers**: build once, run anywhere.

```bash
armature package build --spec my_workflow.yml --out my_pkg --tools ./tools
armature package run my_pkg --profile secrets.env   # default: runs in a container
armature package run my_pkg --direct               # in-process (no Docker)
armature package verify my_pkg                     # nine completeness checks, no execution
```

- **Reference-only secrets.** The bundle carries secret *names* (`api_key_env`), never values. Values are injected at run time from an owner-supplied `--profile` `.env` (gitignored); the run fails closed if a declared secret is unresolvable.
- **Integrity.** `manifest.sha256` is verified before every run; any tampering (changed spec, swapped tool) fails before the workflow executes.
- **Nested sandbox (DooD).** A package with `sandbox.mode: docker` spawns its sandboxed shell/file stages as *sibling* containers on the host Docker daemon — the runner mounts the host socket and runs as root only for this case (least privilege otherwise).
- **Results.** Each run writes `receipt.json` + declared artifacts + optional `trace.jsonl` to a host `--results` dir.

Three example packages live in `examples/packages/` (`echo-tool`, `topic-researcher`, `sandbox-shell`) and double as the Docker integration test corpus (`pytest -m docker`). Full feature set, the secrets model, the pool-of-worker-containers path, and the test catalog: **[`docs/WORKFLOW-PACKAGES.md`](docs/WORKFLOW-PACKAGES.md)**.

## Resident workflows

On the platform server, a workflow spec can declare a `resident:` block and become a long-lived **instance** — durable state, an inbox of events, and one short run — a **turn** — per batch of events. There is no daemon: an instance is rows plus the worker's sweep. Reading an instance's published state is free (served straight from the row, for dashboards); asking it a question is an asynchronous event. Webhook rules, the Jira/Linear pollers, and schedules route events to **keyed instances** (one per PR, issue, or customer), an idle loop lets an instance maintain itself between events under backoff and per-day budgets, and `resident.emit` fans out between instances under chain/run/day caps. The whole feature ships dark and is switched on with the server's `resident.enabled` setting.

- Design and grammar: **[docs/RESIDENT-WORKFLOWS.md](docs/RESIDENT-WORKFLOWS.md)**
- What it buys you: **[docs/RESIDENT-WORKFLOWS-BENEFITS.md](docs/RESIDENT-WORKFLOWS-BENEFITS.md)**
- How it's built: **[docs/RESIDENT-WORKFLOWS-TECHNICAL.md](docs/RESIDENT-WORKFLOWS-TECHNICAL.md)**
- Worked example: **[`examples/residents/housekeeping.yml`](examples/residents/housekeeping.yml)**

## Cloud transport

Missions can execute against a remote provider. The transport — S3-backed
work store, submit gates, runner inject/settle, and the flock sweep — lives
in `armature/transport/` behind an optional extra:

```bash
pip install armature-agents[cloud]   # makes boto3 an explicit dependency;
                                      # unlocks armature/transport
```

The default install never imports boto3 (litellm may install it transitively,
but `import armature` never loads it) or the transport subpackage. The
sweep runs as a cron package (`python -m armature.transport.sweep`) with the
launch target injected via `ARMATURE_SWEEP_LAUNCH`, so the engine owns the
launch *policy* while the deployment owns the mechanism (today: ECS Fargate).
Boundary rule, provider seams, and env contracts:
**[`docs/TRANSPORT-AND-PROVIDERS.md`](docs/TRANSPORT-AND-PROVIDERS.md)**.

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

Armature's design is not improvised: every major decision traces to an experimentally validated finding from recent harness research — **the harness matters more than the model.** The spec format and role types come from NLAH; the trace-driven optimizer from Meta-Harness; the NL-to-spec authoring assistant from AutoHarness; runtime safety rules from AgentSpec; the two-loop self-improvement design from Continual Harness; prediction-verified improvement contracts from AHE; memory staleness and drift scoring from System Scaling; reversibility classification and tamper-evident traces from Microsoft's Agent Governance Toolkit; static spec risk scoring from KYA; LoRA adapter skills from Skill-to-LoRA and C-LoRA; and active memory navigation from NapMem. All of it is implemented.

- **[ARCHITECTURE](docs/ARCHITECTURE.md)** — the research table: every source and the Armature feature it maps to
- **[ARMATURE-PHILOSOPHY](docs/ARMATURE-PHILOSOPHY.md)** — per-paper deep dives and the design rationale
- **[RESEARCH-CURRENCY-CHECK](docs/RESEARCH-CURRENCY-CHECK.md)** — the mid-2026 literature check: what later work confirmed, what it added, and what was deliberately not adopted

---

## The self-improvement flywheel

Armature is the **execution layer** of a system designed to improve itself the more it runs. Four loops compound each other. Where each stands today:

1. **Harness optimization — implemented.** Every run records traces to `TraceStore`; `armature improve` reads traces and proposal history, proposes YAML spec improvements, verifies each proposal's predictions, and A/B tests by HQS.
2. **SLM fine-tuning — implemented.** `armature export-traces` exports high-quality judge traces as SFT/DPO training data, and the trained LoRA adapter plugs in at runtime (`skill_library.adapter`, `adapter_support: dynamic`). The training run itself happens out-of-band: Armature defines the trainer interface (`s2l` and `trace` backends, C-LoRA continual settings) and serves the result — the shipped PEFT trainer is a scaffold for your own training pipeline.
3. **RAG enrichment — substrate running.** Cross-run memory captures stage outputs into raw captures (L0) and reconciled facts (L1) with hybrid keyword + semantic search (`memory.search_records`), so runs accumulate knowledge. The automatic step — trace failures revealing knowledge gaps and improving the retrieval index — is future work.
4. **Consensus calibration — substrate running.** `fan_in: consensus` scores agreement across parallel runs, and the quorum score feeds HQS, which the optimizer optimizes against. Calibrating deliberation priors from outcomes is future work.

Each loop feeds the next: better traces → better proposals → better specs → better traces. The flywheel diagram and the HQS formula that drives it: **[ARCHITECTURE](docs/ARCHITECTURE.md#the-self-improvement-flywheel)** and **[HQS-AND-SELF-IMPROVEMENT](docs/HQS-AND-SELF-IMPROVEMENT.md)**.

---

## Key concepts

| Concept | Description |
|---|---|
| **Spec** | YAML file defining the complete workflow — model tiers, stages, safety rules, memory |
| **Stage** | One unit of work: an LLM call, script, gate, direct tool call, or subagent |
| **DAG** | Stages declare `depends_on`; the engine resolves execution order |
| **Context** | Shared dict that accumulates stage outputs; every stage sees all upstream results |
| **Model tiers** | Named model slots (`tiny`, `small`, `medium`, `large`, `frontier`); the using app defines what each name maps to (provider, model, temperature, max_tokens, reasoning_effort) |
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
| **Resident workflow** | A `resident:` block turns the spec into a long-lived instance on the platform server — durable state, an event inbox, one short **turn** per batch of events, an idle loop, and per-day budgets; keyed instances give one per PR, issue, or customer. Gated by the server's `resident.enabled` setting. |
| **HQS** | Harness Quality Score — Armature's own 5-component quality score: output validity (35%), success rate (25%), quorum score (20%), latency (10%), harness-following rate / HFR (10%). HFR = fraction of stages that succeed without escalation, a metric adapted from [arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1 |
| **Leverage stage** | A stage whose per-run signal (quorum score for judge stages, `success × valid` otherwise) is highly correlated (Pearson `r`) with the run's final HQS — improving it moves overall HQS the most. `armature dashboard` shows a per-stage leverage heatmap; `armature improve` weights proposal coverage by leverage once enough runs exist (`min_runs` ≥ 8 and a stage reaches `|r| ≥ 0.4`). Dormant below that guard, so the optimizer is unchanged until traces support the claim. |
| **Sandbox isolation** | `sandbox.mode: docker` routes shell, file_write, and file_read tool calls through ephemeral Docker containers — network-isolated, CPU/memory bounded, workspace-scoped. Per-stage image overrides with `sandbox_image`. Image content digest recorded on every trace for audit. |
| **LoRA adapter skills** | `skill_library` entries can reference a registered LoRA adapter via `skill.adapter`. On tiers with `adapter_support: dynamic`, the adapter replaces the skill text at runtime; on `none` tiers the skill text is used or the configured `fallback` policy is applied. Developed from the Skill-to-LoRA paper ([arXiv:2606.16769](https://arxiv.org/abs/2606.16769)); continual updates follow C-LoRA ([arXiv:2502.17920](https://arxiv.org/abs/2502.17920)) |
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
| `07_lora_adapter.yml` | **LoRA adapter skills** — replace skill text with a fine-tuned adapter at runtime |
| `11_iterative_refinement.yml` | Deliberate iteration with `loop:` and an `until:` stop condition |
| `starter_template.yml` | **Full-featured reference** — every section documented inline, showing model tiers, context filtering, cross-run memory, safety rules, guided JSON, and a human gate |
| `decision-typesafe/` | **A/B benchmark: non-LLM decision API vs LLM judge** — script adapters with `parse: json`, fan-out over a 20-item labeled benchmark, measured accuracy/latency/cost in the example README (needs `TYPESAFE_API_KEY` + `OPENROUTER_API_KEY`) |
| `residents/housekeeping.yml` | **Resident workflow** — a server-side instance with an idle loop and event turns (see the [Resident workflows](#resident-workflows) section) |

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
├── server/         # The platform server — web UI, teams, stored workflows, runs, webhooks, packs, residents
├── transport/      # Cloud transport (optional [cloud] extra) — S3 work store, submit gates, sweep
└── cli.py          # CLI entry point

examples/           # Annotated workflow YAML specs (copy and modify)
docs/               # Full documentation (see index below)
```

## Documentation

### Getting started

| Document | Purpose |
|---|---|
| [BUILD_FIRST_WORKFLOW](docs/BUILD_FIRST_WORKFLOW.md) | Hands-on tutorial — build a working workflow from scratch |
| [platform/DEMO](docs/platform/DEMO.md) | Guided walkthrough of the server platform — web UI, teams, workflows, runs |
| [platform/DEPLOY](docs/platform/DEPLOY.md) | Deploying the server behind a reverse proxy; first-run setup |
| [platform/DESIGN](docs/platform/DESIGN.md) | The platform's design: stored workflows, runners, webhooks, dry runs |
| [USER-GUIDE](docs/USER-GUIDE.md) | Full spec reference — every field, every option, worked examples |
| [ARMATURE-SPEC-REF](docs/ARMATURE-SPEC-REF.md) | All spec fields and valid values on one page |
| [ADAPTER-POWERED-TEAMS](docs/ADAPTER-POWERED-TEAMS.md) | How to set up, train, and continually refresh LoRA adapter-backed skills |
| [FAQ](docs/FAQ.md) | Common questions — positioning, capabilities, comparisons |

### Design & philosophy

| Document | Purpose |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Design rationale, research foundation, flywheel diagram, implementation table |
| [ARMATURE-PHILOSOPHY](docs/ARMATURE-PHILOSOPHY.md) | Why a harness — philosophy, research papers, architecture deep-dive |
| [RESEARCH-CURRENCY-CHECK](docs/RESEARCH-CURRENCY-CHECK.md) | Mid-2026 literature check — confirmations, additions, deliberately not adopted |
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
| [RESIDENT-WORKFLOWS](docs/RESIDENT-WORKFLOWS.md) | Resident workflows — long-lived instances, event turns, idle loop, budgets (design + grammar) |
| [RESIDENT-WORKFLOWS-BENEFITS](docs/RESIDENT-WORKFLOWS-BENEFITS.md) | What resident workflows buy you — free reads, durable per-object memory, event-driven turns |
| [RESIDENT-WORKFLOWS-TECHNICAL](docs/RESIDENT-WORKFLOWS-TECHNICAL.md) | How residents are built — rows not processes, the five-function substrate, invariants |
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

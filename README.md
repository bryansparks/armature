# Armature

A lightweight, declarative agent execution harness. Define multi-agent workflows as YAML specs. Run them with a single Python call or from the CLI.

No framework dependency. No prescribed team structure. Just a DAG executor, an LLM adapter, and your workflow spec.

Armature is the execution engine for **Reasoning Automation** — end-to-end business processes where multi-agent deliberation replaces brittle rule-based logic. The harness owns orchestration, retries, safety, telemetry, and human approval gates. You supply the domain logic as YAML workflow specs and Python tool modules. The same engine that runs a code-review pipeline can run a contract risk assessment, a social media creative chain, or a compliance audit — without any changes to Armature itself.

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
pip install armature
```

With optional extras:

```bash
pip install 'armature[service]'    # FastAPI HTTP service
pip install 'armature[telemetry]'  # OpenTelemetry export
```

Set your LLM provider key:

```bash
export ANTHROPIC_API_KEY=sk-...
# or OPENAI_API_KEY, or configure any litellm-supported provider
```

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

---

## CLI

```
armature run <spec.yml> [--input key=value ...] [--dry-run]
armature serve [--host 0.0.0.0] [--port 8080]
armature optimize <spec.yml> [--traces path/to/traces.db] [--apply]
```

| Command | Purpose |
|---|---|
| `run` | Execute a workflow spec |
| `serve` | Start the HTTP service (requires `armature[service]`) |
| `optimize` | Run the Meta-Harness optimizer against trace history |

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
| `tessera.retrieve` | NETWORK | Retrieve relevant document chunks from a Tessera RAG corpus |
| `quorum.deliberate` | NETWORK | Run structured multi-agent deliberation via Quorum |
| `alembic.submit` | NETWORK | Submit a high-quality execution trace to Alembic for SLM fine-tuning |

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

Each use case is a YAML workflow spec + a small set of Python tool modules. The Armature engine, Steward approval lifecycle, and Tessera knowledge layer are shared infrastructure across all of them.

### Reference implementation

`docs/use-case-ad-campaign.md` — complete architecture, workflow specs, tool modules, and brand corpus for a social ad campaign automation system built on Armature, Steward, and Tessera.

---

## Research foundation

Armature is built directly from four papers published in early 2026, all converging on a single insight: **the harness matters more than the model.** The architecture is not speculative — every major design decision traces to an experimentally validated finding.

### The papers

**[NLAH] Natural-Language Agent Harnesses** — Tsinghua University, March 2026 (arXiv:2603.25723)

Establishes the architectural model. NLAH defines seven mandatory harness components (Contracts, Roles, Stages, Adapters, State, Failure Taxonomy, File-backed State) and shows that workflows defined in structured natural language outperform code-based equivalents on complex benchmark tasks (47.2% vs. 30.4% on OSWorld). It also defines IHR (Implicit Harness Rating), a composite quality metric for scoring run quality objectively, and specifies parallel fan-out as a core orchestration primitive.

**[Meta-Harness] Automated Optimization End-to-End** — Stanford University, March 2026 (arXiv:2603.28052)

The paper behind the optimizer. Meta-Harness introduces an outer optimization loop where a frontier model reads execution traces and proposes improvements to the harness spec itself. Key finding: giving the optimizer access to the *history* of prior proposals — what was tried, whether it was accepted, and what score it achieved — improves accuracy from 41% to 57% by enabling causal reasoning. This is directly implemented in Armature's `ProposalStore` and `run_loop()`.

**[AutoHarness] LLM-Synthesized Harnesses** — February 2026 (arXiv:2603.03329)

Demonstrates that LLMs can iteratively write their own harness code and produce systems that outperform larger models without harnesses. The concept most directly applied here: the **harness-as-verifier**, where the harness validates that agent outputs meet domain-specific legality constraints before accepting them — the conceptual ancestor of the `judge` role type.

**[AgentSpec] Runtime Enforcement for Safe Agents** — March 2025 (arXiv:2503.18666)

Introduces a declarative rule language for constraining agent behavior at runtime. Rules are composable, lightweight (sub-millisecond evaluation), and can be generated by LLMs. Armature implements the full AgentSpec enforcement architecture: pre/post-tool hooks wired into the engine, and a declarative condition DSL (`ToolSafetyRule` + `SafetyCondition`) that workflow authors write directly in YAML.

---

### What was adopted and what was not

| Paper | Concept | Status | Notes |
|---|---|---|---|
| NLAH | 7-component spec format | ✅ Fully adopted | Contracts, Roles, Stages, Adapters, State, Failure, File State |
| NLAH | Four role types with model routing | ✅ Fully adopted | worker, orchestrator, judge, researcher |
| NLAH | IHR quality metric | ✅ Fully adopted | 4-component composite score per run |
| NLAH | Parallel fan-out / fan-in | ✅ Fully adopted | `fan_out`, `fan_in`, `partition_key` on Stage |
| NLAH | Cross-stage typed signatures | 🔶 Partial | `signature.input` filters context keys per stage; schema-level type validation of outputs is roadmap |
| NLAH | NL-to-spec generation | ⏳ Roadmap | Requires AutoHarness synthesis loop |
| Meta-Harness | Single-shot optimizer | ✅ Fully adopted | Analyze traces → propose diff → A/B test by IHR |
| Meta-Harness | Multi-iteration optimizer with history | ✅ Fully adopted | `run_loop()` + `ProposalStore` (causal reasoning) |
| Meta-Harness | Metric-driven optimization | ✅ Fully adopted | Caller-supplied `metric_fn` |
| Meta-Harness | Prompt bootstrapping from traces | ⏳ Roadmap | Trace-informed prompt generation not yet implemented |
| AutoHarness | Harness-as-verifier (judge role) | ✅ Adapted | Judge role type with quality scoring and blocking |
| AutoHarness | LLM harness synthesis loop | ⏳ Roadmap | Full AutoHarness synthesis planned for Editor layer |
| AgentSpec | Pre/post-stage and pre/post-tool hooks | ✅ Fully adopted | `HookRegistry`, `HookDecision` |
| AgentSpec | Declarative safety rule DSL | ✅ Fully adopted | `ToolSafetyRule` + `SafetyCondition`; 6 operators; block/warn/log |

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
  │  • IHR scoring   │                              │  Loop 1:            │
  │  • Session log   │                              │  Harness Optimizer  │
  └──────────────────┘                              │                     │
                                                    │  Reads traces +     │
                                                    │  proposal history   │
                                                    │  → proposes YAML    │
                                                    │  spec improvements  │
                                                    │  → A/B tests by IHR │
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
                                                    │  RaaS               │
                                                    │  (Reasoning as a    │
                                                    │  Service)           │
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
  STATUS    ■ Built         □ Roadmap — Loop 1 partially built
```

**The compounding property:** Each loop feeds the next. Better traces → better optimizer proposals → better specs → better traces. Fine-tuned worker models produce better outputs → fewer judge rejections → cleaner quality signal. The harness measurably improves the more it runs, without engineering effort after initial deployment.

**Current position:** The execution layer (left column) and Loop 1 (Harness Optimizer) are fully built. Loops 2–4 are the roadmap. Every run you execute today is building the trace history that Loop 2 will need.

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
| **Context filtering** | A stage's `signature.input` declares which context keys appear in its prompt — keeps prompts focused, hides internal state from irrelevant stages |
| **Cross-run memory** | The `memory:` spec section captures stage outputs across runs and injects them into subsequent runs — lets workflows accumulate knowledge without code changes |
| **IHR** | Implicit Harness Rating — composite quality score (output validity, success rate, quorum score, latency) computed per run from the trace store |
| **Templates** | Pre-built spec files for common patterns (Six Thinking Hats deliberation, etc.) |

---

## Examples

`examples/` — annotated workflows you can copy and modify:

| File | What it demonstrates |
|---|---|
| `01_hello_world.yml` | Minimal single-stage LLM workflow |
| `02_research_pipeline.yml` | Multi-stage pipeline with dependencies |
| `03_deliberation_standard.yml` | Judge/evaluator pattern with quality scoring |
| `starter_template.yml` | **Full-featured reference** — every section documented inline, showing model tiers, context filtering, cross-run memory, safety rules, guided JSON, and a human gate |

## Templates

Ready-to-use deliberation patterns in `armature/templates/`:

| Template | Pattern |
|---|---|
| `six_thinking_hats.yml` | Edward de Bono's Six Thinking Hats — structured multi-perspective deliberation |

---

## Project layout

```
armature/
├── nodes/          # Stage executors (LLMNode, ScriptNode, HumanGateNode, SubagentNode)
├── registry/       # Tool registry, built-in tools (file_read, http_post, tessera.retrieve, ...)
├── runtime/        # DAG executor, engine, prompt assembler (with context filtering)
├── spec/           # YAML loader, Pydantic models
├── hooks/          # Lifecycle hooks, safety rule evaluation
├── optimizer/      # Meta-Harness: trace-driven prompt/model optimization
├── state/          # Session log, artifact store, trace store, memory store (SQLite)
├── templates/      # Reusable workflow spec templates
├── examples/       # Annotated example workflows (see starter_template.yml)
└── cli.py          # CLI entry point

docs/
├── use-case-ad-campaign.md   # Reference: social ad campaign automation (Dangerous Pretzel Co.)
└── ...                       # Architecture notes and planning docs
```

See the [User Guide](USER-GUIDE.md) for full developer documentation and examples.

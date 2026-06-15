# Armature FAQ

Frequently asked questions about Armature — the declarative agentic workflow harness.

---

## What is Armature?

**What is Armature in one sentence?**

Armature is a declarative, YAML-based execution harness for multi-agent LLM workflows — you describe what agents should do and how they relate, and the harness handles orchestration, concurrency, retries, safety, quality scoring, and self-improvement automatically.

**Who is Armature for?**

Teams building production agentic pipelines that need to run reliably, improve over time, and be understood by non-engineers. It fits well when: the workflow has a defined structure (even a complex one), quality and observability matter, and you want the harness to handle production concerns without building them yourself.

**Is Armature a framework or a library?**

Neither, precisely. It is a **harness** — a finished execution environment for workflows. A library gives you building blocks; a framework prescribes patterns; a harness handles everything operational and lets you focus on domain logic. Orchestration, concurrency, retries, telemetry, quality scoring, safety enforcement, and a REST API are all built in. You supply the workflow spec (YAML) and any custom tool modules (Python).

**What kind of workflows is Armature designed for?**

Directed pipelines where the structure is known in advance: gather → analyze → synthesize → judge → report. Fan-out patterns (process N documents in parallel), recurring workflows (daily monitors, nightly analysis), and workflows that need to improve themselves based on accumulated results. It is not designed for open-ended ReAct-style agents that loop indefinitely until they decide they are done. See `DAG-vs-LANGGRAPH.md` for a detailed breakdown of where each tool fits.

---

## Getting Started

**What are the system requirements?**

Python 3.11 or later. No other infrastructure required — traces are stored in SQLite, the service layer uses FastAPI (optional), and LLM calls go through LiteLLM. No message queue, no Redis, no vector store needed for basic operation.

**Which LLM providers does Armature support?**

Any provider supported by [LiteLLM](https://litellm.ai): Anthropic, OpenAI, Google (Gemini), Azure OpenAI, AWS Bedrock, Cohere, Mistral, and local models via Ollama. Different model tiers in the same workflow can use different providers — your `small` tier can run on Ollama locally while `frontier` calls Anthropic.

**How do I install Armature?**

```bash
pip install armature                    # core
pip install 'armature[service]'         # adds FastAPI HTTP service
pip install 'armature[telemetry]'       # adds OpenTelemetry export
```

**What does the simplest possible workflow look like?**

```yaml
name: hello-world
version: "1.0"
model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001

stages:
  - id: greet
    role:
      name: Greeter
      type: worker
      model_tier: small
      description: |
        Write a one-sentence welcome message for a new user.
        Return {"message": "..."}.
```

Run it:

```bash
armature run hello-world.yaml
```

See `BUILD_FIRST_WORKFLOW.md` for a step-by-step walkthrough of building your first real workflow.

---

## Core Concepts

**What is a workflow spec?**

A YAML file that defines a workflow as a directed acyclic graph (DAG) of stages. The spec declares the workflow name, model tiers, the stages and their roles, `depends_on` relationships, safety rules, and optional features like `continuation:` (rolling memory) and `triggers:` (cron + webhook activation). The harness reads the spec and handles everything else. See `USER-GUIDE.md` for the complete spec field reference.

**What are the four stage types?**

1. **LLM call** — a `role:` block with a system prompt, model tier, and output format
2. **Tool call** — a `tool_call:` block that invokes a registered tool deterministically (no LLM)
3. **Adapter** — a `adapter:` reference to a Python function or shell command
4. **Subagent** — a `subagent_spec:` that spawns a child workflow (optionally with fan-out/fan-in)

**What are the four role types?**

| Role | Default tier | Cognitive posture |
|------|-------------|-------------------|
| `worker` | small | Execute a well-defined task on a well-defined input |
| `researcher` | large | Gather, synthesize, surface relevant context |
| `judge` | frontier | Evaluate, score, validate, decide |
| `orchestrator` | frontier | Integrate, synthesize, produce final output |

See `ROLE-TAXONOMY.md` for the full explanation including default tier rationale and the organizational analogy.

**What is a model tier?**

An abstract name (`tiny`, `small`, `medium`, `large`, `frontier`, or custom) that maps to a specific model + provider. You configure the actual model in one `model_tiers:` block at the top of the spec. Stages reference the tier name, not the model directly. Swapping models means editing one line, not touching every stage. See `MODEL-TIERS.md`.

**What is the `mission:` field?**

A string at the spec level that is automatically prepended to every LLM agent's system prompt. It keeps all agents oriented toward the same goal without repeating it in every stage description. The harness also injects a `[Prior stages]` breadcrumb (200-char preview of each completed stage's output) alongside the mission. See `MISSION-AS-CONTEXT.md`.

---

## DAG, Parallelism, and Control Flow

**How does Armature handle stage ordering?**

Each stage declares `depends_on: [stage_a, stage_b]`. The DAGExecutor uses Kahn's topological sort to compute execution waves: all stages whose dependencies are satisfied run concurrently via `asyncio.gather`. No explicit parallelism code required. See `DECLARATIVE-CONTROL-FLOW.md` for the full control flow reference including `skip_if`, `condition`, and `on_fail.loop`.

**How does fan-out/fan-in work?**

Declare `fan_out: N` and `partition_source: "{{ some_list }}"` on a stage. The harness evaluates the Jinja2 expression, gets a Python list, and runs the stage once per item — up to N concurrent executions, bounded by a semaphore. Per-item failures return `{"_fan_out_error": "..."}` rather than aborting the batch. The `fan_in` strategy (`list`, `merge`, `first`, `consensus`) controls how N results are collapsed back into one value for downstream stages. See `FAN-IN_FAN-OUT.md`.

**Does Armature support cycles in the workflow graph?**

No — deliberately. Armature is a DAG executor. If you need a ReAct-style loop that runs indefinitely until the model decides to stop, LangGraph is the right tool. For bounded retries (retry this stage up to N times if it fails), use `on_fail.loop`. For conditional re-runs of a downstream stage, restructure the workflow. See `DAG-vs-LANGGRAPH.md` for the full comparison.

**How do I skip a stage conditionally?**

Use `skip_if:` with a Jinja2 expression:

```yaml
skip_if: "{{ review_results | selectattr('requires_escalation') | list | length == 0 }}"
```

When the expression renders truthy, the stage is skipped (returns `{"_skipped": True}`) with zero LLM cost. The inverse (`condition:`) runs the stage only when the expression is truthy. See `DECLARATIVE-CONTROL-FLOW.md`.

**How do I retry a failed stage?**

```yaml
on_fail:
  loop:
    stage: self
    max: 3
    backoff_s: 2.0         # wait 2s, 4s, 8s between attempts
    backoff_max_s: 30.0
    until: "{{ score >= 0.85 }}"  # stop retrying when this is satisfied
```

On each retry, `_retry_attempt`, `_last_error`, and `_last_result` are injected into the stage's context. The role description can reference `{{ _last_result }}` so the model sees its previous attempt and can correct it. See `DECLARATIVE-CONTROL-FLOW.md`.

**When should I use `loop` instead of `on_fail.loop`?**

Use `loop` when iteration is the intent — research deepening, iterative refinement, convergence loops where each pass builds on the previous. Use `on_fail.loop` when the stage failed and you want to retry.

| | `loop` | `on_fail.loop` |
|---|---|---|
| Trigger | Always — runs `max` times regardless of success | Only on stage failure or unmet `until` condition |
| Variable injected | `_iteration` (always defined, even on round 1) | `_retry_attempt`, `_last_error`, `_last_result` |
| Typical use | Research deepening, deliberative rounds, convergence | Transient LLM errors, quality gates that sometimes fail |

A clear signal that you want `loop` rather than `on_fail.loop`: if you find yourself writing `{% if not _retry_attempt %}` to detect the first attempt so you can skip a "here is what went wrong" preamble — you are using a retry to do deliberate iteration. Switch to `loop` and use `{% if _iteration.round > 1 %}` instead.

Both can coexist on the same stage: `loop` controls how many deliberate iterations run; `on_fail` (with its own nested `loop`) handles failures that occur within each individual iteration.

```yaml
- id: research_round
  loop:
    max: 3
    until: "{{ research_round.complete }}"
    carry_forward:
      - "research_round.findings"
  on_fail:
    loop:
      stage: self
      max: 2
      backoff_s: 1.0
  role: ...
```

**How does `loop.carry_forward` work?**

`carry_forward` is a list of dot-paths extracted from the previous iteration's result and made available to the next. It is the mechanism for selective state — instead of passing the entire previous result (which can bloat context), you name only the fields you need:

```yaml
loop:
  max: 4
  carry_forward:
    - "decide_round.report"
    - "decide_round.gaps"
```

On iteration 2 and later, each path is extracted from the previous round's stage result and injected in two places simultaneously:

- **Namespaced**: `{{ _iteration.carry_forward.decide_round.report }}` — accessed through the `_iteration` dict, safe to reference even on round 1 (where it will be an empty dict)
- **Top-level**: `{{ decide_round.report }}` — overwritten in the shared context, so downstream stages and descriptions that already reference the stage output continue to work without changes

On the first iteration, `_iteration.carry_forward` is `{}` — nothing to carry yet. Reference it with `{% if _iteration.carry_forward.decide_round is defined %}` to guard iteration-1 paths.

Omit `carry_forward` entirely (or set it to `null`) to carry the complete previous iteration result as-is. This is simpler but can cause context bloat when the stage output is large — prefer explicit dot-paths for production workflows.

**What fields are available in `_iteration`?**

`_iteration` is injected into every stage that uses `loop`, on every round including the first. It always contains four fields:

| Field | Type | Description |
|-------|------|-------------|
| `round` | `int` | Current iteration number, starting at `1`. Use `{{ _iteration.round }}` to tell agents which pass they are on or to guard first-round preamble. |
| `max` | `int` | The `loop.max` value from the spec. Lets the agent know how many total rounds are planned: `{{ _iteration.round }} of {{ _iteration.max }}`. |
| `is_last` | `bool` | `true` when `round == max` or when the `until` condition will stop iteration after this round. Useful for telling the agent to produce a final synthesis rather than another exploratory pass. |
| `carry_forward` | `dict` | Values extracted by `loop.carry_forward` from the previous round's result. Empty dict `{}` on round 1. Keys mirror the dot-paths declared in `carry_forward` — e.g., `_iteration.carry_forward.decide_round.report`. |

A typical role description using all four:

```yaml
description: |
  This is research round {{ _iteration.round }} of {{ _iteration.max }}.
  {% if _iteration.round == 1 %}
  Begin with a broad survey of the topic.
  {% else %}
  Prior findings: {{ _iteration.carry_forward.research_round.findings }}
  Gaps identified: {{ _iteration.carry_forward.research_round.gaps }}
  Focus this round on resolving those gaps.
  {% endif %}
  {% if _iteration.is_last %}
  Produce a final synthesis. Mark complete: true in your output.
  {% endif %}
```

**What happens when a stage times out?**

Set `timeout_s:` on the stage. If the wall-clock time (including all retries) exceeds the limit, a `TimeoutError` is raised and propagated as a stage failure. Combine with `fail_as_value: true` to catch it as a structured value rather than aborting the run. See `DECLARATIVE-CONTROL-FLOW.md`.

---

## Quality and Observability

**What is HQS?**

Harness Quality Score — a composite quality metric (0.0–1.0) computed over accumulated traces:

```
HQS = 0.35 × output_valid_rate
    + 0.25 × success_rate
    + 0.20 × avg_quorum_score
    + 0.10 × latency_score
    + 0.10 × happy_path_rate
```

It gives you a single number to track across runs — the equivalent of error rate for traditional software. See `HQS-AND-SELF-IMPROVEMENT.md`.

**What are traces and what do they record?**

Every stage execution writes a `TraceRecord` to SQLite: stage ID, run ID, workflow name, inputs, outputs (truncated at 200 chars by default, 2000 for continuation stages), latency, success flag, output validity, quorum score, and escalation count. Traces persist across runs and are the input to self-improvement. See `HQS-AND-SELF-IMPROVEMENT.md`.

**How do I view traces and quality reports?**

```bash
armature report --workflow my-workflow          # aggregate quality dashboard
armature report --run-id abc123                 # single-run detail
```

**How do I declare quality criteria for a stage?**

Use `evaluate:` on a stage:

```yaml
evaluate:
  - "Output contains specific numerical evidence"
  - "Risk level is classified as low, medium, or high"
  - "No recommendations contradict the cited data"
```

After the run, `EvaluationRunner` scores each criterion using an LLM evaluator and records pass/fail + score (0.0–1.0) to the evaluation store. Think of these as acceptance tests for individual stages. See `HQS-AND-SELF-IMPROVEMENT.md`.

**What is the Judge pattern?**

Using one LLM to evaluate the output of another. In Armature, a `judge` role type signals a stage whose purpose is evaluation rather than production. The common pattern: many cheap `worker` stages do the work, one expensive `judge` stage validates the result. See `JUDGE-PATTERN.md`.

---

## Self-Improvement

**How does trace-driven self-improvement work?**

After enough runs accumulate:

1. `armature improve myworkflow.yaml` loads all traces for the workflow
2. Computes rolling HQS and runs `DiagnosticAnalyzer` to identify failure signatures (`output_invalid`, `stage_failed`, `low_confidence`, `high_escalation`, `low_skill_activation`)
3. If HQS < target (default 0.90) and ≥ 3 traces exist, `SpecRefiner` (an LLM call to a medium-tier model) proposes targeted YAML changes
4. Safe changes (descriptions, retries, model tier upgrades) auto-apply; risky changes (stage additions/removals, schema changes, safety rule modifications) go to `.pending.yaml` for human review
5. The refiner declares falsifiable predictions about what it expects to fix; the next cycle verifies those predictions

See `HQS-AND-SELF-IMPROVEMENT.md`.

**Is it safe to auto-apply spec improvements?**

The harness classifies every proposed change. Changes to `role.description`, `on_fail`, `model_tier`, and `timeout_s` auto-apply. Changes that add or remove stages, modify `output_schema`, or alter safety rules are written to a `.pending.yaml` file and require explicit human approval. You can also run with `--dry-run` to preview changes without applying. See `HQS-AND-SELF-IMPROVEMENT.md` for the full governance model.

**How many runs before self-improvement activates?**

By default, `min_traces: 3`. Configurable via `--min-traces N`. The improvement cycle only fires if HQS is also below `target_hqs` (default 0.90). See `HQS-AND-SELF-IMPROVEMENT.md`.

**Can I run self-improvement automatically after every run?**

```bash
armature run myworkflow.yaml --auto-improve
```

This runs the improvement cycle immediately after the workflow completes. See `HQS-AND-SELF-IMPROVEMENT.md`.

---

## Production and Operations

**Can I expose Armature workflows as an HTTP API?**

Yes. `armature serve --specs-dir ./specs/` starts a FastAPI service with:

- `GET /workflows` — list all registered workflows
- `GET /workflows/{name}` — workflow metadata
- `POST /workflows/{name}/run` — synchronous run
- `POST /workflows/{name}/run/async` — returns a `job_id` immediately
- `GET /run/{job_id}` — poll job status
- `GET /run/{job_id}/events` — SSE stream for real-time stage events

**How do I trigger workflows on a schedule or via webhook?**

Add a `triggers:` block to the spec:

```yaml
triggers:
  - type: cron
    schedule: "0 9 * * 1-5"        # weekdays at 9am
  - type: webhook
    path: /webhook/my-workflow
```

Then run `armature watch myworkflow.yaml`. The daemon blocks until Ctrl-C, fires `Harness.run()` on each trigger event, and injects the trigger payload as `trigger_payload` in the context.

**What is the `continuation:` block?**

It enables rolling memory across runs — the agentic equivalent of stateful services. Declare which stage outputs to carry forward:

```yaml
continuation:
  carry_forward:
    - key: monitor.summary
    - key: analyst.recommendations
  inject_as: prior_run
```

On every activation after the first, the harness loads those values from the previous run's traces and injects them as `prior_run` in the context. Every stage that references `{{ prior_run.summary }}` can reason about prior work.

**How do safety rules work?**

Declare `safety_rules:` in the spec. Each rule names a tool, a condition on one of its arguments, and an action (`block`, `warn`, `log`, `require_approval`). In `safety_mode: strict`, any blocked tool call raises `PermissionError` — useful for production environments where unintended file writes or API calls would be costly. See `SAFETY-AND-GOVERNANCE.md`.

**Can I pause a workflow and require a human to approve before continuing?**

Yes — use a `gate: human` stage. It renders a Jinja2 message (the `present:` field) to the terminal, prompts for `yes/no/feedback`, and returns `{"approved": true, "feedback": null}` or `{"approved": false, "feedback": "..."}`. Downstream stages can reference `{{ gate_stage.approved }}` and `{{ gate_stage.feedback }}`. Combine with `skip_if:` to skip all downstream stages if not approved, or pass `feedback` into a revision stage's description so the model can correct its output. See `HUMAN-IN-THE-LOOP.md`.

**What happens if a long workflow fails partway through? Do I have to start over?**

Not if you enable `checkpoint: true` in the spec. After every completed stage, the harness atomically writes the result to `checkpoint.json` in the session directory. On the next run, completed stages are skipped and their results are loaded directly — the workflow resumes from the last successful point. A 45-minute, $30 pipeline that fails on stage 10 of 12 resumes in seconds at near-zero cost. See `CHECKPOINT-AND-RESUME.md`.

**How do I build a workflow out of smaller, reusable workflows?**

Use `subagent_spec: path/to/child.yaml` on a stage. The harness loads the child spec and runs it as a full `Harness` instance. The child's result dict flows back as the parent stage's result. Combine with `fan_out: N` to run N child workflows concurrently — each gets a partition of the input. Child workflows have their own traces, can be run standalone for testing, and can be self-improved independently with `armature improve`. See `SUBAGENT-COMPOSITION.md`.

**How do I prevent a stage from seeing context it shouldn't (credentials, upstream outputs it doesn't need)?**

Set `isolated: true` on the stage and declare `signature.input` with the keys the stage needs. The harness filters the context to only those keys before passing it to the stage or child workflow. This prevents sensitive data from leaking into workers, creates a typed interface between pipeline sections, and makes stage behavior more predictable (LLMs are influenced by everything they see). See `CONTEXT-ISOLATION.md`.

**What is quorum scoring and how is it different from HQS?**

Quorum score is a per-execution confidence value extracted from `judge` stage outputs — specifically the `score`, `quality_score`, or `confidence` field (searched in that order). It represents how certain the judge is about its own output. HQS uses `avg_quorum_score` (weighted at 20%) across all traces for the workflow. Consistently low quorum scores (near 0.5) trigger the `LOW_CONFIDENCE` diagnostic and drive the self-improvement loop to enrich the judge's description. See `QUORUM-SCORING.md`.

**How do all these features work together in a production deployment?**

They compose. The governance stack (safety rules + human gates + strict mode) defines what agents can do. The reliability stack (checkpoint + continuation + model tiers + on_fail.loop) ensures the workflow completes at scale. The quality stack (HQS + traces + judge pattern + self-improvement) ensures the output is worth running at all. No other framework combines all three in a single declarative spec. See `ARMATURE-IN-PRODUCTION.md` for the full combinatorial story.

---

## Memory and Context

**How does Armature handle agent memory?**

Armature provides four distinct memory layers, each operating at a different time horizon:

1. **Mission context** — the `mission:` string plus a prior-stages breadcrumb is injected automatically into every LLM system prompt. Zero configuration.
2. **Continuation** — `carry_forward:` keys bring selected structured outputs from the previous run into the current run as `prior_run`. Rolling cross-run memory.
3. **MemoryStore** — a rolling window of named stage output captures across many runs. Newest-to-oldest, quality-ranked, staleness-aware. Injected as `_memory`.
4. **KnowledgeStore** — LLM-extracted entity/fact/confidence triples stored in SQLite with FTS5 full-text search. Injected as `_knowledge`. Accumulates indefinitely.

See `MEMORY-AND-CONTEXT.md` for the full breakdown of all four layers, configuration examples, and comparisons to RAG and vector stores.

**How is Armature's memory different from LangChain's ConversationBufferMemory?**

LangChain's buffer stores raw message text and grows unboundedly. Armature's MemoryStore stores **structured JSON outputs** — only what you declare worth capturing — in a bounded rolling window with quality-ranked eviction. The signal-to-noise ratio is orders of magnitude higher because you choose what to remember, not every token that ever appeared. `KnowledgeStore` goes further: an LLM distills raw captures into entity/fact triples that can be queried across all accumulated runs.

**Do I need a vector database for Armature's knowledge system?**

No external infrastructure required. KnowledgeStore uses SQLite with the FTS5 extension (full-text search, built into Python's standard `sqlite3` module). For most structured knowledge retrieval — facts about companies, customers, domains, or recurring workflows — keyword search over LLM-extracted triples is more precise than vector similarity. If you need semantic search over large unstructured document corpora, use an external vector DB accessed via a tool call.

**How do I prevent a workflow's memory from polluting fan-out workers?**

Use `isolated: true` + `signature.input` on the fan-out stage. Declare exactly which context keys each worker receives — for example, `[doc_path, _knowledge, prior_run]`. The harness filters the context to those keys before passing it to the worker. Workers cannot accidentally see parent pipeline state or each other's outputs. See `CONTEXT-ISOLATION.md`.

**How many runs does memory persist across?**

- **Continuation**: carries values from exactly the most recent prior run.
- **MemoryStore**: rolling window per `(stage_id, capture_key)` pair; default 5 entries, configurable per capture. Entries older than 30 days are flagged as stale.
- **KnowledgeStore**: cumulative across all runs — facts are added each run and never automatically evicted. The FTS5 index returns the top-10 most relevant records per query.

---

## Streaming and Chat

**Can Armature stream tokens to a user interface?**

Yes. Mark any stage with `response_stage: true`. When that stage executes, tokens are streamed token-by-token to the job's SSE event queue rather than being buffered until completion. Use the async endpoint (`POST /workflows/{name}/run/async`) to get a `job_id`, then connect to `GET /run/{job_id}/events` to receive the SSE stream. Token events have type `"token"` and a `"content"` field containing the individual token.

**How do I use Armature as a backend for a chat application?**

The sidecar pattern: your chat application calls Armature over HTTP, streams the response back to the user. The application handles UI; Armature handles all AI reasoning. A typical flow:
1. User submits a query to your application
2. Application `POST`s to `/workflows/your-assistant/run/async`
3. Application connects to `/run/{job_id}/events` and streams token events to the frontend
4. User sees the response arriving in real time, backed by multi-stage reasoning (classify → retrieve → draft → validate)

See `CHATBOT-AND-STREAMING.md` for the full pattern, code examples, latency optimization, and WebSocket integration.

**How does Armature handle multi-turn conversations?**

Use `continuation:` to carry structured outputs across turns. Each turn is a fresh workflow activation; the harness loads the prior turn's declared outputs (e.g., `response_text`, `conversation_summary`) and injects them as `prior_run`. A rolling summary field compresses conversation history without unbounded growth — cheaper and more durable than replaying a full transcript.

**How fast is time-to-first-token?**

Depends on your pre-processing pipeline. A typical support assistant with two fast classification/retrieval stages (haiku, ~80ms + ~150ms) before a streaming response stage (opus) achieves time-to-first-token around 700–800ms. Parallel pre-processing (multiple stages with no `depends_on`) reduces this further. See `CHATBOT-AND-STREAMING.md` for the latency analysis.

**Is Armature appropriate for general-purpose chatbots?**

For structured reasoning behind a chat interface — yes. For open-ended free-form conversation where the AI decides at runtime how many steps to take — better handled by a framework designed for cycles (LangGraph, etc.). Armature's strength is that the workflow structure is defined in the spec: you control which stages run, in what order, at what cost. The chat user gets a streaming response; the workflow author gets full auditability of every step that produced it.

---

## Comparisons

**How does Armature compare to LangGraph?**

LangGraph is a graph-construction library built around **cycles** — the core primitive is a stateful loop (`think → act → observe → think`). You write Python to construct the graph explicitly. Observability, safety, quality scoring, and APIs are left to you.

Armature is a finished harness built around **directed pipelines**. The DAG is implicit from `depends_on:` declarations in YAML. Observability, safety, HQS scoring, self-improvement, and a REST API are built in. The tradeoff: no cycles, but everything production requires is already there.

They compose: a LangGraph ReAct agent can be one tool that an Armature worker stage calls via HTTP. See `DAG-vs-LANGGRAPH.md`.

**How does Armature compare to LangChain?**

LangChain is a large, broad library of LLM utilities — chains, retrievers, memory abstractions, document loaders. It provides building blocks. Armature is opinionated about execution: YAML spec, DAG execution, four role types, built-in quality metrics. If you want full flexibility in how you assemble components, LangChain; if you want a production-ready harness with governance built in, Armature.

**How does Armature compare to CrewAI?**

CrewAI uses a "crew" metaphor with agents and tasks, typically with a manager agent orchestrating others. Armature uses explicit DAG stages instead of dynamic agent delegation. The result: Armature workflows are more predictable and auditable (every execution path is determined by the spec), CrewAI allows more dynamic task allocation. For regulated industries or workflows where auditability matters, the deterministic Armature DAG is usually preferable.

**How does Armature compare to AutoGen?**

AutoGen is built around multi-agent conversation — agents message each other in flexible dialogue patterns. Armature stages are not conversational; they produce structured outputs that flow downstream. If the problem is "simulate a conversation between agents to reach a decision," AutoGen fits. If the problem is "process these 500 documents in a defined pipeline," Armature fits.

**Can I use Armature with local/self-hosted models?**

Yes. Configure a tier to use Ollama or any OpenAI-compatible local endpoint:

```yaml
model_tiers:
  small:
    provider: ollama
    model: llama3.2
    api_base: http://localhost:11434
  frontier:
    provider: anthropic
    model: claude-opus-4-7
```

Data-sensitive stages use `model_tier: small` (local); synthesis and judgment use `model_tier: frontier` (cloud). See `MODEL-TIERS.md`.

---

## Limitations and When Not to Use Armature

**When should I NOT use Armature?**

- **Open-ended tool-use agents** that loop indefinitely until they decide they are done. Use LangGraph.
- **Purely conversational agents** where agent-to-agent messaging is the core pattern. Use AutoGen.
- **Simple single-call LLM applications** with no orchestration. Use the provider SDK directly.
- **Workflows where the number of steps is determined at runtime by the model.** Armature's DAG is fixed at spec-load time.

See `DAG-vs-LANGGRAPH.md` for the full comparison including where the two tools compose well together.

**Does Armature handle streaming responses?**

Yes. Mark a stage with `response_stage: true` and attach an `on_token` callback (or use the service layer's SSE endpoint). Tokens stream in real time while the stage executes.

**Can non-engineers write Armature specs?**

Yes, with some ramp-up. YAML is readable by anyone familiar with CI/CD pipelines or Kubernetes configs. The four role types and `depends_on` are intuitive. Complex Jinja2 expressions in `skip_if` or `partition_source` may need engineering help, but the bulk of a workflow spec — stage descriptions, model tier assignments, role names — is accessible to product managers and domain experts. See `ARMATURE-PHILOSOPHY.md` for the design principles behind the YAML-first authoring surface.

**Is the spec format stable?**

The core fields (`stages`, `depends_on`, `role`, `model_tiers`, `fan_out`, `fan_in`, `skip_if`, `on_fail`) are stable. Fields added in recent versions (`continuation`, `triggers`, `mission`) follow the same Pydantic validation and are backward-compatible — existing specs without those fields continue to work.

---

## Sandbox and Isolation

**How do I run tool calls inside an isolated Docker container?**

Add a `sandbox:` block to your spec and set `mode: docker`:

```yaml
sandbox:
  mode: docker
  image: python:3.11-slim
  allow_network: false
  cpu_limit: "1.0"
  memory_limit: "512m"
  host_workspace: ./workspace
```

With this set, every `shell` tool call runs inside an ephemeral Docker container that disappears after each call. The container can only see the `host_workspace` directory — nothing else on the host filesystem. Network is off by default. CPU and memory are bounded. The default is `mode: none`, which leaves all tool handlers unchanged.

See `SANDBOX-AND-ISOLATION.md` for the full reference.

---

**Can I use different Docker images for different stages?**

Yes — set `sandbox_image` on any individual stage to override the spec-level `sandbox.image` default:

```yaml
sandbox:
  mode: docker
  image: python:3.11-slim       # default

stages:
  - id: extract                 # uses python:3.11-slim
    role: ...

  - id: transform
    sandbox_image: ubuntu:22.04 # this stage only
    role: ...

  - id: render
    sandbox_image: node:20-slim # different image
    role: ...
    depends_on: [transform]
```

The override applies only to that stage's shell calls and resets automatically afterward. This is useful when different stages need different tool dependencies without building a single monolithic image.

---

**What resource constraints can I apply to containers?**

Two fields control resource limits on individual container executions:

- `cpu_limit` — passed as `--cpus <value>` to Docker. Example: `"1.0"` (one full core), `"0.5"` (half a core). `null` (the default) omits the flag, leaving no CPU cap.
- `memory_limit` — passed as `--memory <value>` to Docker. Example: `"512m"`, `"1g"`. `null` omits the flag.

These prevent runaway resource consumption from LLM-generated shell commands and enable predictable resource budgets when running multiple concurrent workflows on the same host.

---

**How do I audit which exact Docker image ran each stage?**

When `sandbox.mode: docker`, Armature runs `docker inspect` at harness startup to capture the image content digest (SHA256). This digest is stored on every `TraceRecord` as `sandbox_image_digest`.

Query it from the trace store:

```python
traces = await store.query(workflow_name="my-workflow")
for t in traces:
    print(f"{t.stage_id}: {t.sandbox_image_digest}")
```

The digest is the content hash of the image — immutable, unlike a tag. Even if `python:3.11-slim` is updated on the registry, runs before and after the update will show different digests in the trace. This gives you proof of exactly which image content executed at any point in time — useful for regulated environments, incident response, and reproducibility audits.

---

**Does the sandbox replace safety rules, or do they work together?**

They work together at different layers.

Safety rules inspect tool arguments *before dispatch* — they define what the agent is *allowed to request*. The sandbox constrains the execution environment *at runtime* — it defines what the container is *capable of doing*. These are complementary controls, not alternatives.

A safety rule can block shell calls containing `rm -rf`. The sandbox independently prevents the container from accessing anything outside the mounted workspace. A security reviewer reads both in the same YAML file: what the policy permits, and what the container is physically capable of.

The practical result: the answer to "what can this agent touch on our infrastructure?" becomes:

- Computation in ephemeral, resource-bounded, network-isolated containers that disappear after each call
- Files scoped to one directory; nothing outside it is visible
- Network off by default; enabled only when declared
- Environment is the specified image, not the host's installed software
- Every execution traceable by model, inputs, policy version, and image digest

The container boundary is the security boundary — an established concept that does not require explaining a new abstraction. See `SANDBOX-AND-ISOLATION.md` for the full picture, and `SAFETY-AND-GOVERNANCE.md` for the policy layer.

---

## Embedding Armature in Your Application

**Can I use Armature from inside my existing Python application?**

Yes — the simplest integration is a direct Python call. Import `Harness` and `load_spec`, load your spec once at startup, then invoke `harness.run(inputs)` wherever you need it:

```python
import asyncio
from armature.spec.loader import load_spec
from armature.runtime.engine import Harness

spec = load_spec("specs/risk-assessment.yaml")

async def assess_contract(contract_text: str) -> dict:
    harness = Harness(spec=spec)
    return await harness.run({"contract_text": contract_text})
```

`Harness.run()` is a standard Python coroutine. It fits naturally into any `asyncio`-based application (FastAPI, Starlette, AIOHTTP, Celery async workers, etc.). For synchronous callers, wrap it with `asyncio.run()`.

**How do I add Armature to an existing FastAPI application?**

Use `build_app()` to create the Armature FastAPI sub-application and mount it under a path prefix:

```python
from fastapi import FastAPI
from armature.service.app import build_app
from armature.service.registry import WorkflowRegistry

# Your existing app
app = FastAPI(title="MyApp")

# Load Armature workflows from a directory
registry = WorkflowRegistry()
registry.load_dir(Path("specs/"))

# Mount Armature under /ai — all /workflows routes available at /ai/workflows
armature_app = build_app(registry)
app.mount("/ai", armature_app)
```

Your application and Armature share one process and one port. Clients POST to `/ai/workflows/risk-assessment/run` and get structured results back. No separate service to deploy or manage.

**Can I register individual workflows from code rather than a directory?**

Yes — register specs one at a time with `registry.register()`:

```python
from armature.service.registry import WorkflowRegistry
from armature.spec.loader import load_spec

registry = WorkflowRegistry()
registry.register(load_spec("specs/summarizer.yaml"))
registry.register(load_spec("specs/classifier.yaml"))
# ... add more as needed
```

This is useful when your app loads specs from a database, generates them dynamically, or controls which workflows are available based on tenant configuration.

**How do I call Armature from a non-Python app (Ruby, Go, Node.js, etc.)?**

Run Armature as a sidecar service and call it over HTTP:

```bash
# Start the Armature service (separate process, same host)
armature serve --specs-dir ./specs/ --port 8765
```

Then from any language:

```bash
# Any HTTP client
curl -X POST http://localhost:8765/workflows/risk-assessment/run \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"contract_text": "..."}}'
```

The response is a JSON object with `run_id`, `status`, and `result`. This is the standard service integration pattern for polyglot architectures — Armature becomes an AI capability endpoint that any service on the network can call.

**What if the workflow is slow? I don't want to block my HTTP request.**

Use the async endpoint, which returns a `job_id` immediately:

```bash
# Fire and forget — returns in milliseconds
curl -X POST http://localhost:8765/workflows/risk-assessment/run/async \
  -d '{"inputs": {"contract_text": "..."}}'
# → {"job_id": "abc123", "status": "pending"}

# Poll for completion
curl http://localhost:8765/run/abc123
# → {"status": "complete", "result": {...}}
```

Or stream real-time stage events via SSE while the workflow runs:

```javascript
const es = new EventSource('/run/abc123/events');
es.onmessage = (e) => {
  const event = JSON.parse(e.data);
  if (event.type === 'stage_complete') updateProgressUI(event.stage_id);
  if (event.type === 'run_complete') es.close();
};
```

**How do I pass runtime data from my app into a workflow?**

Pass an `inputs` dict to `harness.run()`. Every key becomes available as a Jinja2 variable in stage descriptions and `skip_if` expressions:

```python
result = await harness.run({
    "user_id": current_user.id,
    "document_url": upload.url,
    "tenant_config": tenant.settings,
})
```

In the spec:

```yaml
stages:
  - id: analyse
    role:
      description: |
        Analyse the document at {{ document_url }} for tenant {{ tenant_config.name }}.
```

Any serializable Python value works — strings, numbers, lists, dicts.

**How do I get structured data back from a workflow to use in my app?**

Declare an `output_schema` on the final stage and use `output_mode: guided_json`. The harness validates and returns a clean dict:

```yaml
stages:
  - id: assess
    role:
      output_mode: guided_json
      output_schema:
        type: object
        required: [risk_level, confidence, summary]
        properties:
          risk_level:
            type: string
            enum: [low, medium, high, critical]
          confidence:
            type: number
          summary:
            type: string
```

The result of `harness.run()` is a dict keyed by stage ID. Access the final stage output directly:

```python
result = await harness.run(inputs)
risk = result["assess"]["risk_level"]      # "high"
confidence = result["assess"]["confidence"] # 0.92
```

**How do I trigger a workflow from an application event (new record, file upload, webhook)?**

Three patterns depending on your architecture:

**1. Direct call from event handler** — simplest, works when your app is Python:
```python
@app.post("/documents/upload")
async def upload_document(file: UploadFile):
    path = await save_file(file)
    harness = Harness(spec=spec)
    result = await harness.run({"file_path": str(path)})
    await db.save_analysis(result)
    return {"analysis": result}
```

**2. Background task** — for long workflows that shouldn't block the HTTP response:
```python
from fastapi import BackgroundTasks

@app.post("/documents/upload")
async def upload_document(file: UploadFile, bg: BackgroundTasks):
    path = await save_file(file)
    bg.add_task(run_analysis_workflow, str(path))
    return {"status": "processing"}

async def run_analysis_workflow(file_path: str):
    harness = Harness(spec=spec)
    result = await harness.run({"file_path": file_path})
    await db.save_analysis(result)
```

**3. Armature webhook trigger** — for external events (Stripe webhooks, GitHub events, form submissions) when you want the trigger handled declaratively in the spec:
```yaml
triggers:
  - type: webhook
    path: /webhook/document-uploaded
```
```bash
armature watch specs/document-analysis.yaml --port 8081
```
Your upstream service POSTs to `http://your-host:8081/webhook/document-uploaded` and Armature fires the workflow with the request body as `trigger_payload`.

**Can I run multiple different workflows from one service?**

Yes — that is exactly what the named workflow registry is for. Load all your specs from a directory and every workflow gets its own route:

```bash
armature serve --specs-dir ./specs/
# → GET  /workflows                        (list all)
# → POST /workflows/risk-assessment/run    (run one)
# → POST /workflows/document-summary/run   (run another)
# → POST /workflows/compliance-audit/run   (run a third)
```

All workflows share one process and one SQLite trace database. Each workflow's traces are namespaced by workflow name so quality reports stay separate.

**Does embedding Armature add significant overhead to my application?**

No measurable startup overhead beyond loading the spec (a Pydantic parse, typically <50ms). Per-run overhead is negligible compared to LLM call latency — the DAG executor, context management, and trace recording add a few milliseconds per stage. The dominant cost is always the LLM calls themselves.

---

## Role Types and Model Tiers

**What are the four role types and when do I use each?**

Every LLM stage declares one of four role types. The type sets the model's cognitive posture and maps to a default cost tier via `role_type_defaults`:

| Type | Cognitive posture | Default tier |
|------|-------------------|-------------|
| `researcher` | Gather and synthesize information, explore breadth | `large` |
| `worker` | Execute a defined task with narrow scope | `small` |
| `judge` | Evaluate quality, resolve conflicts, make decisions | `large` |
| `orchestrator` | Plan, decompose, direct downstream stages | `large` |

`worker` is the cheapest role — use it for formatting, extraction, or transformation tasks where a capable small model suffices. `judge` should always run on a larger, more capable model; its job is to catch errors in worker outputs. See `ROLE-TAXONOMY.md` for detailed guidance.

**What are model tiers and why use them instead of naming a model directly?**

A model tier is a named capability level (`tiny`, `small`, `medium`, `large`, `frontier`) that you configure once at the top of the spec. Stages reference tier names, never model names. When you want to swap `frontier` from GPT-4 to Claude, you change one line — all stages that use `frontier` update automatically.

The practical benefit: different providers and models in the same workflow. Your `small` tier can run on a local Ollama model while `frontier` calls Anthropic. Your `medium` can be on OpenRouter. Different tiers, one spec.

**What happens when `guided_json` fails on a small model?**

The engine automatically escalates to the next tier. If `small` produces invalid JSON for a stage with `output_mode: guided_json`, the engine retries with `medium`, then `large` if needed. This escalation is logged and tracked in HQS as the Harness-Following Rate (HFR) component. Armature also emits a validator warning (`GUIDED_JSON_LOW_TIER_RISK`) if you declare a `guided_json` stage on a small/tiny tier at spec-write time. See `MODEL-TIERS.md`.

---

## The Judge Pattern and Quorum Scoring

**What is the judge pattern?**

One LLM evaluates the output of another. A `judge` role stage receives a prior stage's output and assesses it for quality, accuracy, scope, or format compliance — then either accepts it, requests a retry, or flags it for human review.

The pattern catches confident hallucinations, scope drift, and uncalibrated confidence before they leave the workflow. A single LLM call is a sample from a distribution; a judge stage inserts a second draw whose sole job is to detect failure modes in the first.

```yaml
- id: judge
  role:
    name: QualityReviewer
    type: judge
    description: |
      Review the analyst's output for accuracy and completeness.
      ANALYST OUTPUT: {{ analyst.content }}
  output_mode: guided_json
  output_schema:
    type: object
    required: [accept, confidence, issues]
    properties:
      accept: {type: boolean}
      confidence: {type: number}
      issues: {type: array, items: {type: string}}
  on_fail:
    loop: {stage: analyst, max: 2}
  depends_on: [analyst]
```

When `accept` is false, `on_fail.loop` restarts the `analyst` stage with the judge's `issues` list injected into its context as `_last_error`.

**What is quorum scoring?**

Quorum scoring is the fan-in strategy `fan_in: "consensus"`. When multiple parallel stages produce conflicting outputs, an LLM judge synthesizes them into a single result. Each parallel result gets a `quorum_score` (0–1) reflecting its alignment with the synthesized consensus. Quorum scores feed into HQS's quorum component.

Use it for: parallel research where agents disagree on facts, parallel code reviews where different reviewers flag different issues, or any fan-out where "what did most agents agree on?" is more reliable than any single output. See `QUORUM-SCORING.md`.

---

## Human-in-the-Loop Gates

**How does `gate: human` work?**

A human gate is a stage that blocks execution until a human approves or provides feedback. Set `gate: human` on any stage:

```yaml
- id: approval_gate
  gate: human
  present: |
    Please review the proposed contract terms before we proceed.
    TERMS: {{ drafter.content }}
  depends_on: [drafter]
```

When the harness reaches this stage, it prints the `present:` message to stdout and waits for keyboard input. The human can type `approve`, `reject`, or free-form feedback. Their response is stored in context as `{{ approval_gate.response }}` and `{{ approval_gate.approved }}` (boolean) for downstream stages to act on.

**Can I use gates in a long-running workflow running in CI or as a service?**

Yes — via the HTTP service. When running `armature serve`, gates dispatch to an approval queue rather than stdin. A `GET /approvals` endpoint lists pending gates; `POST /approvals/{id}/approve` or `/reject` resolves them. The workflow resumes automatically. See `HUMAN-IN-THE-LOOP.md`.

---

## Checkpoint and Resume

**What is checkpoint mode and when should I use it?**

Checkpoint mode persists each stage's result to disk as it completes. If the workflow crashes or is interrupted, the next run detects which stages already have valid results and skips them — resuming from the last successful point.

Enable it with one line: `checkpoint: true` in the spec. Use it for any workflow that takes more than a few minutes, fans out across many items, or makes expensive external API calls that you don't want to repeat.

```yaml
name: compliance-audit
checkpoint: true

stages:
  - id: fetch_documents     # if this completes, it won't re-run on resume
    ...
  - id: review_each         # fan-out: 100 documents — partial completion is preserved
    fan_out: 100
    ...
```

**Does checkpoint mode affect normal (non-interrupted) runs?**

No. On a clean run, stages complete and write their checkpoints, but the next `armature run` starts fresh unless you pass `--resume`. Checkpoint files are stored in `.armature/checkpoints/{run_id}/`. See `CHECKPOINT-AND-RESUME.md`.

---

## Subagent Composition

**What is a subagent stage?**

A subagent stage loads a separate YAML spec and runs it as a full, independent workflow inside a single parent stage. The parent sees one stage; the child is an entire DAG executing inside it.

```yaml
- id: deep_analysis
  subagent_spec: workflows/deep-analysis.yaml
  depends_on: [gather]
```

Use subagents to: reuse a workflow across multiple parent specs, run the same child workflow in parallel across N items (fan-out of subagents), or keep complex sub-pipelines in separate files that can be tested independently.

**How does fan-out of subagents work?**

Combine `fan_out`, `partition_source`, and `subagent_spec`:

```yaml
- id: analyze_each_doc
  fan_out: 20
  partition_source: "{{ gather.documents }}"
  partition_key: doc_item
  subagent_spec: workflows/single-doc-review.yaml
  fan_in: list
  depends_on: [gather]
```

This runs up to 20 instances of `single-doc-review.yaml` concurrently, one per document, and collects results into a list. Each child receives `doc_item` in its context. See `SUBAGENT-COMPOSITION.md`.

---

## Mission Context and Long-Horizon Workflows

**What is the `mission:` field?**

The `mission:` field is a workflow-level statement of purpose that is automatically injected into every LLM stage's system prompt. Every agent in the workflow sees it, without any per-stage configuration.

```yaml
name: legal-review
mission: >
  You are reviewing contracts for a healthcare SaaS company.
  Our primary concern is HIPAA compliance, data residency, and liability caps.
  Flag anything that requires legal counsel before signing.
```

Without `mission:`, long workflows drift — later agents forget what the early ones were trying to accomplish. With `mission:`, every agent has the workflow's north star. See `MISSION-AS-CONTEXT.md`.

**What is the `continuation:` block for?**

`continuation:` enables long-horizon workflows that carry forward outputs from their previous activation. On each run after the first, the harness retrieves named keys from the prior run's traces and injects them into the context as `prior_run` (or a custom name):

```yaml
continuation:
  carry_forward:
    - key: analyst.recommendations
    - key: monitor.summary
  inject_as: prior_run
```

Use it for daily monitors, weekly analysis workflows, or any workflow that needs to reason about "what did we find last time?" See the continuation section in `USER-GUIDE.md`.

---

## Documentation Map

| Topic | Document |
|-------|----------|
| Quick spec reference (one page) | `ARMATURE-SPEC-REF.md` |
| Getting started | `BUILD_FIRST_WORKFLOW.md` |
| Full spec reference | `USER-GUIDE.md` |
| Architecture internals | `ARCHITECTURE.md` |
| For AI coding agents | `AGENTS.md` |
| DAG vs. LangGraph | `DAG-vs-LANGGRAPH.md` |
| Fan-out/fan-in | `FAN-IN_FAN-OUT.md` |
| Role taxonomy | `ROLE-TAXONOMY.md` |
| Model tiers | `MODEL-TIERS.md` |
| Judge pattern | `JUDGE-PATTERN.md` |
| Quorum scoring | `QUORUM-SCORING.md` |
| Mission context | `MISSION-AS-CONTEXT.md` |
| Declarative control flow | `DECLARATIVE-CONTROL-FLOW.md` |
| HQS and self-improvement | `HQS-AND-SELF-IMPROVEMENT.md` |
| Safety and governance | `SAFETY-AND-GOVERNANCE.md` |
| Sandbox and container isolation | `SANDBOX-AND-ISOLATION.md` |
| Human-in-the-loop gates | `HUMAN-IN-THE-LOOP.md` |
| Checkpoint and resume | `CHECKPOINT-AND-RESUME.md` |
| Subagent composition | `SUBAGENT-COMPOSITION.md` |
| Context isolation | `CONTEXT-ISOLATION.md` |
| Memory and context (all layers) | `MEMORY-AND-CONTEXT.md` |
| Chat and streaming (sidecar pattern) | `CHATBOT-AND-STREAMING.md` |
| All features in production | `ARMATURE-IN-PRODUCTION.md` |
| Philosophy and design decisions | `ARMATURE-PHILOSOPHY.md` |

---

*Armature — the harness is more important than the model.*

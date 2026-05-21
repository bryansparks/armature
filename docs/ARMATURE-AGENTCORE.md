# Armature vs. AWS AgentCore

*Researched May 2026. AgentCore announced April 2026.*

---

## What AgentCore Is

Amazon Bedrock AgentCore is a **managed cloud platform** — eleven modular AWS services you opt into individually or together. It is not a framework you embed in your code; it is infrastructure AWS operates on your behalf. Its core value proposition is: "We operate it; you define it."

### The Eleven Services

| Service | What it does |
|---|---|
| **Runtime** | Serverless agent hosting. One microVM per session (15-min idle timeout / 8-hr max lifetime), auto-scaling, versioned deployments with rollback, WebSocket + HTTP + MCP + A2A protocol support. Framework-agnostic: works with LangGraph, CrewAI, LlamaIndex, Strands, OpenAI Agents SDK, Google ADK, and custom code. |
| **Memory** | Short-term memory (session turn history) + long-term memory (automatic cross-session insight extraction: preferences, facts, summaries). Semantic search. Shareable across agents. |
| **Gateway** | Converts any REST API, Lambda function, or OpenAPI spec into an MCP-compatible tool with no code changes. Semantic tool discovery. Pre-built integrations for Salesforce, Slack, JIRA, Zoom, and others. |
| **Identity** | Inbound authentication (IAM SigV4 or OAuth 2.0) and outbound authentication (agent-to-third-party-service credentials). Integrates with Cognito, Okta, Azure Entra ID, Auth0. |
| **Policy** | Real-time tool-call interception at the Gateway using natural language rules or Cedar (AWS's open-source policy language). Deterministic guardrails before any tool executes. |
| **Observability** | CloudWatch-backed dashboards. Per-step trace visualization. OTel-compatible export. Debugging and performance bottleneck detection across all agent steps. |
| **Evaluations** | Automated quality scoring on live traces: correctness, helpfulness, safety, goal success rate. Works with OTel/OpenInference-instrumented agents. Integrated into CloudWatch. |
| **Code Interpreter** | Sandboxed, managed Python / JavaScript / TypeScript execution for agents that need to write and run code. |
| **Browser** | Managed serverless cloud browser runtime (Playwright-compatible). Auto-scales from zero to hundreds of sessions. Reduced CAPTCHA interruptions. |
| **Payments** | Microtransaction capability via the x402 protocol. Wallet integration with Coinbase CDP and Stripe (Privy). Configurable spending limits. End-to-end payment observability. |
| **Registry** | Org-wide catalog of agents, MCP servers, tools, skills, and custom resources. Hybrid semantic + keyword search. Governed publish/approve workflow for internal tool discovery. |

### The Managed Harness (New, Preview — April 2026)

A thin layer built on Strands Agents technology that eliminates orchestration boilerplate for simple single-agent patterns. You declare `model + system prompt + tools`; the harness manages the full agent loop:

```
reason → select tool → execute → return results → manage context window → handle errors
```

Infrastructure (compute, session isolation, tooling, memory, identity, security) is stitched together automatically. The AgentCore CLI (`agentcore deploy`) handles the infrastructure-as-code path via CDK (Terraform support coming). No additional charge for the harness, CLI, or pre-built skills.

Critical distinction the AWS team themselves draw: *"the labor of writing orchestration code shifts to AWS, but the judgment of designing the agent stays with the developer."* Configuration files replace code, but architectural choices — knowledge granularity, tool selection scope, role distribution — still require human judgment.

---

## Where They Play the Same Game

| Capability | AgentCore | Armature |
|---|---|---|
| Config-driven agent definition | API declaration / managed harness | YAML `HarnessSpec` |
| Model agnosticism | Claude, GPT-4o, Gemini, Nova, Llama, Mistral, and more | Any litellm-supported provider |
| Tool safety / policy | Cedar language, real-time Gateway interception | `ToolSafetyRule` + `SafetyCondition` YAML DSL |
| Execution tracing | OTel + CloudWatch dashboards | `TraceStore` (SQLite), OTel span emission |
| Quality evaluation | Evaluations service (live traffic scoring) | IHR computation, quorum scoring |
| Multi-agent composition | A2A protocol (agent-to-agent calls) | `SubagentNode`, fan-out/fan-in DAG |
| Memory | Memory service (short-term + long-term) | `TraceStore`, `MemoryStore`, `SessionStore`, `BootstrapStore` |
| Checkpoint / resume | Persistent filesystem (preview) | `CheckpointStore` (SQLite-backed) |
| Retry / recovery | Infrastructure-level session restart | `on_fail` loop with `until` conditions + exponential backoff |
| Observability | CloudWatch + OTel | OTel spans, basic telemetry module |

---

## Where AgentCore Has the Clear Edge

### 1. Managed Infrastructure
AgentCore's entire value proposition. MicroVM isolation per session, auto-scaling, health monitoring, versioned deployments with rollback, WebSocket streaming, 8-hour async jobs. Armature is a Python library — you provide and operate the compute.

### 2. Tool Ecosystem Depth
Gateway wraps any API/Lambda/OpenAPI spec in minutes without code changes. Registry provides org-wide semantic tool discovery with a governance workflow (publish → review → approve). Pre-built integrations for enterprise SaaS (Salesforce, Slack, JIRA, Zoom). Armature's registry is Python callables you register manually; there is no discovery layer.

### 3. Browser + Code Interpreter
Fully managed, sandboxed, auto-scaling. Armature can `shell_run` scripts via the built-in tool, but nothing comparable to a managed browser runtime with Playwright support and session-level isolation.

### 4. Identity and Auth
First-class inbound (IAM / OAuth 2.0) and outbound (agent-to-third-party) authentication. Cedar policy language is significantly more expressive and auditable than Armature's YAML safety rules. Armature has no authentication layer at all — callers are trusted implicitly.

### 5. Payments
Completely unique to AgentCore. No analog exists in Armature or any other open-source agentic framework at time of writing. The x402 microtransaction protocol with Coinbase / Stripe integration opens up agent-driven commerce patterns that are otherwise impossible to implement cleanly.

### 6. Long-Term Cross-Session Memory
AgentCore Memory automatically extracts and persists facts, preferences, and summaries across sessions using semantic indexing, and makes that memory searchable and shareable across agents. Armature's `MemoryStore` is in-process and session-scoped. `TraceStore` captures raw I/O but does not auto-extract or structure long-term knowledge.

### 7. Production Observability
CloudWatch dashboards with per-step trace visualization, built-in Evaluations scoring on live traffic, and a unified view across all agent steps. Armature's OTel integration emits spans but leaves dashboarding, alerting, and evaluation entirely to the operator.

### 8. Enterprise Ecosystem
AgentCore is designed to drop into existing AWS and enterprise environments — existing IdPs, existing Lambda functions, existing CloudWatch stacks. For teams already running on AWS, the integration cost is near-zero. Armature requires you to wire its outputs into whatever observability and ops tooling you have.

---

## Where Armature Has the Clear Edge

### 1. Explicit Multi-Stage DAG with Typed Data Flow
This is the deepest structural difference. Armature workflows are directed acyclic graphs of typed stages: `research → judge → writer`, each with a declared `Signature` specifying input and output types. Cross-stage type mismatches are caught at spec-load time, not at runtime. The DAG is the unit of composition.

AgentCore's Managed Harness is a single flat agent loop — one model, one system prompt, one tool set. There is no concept of sequential stages with typed handoffs between them. A2A multi-agent composition is peer-to-peer async calls between independent agents, not a declared workflow graph with guaranteed data flow contracts.

### 2. Role Specialization
Armature's `worker`, `judge`, `researcher`, and `orchestrator` role types carry distinct prompt preambles, model-tier mappings, temperature defaults, and quality behavior. A judge stage in Armature is structurally and behaviorally different from a worker stage — at the spec level, not just the system prompt level. In AgentCore, role differentiation is entirely a matter of what you write in your system prompt.

### 3. Automatic Self-Improvement
This is Armature's most distinctive capability cluster, and it has no AgentCore equivalent:

- **`OptimizerRunner`** — analyzes execution traces and proposes concrete YAML spec diffs (e.g., "add `output_mode: guided_json` to the judge stage") using a meta-harness LLM workflow. `ProposalStore` retains history so proposals are context-aware across runs.
- **`BootstrapStore`** — retrieves high-quality (I/O pairs, `quorum_score`) examples from `TraceStore` and injects them as few-shot demonstrations into system prompts. Expected 10–30% quality improvement on structured tasks without fine-tuning.
- **`AutoHarness`** — synthesizes entire harness specs from natural language task descriptions, iterating through draft → validate → constraint-check → refine cycles with structured error feedback fed back to the LLM.

AgentCore's Evaluations service measures quality. It does not mutate agent definitions, propose spec changes, or inject few-shot examples based on what it observes.

### 4. Quorum Voting
The `Quorum` skill runs a stage across N models and synthesizes a consensus answer, using voting or scoring across responses. This is Armature's primary mechanism for quality assurance on uncertain or high-stakes outputs. No equivalent exists in AgentCore.

### 5. Retry Loop Convergence
`on_fail` loops with `until` conditions give Armature the ability to keep retrying a stage until its output satisfies a declarative predicate, with exponential backoff. This is workflow-level recovery logic — the spec defines what "good enough" means and the engine enforces it. AgentCore's error recovery is infrastructure-level (session restart), not workflow-logic-level.

### 6. Fan-Out / Fan-In with Typed Aggregation
Armature's `partition_source` / `fan_out` / `fan_in` mechanism splits a list of inputs across parallel worker stages and aggregates results with configurable strategies (concat, union, first, majority). This is structured parallel processing declared in the spec. AgentCore supports parallel agents via A2A calls but not structured partition/aggregate within a declared workflow graph.

### 7. Cost and Vendor Independence
Armature runs on any Python runtime — a laptop, a bare-metal server, Kubernetes, or alongside any cloud. No per-operation charges. AgentCore's pay-per-use model accumulates: Runtime per compute-millisecond, Memory per operation, Browser per session-minute, Code Interpreter per execution-second. For high-volume or cost-sensitive workloads, this adds up fast. Armature has no AWS dependency.

---

## The Core Strategic Distinction

| Dimension | AgentCore | Armature |
|---|---|---|
| **Category** | Managed infrastructure platform | Workflow composition framework |
| **Primary value** | Operate agents at scale without managing infra | Define complex typed workflows that self-improve |
| **Agent model** | Single flat agent loop (Managed Harness) or bring-your-own framework | Typed DAG of role-specialized stages |
| **Self-improvement** | Evaluate → observe (no mutation) | Evaluate → propose diff → bootstrap examples → synthesize specs |
| **Tool integration** | Deep (Gateway, Registry, MCP ecosystem, enterprise SaaS) | Thin (callable registry, safety rules) |
| **Quality control** | Evaluation scoring on live traces | Quorum voting, IHR, output validation, Optimizer |
| **Infrastructure** | AWS-managed, serverless, microVM isolation | Self-hosted, bring-your-own-compute |
| **Auth / identity** | Full enterprise grade | None |
| **Payments** | Yes (x402, Coinbase, Stripe) | No |
| **Typed data flow** | No (no inter-stage contracts) | Yes (cross-stage Signature validation) |
| **Cost model** | Pay-per-use (can be significant at scale) | Open source, runs on owned compute |
| **Vendor lock-in** | AWS-native | None |

AgentCore is what enterprise teams reach for when **"get agents into production without building infrastructure"** is the top constraint. It solves ops, auth, scaling, observability, and tool discovery.

Armature is what teams reach for when **the agent's reasoning structure is the hard problem** — workflows where different stages must play different roles, where output quality must be verifiable and improvable over time, and where you want the harness to help write better versions of itself.

### Complementarity

These two are not really competing in the same category. They solve different halves of the production agentic problem:

- AgentCore answers: *Where does it run? How does it scale? How do tools connect? How do we auth?*
- Armature answers: *How should the workflow reason? How do we ensure quality? How does the workflow improve itself?*

The natural synthesis: **Armature workflows deployed on AgentCore Runtime**, with AgentCore Memory for cross-session persistence, Gateway for enterprise tool integration, Policy for Cedar-enforced guardrails, and Observability for CloudWatch dashboards — while Armature owns the DAG composition, role specialization, quorum voting, and self-improvement loop.

---

## Gaps AgentCore Reveals in Armature

The comparison also surfaces areas where Armature is thin relative to production needs:

1. **Tool ecosystem** — Armature's callable registry works well for small, controlled tool sets but has no semantic discovery, no governance workflow, and no pre-built integrations. Gateway-style wrapping of arbitrary APIs is not a solved problem in Armature.

2. **Long-term memory** — `TraceStore` stores raw I/O faithfully but does not extract structured knowledge across sessions. There is no semantic search across prior runs. AgentCore Memory's automatic insight extraction is meaningfully more useful for agents that need to "learn" from past interactions.

3. **Auth** — Armature has no identity layer. Any production deployment has to bolt auth on at the infrastructure level (reverse proxy, API gateway, etc.). AgentCore Identity solves this natively.

4. **Browser / code execution** — Sandboxed, managed browser and code interpreter are significant capabilities for agents doing web research or data analysis. Armature's `shell_run` tool is a footgun by comparison.

5. **Policy expressiveness** — Cedar is a well-designed policy language with decades of prior art. Armature's YAML safety rules handle common cases but are not Turing-complete or formally verifiable.

6. **Observability depth** — Per-step trace visualization with live evaluation scoring is significantly more actionable than Armature's current OTel span emission.

---

*Last updated: 2026-05-11*
*Sources: AWS documentation, AWS ML Blog, DEV Community (AWS Builders), SiliconANGLE*

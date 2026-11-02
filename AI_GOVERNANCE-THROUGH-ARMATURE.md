# MEMO — Governed Agentic AI: A Reference Pattern for the AI Governance Requirements

**To:** [Governance review board / AI Governance stakeholders]
**From:** [Bryan Sparks]
**Date:** 2026-11-02 (rev. 2 — refreshed for engine 0.10.x; first issued 2026-09-24)
**Re:** How agentic AI workflows can satisfy governance requirements through engine-enforced control — with Armature as a working reference implementation

---

## Purpose

Our AI Governance requirements were written largely with two modes of AI use in mind:
humans using AI assistants interactively, and traditional ML/data science pipelines.
Agentic AI — systems that pursue multi-step goals and take actions with minimal
supervision — is the emerging third mode, and it is the mode where traditional
controls erode fastest: output review becomes impossible (thousands of calls per
run), scope becomes whatever the agent decides it is, and cost and data exposure
become emergent rather than declared.

This memo describes a concrete pattern for governed agentic AI in which every
major governance concern is enforced by the execution engine itself — testable and
auditable rather than memo-enforced — and points to Armature, an open workflow
engine for agentic teams, as a working reference implementation of that pattern.

## The pattern: engine-enforced control

The core idea is simple: **an agent's authority should be declared, reviewed, and
machine-enforced — never emergent.**

1. Every agentic workflow is a **declarative specification** (a versioned YAML
   document), reviewed and approved like any other change to production systems —
   with an audit trail of who approved which instruction set, and when.
2. The engine executes exactly that specification: a fixed graph of declared steps,
   under hard budgets, with guardrails at the enforcement layer (not in prompts),
   and a complete trace of every model call, tool call, and decision.
3. What was approved is what runs: workflow definitions are sealed with integrity
   digests, and any post-approval tampering fails the run before execution.

Armature (https://github.com/bryansparks/armature) implements this pattern
end-to-end. The mapping below shows how its mechanisms line up against the
governance concerns common to the requirements set.

## Governance concern → engine mechanism (Armature)

| Governance concern | How Armature enforces it |
|---|---|
| **Approved, reviewable AI configurations** | Every workflow is a declarative spec under version control — diffable and reviewable in a pull request, with full history of approval. The spec *is* the boundary: agents cannot improvise scope beyond their declared task graph. |
| **Bounded autonomy** | Hard execution budgets (`max_iterations`, `max_llm_calls`, `timeout_hours`), declared dependency graph, explicit retry/loop limits. An agent cannot run open-ended or wander outside its assigned steps. |
| **Bounded autonomy across process boundaries** | When work is distributed to cloud executors (the missions transport), the same bounds travel with it: each work unit carries a declared posture (`human-led` by default — executors notify, never act — with explicit per-unit delegation), a per-unit `budget_usd` cap, and a closure contract that defines what a legitimate completion looks like. A sweep settles every unit — success, failure, or incomplete closure is recorded, never assumed. Autonomy does not expand because execution moved. |
| **Guardrails on tool use** | Safety rules enforced at the tool-dispatch chokepoint — not in prompts, which are bypassable by prompt injection. Per-tool permission levels, shell-command classification, path-traversal containment on file operations, and Docker sandbox isolation for execution stages. |
| **Third-party tool provenance** | Workflow packs are the supply-chain surface for agent capability, and they are gated: `armature pack validate` checks the pack manifest and that every adapter's script command is static (no shell interpolation), adapters' parameters are checked against a declared JSON Schema, and tool and MCP-server access is restricted by declared allowlists. A pack cannot smuggle in an undeclared tool or a dynamic shell command. |
| **Prompt-injection resistance** | Template inputs render in an immutable Jinja2 sandbox — no attribute writes, no dangerous builtins — and `armature validate --strict-templates` refuses any template the sandbox would not evaluate safely, before approval rather than at runtime. |
| **Access control on agentic work itself** | The optional platform server extends governance from the engine to *who may launch what*: named teams with role-based access (instance and team scoping), TOTP two-factor and OIDC login, invites, and an audit log. Agentic workloads get the same access governance as any other production system. |
| **Human-in-the-loop checkpoints** | `gate: human` stages make approval a first-class execution step: the workflow pauses until a named human signs off, and the approval is recorded in the run trace. Structured decision gates handle classify/choose/score judgments deterministically, without an LLM. |
| **Data governance & confidentiality** | Context governance is declarative: each workflow and stage declares what context it must and must **never** see (stage outputs, runtime inputs, named layers). Sensitive data is structurally excluded from stages — not trusted to prompt discipline — and the effective policy is recorded on every trace row. |
| **Model governance & inventory** | Models are referenced only through named tiers defined in one block: a complete, current inventory of every model in use with provider, sampling parameters, and key handling. Swapping or deprecating a model is a one-line, reviewable change. No shadow model usage; every call's model is traceable per row. |
| **Auditability & traceability** | Every LLM call, tool call, and stage transition is written to a per-run trace database with inputs, outputs, model, and effective context policy. Every run produces a receipt (run id, duration, exit code, engine version, artifact digests). |
| **Integrity & tamper evidence** | Workflow packages seal every file with a SHA-256 integrity manifest, verified at build, again before every run, and on demand. A workflow changed after approval fails closed before executing. |
| **Resiliency & failure containment** | Fail-closed posture throughout: missing secrets abort before execution; incomplete packages refuse to run; output parse failures escalate or fail explicitly. Checkpoint/resume with a session lock means a crashed run restarts exactly where it stopped — no duplicated or diverged effects. Credential and quota exhaustion is surfaced, never swallowed. |
| **Explicit fault-tolerance decisions** | Which stages may fail without aborting the run (`fail_as_value`) is a declared, reviewed property of the spec — not emergent runtime behavior. |
| **Output assurance** | Strongly typed stage outputs (JSON schemas), judge stages for quality gating, quorum scoring, and a post-run self-analyst that reviews each completed run and records quality findings. |
| **Cost & usage control** | Central budget enforcement (runs proceed only under a declared budget), per-workflow call caps, and per-run cost/duration records in the receipt. |
| **Reproducibility** | A workflow is a portable, verified package — spec, tools, inputs, integrity digest — that runs identically anywhere. What was approved is exactly what executed. |

**Why engine enforcement matters:** policy memos describe intended behavior; engines
enforce actual behavior. Armature's guardrails are exercised by a 4,700+-test
suite (plus a separately-gated suite for the cloud transport), which means the
controls themselves are continuously verified — a claim a
document-only policy cannot make.

## Context as a governed contract

The rows above treat context mostly as a restriction — what a stage must **never**
see. That is the security half. The effectiveness half matters just as much to
governance, and it is the half most often left to prompt engineering.

The argument is simple. An agentic team acts on exactly what is in its context:
its mission, the outputs of prior stages, memory of earlier runs, retrieved
knowledge. That context *is* the operational reality the team works from —
under-provisioned context is a failure mode with the same blast radius as
over-provisioned context. A researcher who cannot see prior findings invents
them; a judge who cannot see the deliverable grades the wrong thing; a writer
without the source material paraphrases confidently. If a stage's authority is
declared and enforced, but its *inputs* are improvised, then what it actually
does is still improvised.

The governance answer is the same pattern applied to inputs: **the context
contract should be declared, versioned, and auditable — not implicit.** Armature
implements this today:

| Contract element | Mechanism (declared in the spec) |
|---|---|
| Mission & orientation | A workflow-level `mission` inherited by every stage; missions sourced statically or per work unit (`mission_source`), with posture, budget, and closure semantics in mission documents |
| Principles & invariants | Named `context_layers` (style rules, domain constraints, safety principles) forced into stage prompts by policy, with precedence — invariant content is declared once and reviewed, not pasted into every prompt |
| Scoping | Stage `signature.input` declares exactly which upstream outputs a stage consumes, in a typed contract — cumulative-by-default context is narrowed to what the stage is accountable for |
| Restrictions | `context_policy` (`must`/`never`) closes sensitive outputs, inputs, and layers at any level; a closure always wins over a force; the effective policy is recorded on every trace row |
| Cross-run memory | Continuation/carry-forward and per-conversation memory carry prior results forward under the same declared scoping; persistent knowledge lives in a typed store with provenance, not free-floating prompt text |

The audit story is the same as for tools: because the context contract is part
of the versioned spec, a reviewer approving the spec is approving what the team
will see — and the trace records what it actually saw.

One honest boundary: retrieval is currently provisioned declaratively and
passively. Active, on-demand context navigation — stages fetching what they
need from memory under the same policies, rather than receiving what a spec
author guessed they'd need — is designed and implemented on a development
branch, and independent evaluations through 2026 have consistently found that
scoped, on-demand retrieval beats passive injection once context volume grows.
The pattern above is built so that transition changes no governance property:
navigated context passes through the same policies and the same trace.

## What this pattern does not cover (stated plainly)

To be credible, this memo should also be honest about scope:

1. **It governs agentic workflows, not the whole AI estate.** Retention policies,
   vendor due diligence, training requirements, procurement, incident response
   procedure, and use-case risk classification remain matters for the broader
   governance program. This pattern is an answer to "how do we let agents act at
   all, safely" — not to the entire requirements set.
2. **It only governs work that runs through it.** An unsupervised agent loop run
   outside an engine of this class is untouched by these controls — and is exactly
   the risk the requirements should be most concerned with. The implication for
   policy: agentic AI use should be *required* to run under engine enforcement of
   this kind, not merely permitted to.
3. **A reference implementation is not a certification.** Armature demonstrates
   that the controls are achievable and testable today. Formal adoption would
   still follow our normal tool review, ownership, and risk processes.

## Recommendation

1. Treat **engine-enforced control** (declared authority, machine-enforced
   guardrails, complete traces, integrity sealing, fail-closed operation) as the
   minimum bar for any agentic AI use in the organization. A declared, auditable
   **context contract** is part of that bar: approving an agentic workflow means
   approving what it may do *and* what it will see.
2. Use Armature as a **reference implementation** when evaluating the requirement
   set against agentic use cases — a concrete, inspectable answer to "what would
   compliance look like for agents?"
3. Require agentic AI workloads to demonstrate this class of control before
   approval, regardless of the specific engine used.

---

*Supporting detail: Armature's architecture and control surface are documented in
`docs/ARCHITECTURE.md`, `docs/SAFETY-AND-GOVERNANCE.md`, `docs/CONTEXT-ISOLATION.md`,
`docs/MISSION-AS-CONTEXT.md`, `docs/MEMORY-AND-CONTEXT.md`,
`docs/SANDBOX-AND-ISOLATION.md`, and `docs/WORKFLOW-PACKAGES.md` in the repository.*

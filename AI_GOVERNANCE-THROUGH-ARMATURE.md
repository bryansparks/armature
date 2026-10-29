# MEMO — Governed Agentic AI: A Reference Pattern for the AI Governance Requirements

**To:** [Governance review board / AI Governance stakeholders]
**From:** [Bryan Sparks]
**Date:** 2026-09-24
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
| **Guardrails on tool use** | Safety rules enforced at the tool-dispatch chokepoint — not in prompts, which are bypassable by prompt injection. Per-tool permission levels, shell-command classification, path-traversal containment on file operations, and Docker sandbox isolation for execution stages. |
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
enforce actual behavior. Armature's guardrails are exercised by a 2,000+ test
suite, which means the controls themselves are continuously verified — a claim a
document-only policy cannot make.

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
   minimum bar for any agentic AI use in the organization.
2. Use Armature as a **reference implementation** when evaluating the requirement
   set against agentic use cases — a concrete, inspectable answer to "what would
   compliance look like for agents?"
3. Require agentic AI workloads to demonstrate this class of control before
   approval, regardless of the specific engine used.

---

*Supporting detail: Armature's architecture and control surface are documented in
`docs/ARCHITECTURE.md`, `docs/SAFETY-AND-GOVERNANCE.md`, `docs/CONTEXT-ISOLATION.md`,
`docs/SANDBOX-AND-ISOLATION.md`, and `docs/WORKFLOW-PACKAGES.md` in the repository.*

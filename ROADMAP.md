# Roadmap

Armature is stable at v0.1.0. The features below are directional — not committed dates or release targets.

---

## Near-term (execution continuity)

**Audit replay with re-execution**
`armature replay <run_id>` currently displays a recorded run from the TraceStore. The next step is true re-execution: reconstruct the full context from the cache and re-run from any stage, enabling deterministic regression testing and post-mortem debugging without re-paying LLM costs.

**Fork-and-diff**
`armature fork <run_id> --at-stage <id> --spec new.yaml` — take a historical run, branch it at a specific stage with a different spec, and compare outputs side-by-side. This makes spec iteration cheap: change one stage, re-run from that point, see the delta.

**Event sourcing**
Replace the current append-only session log with a full event store: every harness decision (stage start, tool call, block, escalation, output) is a first-class event. Enables time-travel debugging, audit compliance, and deterministic replay.

---

## Medium-term (scaling and operations)

**Model-tier auto-registration**
Run a benchmark suite against configured providers; automatically assign model tiers based on measured cost/quality tradeoffs. Removes manual tier configuration for common providers.

**Multi-tenant hosted execution**
Namespace trace stores, memory stores, and artifact stores by tenant ID. Enables SaaS products to run per-user workflow instances without cross-contamination.

**Visual spec editor**
Browser-based DAG editor that reads and writes YAML specs. Drag stages, wire dependencies, configure safety rules — no YAML required. Exports a spec that runs directly in Armature.

---

## Longer-term (ecosystem)

**Registered model fine-tuning loop**
After `armature export-traces` produces an SFT dataset, automatically submit it to a fine-tuning provider (OpenAI, Anthropic, Together) and register the resulting checkpoint as a new model tier in the spec.

**Distributed execution**
Fan-out stages currently use `asyncio.gather` within one process. A distributed backend (Ray, Celery, or native async queues) would let fan-out branches run across machines — enabling workflows that exceed single-machine parallelism or memory limits.

**Spec marketplace**
A registry of community-contributed workflow specs and tool modules. `armature pull research-pipeline` fetches a verified spec and its dependencies.

---

## Deferred (pending validation)

These were considered during the research integration phase and deferred pending evidence that they address real user pain:

- **Reinforcement learning from IHR** — use IHR as a reward signal for automated prompt evolution (requires a stable benchmark environment)
- **Cross-workflow memory federation** — share memory stores across workflow specs (raises governance questions not yet resolved)
- **Spec versioning and migration** — automatically migrate older specs when the schema changes (deferred until the schema stabilizes)

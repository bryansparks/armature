# Armature Roadmap

Features below are directional — no committed dates or release targets.

---

## v1 — Current state (complete)

All v1 scope is implemented. 1,330 tests passing.

The v1 harness delivers: YAML-declarative workflow execution, four role types, DAG orchestration with automatic parallelism, fan-out/fan-in, IHR quality scoring, self-improvement loop (inner + outer), prediction-verification accountability, declarative safety rules with strict mode, human-in-the-loop gates, checkpoint/resume, four memory layers (mission context, continuation, MemoryStore, KnowledgeStore), model tiers, a REST API with SSE streaming, the `armature watch` trigger daemon, and a Rich terminal dashboard.

The `armature export-traces` command is also shipped — it produces LoRA-ready SFT and DPO training data from accumulated traces, quality-filtered by judge quorum score. The infrastructure for fine-tuning is in place. Executing the fine-tuning pipeline itself is the v2 story below.

---

## v2 — The SLM fine-tuning loop

The single most impactful capability not yet wired end-to-end is closing the **fine-tuning flywheel**: taking the training data that `export-traces` already produces and automatically submitting it to a GPU compute provider, running the LoRA fine-tune, and registering the resulting checkpoint as a new model tier in the spec.

### Why this matters

Running Armature workflows generates high-quality domain-specific training data as a side effect — because the judge tier selects only the best outputs and quorum scores rank them. This data has properties that make it unusually good for fine-tuning:

- **Quality-filtered automatically** — quorum scores do the curation work; no manual labeling
- **Structurally consistent** — harness-assembled prompts are uniform; outputs are schema-validated JSON
- **Domain-specific** — traces reflect your actual task vocabulary, not generic web text

Fine-tuning a 7B–14B SLM on 500–2,000 of these traces for a specific workflow stage produces a model that:
1. Follows the expected output schema on the first attempt (higher HFR, fewer retries)
2. Uses domain vocabulary and reasoning patterns correctly
3. Runs locally — no API call, no data leaving your infrastructure
4. Costs a fraction of a cloud frontier call at inference time

The practical result: over time, frontier models earn their own obsolescence for specific tasks. You start with a frontier model for a stage, accumulate traces, fine-tune a small specialist, register it as the stage's model tier. The frontier model moves to the judge and optimizer roles — where it is genuinely needed. Workers and researchers run on fine-tuned SLMs.

### The two-axis improvement story

This is why the fine-tuning loop is categorically important, not just a cost optimization:

- **Axis 1 (current):** `armature improve` refines the *spec* — stage descriptions, retry limits, output schemas. IHR goes up because the instructions get clearer.
- **Axis 2 (v2):** The fine-tuning loop refines the *model* — the worker and researcher stages become more capable on the specific domain. IHR goes up because the executor gets better.

Both axes are available. They are orthogonal and they reinforce each other: a better spec produces better traces, which produces better fine-tuning data, which produces a better model, which produces better traces, which drives better spec improvements.

### The GPU infrastructure plan

LoRA fine-tuning requires GPU access. The proposed integration is [Modal](https://modal.com) — a serverless GPU compute platform. The workflow would be:

1. Developer runs `armature export-traces --workflow my-workflow --output training.jsonl --min-score 0.85`
2. Developer configures a Modal account (or another supported provider) in Armature settings
3. `armature finetune --training training.jsonl --base-model qwen2.5-7b --output my-specialist` submits the LoRA job to Modal, waits for completion, downloads the adapter
4. `armature register-model --name my-specialist --adapter ./my-specialist` registers the checkpoint as a new model tier
5. Developer updates the relevant stages in their spec to use `model_tier: my-specialist`

This is a developer workflow, not end-user facing. The developer controls which provider, which base model, and which stages get specialized. Armature handles the data export format, the job submission, and the tier registration. The actual compute happens on infrastructure the developer already has or rents.

Supported providers to consider: Modal (first), Together AI, RunPod, Lambda Labs, Replicate. Any provider with an API-accessible fine-tuning endpoint and LoRA support can be added as an adapter.

### What gets better with fine-tuned SLMs

Concretely, per stage type:

**Worker stages** — schema adherence improves dramatically (fewer `OUTPUT_INVALID` hits). Domain vocabulary accuracy improves. HFR (first-attempt compliance) goes up. Latency drops from 500ms+ (cloud API) to 50–150ms (local inference).

**Researcher stages** — fact extraction patterns improve. The model learns which sources are relevant in your domain and which citation formats your downstream stages expect.

**Data sovereignty** — once fine-tuned, the model runs entirely on your infrastructure. Sensitive data (customer records, proprietary documents, internal analyses) never leaves your network at inference time.

---

## Near-term (execution continuity)

**Audit replay with re-execution**
`armature replay <run_id>` currently displays a recorded run from the TraceStore. True re-execution: reconstruct full context from the LLM cache and re-run from any stage — deterministic regression testing and post-mortem debugging without re-paying LLM costs.

**Fork-and-diff**
`armature fork <run_id> --at-stage <id> --spec new.yaml` — branch a historical run at a specific stage with a different spec and compare outputs side-by-side. Makes spec iteration cheap: change one stage description, re-run from that point, see the delta immediately.

**Full event sourcing**
Replace the current append-only session log with a proper event store: every harness decision (stage start, tool call, block, escalation, output) is a first-class immutable event. Enables time-travel debugging, strong audit compliance, and deterministic replay for regulated environments.

---

## Medium-term (scaling and operations)

**Model-tier auto-registration**
Run a benchmark suite against configured providers and automatically assign model tiers based on measured cost/quality tradeoffs for your specific workflow. Removes manual tier configuration.

**Multi-tenant hosted execution**
Namespace trace stores, memory stores, and artifact stores by tenant ID. Enables SaaS products to run per-user workflow instances without cross-contamination and with per-tenant quality tracking.

**Visual spec editor**
Browser-based DAG editor that reads and writes YAML specs. Drag stages, wire dependencies, configure safety rules — no YAML required. Exports a spec that runs directly in Armature. Particularly useful for non-engineers editing workflows.

---

## Near-term (reward signals and trace intelligence)

### Why IHR is not a reward signal

This distinction is worth stating clearly because it's easy to conflate the two. IHR is an excellent *health metric* — a composite number that tells you whether your workflow is trending better or worse across runs. It is a poor *reward signal* for training or optimization for three reasons:

**Sparse.** One value per completed run. You cannot use it to credit individual stage decisions — by the time you have IHR, the entire run has already executed and been paid for.

**Lagging.** IHR is a consequence of what stages did, not a signal you can act on during execution. It tells you how the run went, not which specific decisions caused the outcome.

**Non-causal.** A high IHR doesn't tell you *why* stage 3 succeeded. It tells you the whole run went well. Credit assignment — attributing the outcome back to specific stage behaviors — is invisible from IHR alone.

This is the classic sparse terminal reward problem in reinforcement learning. The field's answer is **process reward models (PRMs)**: instead of rewarding only the final outcome, reward each step of the process. Armature already collects exactly the data needed for this — per-stage success, quorum scores, retry sequences, escalation patterns, and final IHR — it just isn't analyzed as a causal structure yet.

---

**Stage credit attribution (near-term)**

Analyze accumulated traces to identify which stages are most predictive of final IHR — the "leverage stages" that, when they go well, pull the whole run up with them. Not all stages are equally important to final quality; in most workflows there are 1–2 stages whose quorum score has high correlation with IHR and several that are relatively independent noise.

Beyond leverage identification, trace analysis surfaces several distinct pattern types:

- **Retry quality** — when a stage fails and retries with `_last_error` injected, does the second attempt actually succeed? Or does the stage fail the same way repeatedly? Retries that succeed indicate a prompt could be sharpened to get the first attempt right; retries that also fail indicate a structural prompt problem that increasing `max` retries won't fix.

- **Cascade correlation** — does a good output from stage A predict better outcomes at stage B downstream? If yes, improving stage A is a multiplier. The optimizer currently treats all stages equally; leverage-weighted targeting changes that.

- **Context utilization** — which context keys injected into a stage's prompt appear to be unused (no reference in output, no downstream dependence on them)? Cluttered context degrades LLM output quality. Identifying and pruning unused injections is a clean prompt-quality win with no spec restructuring required.

- **Escalation ROI** — when `on_fail.loop` escalates to a higher model tier, does the upgraded model succeed? Or does it also fail? Escalation that reliably succeeds signals a model capability boundary (right fix: upgrade the base tier). Escalation that also fails signals a spec problem (right fix: rewrite the prompt, not raise retry limits).

What this enables: `SelfImproveRunner` currently proposes changes without knowing which stages are highest-leverage. With credit attribution, the optimizer targets the stages that will produce the largest IHR improvement first — instead of treating a low-impact stage the same as a high-impact one.

Surface this in `armature dashboard` as a per-stage leverage heatmap, and incorporate leverage weights into the `DiagnosticAnalyzer` prioritization.

---

## Longer-term (process reward models and trajectory intelligence)

**Trajectory-level process reward model**

The stage credit attribution above is still a run-level analysis — it tells you which stages matter more, but it doesn't learn to *predict* final IHR from partial run state. A process reward model goes further: it is a learned function that takes the execution state at any point mid-run and predicts the probability of reaching a high-IHR outcome.

The data to train this already accumulates in the TraceStore: partial-run traces as inputs, final IHR as the label. A lightweight model (gradient-boosted trees or a small neural net) can learn this mapping from a few hundred completed runs.

Once the PRM exists, it enables three things that aren't possible today:

**Early-exit on doomed runs.** If the first two stages produce patterns associated with low IHR outcomes, abort the run before the expensive downstream stages execute. This is meaningful cost savings for long, multi-stage workflows — you pay for a failed classification, not for the full 45-minute pipeline that was going to fail anyway.

**Early human escalation.** Rather than waiting for a judge to flag low confidence at the end, surface a warning when early-stage patterns predict quality problems. A human can review and redirect before irreversible downstream work happens.

**Downstream-weighted fine-tuning.** This is the connection back to the SLM fine-tuning loop. Current trace export filters by per-stage quorum score: a stage output that scored 0.85 is included in training data. With a PRM, you can weight training examples by their *downstream impact* — a stage-A output that scored 0.85 and led to a final IHR of 0.93 is a more valuable training example than one that also scored 0.85 but was followed by IHR 0.61. The first example was load-bearing; the second may have been good in isolation but didn't actually carry the run. Downstream-weighted DPO pairs are categorically richer training data than per-stage-filtered SFT data.

The compounding effect: a PRM trained on traces improves fine-tuning data quality, which produces better SLMs, which produce better traces, which produce a better PRM. This is a genuine second-order flywheel on top of the spec improvement loop.

This is research-phase work — it requires enough accumulated traces to train from, and a benchmark environment to evaluate against reward hacking. But the data infrastructure is already there. The gap is the analysis layer.

---

## Longer-term (ecosystem)

**Spec marketplace**
A registry of community-contributed workflow specs and tool modules. `armature pull compliance-review-pipeline` fetches a verified spec and its dependencies. Teams share proven patterns rather than rebuilding from scratch.

**Distributed fan-out**
Fan-out stages currently use `asyncio.gather` within one process. A distributed backend (Ray or native async queues) would let fan-out branches run across machines — enabling workflows that exceed single-machine parallelism or memory limits at very large scale.

**Compile-time optimization (teleprompters)**
Offline multi-pass spec optimization before deployment, similar to DSPy's compilation approach. High effort — consider whether using DSPy directly for individual stages is the better path rather than reimplementing inside Armature.

---

## Deferred (pending validation)

These were considered and deferred pending evidence they address real user pain:

- **Reinforcement learning from IHR** — originally framed as using IHR directly as a reward signal for automated prompt evolution. Superseded by the process reward model approach above, which recognizes that IHR is a useful health metric but a poor training signal (sparse, lagging, non-causal). The PRM approach addresses these limitations properly; this item is deferred pending the trace volume and benchmark environment the PRM approach requires.
- **Cross-workflow memory federation** — share memory stores across workflow specs (raises governance questions not yet resolved)
- **Spec versioning and migration** — automatically migrate older specs when the schema changes (deferred until the schema stabilizes across several releases)

---

*Armature follows the principle that made it good to begin with: ship what is proven, defer what is speculative, and let accumulated traces tell you what to build next.*

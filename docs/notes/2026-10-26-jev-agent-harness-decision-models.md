# JevAgentHarness — decision models sharing control with LLMs

**Date:** 2026-10-26
**Source:** arXiv:2610.21412v1 (submitted 2026-10-15), Kunlun Zhu, Tao Feng, Xu Han, Heng Wang, Ling Yang, Xiangru Tang, Jiaxuan You, Heng Ji (cs.AI). Code: github.com/JevAgentHarness/JevAgentHarness (CC BY-NC-SA 4.0).
**Primary source:** https://arxiv.org/html/2610.21412v1 (paper citations below are to this HTML).
**Armature context:** We incorporated Jev (the Typesafe/System One decision API) as a Layer-1 script adapter in PR #24 (182583a) + docs PR #25 (ee0b2d0), merged to main: `examples/decision-typesafe/` — A/B'd against an LLM judge, 20 states, measured 2026-09-21. This paper is the vendor-model's full research treatment: what does it offer past what we have?

---

## 1. What the paper is

A framework for **delegating agent decisions to a small decision model that shares control with the LLM** — not replacing it, cascading: the decision model answers multiple-choice decisions (tool choice, argument choice), and the LLM decides only when the decision model's confidence is below a fitted threshold.

**"Jev" defined (§2):** a commercial System-1 decision model from Typesafe — given a task, history, and one multiple-choice question, it returns a probability per option instead of generating text. Confidence = top option's probability, returned directly. ~2.6% of an LLM call's price, 28% of its latency.

**Core mechanism — the confidence cascade (§2.1, App. A):** delegate only when confidence c(s) ≥ τ_d where the optimal threshold is **c(s) ≥ q_L − ΔC/V** (q_L = the LLM's per-choice accuracy, ΔC = price of the saved LLM call, V = cost of a wrong choice). Fitted by running the agent on 150 calibration tasks with the decision model answering **in shadow mode** (silently, not affecting the run), then estimating the terms from those trajectories. Refit when the LLM, decision model, or task rules change (§4). Below threshold: the LLM decides; an unsure cheap answer wastes one cheap call, never risks the task.

**Critical empirical finding (Table 2, §7.2):** a naive fixed threshold (0.5) **lost 21.5 points** while saving 84.5%; the fitted threshold (τ=1.00 tools / 0.99 args for Jev+Flash — only near-certain answers) **cut cost 14.2% with zero changed outcomes** across 500 tasks. And per §1: the best isolated-question model (92.2% BFCL) solved only **69.5% of live agent tasks** — L1 accuracy overestimates L3 usefulness; without any fitted threshold, delegation **dropped success 19.0 points**.

**Benchmark (§3):** AgentDecisionBench at three levels — L1 isolated questions (BFCL v4 + When2Call items); L2 step-level on recorded runs scored against an answer key ("testing driving policies on recorded drives"); L3 full control of a live agent (τ²-bench, ALFWorld, SWE-bench Verified via mini-SWE-agent/SWE-agent, Terminal-Bench). **DecisionGym** (App. B.1): a scripted customer-service desk with 68 shuffled tools (7 core + 31 near-duplicates + 29 distractors) and answer keys valid even after detours; Shift and Hard variants.

**Trained model (§5):** AgentDecision-4B (Qwen3-4B + LoRA) SFT'd on 15,694 decision states, **30.9% of which follow the agent's own mistakes** (DAgger-style scripted mistake repetition) — the states off-the-shelf models never see. Targets mix gold labels with Jev's probabilities (α=0.3) plus a Brier calibration term (Eq. 1). GRPO in the loop added **no measurable gain** over SFT (4.5 GPU-hours + $26.51 wasted).

**Results (§7):** OOD shifted tasks: AgentDecision-4B solved 91.5% vs Jev 82.0% vs best-isolated-model 69.5%. Deployment: up to 89.0% cheaper, 2.5× faster, median 31 ms vs 809 ms per Flash call; on ALFWorld success *rose* 4.3 points. **Non-transfer results (§7.5, Table 1):** τ²-bench lost 8.1 points → not deployed; coding agents saved only 7.0–10.5% (free-text actions resist delegation); all 34 Jev task failures began with a **confident wrong irreversible choice** (usually cancelling when it should ask); a hard-rule retrain raised held-out accuracy 76.5%→95.3%.

**Stated limitations:** English-only; mostly Gemini agent LLMs; non-inferiority (3-pt) not equivalence tests; thresholds/models fitted per agent — new agents/tools require refitting; no adversarial/prompt-injection testing ("results hold for benign states only"); no RouteLLM comparison; the paper explicitly warns these adapters "should not be used as security boundaries."

## 2. Mechanism-by-mechanism Armature mapping

| Paper mechanism | Armature status | Evidence / what a slice looks like |
|---|---|---|
| Decision model as first-class callable (typed questions, probability output) | **Already have (Layer 1)** | `examples/decision-typesafe/` — `parse: json` script adapter, noul/choice/score questions, per-option probabilities captured and mocked in tests (`decide.py`, PR #24 182583a) |
| Confidence threshold + LLM fallback (the cascade) | **Partial — anticipated, not built** | The spike's own Layer-2 option (README L86–88): "use the returned probabilities as a router (escalate to an LLM only when mass splits across levels)". Not implemented; `guided_json` escalation triggers on *parse failure*, never on confidence |
| Threshold **fitting** (q_L − ΔC/V rule, 150-task shadow calibration) | **Missing** | No calibration procedure exists anywhere. Slice: a shadow-mode arm in the decision workflow — decision adapter answers silently beside the LLM stage over N runs, fit τ per question type, store in the spec |
| Multi-level evaluation (L1 isolated → L3 live-agent) | **Partial — L1 only** | Our A/B is L1 (isolated questions, n=20). The 69.5%-vs-92.2% finding says a Layer-2 go/no-go must include an L3-style eval (decision model answering inside a live workflow loop) before adoption |
| Irreversibility-aware delegation | **Missing (as decision policy)** | Armature gates irreversibility via `gate: human`/missions submit gates — but nothing modulates *decision-model* delegation by action reversibility. Paper: every Jev failure began with a confident wrong IRREVERSIBLE choice |
| Trained decision model on mistake states | **Not ours to build** | Vendor-side training. Relevant only as vendor-evaluation evidence (the recorded 2nd-vendor trigger for Layer 2): training on after-mistake states demonstrably fixes OOD (91.5% vs 82.0%) |
| Cascade inside runtime tool choice | **Mostly N/A structurally** | Armature's DAG makes tool decisions at *spec time* (the workflow author wires `tool_call:` stages), not at runtime — the paper's main use case (ReAct tool routing) has little surface here. Armature's decision surface is gates/judges/triage |

## 3. The delta — what this paper offers past what we have

**The paper supplies the missing design for the already-deferred Layer 2.** Layer 2 (native `decision:` stage type) was deferred 2026-09-21 with three reopening triggers: 2nd consumer, in-spec escalation need, 2nd vendor. The paper converts the Layer-2 sketch into a concrete design with three pieces we don't have:

1. **The escalation trigger becomes confidence, not parse failure.** The optimal-threshold rule (q_L − ΔC/V) plus shadow-mode fitting is the calibration procedure our README's "escalate when mass splits" hand-wave was missing. Crucially the fitted thresholds are *extreme* (1.00/0.99 for Jev): at our measured Jev accuracy (0.8 graded, noul perfect), the honest expectation is that a fitted cascade delegates **noul questions almost always, graded questions rarely** — insurance, not a cost revolution.
2. **The go/no-go methodology.** Any Layer-2 adoption eval should be L3-style (live workflow with the decision model in the loop), because L1 accuracy overestimates utility — our own 0.87-vs-0.98 A/B is an L1 number and cannot alone justify adoption.
3. **A reversibility rule for delegation.** Confident-wrong-on-irreversible-actions is the entire failure taxonomy (34/34). Slice: delegate decision calls freely on reversible steps; on steps feeding irreversible effects (publish, deploy, spend), threshold to 1.0 or keep the LLM/human gate regardless of confidence.

**Validations of decisions we already made (evidence, not capability):**
- Our conservative posture — decision as *pre-filter/escalation trigger*, never sole judge — matches the paper: naive delegation loses 19–21.5 points; even fitted, Jev saved only 14.2% on Flash because thresholds came out ~1.0.
- Our measured "misses cluster at probability boundaries" (spike README) is independently the paper's threshold motivation — the probability mass IS the routing signal.
- The paper's "not security boundaries" warning matches the governance brief's "LLM never the root of trust."

**What does NOT transfer:** the headline savings (81–89%) are in tool-dense, action-sparse domains (customer service, ALFWorld) with runtime tool choice — Armature's fleet is content/research generation with spec-time tool wiring, where the relevant win is the one we already measured (79× latency on structured judgments). Coding-agent savings were only 7–10.5%; τ²-bench didn't transfer at all. English-only, no-adversary eval.

## 4. Verdict

**Shelve implementation; promote to Layer-2 design input** (same disposition as Harness² for improve). When Layer 2 reopens (any trigger), the spec should specify: native decision stage with per-question-type confidence threshold → guided_json LLM fallback below τ; shadow-mode calibration procedure to fit τ; L3-style live eval before adoption; reversibility floor for irreversible effects. Until then, nothing to build — Layer 1 plus the README's routing guidance already covers current use, and a fitted threshold at today's Jev accuracy would delegate little beyond noul.

**Weaknesses to carry into any citation:** non-inferiority (3-pt) not equivalence; one agent-LLM family (Gemini) dominates; thresholds are per-agent fits that don't travel; no adversarial testing; GRPO negative result half the training cost.

Relation to the other notes: the third harness-paper pass this week — unlike 2610.30499/Harness² (improve-loop design input) and 2610.28577 (shelved, layer mismatch), this one lands on the **typesafe-decision** arc, and is the only paper of the four whose mechanism Armature already half-anticipated in its own spike README.
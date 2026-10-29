# Rethinking Verification in Agent Harnesses

**Date:** 2026-10-27
**Source:** arXiv:2610.34577v1, "Rethinking Verification in Agent Harnesses", Zhengxin Zhang (Cornell; work done during Alibaba internship), Erkang Zhu, Yin Lin (Alibaba), Claire Cardie (Cornell), Bolin Ding, Jingren Zhou (Alibaba). Submitted 2026-10-27, cs.CL. Primary source: https://arxiv.org/html/2610.34577v1 (all §-citations below are to this HTML).
**Question:** Ninth paper of the 2026-10-26/27 shelf. Verification as a harness-design axis — when to intervene, who checks, how checking leads to revision. Does it move the needle past what Armature already has, or re-cover ground?

---

## 1. What the paper is

A large empirical study of **where in the agent lifecycle verification should live**, comparing 17 verification designs across seven frontier coding models (Qwen3.7-Max, Qwen3.8-Max, GLM-5.2, DeepSeek V4-Pro, DeepSeek V4-Flash, Kimi-K2.7-Code, MiniMax-M2.5) on Terminal-Bench 2.1 (TB) and SWE-bench Verified (SWE), plus a new **VisualBench** (18 self-built visual/interactive tasks, blinded side-randomized human preference from compensated internal raters). E2B sandboxes, Harbor orchestration, three trials per model–task, costs priced per-model. **No significance tests, CIs, or error bars reported anywhere.**

The framework (§1, §3, Table 1) spans four lifecycle layers:

| Layer | Timing | Who checks | Armature analog |
|---|---|---|---|
| **L0** initial instructions | before execution | agent itself | `mission:` / role.description |
| **L1** execution-time reminders | mid-turn, after tool results | agent itself | — (nothing in ReAct stages) |
| **L2** completion gates | at attempted finish | agent reopens own work | `on_fail` loops on the producing stage |
| **L3** independent review | before acceptance | **separate critic, fresh context** | judge stages / `post_run` self_analyst |

## 2. The four layers, measured

**L0 — initial instructions (§5.1).** Verification guidance in the system prompt: +3.3 TB / +3.9 SWE (system role); +1.2 / −1.1 (user role), at ~1.0× cost. Wildly model-dependent (MiniMax +12.6, Qwen3.8-Max −2.3 on SWE). Takeaway: keep system-prompt verification guidance **lightweight**; user messages reserved for task requirements.

**L1 — execution-time reminders (§5.2).** Diagnostic first: baseline agents run a recognized check after only **6.2–13.6% of editing turns** (Appendix Table 7). After-edit reminders reach 80–100% of runs; checks follow 58–72% of reminder events. Gains +3.5–5.4 TB at **0.90–1.12× cost with output tokens down 14–25%** (Qwen3.7-Max +7.1/+14.3 pp with 33%/28% *fewer* tokens). But model-dependent on SWE and VisualBench (GLM-5.2 and Qwen3.7-Max **−12.5** each on VisualBench). Capped at four reminders/run (B.6).

**L2 — completion gates (§5.3) — the negative result.** When the agent tries to finish, the gate forces self-re-checking. Best variant (reject + early trigger) +6.0 TB / +6.2 SWE — but rejection-based designs cost **1.60–1.76× baseline (TB) / 1.48–1.56× (SWE)**, medians of 12–19 additional steps, file-changing actions in 86–96% of reopened runs (B.7). And the reopen is dangerous: case C7 — state-changing tests run at a completion gate **mutated the deliverable and it failed grading**. Takeaway box: "avoid completion-time self-checking."

**L3 — independent review (§5.4) — the headline.** A fixed, cheap critic (Qwen3.7-Plus, $1.194/M output vs Max $5.373/M) reviews in a **fresh context**: task spec + **live sandbox access** (runs the tests the agent overlooked), **cannot modify the deliverable**, returns findings the agent uses to revise. Result: **+5.9–9.5 pp TB at 1.13–1.25× cost; +3.9–6.6 pp SWE at 1.00–1.04× cost** — best L3 beats best L2 on both benchmarks *at lower cost*. On VisualBench all four L3 designs positive (+13.2 to +23.8 net preference) while every L0/L1/L2 design averages ≈0 or negative.

Two context axes (§3, designs 14–17):
- **Changes vs trajectory:** critic sees task + the agent's code changes vs. task + changes + **full execution trajectory**. Changes-based wins (+21.2 vs +13.2 without required visual inspection; +23.8 vs +21.6 with), and trajectory costs **29–43% more critic tokens** without consistent gain.
- **Task-first vs claim-guided:** claim-guided adds the agent's own assumptions/claims as checking surface — guards against the "shared mistake" failure (critic repeats the agent's error).

**Scaling L3 (§5.4):** *Depth* (more rounds): 1 round 62.7% @ 38.2k critic tokens → 2 rounds 65.6% @ 79.5k → 4 rounds 66.1% (diminishing). *Width* (parallel independent critics, blind to each other, "acceptance requires no returned objection"): 4 critics 65.8% (+3.1 pp over one) at 1.33×. Best combination: **consolidate feedback from 4 critics BEFORE revision — approaches 4-round accuracy at 19% lower total cost** (repeated re-execution by the main agent is the expensive part; width avoids it). Round-2 gains are model-dependent: Qwen3.7-Max 59.5→70.2; Kimi-K2.7-Code 60.7→**58.3**.

**Failure modes (§D, E):** verification-induced damage (C2: instructed self-check deletes and recreates the repo, fails integration; C7 above); false confidence (C4: compile-success as proxy while a constraint is unsatisfied; C5: output equality checked, speedup requirement missed); critic confirms defect but revision times out (C10). Prompt mitigations worth stealing: "SHARED MISTAKE (you repeated the agent's own error)"; later-round critics get prior feedback as "a **COVERAGE MAP, not your conclusion**" and must re-derive requirements from scratch; "rework breaks things" → re-check for regressions.

## 3. Position vs the 2026-10-26/27 shelf

**This is the strongest external validation of Armature's core architecture in the whole batch — and it is orthogonal to both established convergences.** It doesn't touch memory (convergence a) or credentials (c). Its relationship to the improve arc (b) is methodological: 17 designs × 7 models against a fixed baseline with cost instrumentation *is* the probe-and-contrast protocol the improve-revision brief calls for — another citation for paired behavioral variation, and one more voice for per-model diagnosis (the model-dependence tables argue directly for the six-dimensional diagnosis rows in the harness-improvement v2 memo).

**But the interesting synthesis is with the memory papers, not the improve papers.** Raw-RAG and Memory Tax said: give the *reader* raw evidence + spec, not digests of process history. This paper says the same thing about the *checker*: **judge the deliverable against the spec, not the trajectory** — changes-based critic context beats trajectory-based, at 29–43% lower critic cost. The same principle now appears on both the retrieval side and the verification side, from three independent groups: *the artifact plus the specification is the right evidence; the process history is noise that costs money.*

## 4. Mapping to Armature

| Paper mechanism | Armature status | Delta |
|---|---|---|
| **L3 as the best lifecycle point** — separate critic, fresh context, live-sandbox access, cannot modify, findings→revision | **Already the native pattern.** Judge stages, verdict stages, `post_run` self_analyst, the campaign's verdict_h4 — Armature's DAG makes the next stage the check, i.e. the engine's core shape IS L3 | **Validation, no change.** The batch's best citation that the judge-stage architecture is the right accuracy–cost point; keep for the next architecture write-up |
| **Changes-based critic context (not trajectory)** | **Mechanism exists, default is wrong-leaning.** Judges see cumulative upstream context (trajectory-like) unless `signature.input` filters | **Citation + discipline.** CLAUDE.md already bans `_transcript` for post_run in fan-out workflows; this extends the rule: judge stages should default to deliverable+spec, not run history. Costs 29–43% less and scores higher |
| **Fixed low-cost critic across frontier workers** | **Already have it** — `role_type_defaults: judge: <cheaper tier>` | **Validation** with numbers (critic at ~22% of Max output price still +5.9–9.5 pp) |
| **Width: critic quorum with consolidated feedback before revision** | **Missing as a named pattern** (fan_out/judge exist; nothing composes them) | **The one concrete spec slice.** `fan_out: N` independent judges (blind to each other) → `fan_in: list` → one consolidator stage → revision loop. Approaches multi-round accuracy at 19% lower cost; acceptance = "no returned objection". **This is also the H4-v3 idea** ("score quorum not aggregate HQS") with independent evidence behind it, and it feeds the LLM-as-verifier plan's pairwise/tournament selection |
| **L2's negative result** — self-check reopen costs 1.5–1.8× and can damage the deliverable | **Latent exposure in `on_fail`.** Armature's `on_fail: loop` revisits the producing stage — the same self-reopen shape | **Design caution.** When a gate fails, prefer *independent critic → findings → revision* over reject-and-continue self-rework. Worth a line in SPEC-REF's on_fail guidance when docs are next touched |
| **L1 mid-turn reminders** (checking follows only 6–13% of edits; reminders flip it, cheaply, some models) | **Missing.** No mechanism injects reminders into a ReAct stage's tool-result loop | **Shelve.** Only applies to long ReAct stages; single-shot stages have no "turns." Note as the one lifecycle layer Armature genuinely lacks, revisit if ReAct stages grow long-horizon work |
| **L0 lightweight verification guidance in system prompt** | **Already have** — `mission:` | **Validation** — keep it short; heavy verification instruction is where the −8 to −12.5 pp model regressions lived |
| **Coverage-map / shared-mistake / regression prompts** (App. E) | **Not present** in judge prompt conventions | **Cheap import for the judge-prompt cookbook**: multi-round critics get prior feedback as coverage map, re-derive from scratch; claim-guided review against the agent's own stated assumptions |

## 5. Verdict

**SHELVE for the engine — but as design-input, this is the highest-validation paper of the batch.** Armature is already an L3-native architecture, and this paper is the first rigorous measurement saying that shape is the best accuracy–cost point in the lifecycle — no engine change follows. What follows are citations and two concrete carries:

1. **Critic quorum** (fan_out N judges → consolidate → revise) — the one new spec pattern, directly evidenced (+3.1 pp at 1.33×, 19% cheaper than rounds), and it independently lands on the already-recorded H4-v3 direction.
2. **"Deliverable + spec, not trajectory"** for judge context — now evidence-backed from the verification side, joining Raw-RAG/Memory Tax from the retrieval side. Costs 29–43% less and wins.
3. An `on_fail` design caution (independent critic beats self-reopen) and a handful of judge-prompt idioms (coverage map, shared-mistake, recheck-for-regressions).

**Toy/honesty flags:** single-agent interactive *coding* domain — not multi-agent workflow orchestration; 3 trials, zero significance testing, several headline deltas are within plausible trial noise; VisualBench is self-constructed with internal raters (18 tasks, single eval round); the critic is fixed (Qwen3.7-Plus) and same-family with two of the seven workers — family-affinity confound unexamined; model-dependent results mean every takeaway is per-model, not universal.

---

*Analyzed against the shelf: docs/notes/2026-10-26-self-harness-improvement-arc-agi-3.md, -harness2-recursive-agent-harnessing.md, -harness-scaling-agentic-rl.md (probe-and-contrast / candidate-set geometry), 2026-10-26-raw-rag-vs-agentic-memory.md, 2026-10-27-memory-tax-net-value-agent-memory.md (evidence-shape synthesis), 2026-10-26-jev-agent-harness-decision-models.md, 2026-09-29-openrig-analysis.md.*
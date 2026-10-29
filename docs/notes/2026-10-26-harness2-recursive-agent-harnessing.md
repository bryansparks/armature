# Harness²: Recursive Agent Harnessing for an Open World

**Date:** 2026-10-26
**Source:** arXiv:2610.14224v1 (submitted 2026-10-09), Ruiyao Xu, Yanfei Chen, Zhongying CuiZhu, Bhavana Dalvi Mishra, Yifei Ming, Han Yu, Rujun Han, Chen-Yu Lee, Tomas Pfister (Google Research / Cloud AI; cs.AI).
Primary source: https://arxiv.org/html/2610.14224v1 (paper citations below). Repo inspected at commit: google-research/harness2 (Apache 2.0, shallow clone; repo citations are file paths). Code: https://github.com/google-research/harness2.
**Question:** A Google framework for *recursive self-improvement of agent harnesses* in open-world settings — the same problem space as `armature improve`/`tune` and the campaign's self_improve stage. Does it move the needle past what Armature has?

---

## 1. What the paper is

A **positive-result RSI method** where 2610.30499 was a negative-result controlled study. Agent = frozen model M + harness H = (c¹..c⁸): eight editable components, each mounted as a file — system prompt, guardrails, memory, skills, tool guidance, scripts, subagents, and a middleware plugin (§2.1; extends AHE's decomposition by the plugin). Tasks arrive as a stream with **no ground truth at test time**; the pipeline may re-run a task within a bounded budget. Contrastive step (Eq. 1): `(H', e', ξ) = Φ(q_t, H, E; R_t)` — successor harness, execution record, step record; the improvement harness R_t stays fixed *during* a task.

**Task-level recursion (§2.2)** — the **Propose–Probe–Compose** step:
- **Propose:** sample a reference execution under the current harness ("reveals how the harness behaves, not whether its deliverable is correct"), then propose two executable candidates — an *invariant* edit targeting cross-domain agentic weaknesses (planning, tool use, context, delegation) and a *domain-specific* edit targeting the current domain (§2.2.1).
- **Probe:** run each candidate on the **same task** under matched conditions; comparing candidate trajectory/deliverable against the reference yields a **behavioral contrast** — evidence of mitigation, no-op, or regression. "Contrasts inform composition rather than verify task correctness."
- **Compose:** read the reference, candidates, diffs, and all execution evidence; selectively form H′ — merge supported changes, revise conflicts, adopt one candidate, **or keep the current harness**. "Each decision is tied to a concrete observation rather than a presumed score gain."
- **Scaling (§2.2.2):** *parallel* recursion probes k invariant + k domain candidates, then k independent compositions each seeing the full pool (width); *sequential* recursion chains k contrastive steps on the evolving harness (depth). For k>1 an agentic selector compares **anonymized deliverables** — it picks an existing output without knowing its source harness ("adoption rather than verified optimality").

**Harnessing-level recursion (§2.3)** — improve the *improvement process*: after each task, reflect on the full improvement record, extract lessons ("retained only when supported by execution contrasts; selecting an output alone does not establish transferability"), and update the improvement harness R (Eq. 4). Each task **restarts from the common H₀** — the library guides refinement, it is never auto-inherited; components are reused selectively through Propose. If evidence is insufficient, no entry is added.

## 2. Repo reality (what the code actually is)

A real research artifact, not paper-ware — ~6,700 lines of core Python with documented seams (`harness2/core/spec.py`: "Three axes stay separate — method (recursion, prompts, selection, library), substrate (the agent framework), benchmark"). Concretely:

- **`core/tree.py`** — the c1..c8 component tree: read/validate/write/diff, parametrized by `SurfaceSpec` (paths, caps, per-surface regex validators) so OpenCode and Codex substrates share one implementation. **Edit contract is "omitted-is-inherited": a proposal emits only what it changes.** Skills/subagents/scripts/plugins are validated by path-pattern (`tree.py: RESERVED_SCRIPTS`, `_rx`); invalid proposals get one retry, then **inherit the current harness** (`core/recursion.py: STAGE_ATTEMPTS = 2`, `Stage.kept_base` / `fell_back`).
- **`core/proposer.py`** — improver sessions as isolated subprocess workspaces (OpenCode or Claude CLI), cost-per-call tracking, fingerprint-based caching, full audit writes.
- **`core/selection.py`** — the anonymized selector: candidate deliverables get an `id_map` (labels dropped), the model chooses via ```choice```/`<why>`/`<experience>`/`<edits>` tags; `chosen_is_base` records the selector keeping the base harness.
- **`core/library.py`** — the per-domain experience library: `INDEX.md` + `harnesses/<task>/harness/<tree>` (the **full adopted tree, never a summary**) + `EXPERIENCE.md` with a `(cue, lesson, evidence)` triple — all three required (`has_experience`). Retrieval is by construction (one directory per domain).
- **`core/ledger.py`** — budget accounting with exact identities: R = 4k rollouts, P = 3k improvement sessions; `BudgetError` if the ledger doesn't land on the identity.
- **Substrates:** `substrate/opencode.py` (395 lines), `substrate/codex.py` (480). **Benchmarks:** JobBench, LAB, WorkBuddy-Bench adapters under `benchmarks/`. A deterministic demo (`harness2-demo`) runs both recursion modes with no model credentials.

## 3. Results (and how much to trust them)

Three professional benchmarks (§5): WorkBuddy-Bench (composite verifier incl. Gemini 3.5 Flash judge), JobBench (Claude Opus 4.8 rubric judge; rubric never shown to the agent), LAB legal (Claude Sonnet 4.6 judge, partial-pass rate). Four configurations: Gemini 3.5 Flash and GLM-5 frozen executors × OpenCode/Codex stock harnesses; harness **editor is Claude Opus 4.8**.

- One step: up to **+11.5** (WorkBuddy), **+4.5** (JobBench), **+11.8 pp** (LAB). Parallel k=5 adds a further ~3–4 points. Gains grow monotonically with k.
- **Ablations (Table 3):** library removal costs 3.1/1.8; scoped-editors removal 2.9/3.7; composition removal 1.6/1.9 — all three components carry weight.
- **Baselines matter (§5):** "Harness Scaling" (naive per-task test-time revision, no probe/compose/library) **underperforms the base harness in 7 of 12 pairs**; ReasoningBank (retrieval-only memory) drops up to 6.6 below base. So naive test-time harness editing is harmful — the full protocol is what makes it work.
- **Cost:** both configurations sit on the empirical performance/token Pareto frontier (Fig. 6).

**Honest weaknesses:** no significance tests (3 runs, SDs only); **judge contamination** — the WorkBuddy judge (Gemini 3.5 Flash) is also an executor, and Claude Opus 4.8 is both editor and JobBench judge; the paper itself admits "execution stochasticity and shared model biases limit the reliability of the evidence"; recursion does not uniformly close initial-harness gaps (GLM-5's OpenCode↔Codex gap *widens* 0.2→1.3); cross-benchmark library transfer is modest (0.6–2.9); and the per-task cost is real — 4k rollouts + 3k improvement sessions (k=5 ⇒ 20 rollouts + 15 frontier-model sessions **per task**).

## 4. Applicability mapping to Armature

| Mechanism | Armature status | Verdict |
|---|---|---|
| Probe candidates on the same input; select on **behavioral contrast**, not predicted score | **Missing — and this is the needle-mover.** `armature improve` selects *pre-execution* by `predicted_fixes` coverage against diagnostics (`synthesis/improve.py:322` `_pick_best_proposal`); nothing ever runs a candidate and watches what it does. Armature's built-in `fan_out`/partition machinery is a natural substrate for probe-style candidate execution. | **Worth a slice when improve is next revised.** See verdict below. |
| Abstention: Compose may keep the current harness; invalid proposals inherit it; selector may pick base | **Missing.** Same gap already recorded from 2610.30499 (no parent-as-candidate at `improve.py:882`; single-proposal mode applies regression-risky edits, "informational only"). Harness² embeds abstention *structurally* (`kept_base`/`fell_back`/`chosen_is_base`). | **Two independent sources now converge** — this strengthens the existing revisit trigger from "one paper" to "one paper + one Google framework." |
| Improvement library: adopted artifact + `(cue, lesson, evidence)`, lessons gated by execution evidence, never auto-inherited | **Partial.** `ImprovementStore` (PR #14) keeps history records shared both directions; the memory pyramid's P1 records have provenance. The refinements Armature lacks: store the *adopted artifact* not just the record; gate lessons on contrast evidence ("selecting an output alone does not establish transferability"); fresh-H₀-per-task anti-overfit posture. | Refinement, not gap. Fold into the improve revision if it happens. |
| Harness-as-tree with editable-surface validation + omitted-is-inherited diffs | **Covered, differently.** Armature's `editable_surfaces` hard gate + locked-surface rejection (`improve.py`) is the same concept over spec fields instead of component files. | Already have it. |
| Invariant vs domain-scoped proposal channels | **Missing, small.** One diagnosis→proposal channel today. The two-channel split (cross-workflow robustness vs domain knowledge) lets composition attribute effects. | Cheap idea; only matters inside a probe-style improve. |
| Anonymized deliverable selection | **Missing, small.** If candidate outputs are ever compared, blind the selector to which variant produced which output (editor-favoring-own-edits bias). | One-line principle; apply whenever execute-and-compare happens. |
| Budget ledger with exact identities | **Covered.** `max_llm_calls`/`max_iterations`, missions budget metering. | Already have it. |

## 5. Relation to the other two notes — the tension is the finding

2610.30499 ([self-harness note](2026-10-26-self-harness-improvement-arc-agi-3.md)) found harness improvement *doesn't reliably work* (last-write-wins, evidence-bundle protocol: +2.80 held-out, CI spans negative). Harness² finds it *does* (up to +11.5, k-scaled) with a different protocol. Read together, the papers agree on the diagnosis and isolate the difference:

1. **Both say naive self-editing loses.** 2610.30499's lineages all changed code and half lost; Harness²'s "Harness Scaling" baseline (per-task revision without probe/compose/library) *underperformed base in 7/12 pairs*. Identical conclusion from opposite directions.
2. **The protocol is the variable.** What separates Harness²'s positive result from both the negative result and the naive baseline: (a) candidates are **executed and contrasted** before adoption, (b) composition is **selective with abstention**, (c) the improvement library is **evidence-gated and never blindly inherited**.
3. **Converging evidence on Armature's improve loop:** `improve.py` is structurally closest to the *losing* protocols (pre-execution prediction, apply-even-risky in single-proposal mode, no parent-as-candidate). That's now flagged by a controlled negative study, a Google positive-result framework, and our own H2/H4 campaign failures — three independent sources.

## 6. Verdict

**Shelve implementation; keep as design input — this is the strongest of the three papers.** Unlike 2610.28577 (wrong layer) and the ARC-AGI-3 study (evidence only), Harness² proposes a mechanism Armature genuinely lacks (probe-and-contrast candidate evaluation) that directly targets the improve loop's known-open problems (H2: self_improve never fires — contrast needs no quality score to detect behavioral change; H4: cost/quality trade — contrast can catch cheaper-and-worse before adoption). The repo is a usable Apache-2.0 reference implementation if a probe-style improve method is ever built.

**Revisit triggers (recorded to memory):**
1. **Parent-as-candidate + abstention in `_pick_best_proposal`** — now *two-source* confirmed (2610.30499 + Harness²'s `kept_base`/`fell_back`/`chosen_is_base`). Still the first item when improve is next revised.
2. **Probe-style improve method** (propose variants → fan_out on same input → contrast → compose-or-keep) as the *second* item — a genuinely new capability, but per-task cost is frontier-model-heavy (4k/3k sessions) and live validation remains parked on OpenRouter credits. Design it with the library evidence-gating refinements, not the naive version — the ablations say the naive version is worse than doing nothing.

Honest discount: no significance testing, judge/editor contamination, and modest cross-benchmark transfer (0.6–2.9) mean the headline +11.5 should be read as "protocol shows promise," not "protocol is proven." The transferable part is the *shape* — probe, contrast, selective compose, evidence-gated library — not the numbers.
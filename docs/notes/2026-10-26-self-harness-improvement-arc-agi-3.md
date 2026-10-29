# "Can LLMs Improve Their Own Harness?" — A Controlled Study on ARC-AGI-3

**Date:** 2026-10-26
**Source:** arXiv:2610.30499v1 (submitted 2026-10-21), Jens Lehmann & Sahar Vahdati (cs.AI, cs.SE).
Primary source: https://arxiv.org/html/2610.30499v1 (all citations below are to this HTML).
**Question:** The paper studies whether frontier models can autonomously build *and improve* their own harness — exactly the capability `armature improve` / `armature tune` and the phase-D factory bet on. What does it say, and what applies to Armature?

---

## 1. What the paper is

A controlled empirical study of **self-harness-improvement**, not a new mechanism. A deployed agent is formalized as `S_t = (M, H_t)`: a frozen model M plus an executable harness H_t — "a symbolic program orchestrating model calls... handling perception, memory, tool use, recovery, and inference allocation" (Abstract, §2). The harness is the artifact under autonomous construction and revision; the model edits it through a **fixed** outer coding bootstrap (OpenCode, same category as Claude Code) (§4.2).

**Protocol** (§2, §4.2, Fig. 1):

1. **Construct:** from a minimal repository (sandboxed Python, game docs, model client, pricing), the model builds H_0 — any architecture; the only contract is a `run.sh` entrypoint.
2. **Qualify:** a smoke gate checks *executable, interacting behavior* on 2 dev games (50 actions, no score threshold), with up to 10 repair cycles. Then H_0 is **frozen**.
3. **Evaluate on dev:** H_0 runs on 5 development games.
4. **Revise:** the model gets a fixed evidence bundle — its own source, traces, scores, actions, and dollar inference costs — and may change anything or preserve the parent. Twice (H_1, H_2). No human edits between stages.
5. **All-snapshot evaluation on a strict split:** every frozen snapshot is scored on all 25 games, but only the 5 dev games feed adaptation; the **20 held-out games "provide no feedback for harness construction, revision, or selection"** (§4.2).

Scale: 9 model–effort conditions, 3 constructions each → 27 lineages, 81 frozen snapshots, 2,025 game evaluations, ≥$18,129 total cost (§1, App. D).

**Metric (§4.1, App. A):** RHAE — Relative Human Action Efficiency. Credit only for completed levels, within a 5×human-baseline action budget; `s_ℓ = min{100(b_ℓ/a_ℓ)², 115}`, later levels weighted more, game score capped by weighted completion. Dollar cost is tracked jointly — performance and spend are never reported separately.

## 2. Key findings

- **Construction works.** Initial harnesses ranged "from zero to near-ceiling scores" (Abstract). Emergent architectures were diverse: "compact direct model controllers, heuristic-first systems, strategist–executor hybrids, persistent manuals, explicit world models, replay systems, deterministic planners, and inference-budget managers" (§6.1).
- **Improvement does not (reliably).** Two revisions raised mean held-out RHAE by **only +2.80 points (95% CI [−1.44, 7.35])** — compatible with zero. 15 gains, 12 losses (p=.70). A repeated-evaluation diagnostic showed sample SD ≈ 3.99 RHAE on one frozen harness (§5).
- **Dev gains overstate held-out transfer.** Dev RHAE rose +8.42 while held-out rose +2.80; adaptation gap G₂ = **5.62 points (95% CI [1.78, 9.77])** (§5). "Development improvement therefore overstates the average held-out gain" (§5).
- **Cost cuts often eat performance.** Of revision-2 descendants: 9 improved both, 6 gained score at higher cost, **9 got cheaper while losing RHAE**, 3 worsened on both. "Half of the cheaper final harnesses lose held-out performance" (§9). Mean cost fell $2.72/game but median only −$0.54 — savings mix efficiency with "revisions that spend less while also solving less effectively" (§5).
- **Edit volume is uninformative.** "Both large and small diffs lie on each side of zero held-out change" (App. E, Fig. 5). Replicates of the same model–effort condition diverged wildly (Sol-high: one replicate +33.18, another −15.18) (§6.1). First revisions overwhelmingly "reoriented" (22/27 P/E/R coding), but "presence or coarse type [of strategy change] does not certify improvement" (App. E, Table 13).
- **Evidence-grounded repair is the success pattern.** The illustrative success (T001) acted on metered evidence — cache written but rarely read; a self-imposed decision limit hit — and its fix (raise limit to 120, drop cache writes, add animation-aware observations) preceded 17 passing local tests and a 29.24 → 53.56 held-out gain at flat cost (App. E).
- **Nobody abstained.** "Every scored trajectory changed code despite retention being allowed" (§7). The paper's #1 recommendation: compare candidates against a **preserved baseline** and roll back when the evidence doesn't justify the change — "reliable self-improvement includes recognizing when the evidence favors keeping the current harness" (§7).
- **A post hoc dev-based checkpoint-selection rule yielded +4.53 held-out** (vs +2.80 for always-take-latest), suggesting selection policies beat last-write-wins — but needs prospective evaluation (§5).
- **Humans still far ahead.** Human-steered systems (Tycho, VISTA, AVO) reach ~100 RHAE with the same models; the best autonomous lineage averaged 39.95 (§7). And the paper's own threats-to-validity are honest: CIs condition on the fixed game split; leave-one-out checks are descriptive, not counterfactual; case studies changed "several components together" so no causal mechanism is isolated (App. D, E, F).

## 3. Applicability to Armature

The paper is a **controlled cautionary evaluation** of the exact loop `armature improve`/`tune` implements, and an **existence proof** for the factory's Tier 2 (the engine writing the engine's specs). Mechanism-by-mechanism:

| Paper mechanism | Armature status | Concrete slice |
|---|---|---|
| `S = (M, H)` — frozen engine/evolvable harness split | **Already the architecture.** Engine is fixed PyPI; the harness is spec YAML. The paper validates Armature's core bet: the harness is software a model can write (construction went zero→near-ceiling from a minimal prompt). | Nothing. This is the factory's charter evidence: Tier-2 generation is plausible because *construction* is the reliable half. |
| Smoke qualification — execution gate, not score gate, with bounded repair cycles | **Already present** (validate + package verify's 9 checks + INPUTS_COMPLETE; H_boot ↔ the fixed Claude-Code bootstrap analog). | For the factory: generated specs get up to N repair cycles from execution evidence, then freeze. Never gate admission on quality scores at qualification time — the paper gates on *runs at all*. |
| Strict dev/held-out split; held-out never informs adaptation | **Partial.** Improve/tune triggers on drift/history from *observed* runs — the same runs that judge the change. The paper's G₂ = 5.62 says this overfits. | Add a held-out input set to the improve loop: diagnosis from run set A, promotion judged on run set B (never seen by the diagnosis). Report the adaptation gap, don't just report the improvement. |
| Frozen snapshots + all-snapshot evaluation (parent is always a candidate) | **Missing where it matters.** ImprovementStore applies improvements; the *unchanged parent* is not carried as a competing candidate. "Every scored trajectory changed code despite retention being allowed." | The strongest import: improve must always benchmark candidate-vs-parent and promote only on net held-out win; otherwise record **abstention** as a legitimate outcome. Treat "no change justified by this evidence" as success, not failure. |
| Dev-based checkpoint selection (+4.53 vs +2.80) | Missing. Improve is effectively last-write-wins once a change is proposed. | Cheap slice: keep every frozen candidate (spec + provenance manifest already versions this), score all snapshots on the eval set, select the best — selection is worth more than another revision round. |
| Cost–performance joint accounting; half of cost-cutting revisions lose score | **Partial.** Budgets, missions cost metering, and improve's latency-aware selection exist, but H4-v2 already showed a latency term can cancel a quality gain. The paper generalizes it: cheaper-and-worse is the *modal* bad revision. | Make improve's selection explicitly two-objective (score, cost) with a guard: refuse a change whose only virtue is cost reduction that costs score — report both deltas for every candidate. |
| Evidence-grounded repair (metered evidence → targeted fix → local tests) | **Partial.** Improve's shared diagnosis/history and the six-dimensional harness diagnosis (POTENTIAL_HARNESS_IMPROVEMENT-v2) are exactly the "metered evidence" the one clean success used. The paper confirms evidence-grounded beats volume. | Strengthen the diagnosis bundle to carry the counters that discriminated in the paper's case study (write-vs-read ratios, self-imposed limits hit, cache utilization) as harness-diagnosis dimensions. |
| Track improvement capability separately from task performance | **Missing as a first-class metric.** Campaign H2 already failed this way: corruption didn't degrade operational HQS, so self_improve never fired — the system couldn't see its own degradation. The paper says the ability to improve the software that determines future performance should be a **separate tracked axis**, not inferred from task scores. | Give the campaign/soak harnesses an explicit "revision reliability" metric: fraction of revisions that hold held-out ground, adaptation gap, abstention rate. Armature is unusually positioned to measure this — it already runs controlled multi-arm experiments. |
| RHAE (efficiency-relative-to-human, harder-level-weighted) | Not applicable directly (task-specific), but the *shape* is: score improvements against a baseline efficiency, not raw completion. | Optional: campaign HQS could adopt a relative-to-baseline-efficiency term (actions/cost per quality point vs. a fixed reference run) so "did more with less" is visible per unit. |
| "Updatable updaters" / longer runs needed for acceleration claims | Open. Armature's improve is a bounded loop today; sustained accumulation is the open question the paper leaves. | Defer. Note it as the honest limitation to carry in any factory autonomy-posture decision (briefing Q4): the paper is direct evidence that autonomy in *revision* needs gating even when autonomy in *construction* works. |

**Relation to prior notes:** MemoHarness (arXiv:2607.14159, POTENTIAL_HARNESS_IMPROVEMENT.md) proposed a *mechanism* (learned harness adaptation from execution experience); this paper *evaluates* whether that kind of loop actually generalizes, and mostly says: not without held-out evaluation, baseline preservation, and selection. The two are complementary — MemoHarness says *how* to adapt, 2610.30499 says *how to keep adaptation honest*. The campaign findings (H2 self_improve non-firing, H4 cost/quality cancellation, soak's replicate variance) rhyme with this paper's failure modes line-for-line, which is independent evidence the paper's conclusions transfer beyond ARC-AGI-3.

## 4. Honest limitations of the paper

- Single domain: 25 public ARC-AGI-3 games, interactive puzzle-solving. Nothing about multi-stage document/code workflows. Transfer to Armature's domain is an argument by protocol-shape, not by domain.
- Human comparison systems (Tycho/VISTA/AVO) use different procedures — the 100-vs-39.95 gap is suggestive, not a controlled comparison (§7).
- Process-code associations were exploratory, unadjusted, and two of the coded variables had weak inter-rater reliability (App. F). The "evidence-grounded repair works" claim rests on one clean case study (T001) where several components changed at once.
- Post-hoc selection rule (+4.53) was not prospectively evaluated.

**Verdict:** import the *evaluation protocol*, not any mechanism — dev/held-out separation, frozen snapshots with parent-always-a-candidate, joint cost/performance reporting, and abstention-with-rollback. The single highest-value Armature change this paper motivates: make `armature improve` carry the unchanged parent as a candidate and treat held-out-grounded abstention as a success state.
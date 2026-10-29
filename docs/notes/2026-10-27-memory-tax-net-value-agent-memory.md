# Memory Tax — Dissecting the Net Value of Agent Memory

**Date:** 2026-10-27
**Source:** arXiv:2610.34073v1, "Memory Tax: Dissecting the Net Value of Agent Memory", Xuan He, Sining Zhoubian, Ruozhen Yang, Yuhao Wen, Zhineng Wang, Yuyang Hu, Ke Yang, Yuyu Zhang, Ping Nie, Jiawei Han, ChengXiang Zhai (UIUC group — Han/Zhai). Submitted 2026-10-25, cs.CL, CC BY 4.0. Project page: https://hexuan21.github.io/MemoryTax/
Primary source: https://arxiv.org/html/2610.34073v1 (all citations below are to this HTML).
**Question:** One more agentic-memory paper, assessed against the 2026-10-26 shelf (esp. Raw-RAG, 2610.30755) and the Memory Pyramid (P1 typed records/provenance, P2 agent-pulled navigation, P3 tracks/profile/curator; 3-arm cold/warm/nav eval scaffolded, live run deferred). Does it move the needle, or does it re-cover Raw-RAG's ground?

---

## 1. What the paper is

A large-scale empirical study that **independently replicates Raw-RAG's headline from a different group, then goes substantially further**. Setup: 11 memory systems across four design families (flat: A-Mem, SimpleMem, MemOS; hierarchical/lifecycle: MemoryOS, LightMem, OpenViking; graph: HippoRAG2, PlugMem, MAGMA; trained-policy: Mem-T, Memory-R2) vs a well-specified naive RAG reference, on LongMemEval-s, PersonaMem-v2 (20%), MINT-Eval (10%), three backbones (Qwen3.5-4B, Qwen3.6-27B, GPT-5.6 Luna low), unified embedding+reranker (Qwen3-Embedding-0.6B / Qwen3-Reranker-0.6B), and a fairness knob Raw-RAG didn't have — **Retrieval Scope (RS)**: top-k tuned per system so evidence budget is no lower than RAG's (§3.1, A.2).

The reference RAG is stronger than Raw-RAG's: turn-boundary chunking (never crossing session boundaries, ~1024-char greedy merge, 8192-char cap windows with 1024 overlap, datetime headers), **hybrid BM25+dense with rerank**, and a 1-round vs 2-round variant (1rd/2rd — 2rd lets the backbone issue a follow-up query against the remaining pool) (§2, §3.1).

**Headline (Table 1):** nearly every cell is negative vs RAG (2rd) — e.g. on LongMemEval-s/Q3.6-27B (RAG 81.4): A-Mem 79.4 (−2.0), MemOS 76.6 (−4.8), SimpleMem 73.6 (−7.8), OpenViking 58.8 (−22.6), MAGMA 68.2 (−13.2), Memory-R2 73.0 (−8.4), best-in-family HippoRAG2 78.2 (−3.2). Worst: OpenViking −40.4 (LongMemEval-s, Q3.5-4B). Exceptions cluster on MINT-Eval (PlugMem +3.3, Mem-T +3.9 with Luna) and PersonaMem-v2 (MemOS +1.0/+1.5, Memory-R2 +2.0). Appendix B.2: of all prior memory-system papers evaluated, **only three reported any RAG baseline at all**, and those were weak configs (MiniLM embeddings, unspecified budgets) — the sandbagged-baseline indictment, independently confirmed.

## 2. The conceptual contribution: benefit/tax decomposition

Net value of memory is dissected **per query** against the RAG reference (§4.1): *memory benefit* = queries the memory system solves that RAG misses (organization/distillation helping); *memory tax* = queries RAG solves that the memory system loses. Both are deviations from raw history — the same deviation (reorganize, distill, compress) is double-edged. The paper focuses on the overlooked tax side.

**Tax mechanism 1 — Faithfulness (§4.2):** ingest loss, Raw-RAG's finding replicated at finer grain. Macro: gold-memory-unit replay (remove retrieval) still leaves residual tax → construction loss. Micro: atomic facts traced to their memory units, human-annotated (Table 2): no-loss rates range from MemoryOS 98.4% down to OpenViking **39.4%** (13.0% major loss, 38.0% no unit at all). Conclusion: construction loss is real and unrecoverable — but tax persists even after oracle units, so loss isn't the whole story.

**Tax mechanism 2 — Accessibility (§4.3): the new failure mode.** Evidence can be fully preserved yet **harder to retrieve**: fragmented across units, diluted with unrelated content, or rephrased so query-evidence similarity shifts (Fig. 5 similarity-drift analysis). Memory systems recover fewer gold queries than RAG in the first retrieval round — relevant evidence is buried deeper. Tax exists even when nothing was lost.

**Tax mechanism 3 — Robustness (§4.4):** with construction fixed, swapping the QA backbone causes **larger performance variation for memory systems than for raw RAG** — memory adds fragility on top of raw-history retrieval, i.e., tax appears as point-wise loss AND reduced robustness.

**Tax mechanism 4 — Benchmark headroom (§4.5):** naive baselines are already strong: an IDF-keyword method exceeds 50% absolute on some benchmarks, RAG-Hybrid ~70% (across 14 benchmarks, Table 15). Where retrieval is easy, memory has little room for benefit, so even modest tax dominates — which explains why lightweight/no-memory designs stay competitive.

**Generalization (§5):** the same pattern beyond conversational memory:
- **Experiential memory (§5.1, C.6.1)** — the domain closest to Armature: memory over past *task trajectories*. Six agentic benchmarks (ALFWorld, WebShop, Mind2Web, BigCodeBench-Hard, AppWorld, τ²-Bench), Qwen3.5-4B. Naive RAG (top-2 similar past tasks by embedding) vs Memp/ReasoningBank/G-Memory/SkillPro. **Domain-dependent**: distilled workflows DO beat RAG on embodied/web (Memp 76.1 vs RAG 73.1 on ALFWorld SR; RAG 73.1 vs ReasoningBank 62.7), but RAG wins tool-use/coding (AppWorld 22.6 vs all memory methods 14.3 or tied; BigCodeBench 21.6 RAG-best; WebShop no-mem 45.9 > everything). Cost is brutal: SkillPro 1,226K tokens/task on ALFWorld vs RAG 235K.
- **World-model memory (§5.2, C.6.2):** naive past frames beat raw-KV, learned-KV, latent, and memory-token carriers by ~5.7 PSNR — the benefit/tax lens generalizes across memory forms.

**Design implications (§6)** — the constructive part:
1. **Make deviations recoverable (§6.1):** keep "a lightweight path back to the source history when memory becomes insufficient." Their PoC: retrieve from source history *alongside* normal memory retrieval and **jointly rerank** — consistent improvement and tax reduction across several systems.
2. **Recursive / self-referential memory (§6.2):** "memory can remember how it remembers" — store past queries + retrieved evidence as retrievable memory themselves; deep-round evidence becomes directly accessible to future similar queries; a query-side test-time-scaling axis (inter-query, beyond single-query scaling).
3. **Model future demand (§6.3):** ahead-of-time (AoT) memory pays tax because "without knowing future demand accurately, it is unclear what kind of deviations will be beneficial"; just-in-time (JiT) minimizes tax but moves cost to user-facing latency. Middle path: model future demand — "memory can partly be seen as an implicit recommendation system that estimates future relevance of past information."

## 3. Position vs the 2026-10-26 shelf

**Relationship to Raw-RAG (2610.30755): successor and superseder, not duplicate.** Raw-RAG said: raw evidence + strong naive retrieval beats transformation-heavy memory; ingestion loss is unrecoverable. Memory Tax, from an independent group, replicates that core (different systems, different benchmarks, stronger and *fairer* reference — the RS budget knob; B.2 re-confirms the sandbagged-baseline indictment). Then it adds three things Raw-RAG did not have:

| New in Memory Tax | Why it matters |
|---|---|
| **Accessibility tax** — evidence preserved but fragmented/diluted/rephrased becomes harder to retrieve | A failure mode Raw-RAG couldn't see: its coverage metric conflated loss with burial. Curation can cost even when lossless |
| **Robustness tax** — memory amplifies backbone sensitivity | First quantification that memory is a fragility multiplier, not just an accuracy debit |
| **Experiential-memory generalization** — agent *task-trajectory* memory on real agentic benchmarks (ALFWorld, AppWorld, τ²-Bench) | Raw-RAG explicitly did not cover agentic workflow memory — this paper walks into Armature's actual domain, and finds **the picture is domain-dependent**: distillation (the pyramid's L2 tracks) wins in embodied/web, naive RAG wins in tool-use/coding |

**Relationship to the agent-pulled pattern (4-source validated: our eval, AKV CAT, Octopus, pi): fully compatible, and it sharpens it.** The paper never attacks pulling — its reference is itself pull-based retrieval, and §6.2's recursive memory *is* an agent-pull enhancement. What it attacks is what happens to evidence between capture and pull.

## 4. Mapping to the Memory Pyramid

The pyramid is precisely a "benefit/tax" architecture: P1/L0 keep raw evidence (tax floor), P2 pulls agent-side (the validated bet), P3 distills (tracks/profile/curator — the tax-bearing surface). Mechanism by mechanism:

| Paper mechanism | Pyramid status | Delta |
|---|---|---|
| Faithfulness tax (§4.2) | **Already designed around.** P1 records are verbatim-excerpt + provenance-pointer shaped (the Raw-RAG guardrail); L0 raw captures are retained wholesale | **Confirms the guardrail with the strongest evidence yet** (human-annotated fact-loss table). No change — but the paper makes it a *two-sided* invariant: every P1/P2 surface must keep a working path back to L0 |
| Accessibility tax (§4.3) | **Partial gap.** P1 verbatim excerpts avoid rephrasing, but L2 tracks (≤20 markdown summaries ≤2000 chars) and L3 profile are digests — fragmentation/dilution live there. Mitigation exists structurally: tracks carry `evidence_links` validated against live records | **Eval + design input.** Tracks must be treated as *navigation aids with tax risk*, never as substitute evidence — the live eval should measure whether track reads ever substitute for record reads and cost accuracy |
| §6.1 recoverable deviations — **joint raw+curated retrieval with one rerank** | **Missing as a single surface.** P2 has separate tools (`search_records` RRF over L1; `search_conversation` LIKE scan over L0) — the agent must decide a priori which store to ask | **The one concrete design slice.** Fuse L0 raw-capture hits into `memory.search_records`' RRF candidate pool (one fused query answers from both curated records and raw history). Small, localized to `armature/registry/memory_tools.py`, and it is the paper's empirically-validated tax reducer |
| §6.2 recursive memory (queries+retrievals become retrievable) | **Missing.** Nothing logs (query, retrieved ids, used?) | **Defer.** A P3+ candidate; the pyramid's cost case (H4-v2 latency) must come first, and it needs the live eval's failure modes to justify |
| §6.3 model future demand | **Partial.** The curator is AoT (post-run); tracks are demand-modeling-lite. The paper's AoT-tax argument is exactly the H4-v2 tension: passive injection of wrong-demand content = pure tax | **Design framing only** — the pyramid is already motivated by cost/latency rather than accuracy, which is the paper's honest defense of AoT. No change now |
| RS fairness metric (§3.1) | **Missing from the eval.** The 3/4-arm comparison has no evidence-budget normalization | **Eval import.** Report RS (retrieved tokens / store tokens) per arm so nav vs warm vs raw arms are budget-fair |
| Benchmark headroom (§4.5) | **Missing from the eval.** Campaign research-brief topics are likely retrieval-friendly (exactly the regime where tax dominates) | **Eval import.** Pre-register a headroom check — a keyword/IDF-only retrieval arm over run traces. If a keyword baseline already scores near the arms, the eval's margins are headroom-limited and should be interpreted accordingly |
| Per-query benefit/tax decomposition (§4.1) | **Missing — the eval reports aggregate means + bootstrap CIs** | **Eval import, the best one.** Per-query: which questions does nav solve that cold misses (= benefit), which does the raw-RAG arm solve that nav loses (= tax). This localizes *why* an arm wins, not just that it does — the diagnosis the `report.py` 3-arm page lacks |
| Experiential-memory results (§5.1) | **Directly on-point:** the pyramid's L2 tracks are Memp/SkillPro-shaped (distilled strategy memory over task traces) | **Both directions.** For the pyramid: distillation has external evidence *for* it in embodied/web domains — the first non-against result for a curated layer in this whole literature batch. Against: in tool-use/coding domains naive RAG wins, and Armature's campaign tasks (research briefs) sit nearer the tool/retrieval end than the embodied end. The 4th raw-RAG arm (Raw-RAG's mandate) is now *doubly* mandatory |

**No mechanism here changes the pyramid's architecture.** The two eval-side imports (RS reporting + headroom check), one analysis import (per-query benefit/tax), and one small design slice (RRF-fuse L0 into `search_records`) are the whole delta.

## 5. Weaknesses to weigh

Offline, static, English-only benchmarks (their own Limitations); subset evaluation (20% PersonaMem-v2, 10% MINT-Eval, 20% of LongMemEval-s questions for Luna); official configs for all memory systems — "system-specific tuning may yield better performance" (the charitable reading: tuned memory systems might close some gaps; the uncharitable: none of the 11 systems' authors did); MINT-Eval absolute scores are 26–38% — noisy low regime; experiential arm is Qwen3.5-4B only (small backbone; capability-dependent conclusions); no significance testing surfaced in the main narrative (unlike Raw-RAG's Holm-corrected tests — treat single-cell deltas like Mem-T +3.9 with suspicion); the world-model section is thematically neat but operationally irrelevant to a text workflow engine.

## 6. Verdict

**Shelve for the engine — import into the Memory Pyramid's deferred live eval, and one small design slice for the shelf.** This is the best-evidenced paper of the memory batch alongside Raw-RAG (which it partially supersedes): independent replication of the raw-beats-transformed result, a *fairer* reference (RS), a finer diagnosis (accessibility and robustness are taxes Raw-RAG couldn't see), and — uniquely — evidence from real agentic task benchmarks, where the result is domain-dependent rather than uniformly anti-memory. For the pyramid: it confirms the verbatim/provenance design center, prices the tax risk of the L2/L3 distilled layers, hands the eval a better decomposition (per-query benefit/tax) plus two fairness/calibration tools (RS, headroom check), and contributes one validated mechanism worth keeping on the shelf (joint raw+curated fused retrieval — the natural evolution of `memory.search_records`).

**Shelf convergence:** Raw-RAG (ingest loss) + Memory Tax (accessibility/robustness/headroom) now jointly state the memory design constraint as: keep raw evidence (L0/P1 verbatim), keep the path from every distilled surface back to it (evidence_links + fused retrieval), pull agent-side (the 4-source pattern), and make every curated layer prove net value against a strong naive arm per query. That is, read backwards, the Memory Pyramid's architecture — now with external evidence for every clause.
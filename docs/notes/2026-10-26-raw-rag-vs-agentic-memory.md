# Raw-RAG vs. Agentic Memory — "Naive RAG Is Stronger Than We Thought"

**Date:** 2026-10-26
**Source:** arXiv:2610.30755v1, Zhengye Bao, Shengtao Zhang, Haokai Zhang, Shiqi Lou, Muning Wen, Minnan Luo (cs.AI; likely SJTU — Minnan Luo's group).
Primary source: https://arxiv.org/html/2610.30755v1 (all citations below are to this HTML).
**Question:** The user has built several agentic RAG implementations (one very capable) and suspects this paper "doesn't apply to Armature." This note maps the paper to (a) Armature and (b) agentic RAG practice, and tests that suspicion.

---

## 1. What the paper is

An empirical study / critical evaluation, not a survey and not a method paper. The authors ask whether the complexity of "agentic memory" systems — fact extraction at ingest, mutation (UPDATE/DELETE), structural organization (graphs, hierarchies), learned retrieval — is justified for long-term conversational memory. Their instrument is **Raw-RAG**, a deliberately naive baseline: embed the **raw, unprocessed** chat history in 256-token overlapping windows (32 overlap), dense-retrieve top-128 by cosine, rerank with a cross-encoder to top-15, hand those to the reader LLM. No LLM calls at ingest, no summarization, no graph, no mutation (§3).

They benchmark 12+ agentic memory systems (Mem0, MemOS, EverMemOS, MemMachine, MemU, EMem, A-MEM, Memora, MemoryOS, Nemori, SimpleMem, RecMem) on four benchmarks — LoCoMo, LongMemEval, MEME, MemoryAgentBench — with two reader tiers (GPT-4o-mini, GPT-4.1-mini; plus DeepSeek-V4-Flash on MEME/MAB), plus a diagnostic suite: coverage/utilization decomposition, complete-store oracles, a mutation audit, and ablations (§4–5).

## 2. Findings, with the numbers that matter

1. **Raw-RAG ranks first in 6 of 7 benchmark–model settings** (Table 1): LoCoMo 89.07 vs 88.12 (MemMachine), LongMemEval 87.53 vs 87.40 (Memora), MEME 39.30 vs 33.52, MAB 48.32 vs 43.84. The one loss: MEME on DeepSeek-V4-Flash (43.19 vs MemMachine 44.71), where Full-context leads both by ~20 points (§4.2).
2. **Coverage beats curation.** Decomposing accuracy into *coverage* (Ĉ: fraction of questions whose retrieved context contains sufficient evidence) and *utilization* (Û: conditional accuracy given sufficient evidence) (Eq. 4, §5.1): Raw-RAG has the **highest coverage on both MAB (45.29%) and MEME (76.35%)** with middling utilization (84.43%, 73.76%). The paper's conclusion: coverage is the broader cross-system bottleneck, and transformation systems lose evidence at ingest — the opposite trade is not worth it (§5.1, Tables 7–8, 26).
3. **Raw evidence preserves reasoning paths.** On MEME tracking/cascade after-state questions (Table 9): Raw-RAG's conditional path retention P(path|result) = 62.34% vs Mem0 47.66%, MemOS 24.03%, MemU 9.62%. Raw windows retain the dependency/chronology chains needed to *derive* answers, not just the answer values (§5.2).
4. **Ingestion loss is real and unrecoverable.** Complete-store oracle: give each system's exported store to a *gold-informed* selector (privileged access, phase/provenance filtering, 5k budget) and it still scores 7.58–29.38 points below a raw-evidence control with the same privileges (61.60% vs Mem0 32.26, MemU 31.07, MemOS 45.12, Raw-RAG's own store 57.54). Every paired interval excludes zero. Information discarded or distorted at ingest cannot be recovered by better retrieval (§5.3, A.5.2, Tables 29–30).
5. **Mutation barely happens, and removing it helps.** Mem0 audit: proposed UPDATE+DELETE = 4.39% of 33,902 LoCoMo write decisions (2.46% of 428,858 on LongMemEval); *effective* mutations (persisted text actually changed) = 1.90% / 1.34%; many proposed updates are duplicates or malformed (Figure 2, Table 2). Converting UPDATE→ADD and DELETE→no-op **raised LoCoMo overall 67.36 → 69.42**, improving every category including temporal (+3.32) and open-domain (+6.94) (Table 1 intervention).
6. **Complexity additions to Raw-RAG all hurt** (Table 6, MAB): atomic-fact extraction 48.32 → 28.70 (−19.62!), structured summaries → 42.51, graph expansion → 42.54, removing the reranker → 44.40 (−3.92, the cheapest single lever). Retrieval depth is flat k=5–60 and cliffs at k=120 (−14.57); k=15 default.
7. **Efficiency is not close** (Table 32): Raw-RAG ingest+search = 718 s with **zero LLM calls**; the cheapest complex variant 10,327 s; graph-expanded 15,600 s and 109M tokens. Up to ~44× faster.
8. **Prior RAG baselines were sandbagged.** Appendix E reviews 14 prior evaluations and finds coarse retrieval units, restricted depth, and absent reranking inflated the apparent advantage of agentic systems over RAG.
9. **Where agentic memory still has a case** (§4.3, §6.1): conflict resolution and dynamic state (MEME) — Raw-RAG is weakest on CR (46.00) and loses on DeepSeek-MEME; **weaker readers** — on MEME cascades GPT-4o-mini scores 1.83 vs DeepSeek's 82.32, so explicit state maintenance may matter when the reader can't re-derive state from raw evidence; very long horizons; and **privacy/deletion constraints**, where raw retention has genuine non-accuracy costs (§6.1 admits this).

The paper's own recommendation: future agentic memory systems "should demonstrate gains over a well-specified naive RAG baseline and establish what the added complexity contributes" (§6.2).

## 3. Applicability to Armature — testing the user's suspicion

**The suspicion is ~80% right.** The paper's target systems (Mem0/MemOS-style conversational memory mutation) are not Armature components, and its task domain (offline conversational QA) is not Armature's (agentic workflows). Nothing gets built. But there are three genuine touchpoints:

| Paper concept | Armature status | What it means |
|---|---|---|
| "Beat a well-specified naive baseline" demand | **Applies — evaluation only.** The Memory Pyramid (P1 typed records + provenance, P2 navigation tools, P3 curator/write tools) is the one Armature design in the blast radius: it IS an agentic-memory architecture | The pyramid was built for H4-v2's *latency/cost* pain (passive injection of raw history), not accuracy. The paper doesn't kill it — it defines the eval it must pass: the deferred **live deep run should add a Raw-RAG arm** (dense retrieval + cross-encoder rerank over raw run traces, no curation) alongside the existing cold/warm/nav arms. The 3-arm eval structure makes this a cheap 4th arm |
| Ingestion loss / raw-evidence discipline | **Partial — and the pyramid's design center matters here.** P1 records carry provenance (pointers to source) and P2 is agent-pulled (the 4-source-validated bet) | The paper attacks *transformation*, not *pulling* — it is fully compatible with the CAT/pyramid "agent-pulled context" convergence. The danger line: if pyramid records store **digests/summaries**, they are in Mem0 territory (lossy ingest); if they store **verbatim excerpts + provenance pointers**, they align with the paper's finding. The live design should keep records pointer-and-excerpt shaped |
| Coverage/utilization decomposition | **Missing — the transferable methodology.** Cheap to compute over any pipeline: coverage = "did the right evidence reach the prompt", utilization = "given it did, was it used" | Directly applicable to the campaign runner's diagnosis and to `armature improve`'s diagnostics: a stage failure decomposes into retrieval failure vs reading failure, which are different fixes |
| Fan-out research pipeline | **Already RAG-shaped.** plan_searches → fan_out web_search → fan_in list (CLAUDE.md fan-out pattern) is retrieve-rerank-synthesize already | No action; the trace DB + history streams (`index/<name>/`) are exactly the raw corpus a Raw-RAG arm would retrieve over |
| Mutation/curator (P3 write tools) | The paper's specific target | The pyramid's curator is the surface the paper would interrogate. Its defense is cost (raw history can't be injected wholesale), not accuracy — state that defense in the eval, don't assume it |

**Armature verdict: shelve implementation; import the evaluation discipline into the Memory Pyramid live deep run (a 4th Raw-RAG arm + coverage/utilization decomposition of its results).** No engine changes.

## 4. Applicability to agentic RAG practice (the user's own implementations)

This is where the paper has its real value, in three forms:

1. **The diagnostic toolkit is directly adoptable.** Coverage vs utilization (Eq. 4) is ~30 lines of judge code and tells you which side of the pipeline to fix. The complete-store oracle (gold-informed selection over your own store vs raw-source control) measures *your* ingestion loss directly. A mutation audit (proposed vs effective UPDATE/DELETE rates) tells you whether your update logic actually does anything — Mem0's effective rate was 1.9%, and much of that was duplicates.
2. **The uncomfortable challenge: build the strongest possible naive baseline over YOUR corpus and beat THAT.** If a capable agentic RAG does extraction/summarization/graph-building at ingest, this paper predicts raw windows + a good cross-encoder reranker may match or beat it at ~44× less ingest cost — and 14 prior papers' weak RAG baselines (no reranker, coarse units, shallow depth) are exactly how systems fool themselves. The parameters that mattered here: 256-token windows / 32 overlap / top-128 cosine / cross-encoder rerank to top-15 / ~5k evidence budget; depth is flat 5–60 and cliffs at 120; the reranker alone is worth ~4 points.
3. **Know where the agentic case is real.** Conflict resolution and dynamic-supersession settings; weaker reader models (a capable reader can re-derive deletions/cascades from raw evidence — GPT-4o-mini largely cannot); very long horizons; and any deletion/privacy constraint, where raw retention has non-accuracy costs. If the use case is "documents get superseded and the answer must reflect current state," the paper's data says: give raw evidence + a strong reader the first shot, and treat explicit state maintenance as an escalation, not a default.

## 5. Convergence with the 2026-10-26 note shelf

- **Agent-pulled context (4-source validated)**: compatible and sharpening — Raw-RAG *is* pull-based raw evidence; the paper attacks the *transformation* layer, never the pulling. It refines the pyramid's design center: pull raw evidence, don't curate digests.
- **Abstention/parent-as-candidate (improve brief)**: same epistemic stance — complexity must prove itself against a strong simple baseline, not against a sandbagged one. This paper is the memory-systems expression of the same principle.
- **Octopus mark/verifier separation**: coverage/utilization is the same "measure the pipeline stage, not the end score" discipline.

## 6. Weaknesses to weigh before citing

Offline conversational benchmarks only (§6.1 — no interactive/multimodal/operational claims); PersonaMem-v2 significance untested (Raw-RAG ranks *third* there, behind EverMemOS and MemOS — the least-flattering setting is honestly reported); the complete-store oracle "does not isolate information loss during ingestion" (C.5 admits mixed effects); MEME Abs gives credit for "uncertain" answers; judge = mostly GPT-4o-mini (judge-sensitivity acknowledged); and the raw-retention privacy/storage cost is admitted, not solved. Toy-adjacent? Less than most: four public benchmarks, two+ reader tiers, significance tests with Holm correction — but all conversational memory, none of it agentic *workflow* memory.

---

**Bottom line:** The user's suspicion is right for Armature-the-engine (shelve, no build) but incomplete for Armature-the-eval: the Memory Pyramid's deferred live deep run gains a mandatory Raw-RAG arm and the coverage/utilization decomposition. For the user's own agentic RAG work, the paper's diagnostics are immediately adoptable and its challenge — beat a well-specified naive baseline over your own corpus — is the actionable takeaway.
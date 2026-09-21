# Research Currency Check — Harness Literature Since July 2026

**Edition:** 2026-09-21
**Prompt:** Armature was designed from papers published through ~June 2026 (the research foundation in `docs/ARCHITECTURE.md`). A commissioned external survey covering July 1 – September 21, 2026 asked whether the field moved underneath us. This document records the verdict, what was adopted, and — most of all — what was deliberately *not* adopted, with reasons and reopening triggers.

**Method:** the survey was AI-generated, so its claims were treated as leads, not facts. The three load-bearing claims were independently verified against their sources before any decision was made (see [Verification notes](#verification-notes)). The survey document itself is not committed to this repository.

---

## Verdict

The field's headline shift — **the harness became the optimization target** — is Armature's founding bet, now with independent evidence. Of the seven movements in the survey:

- **One genuine gap** was found and closed: the resume contract (PR #27 — [record below](#adopted-the-resume-contract-pr-27)).
- Four movements landed on machinery Armature already has: harness-as-artifact, constraint pinning, deterministic enforcement, the RL seam.
- The rest is recorded as **tracked, deliberately not adopted** in the [register](#tracked-deliberately-not-adopted).

The July+ literature did not expose staleness — it converged on the same conclusions from the harness-codebase side that Armature reached from the spec side.

---

## Movement dispositions

### 1. Harness as optimization target → already our thesis

The survey's four evidence points: Harness Handbook builds a behavior-to-code map for harness *codebases* ([arXiv:2607.13285](https://arxiv.org/abs/2607.13285)); the eleven-production-harness study argues the harness "completed a turn from tool to platform" in H1 2026 ([arXiv:2609.00006](https://arxiv.org/abs/2609.00006)); self-improvement formalized as fast, reversible scaffold updates behind version history and verifier gates ([arXiv:2607.13104](https://arxiv.org/abs/2607.13104)); harness design as the moving bottleneck ([arXiv:2606.20683](https://arxiv.org/abs/2606.20683)).

That artifact is the YAML spec. Two consequences the survey draws, checked:

- *"Version, diff, navigate, roll back the harness configuration."* Have: specs are git-versioned; `armature improve` / `optimize` / `tune` propose spec edits from run traces; a PR is the rollback unit and the verifier gate.
- *"Expose a behavior-to-code map."* Does not map 1:1 — and that is the point: a spec harness has no behavior/code gap to bridge. The spec is both.

**Disposition: aligned. No action.**

### 2. Harness self-evolution → the loop exists; the statistics are tracked

HarnessBank pairs a task agent with an evolver agent, and gates promotions through four checks (evaluation validity, mechanism activation, paired statistical significance z ≥ 1.96, gain) over a gene bank indexed by modified component × failure pathology ([arXiv:2607.13683](https://arxiv.org/abs/2607.13683)). JIT-Agent synthesizes task-adaptive harnesses on the fly ([arXiv:2608.25593](https://arxiv.org/abs/2608.25593)); HarnessDev shows LLM-generated harnesses match mature references on writing/ML tasks but with unstable, model-dependent gains ([arXiv:2609.01437](https://arxiv.org/abs/2609.01437)).

Armature's answer: `armature improve` (drift trigger, latency-aware selection, shared diagnosis history) plus the `tune` facade with auto-escalation is the same diagnosis → propose → apply loop without the statistics. Notably, the survey's "April precursor" — observability-driven harness evolution — is arXiv:2604.25850, which is AHE, already a founding paper in `docs/ARCHITECTURE.md`, already implemented.

Not adopted: the gene bank and the four screening gates. Register items **#1**.

### 3. New benchmarks → tracked

Evo-Bench measures harness-evolving ability with the policy model fixed ([arXiv:2608.09096](https://arxiv.org/abs/2608.09096)); EVOHARNESSBENCH evaluates agents under cumulative harness *growth* and finds growth can erase previously competent behavior ([arXiv:2609.04280](https://arxiv.org/abs/2609.04280)); the SWE-Bench harness-ablation study finds components are conditionally beneficial ([arXiv:2609.20804](https://arxiv.org/abs/2609.20804)); WildClawBench shows same-model, cross-harness spreads of 8–18 points, making harness attribution a defensible metric ([arXiv:2605.10912](https://arxiv.org/abs/2605.10912)).

Armature's attribution today is within-harness: stage credit attribution, campaign A/B experiments, the dashboard. Cross-harness benchmark participation is a research program. Register item **#2**.

### 4. Context management as a correctness surface → structurally immune

Governance Decay (**verified**): policy violations rise from 0% with the policy visible to 30% after compaction (up to 59% for some models); constraint pinning restores 0%; a compaction-eviction attack defeats every model evaluated ([arXiv:2606.22528](https://arxiv.org/abs/2606.22528)). TRACE shows recurrent compression destabilizes execution ([arXiv:2608.06503](https://arxiv.org/abs/2608.06503)); ACM replaces fixed-threshold compaction with explicit memory tools ([arXiv:2607.23809](https://arxiv.org/abs/2607.23809)).

Armature has no compactor. Every stage builds its prompt fresh from declared context — `signature.input` filters, context governance MUST/NEVER layers re-injected at every stage build, precedence-ordered. Nothing is evicted mid-run, so nothing can be evicted *invisibly* — the failure mode Governance Decay documents is structurally absent. Safety rules are enforced deterministically at the `ToolRegistry.dispatch` chokepoint, not by model attention. The only size-based reduction is per-result truncation (`_maybe_truncate`), which never touches a governance layer. The semantics are pinned by the context-governance back-compat suite.

**Disposition: have, with receipts. No action.**

### 5. Long-horizon execution → mostly have; resume contract adopted

LongHorizon-Harness externalizes task state and runs a Manage–Execute–Audit loop with a fresh-context executor and a read-only auditor ([arXiv:2608.01964](https://arxiv.org/abs/2608.01964)). Expressible today as a spec pattern: manage/execute is stage decomposition; audit is the judge role plus the `post_run` self-analyst; fresh-context executors are subagent composition (`docs/SUBAGENT-COMPOSITION.md`, `docs/CONTEXT-ISOLATION.md`).

Resume Means Resume (**verified**) supplies the exception: "checkpointing" does not guarantee completed effects won't repeat — you need an explicit, machine-checkable resume contract with a durable effect ledger, tested in CI ([arXiv:2608.03836](https://arxiv.org/abs/2608.03836)). This was the survey's one real hit. **Adopted — [record below](#adopted-the-resume-contract-pr-27).**

Prime Agent's persistent REPL / programmatic context processing ([arXiv:2608.23552](https://arxiv.org/abs/2608.23552)): script adapters cover the need at current scope. Register item **#9**.

### 6. Routing, permissions, human control → enforcement already deterministic

The permissions survey's central finding — no existing system achieves low specification overhead, formal grounding, and deterministic enforcement simultaneously; harnesses need enforcement independent of model nondeterminism ([arXiv:2607.13718](https://arxiv.org/abs/2607.13718)) — is the design PR #16 already implemented: safety rules enforced pre-dispatch at the registry chokepoint.

Not adopted from this section: APPA's engine-managed taint confinement via disposable child trajectories ([arXiv:2607.24625](https://arxiv.org/abs/2607.24625)) — the subagent boundary is the natural seam but the label-descent machinery is heavy (register item **#6**); Agentic Routing's per-step learned router and feedback flywheel ([arXiv:2607.11399](https://arxiv.org/abs/2607.11399)) — tune auto-escalation and stage credit are the rule-based first steps (register item **#7**); permission revocation mid-run (register item **#8**); Skills-over-MCP — watching, since the extension surface is tools/adapters/packages and MCP attach already exists (register item **#10**).

### 7. Training against the harness → seam already open

Agent Lightning v1.0 trains against arbitrary harnesses by having the training engine observe only LLM request-response pairs through an endpoint proxy ([arXiv:2608.17528](https://arxiv.org/abs/2608.17528)). Armature's provider layer (`model_tiers`, litellm) is that proxy — all LLM traffic already flows through one seam, and traces capture the observation surface. Preserving the seam costs nothing and is an architectural constraint: stage logic must not inline SDK calls. No RL consumer today. Register item — none needed beyond the constraint itself; adopting RL is out of scope until a concrete training loop wants Armature runs.

---

## Adopted: the resume contract (PR #27)

What the survey called "the kind of thing harnesses get wrong silently" was true on inspection: the semantics were correct but unpinned, unguarded against concurrent runs, and misdocumented. Shipped (merged 2026-09-21):

- **Conformance suite in CI** — `tests/runtime/test_resume_contract.py`, six tests pinning: completed stages exactly-once, in-flight stages at-least-once, fan-out checkpointed as a unit, concurrent runs rejected. The effect-ledger fixture is the paper's durable-ledger pattern at test scope. Any future semantic change is deliberate and visible.
- **Fail-closed session lock** — `SessionLock` (`flock` on `session.lock`): a second concurrent run against one session directory fails loudly with `SessionDirInUse` instead of silently duplicating un-checkpointed effects. OS-released on process death; runs with `checkpoint: false` take no lock.
- **Docs truth-pass** — the FAQ's nonexistent `--resume` flag and wrong checkpoint path corrected; `docs/CHECKPOINT-AND-RESUME.md` gains the Effect-delivery contract section (per-stage-state guarantee table).

Deferred sub-items from the paper are in the register below (**#3–#5**).

---

## Tracked, deliberately not adopted

The register. Same discipline as deferring a feature: each entry names why not now, and the observation that would reopen it.

| # | Item | Why not now | Reopens when |
|---|------|-------------|--------------|
| 1 | Gene bank + statistical screening gates (HarnessBank) | Paired significance needs per-gene run volume the campaigns don't generate; `improve`/ImprovementStore already records proposal → outcome | Per-gene paired corpora (≈30 runs) exist from campaign/soak data |
| 2 | Third-party benchmark participation (Evo-Bench, EVOHARNESSBENCH, WildClawBench) | No current suite accepts an external Python-agent harness; building one is a research program | A third-party suite accepts custom harnesses, and there is a comparison target to attribute against |
| 3 | Per-item fan-out checkpointing | Unit checkpointing is documented and pinned; per-item writes add checkpoint contention for rare mid-batch interruptions | A fan-out workflow with expensive per-item effects and real mid-batch interruption frequency |
| 4 | Cross-process consume-once | Single-host `flock` covers current deployments | Runs share session storage across hosts |
| 5 | Runtime effect journaling | Pays only for non-idempotent effects that can't be redesigned | A workflow with such effects ships |
| 6 | APPA-style taint confinement | Subagent isolation + sandbox cover current needs; label-descent machinery is heavy | Workflows ingest tainted sources where a read must not contaminate the parent trajectory |
| 7 | Per-step learned routing | Tune auto-escalation covers rule-based routing; learned routing needs decision data | A routing-decision corpus plus a cold-start use case |
| 8 | Permission revocation mid-run | Static policy covers current workflows | A dynamic permission downgrade becomes a requirement |
| 9 | Persistent REPL context server (Prime Agent) | Script adapters cover programmatic context processing | A workflow needs interactive state across many stages |
| 10 | Skills-over-MCP migration | Tools/adapters/packages are the extension surface; MCP attach exists | The MCP spec work lands and the ecosystem follows |
| 11 | Native `decision:` stage type (TypeSafe Layer 2) | Zero consumers beyond the spike; thresholds need real stakes; vendor risk in engine coupling | A second consumer of decision-gating (~2–3 workflows), an in-spec escalation need, or a second vendor |

Entry #11 is not from the survey — it is recorded here so this register is the single in-repo home for deliberate non-adoptions.

---

## Verification notes

Independently verified against sources before decisions were made:

- **Governance Decay** ([arXiv:2606.22528](https://arxiv.org/abs/2606.22528)) — the 0% → 30% / 59% violation numbers, constraint-pinning restoration to 0%, the compaction-eviction attack.
- **HarnessBank** ([arXiv:2607.13683](https://arxiv.org/abs/2607.13683)) — gene bank indexed by component × pathology, the four gates, z ≥ 1.96.
- **Resume Means Resume** ([arXiv:2608.03836](https://arxiv.org/abs/2608.03836)) — contract semantics, the silent cross-framework effect-repetition failures.

All other citations are given as the survey recorded them and were taken as directional only. For the founding (pre-July 2026) literature, see `docs/ARCHITECTURE.md` → Research Foundation.

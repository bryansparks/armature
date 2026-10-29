# Agentic Harness Research Since July 2026 — Currency Check for Armature

Scope: papers and talks dated July 1 – September 21, 2026, selected for direct bearing on harness construction (orchestration, context, verification, permissions, observability, evaluation). Pre-July items are included only where they anchor a post-July thread.

## The headline shift: the harness became the optimization target

Until roughly May 2026 the literature treated the harness as scaffolding you hand-write. Since July it is treated as a **first-class, measurable, and machine-editable artifact** — something you version, search, evolve, benchmark, and even train against. If Armature was designed from pre-May papers, this is the single biggest gap.

Four evidence points:

- Harness Handbook builds a behavior-centric representation of a harness codebase via static analysis plus LLM structuring, so agents can localize which code implements a behavior before editing it; Behavior-Guided Progressive Disclosure improved localization and edit-plan quality with fewer planner tokens ([arXiv:2607.13285](https://arxiv.org/abs/2607.13285)).
- Harness Engineering: Anatomy, Architecture, and Evolution of Coding Agents studies eleven production harnesses (Claude Code, Codex CLI, Gemini CLI, Mistral Vibe, OpenHands, and others) and argues the harness "completed a turn from tool to platform" in H1 2026 — loop/workflow engine, tool registry, context and memory management, permission enforcement, multi-agent orchestration, and extension surfaces (hooks, skills, plugins, MCP), with the competitive unit being the ecosystem surface rather than the loop ([arXiv:2609.00006](https://arxiv.org/html/2609.00006v1)).
- Self-Improvements in Modern Agentic Systems formalizes the harness as Σ_t = (prompts, memory, tools, control logic) and splits self-improvement into slow parametric updates versus fast, reversible scaffold updates — recommending fast scaffold exploration behind version history, rollback, and verifier-gated updates ([arXiv:2607.13104](https://arxiv.org/html/2607.13104v1)).
- From Question Answering to Task Completion surveys harness design as the moving bottleneck: prompt elicitation → context/workflow organization → execution control → compositional learnable runtimes → model–harness co-evolution ([arXiv:2606.20683](https://arxiv.org/html/2606.20683v1), June, but the framing all the July+ work builds on).

Implication for Armature: treat the harness configuration itself as a versioned, diffable, machine-navigable artifact with rollback, not as application code. Expose a behavior-to-code map.

## Harness self-evolution and harness-generation

- HarnessBank / gated semantic quality-diversity: a task agent paired with a separate evolver agent that diagnoses failures, generates and recombines harness modifications, and verifies them with no weight changes. A Harness Gene Bank indexes high performers by modified component (prompt, knowledge, runtime, config) × failure pathology to prevent search collapse; Gated Harness Screening checks evaluation validity, mechanism activation, paired statistical significance, then gain. Pass@1 gains of 5.1%–15.4% across seven benchmarks, and gains are model-specific, not universal ([arXiv:2607.13683](https://arxiv.org/pdf/2607.13683)).
- JIT-Agent synthesizes task-adaptive harnesses on the fly for off-the-shelf models under a fixed four-module protocol (memory, planning, action protocol, tool/skill orchestration), and self-evolves from an archive of prior configurations. DeepSeek-V4-Flash with JIT-Agent beat GPT-5.6 by +9.1 on DeepSearchQA and +4.3 on OdysseyBench; GLM-5.2 gained up to +20.2 ([arXiv:2608.25593 summary](https://www.opentrain.ai/papers/jit-agent-scaling-harness-intelligence-via-just-in-time-harness-evolution--arxiv-2608.25593/)).
- HarnessDev asks whether LLMs can create a harness from a minimal seed and then evolve it from execution feedback. Generated harnesses trail mature human references on code and search but match or beat them on writing and ML experimentation; evolution gains were unstable and only partially transferable, and depended heavily on the executing model ([HarnessDev, arXiv:2609.01437](https://papers.fzhiy.net/papers/2609-01437.html)).
- Observability-Driven Automatic Evolution of Coding-Agent Harnesses is the April precursor that ties trace telemetry directly to harness mutation ([arXiv:2604.25850](https://arxiv.org/abs/2604.25850)).

Implication: a harness archive keyed by (component changed × failure mode) plus a statistical gate before promotion is now a documented pattern. Armature's "evidence-driven improvement" pillar can adopt it nearly verbatim.

## New benchmarks you should track (and possibly adopt)

- Evo-Bench measures a model's intrinsic harness-evolving ability with the policy model fixed: 160 visible validation tasks, 448 disjoint eval tasks, drawn from BrowseComp, HLE and others, over search, office, and general agent tasks; the seed policy harness is a minimal CodeAct loop ([arXiv:2608.09096](https://arxiv.org/html/2608.09096v1)).
- EVOHARNESSBENCH evaluates agents under cumulative harness growth (H1 ⊆ H2 ⊆ … ⊆ H_T) across tools, skills, and specialist agents, in both fresh-instance deployment and persistent self-evolving modes, scoring verifier pass rate plus forward/backward transfer, routing, delegation, and coordination costs. The finding that matters: growing the tool/skill surface can erase previously competent behavior ([EVOHARNESSBENCH, arXiv:2609.04280](https://www.alphaxiv.org/abs/2609.04280), summarized in [coverage of harness-evolution forgetting](https://classyainews.com/article/evoharnessbench-harness-evolution-agent-forgetting-research-sep-2026)).
- An Empirical Study of Harness Design for Coding Agents holds the loop fixed and varies three components independently — persistent plan via `update_plan`; structured tools versus bash-only; and five context-management tiers (none / elision / elision+external recall / LLM summarization / staged combination) — across four models on SWE-Bench Verified and Terminal-Bench 2.1. Conclusion: harness components are **conditionally** beneficial, depending on model capability, task type, and context budget ([arXiv:2609.20804](https://arxiv.org/html/2609.20804)).
- WildClawBench varies the surrounding harness while holding task and workspace constant: GPT-5.4 scored 48.4% in Claude Code, 50.3% in OpenClaw, 50.7% in Hermes Agent, 56.8% in Codex, and MiMo V2 Pro shifted 18 points between harnesses ([arXiv:2605.10912, via UNU report](https://unu.edu/sites/default/files/2026-07/Engineering_and_Governing_the_Agent_Harness.pdf)).

Implication: Armature's evaluation layer should report per-component ablations and a harness-growth regression suite, not just aggregate task success. Same-model, cross-harness spreads of 8–18 points make "harness attribution" a defensible metric.

## Context management is now a correctness and safety surface

- ACM gives the agent explicit memory tools — `manage_context` to losslessly summarize and offload, `query_memory` to retrieve archived raw content — instead of fixed-threshold compaction, with dual-teacher training on *when* to invoke them. Peak tokens drop about 20%; on Qwen3.5-9B, Pass@1 of 0.727 on BrowseComp-Plus, 0.425 on DeepSearchQA, 0.530 on SWE-Bench Verified ([arXiv:2607.23809](https://arxiv.org/html/2607.23809v1)).
- TRACE shows recurrent compression destabilizes execution — blocked actions, repeated exploration, run-to-run variance — and evaluates each compaction event with paired closed-loop continuations from the same environment state, optimizing the compression prompt with frozen models ([arXiv:2608.06503](https://arxiv.org/abs/2608.06503)).
- Governance Decay is the one to read first: across 1,323 episodes and seven model families, policy violations rose from 0% with the policy fully visible to 30% after compaction and up to 59% for some models; 0% when constraints survived summarization versus 38% when dropped. Constraint Pinning (quarantining constraints outside compaction) restored 0%, and a Compaction-Eviction Attack defeated every model evaluated ([arXiv:2606.22528](https://arxivsignals.io/papers/2606.22528)).
- RL with Context Compaction trains the compaction policy rather than hand-tuning it ([arXiv:2607.05378](https://arxiv.org/abs/2607.05378)).
- Externalization in LLM Agents frames the whole trend as Weights → Context → Harness, with the harness as a cognitive environment coordinating memory, skills, protocols, observability, permissions, and control points ([arXiv:2604.08224](https://arxiv.org/html/2604.08224v1)).

Implication: if Armature compacts context, governance constraints and policy text must be pinned outside the compactor, and every compaction event needs a paired-continuation regression test. This is a concrete, cheap, high-value change.

## Long-horizon execution: externalize state, verify independently

- LongHorizon-Harness keeps task state outside execution and updates it only with independently environment-verified facts, using a Manage–Execute–Audit loop: a manager holds state and picks the next subtask, a fresh-context executor runs it, a read-only auditor verifies the resulting environment state. An AgentAdapter swaps model and harness backends without touching native loops. Qwen 3.7-Plus went 51.8% → 80.7% on WeaveBench, 69.7% → 77.2% on Terminal-Bench 2.1, 2.8% → 8.3% on OSWorld 2.0; Claude Opus 4.7 went 20.0% → 34.3% on an OSWorld 2.0 subset ([arXiv:2608.01964](https://papers.fzhiy.net/papers/2608-01964.html)).
- Prime Agent is an open-source long-horizon harness worth reading as a reference implementation: recursive subagents, a persistent IPython REPL for programmatic context processing and test-time compute, persistent histories/memories/skills/prompts/subagent specs, direct agent-to-agent messaging, and human-inspectable daemon-backed sessions. It raised ARC-AGI-3 RHAE Best@1 from 30% to 95.5% ([arXiv:2608.23552](https://huggingface.co/papers/2608.23552)).
- Resume Means Resume supplies a deterministic, LLM-free conformance harness for checkpoint/interrupt/resume semantics: 47 probes across five frameworks, a 39-cell fault matrix, TLA+ checking of 7.4M states. The finding: "checkpointing" does not guarantee completed effects won't repeat — you need an explicit, machine-checkable resume contract with a durable effect ledger and consume-once semantics, tested in CI ([arXiv:2608.03836](https://arxiv.org/pdf/2608.03836)).

Implication: two concrete Armature additions — a read-only auditor role separated from the executor, and a durable effect ledger with a CI conformance suite for resume/retry idempotence. The latter is the kind of thing harnesses get wrong silently.

## Routing, permissions, and human control

- Agentic Routing routes per step on full harness state (observation, context, control, action, verification, intermediate failures, traces, outcomes, cost), and turns each routing decision into a labeled feedback record — a harness-native data flywheel. Instantiated in OpenSquilla with a four-layer routing stack and an open LightGBM cold-start ranker ([arXiv:2607.11399](https://arxiv.org/abs/2607.11399)).
- How Agents Ask for Permission surveys permission interfaces through enforcement and finds no existing system achieves low specification overhead, formally grounded policy, and deterministic enforcement simultaneously; it argues harnesses need deterministic run-time enforcement independent of model nondeterminism, plus revocation and disclosure of where LLMs participate in permission decisions ([arXiv:2607.13718](https://arxiv.org/html/2607.13718v1)).
- APPA (Agentic Permissions Policy Algebra) offers a mechanism: engine-managed context branching for taint confinement — restrictive reads happen in a disposable child trajectory whose label descents don't contaminate the parent — plus prospective acquisition enforcement that evaluates the label a tool's output *would* produce and blocks permission-narrowing calls before execution, with structured remedy plans ([arXiv:2607.24625](https://arxiv.org/pdf/2607.24625)).
- Skills-over-MCP moved from convention to spec work during this window, with an MCP working group charter and Microsoft pushing distributed skills as a post-agent architecture ([MCP Skills-over-MCP charter](https://modelcontextprotocol.io/community/working-groups/skills-over-mcp), [coverage of Microsoft's distributed-skills proposal](https://tech.yahoo.com/ai/copilot/articles/microsoft-proposes-distributed-skills-over-100638872.html)).

Implication: Armature's policy layer should be deterministic and enforced pre-dispatch rather than advisory, and its subagent boundary is also a useful taint boundary. Track Skills-over-MCP so the extension surface matches where the ecosystem is standardizing.

## Training against the harness

Agent Lightning v1.0 introduces harnessed agentic RL: the deploy-time harness owns the environment interaction loop and the training engine observes only LLM request-response pairs through an endpoint proxy, so arbitrary harnesses can be trained against. It documents the failure modes this separation creates — retokenization, sample merging, advantage calculation, loss normalization, backend scheduling — in roughly 3,500 lines of framework code ([arXiv:2608.17528](https://huggingface.co/papers/2608.17528)).

Implication: if Armature exposes a clean LLM-endpoint proxy seam, it becomes usable as an RL environment later without redesign. Cheap to preserve now, expensive to retrofit.

## Talks and secondary sources

- The Agentic Harness: What Makes AI Agents Work in Production, Snowflake Dev Day, July 22, 2026 — production harness as complete environment: contextual data, tools/plugins, sandboxed execution, verification loops, human review; notes Codex's 100+ plugins ([YouTube](https://www.youtube.com/watch?v=mirR3GdTgOE)).
- What is an Agentic Harness?, July 16, 2026 — short and useful on one point: decouple the harness from the UI so multiple interfaces, or a purely programmatic one, drive the same harness ([YouTube](https://www.youtube.com/watch?v=W9BX0jyzd2k)).
- Engineering and Governing the Agent Harness, UNU, July 2026 — policy-oriented synthesis with harness-attribution benchmark data ([PDF](https://unu.edu/sites/default/files/2026-07/Engineering_and_Governing_the_Agent_Harness.pdf)).
- A curated subagent-paper roundup covering recursive harnesses, isolated git workspaces (CAID: +26.7% PaperBench, +14.3% Commit0), and the finding that orchestrator-worker appeared in 18.6% of 70 surveyed projects ([Atto Labs subagent survey](https://atto.bot/labs/subagents-2026/)).

## Shortlist: highest-value reads for Armature, in order

1. Governance Decay (arXiv:2606.22528) — actionable safety bug you may already have.
2. An Empirical Study of Harness Design for Coding Agents (arXiv:2609.20804) — tells you which of your components actually earn their place.
3. LongHorizon-Harness (arXiv:2608.01964) — Manage–Execute–Audit with independent verification.
4. Resume Means Resume (arXiv:2608.03836) — resume/effect-ledger conformance contract.
5. HarnessBank (arXiv:2607.13683) + Evo-Bench (arXiv:2608.09096) — the evolve-and-gate loop plus how to measure it.
6. Harness Engineering source study (arXiv:2609.00006) — competitive landscape for your extension surfaces.
7. APPA (arXiv:2607.24625) and permissions survey (arXiv:2607.13718) — deterministic pre-dispatch policy.
8. Agent Lightning v1.0 (arXiv:2608.17528) — keep the RL seam open.

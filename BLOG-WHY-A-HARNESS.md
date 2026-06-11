# Why I Built a Harness Instead of a Framework

*Bryan Sparks — June 2026*

---

Every engineer who has seriously used LLMs to automate something complex has written approximately the same code. A loop. A model call. Output parsing. Error handling. Logging. A retry when the model returns garbage. Another retry when the API times out. A check to make sure the output schema is what downstream code expects. A try-except around the whole thing, hopeful that whatever fails won't take down the system.

This scaffolding is the actual engineering work. The model itself is — and I say this having spent months living inside the research on this — essentially interchangeable. Swap Anthropic for OpenAI for Gemini and your loop still works. But remove the scaffolding, and you have nothing.

The industry calls this scaffolding a **harness**. And the insight that started Armature was simple: nobody ships the harness. They ship the model integration, and they rebuild the scaffolding from scratch every time.

---

## The Moment the Problem Became Clear

I was building a multi-step analysis pipeline — one of those workflows where you gather data, summarize it, cross-reference it against something else, have a different agent evaluate the cross-reference, and produce a report. Probably six or seven steps. Each step needed the output of the prior step. Some steps could run in parallel. One step needed to retry with different framing if the first attempt produced unusable output.

I wrote it in Python. It worked. Then I needed a slightly different version for a different client. So I copied the Python, changed the prompts, changed the model calls, struggled with the places where I'd hardcoded assumptions about the prior version's output structure.

Then I needed a third version. At this point I had three copies of substantially the same scaffolding — the loop, the context management, the retry logic, the logging — all slightly different, all carrying the accumulated bugs and assumptions of whenever I'd written each one.

This is the most reliable sign that something is wrong: you're copying boilerplate instead of describing the problem.

---

## The CrewAI Detour That Taught Me the Most Important Thing

Before Armature, I spent serious time building deliberative agentic systems with CrewAI. I learned a lot from those efforts — more than from any paper. The core insight: when you give multiple agents the same problem and let them debate, the result is measurably better than any single agent working alone. Quality debates produce better outcomes. The dissent matters.

I built an open-source project called [Quorum](https://github.com/elfautomations/quorum) specifically to explore this — a deliberation system where AI agents argue, challenge each other's reasoning, and converge on consensus. Running it, tuning it, watching where it produced coherent results and where it collapsed into incoherent agreement, gave me something no paper could: an intuition for what quality actually looks like in multi-agent work.

What crystallized from that experience was the value of **LLM-as-judge**. Not a human reviewing every output, not a unit test checking a schema — a separate model, with a different prompt and often a different role, evaluating whether the first model's output is actually good. Not just valid. *Good*. The judge catches things the worker can't catch about its own output: circularity, overconfidence, gaps in reasoning, answers that technically satisfy the prompt but miss the point.

Once I understood that, I couldn't build without it. Every serious Armature workflow has a judge stage. The judge is not overhead; it is where quality comes from.

---

## Why Not a Framework?

The obvious answer, after all that, is: build a framework. Wrap the common patterns in classes and helpers, publish it, let others extend it.

But the frameworks already exist, and I'd used them seriously. LangChain gives you building blocks — chains, retrievers, memory abstractions, tool integrations. LangGraph gives you a graph you construct in code, designed around loops and cycles. CrewAI, which I know well, gives you a crew metaphor with agents and tasks. AutoGen gives you agents that talk to each other.

They're all libraries that give you more expressive ways to write the boilerplate. You still write orchestration code. You still wire up the safety checks yourself. You still build your own quality measurement, or you don't have one at all. You still figure out observability on your own. You still implement retry logic, still think about how to handle partial failures in a fan-out operation, still manage the complexity of a workflow that spans many model calls.

A framework lowers the floor. A harness raises the ceiling.

What I wanted was something different: a finished execution environment where you describe the *workflow* — not the *code to execute the workflow*. That distinction turned out to matter enormously.

---

## The Research Moment

I'd been reading papers — the wave of agentic AI research that hit in early 2026 was impossible to ignore if you were building in this space. Several papers converged on the same insight from different angles.

A Tsinghua team published results showing that workflows defined in **structured natural language outperform equivalent code-based harnesses** on complex tasks — 47.2% vs. 30.4% on the OSWorld benchmark. Their key finding: when the harness specification is readable and editable text, the *entire system including an optimizer can reason about it*. You can't feed Python orchestration code to a model and ask it to improve the workflow. You can feed YAML.

Stanford published a paper on meta-harnesses — using a frontier model to read execution traces from prior runs and propose improvements to the harness specification itself. The key result: giving the optimizer access to full execution traces (not just pass/fail scores) improved accuracy from 41% to 57%. The model could reason *causally* about why runs failed and propose targeted edits.

A third paper formalized failure signatures — specific diagnostic codes that categorize run failures in ways that drive targeted improvement. A stage timeout is a different failure mode than a schema validation error, which is different from a judge returning low confidence. Knowing *which* failure mode you're in tells you *what kind of fix to propose*.

There were nine papers in total. They all pointed at the same thing: the harness is more important than the model, and a harness that can read and improve its own specification is categorically different from one that can't.

---

## YAML as the Design Decision — and Why Kubernetes Convinced Me

The most consequential design choice in Armature was writing workflows in YAML instead of Python.

This sounds trivial. It isn't.

The analogy that convinced me: Kubernetes.

I've implemented Kubernetes. I've run clusters, written operators, debugged etcd corruption at 2am. But ask me how k8s internally handles the reconciliation loop — how the scheduler assigns pods, how the controller manager detects drift and drives the cluster back to desired state, how rolling updates sequence across replica sets — and I'll give you a rough picture, not a precise one. I don't need the precise one. I describe what I want in YAML and Kubernetes figures out how to make it so. I get rolling deployments, self-healing, service discovery, resource management, and horizontal autoscaling without knowing the implementation of any of it.

That's the pattern worth stealing for agentic workflows.

When your workflow lives in Python, only engineers can read it, write it, or modify it. When it lives in YAML, your domain expert can read it. Your product manager can understand what each stage does. A model optimizer can propose changes and diff them cleanly. And the people who understand the domain best — who are rarely the people writing orchestration code — can actually engage with what the agents are doing.

More importantly: when the workflow specification is a data structure instead of code, the harness can *reason about it*. The self-improvement loop reads the spec, reads the traces, proposes edits to the YAML, and re-runs. That's only possible because the spec is data. If the workflow were a Python function, the optimizer would need to understand Python semantics — a much harder problem.

You don't need to know how Armature's DAG executor works to build a workflow that fans out across 100 documents, judges each one, and produces a synthesized report. You describe what you want. The harness makes it so.

The tradeoff is expressiveness. YAML is not Turing-complete. You can't write arbitrary control flow. You can't implement a loop that runs for an unknown number of iterations determined at runtime by the model. I think that's fine — most production workflows don't need that, and the cases that do are better served by a different tool. What you can express declaratively covers the vast majority of the real work.

---

## Building It: What Went In

The harness grew in layers, and each layer was driven by a specific production concern.

**First, execution.** A DAG executor that derives the execution order from `depends_on:` declarations, runs each wave of ready stages concurrently, and accumulates results into a shared context. 88 lines. No wiring code required.

**Then, quality measurement.** An IHR score that combines output validity, success rate, judge confidence, latency, and human failure rate into a single number per run. Without this, you have no answer to the question "is my workflow getting better or worse?" The Quorum work taught me to take quality measurement seriously before the research confirmed why.

**Then, the judge.** Every role type in Armature — researcher, worker, judge, orchestrator — has a specific cognitive posture. The judge role exists because of what I learned building Quorum: a separate evaluator using a frontier model as its default produces categorically different results than asking the worker to evaluate its own output. The judge is not a luxury. It is the quality mechanism.

**Then, fan-out.** A workflow author declares `fan_out: 10` on a stage and points it at a list. The harness runs the stage once per item, bounded by a semaphore. Per-item failures are isolated. Results are collected via a configurable strategy: list everything, merge into a dict, take the first, or run consensus voting across multiple runs of the same prompt. The author writes one stage in YAML; the harness handles all the concurrency.

**Then, safety and isolation.** Here is where I thought hard about enterprise deployment anxiety. Any engineer who has shipped production systems knows the anxiety of deploying an AI agent that can call tools, write files, and make HTTP requests. The question every enterprise security team asks first is: *what can this thing actually touch?*

Armature answers this at two levels, and both levels are visible in the same YAML spec.

The first is policy: declarative `safety_rules:` that constrain what agents can request. Block file writes outside a specific directory. Warn on HTTP calls to external domains. Require human approval for anything irreversible. Fail closed if no rule matches in strict mode. The rules are auditable, version-controlled, and applied before any tool call dispatches.

The second is execution: `sandbox.mode: docker` routes every shell call through an ephemeral Docker container that disappears after the call. The container sees only the declared workspace directory — nothing else on the host filesystem. Network is off by default. CPU and memory are bounded. Every trace records the SHA256 content digest of the image that ran, so you have proof of exactly which software executed at any point in time. Add `sandbox_image` to a stage for a per-stage override — different execution environments for different stages of the same workflow, without building a monolithic image.

Policy defines what is allowed. The container enforces what is possible. These are different controls at different layers — defense in depth that enterprise infrastructure teams recognize.

The answer to "what can this agent touch on your infrastructure?" becomes clean enough to deliver in a security review:

- Computation in ephemeral, resource-bounded, network-isolated containers that disappear after each call
- Files scoped to one declared directory; the host's other paths are invisible to the container
- Network access explicit and off by default
- Execution environment is the image you specify, not whatever happens to be installed on the host machine
- Every execution traceable by model, inputs, policy version, and image digest

That is a security posture you can describe in two sentences and defend in a room full of skeptical infrastructure engineers. The container boundary is the security boundary — an established concept that does not require explaining a new abstraction. You can tell your security team exactly what the agent is and isn't allowed to do, and show them the YAML that enforces it. That matters more than most engineers realize until they're in the room trying to get a deployment approved.

**Then, self-improvement.** An optimizer loop that reads traces, computes IHR, identifies failure signatures, proposes edits to the YAML, and applies them. Each proposed improvement declares what it predicts it will fix and what might temporarily worsen — turning "did the score go up?" into "did the change do what we said it would?" That accountability mechanism is borrowed from research, but the motivation came from real experience: improvement loops that aren't accountable drift.

**Then, memory.** Four layers of it: mission context injected into every stage's system prompt, continuation that carries selected outputs across runs, a rolling episodic memory store, and a knowledge store where LLM-extracted entity/fact triples accumulate across many runs. The difference between a workflow that forgets everything on each activation and one that builds institutional knowledge over time.

**Then, model tiers — and a strong opinion about SLMs.** Every stage in Armature declares a model tier: `tiny`, `small`, `medium`, `large`, or `frontier`. Tier names map to actual provider/model combinations in one place. The researcher stage uses a small model. The worker uses small. The judge uses frontier. This is not arbitrary — it is a deliberate cost architecture.

I use SLMs almost everywhere in production. I have done serious LoRA fine-tuning of smaller models on task-specific data, and those fine-tuned SLMs outperform general-purpose frontier models on the specific tasks they were trained for, at a fraction of the cost and with better latency. I believe strongly that the AI world is moving toward on-prem and local-first deployment — enterprise customers are already pushing for it, data sovereignty requirements are accelerating it, and the capability of SLMs has crossed the threshold where frontier models are simply unnecessary for most of the work an agentic pipeline does. A Llama or Mistral model running locally on your hardware, fine-tuned on your domain, beats a cloud API for most classification, retrieval, and structured extraction tasks.

Armature is built with this in mind. The tier system is provider-agnostic. Point a tier at Ollama running locally, or at an OpenAI-compatible endpoint hosting a fine-tuned SLM, and everything works. The built-in trace exporter can produce LoRA training data directly from high-quality runs — frontier model outputs become the training signal for smaller specialist models. The system is designed to let frontier models earn their own obsolescence on any given task.

By the time I was done, there were 1,388 tests passing. That number surprised me. It is the accumulated evidence of how many edge cases production actually has.

---

## What "Harness" Means

The analogy I keep coming back to: the LLM is the engine. Armature is the car.

You don't wire up your own transmission, design your own braking system, and instrument your own dashboard every time you want to drive somewhere. The car handles all of that. You tell it where to go.

A framework gives you better tools for building the car. A harness *is* the car.

The Kubernetes parallel holds here too. You don't ask your application engineers to implement their own scheduling algorithm, their own health-check reconciliation loop, or their own rolling update strategy. Those are infrastructure concerns that belong in the platform. Orchestration, quality measurement, safety enforcement, observability, self-improvement, and a stable API are infrastructure concerns for agentic workflows. They belong in the harness, not in every workflow you write.

---

## On Innovation and Pattern Transfer

There is a myth about how good things get made. The myth says innovation appears out of nowhere — a lone genius, a blank page, an idea that existed nowhere in the world before. That is almost never how it actually works.

Most innovation is pattern transfer. You see something working well in one domain. You recognize that the underlying pattern is not domain-specific. You carry it somewhere else and apply it. The creativity is in the *seeing* — recognizing that a pattern belongs somewhere it has never been, and having the conviction to act on that recognition before anyone else does.

Armature is unapologetically built this way, and I want to name the patterns without embarrassment because the honest accounting makes the project more interesting, not less.

Fan-out and fan-in: distributed systems, MapReduce, Spark, Hadoop — applied to LLM pipelines. DAG execution: Kahn's algorithm from 1962 — applied to agentic stage ordering. YAML-declarative workflows: Kubernetes — applied to agent specification. LLM-as-judge: deliberative systems and my own experience building Quorum — formalized in academic research but felt in practice first. IHR as a quality metric: a Tsinghua paper. The optimizer loop: Stanford. The safety rule DSL: AgentSpec and Microsoft's Agent Governance Toolkit. The prediction-verification accountability loop: Agentic Harness Engineering. The memory architecture: the Continual Harness paper. Model tiers and SLM-first economics: an industry conviction built from doing LoRA fine-tuning and watching the results.

None of those sources built a finished harness. They each established a piece. The creative act was seeing that these pieces — drawn from distributed systems, Kubernetes infrastructure, academic ML research, a deliberation project I built, and enterprise governance frameworks — were actually pointing at the same thing: a finished, declarative, self-improving execution environment for agentic teams.

That is what I mean by the art of creativity. Not invention from nothing. Pattern recognition across distance. The ability to see that the reconciliation loop Kubernetes uses to close the gap between desired and actual state is the same structural idea as an optimizer that closes the gap between a workflow's current performance and its potential. The ability to see that MapReduce's scatter-gather pattern is the right shape for parallel LLM calls over a document corpus. The ability to see that LLM-as-judge is the same idea as peer review, which is itself the same idea as the deliberative systems I'd been building in Quorum.

Each of those recognitions is a small creative act. Armature is their accumulation. That is a real contribution — not a novel algorithm, but a novel synthesis — and I'll state it with confidence rather than hedge it.

---

## Where This Lands

Armature is production software. It runs. 1,388 tests say it runs correctly across a large surface area of edge cases. The self-improvement loop works — run a workflow, let it optimize, watch the IHR go up over multiple iterations.

But it is also, right now, a project that almost nobody knows exists.

That's the problem the documentation and this post are meant to address. The AI tooling space is genuinely noisy — there are hundreds of frameworks, dozens of agent orchestration libraries, and a constant stream of new approaches. Finding something worth using requires more than a GitHub repository.

What Armature offers that I have not found elsewhere: a finished harness that handles the full production surface area — orchestration, quality measurement, safety enforcement, self-improvement, memory, and a stable HTTP API — from a YAML spec that non-engineers can read and models can optimize, designed to work with SLMs running locally as naturally as with frontier cloud APIs.

If you are building agentic pipelines that need to run reliably, improve over time, be understood by people who don't write Python, and eventually migrate off expensive frontier models as your domain knowledge crystallizes into fine-tuned specialists — that is a specific combination of things that is worth knowing exists.

The harness is more important than the model. That turned out to be true.

---

*Armature is open source. The documentation is at the project root. The fastest way to understand it is to read `BUILD_FIRST_WORKFLOW.md` and run the examples. If you've built deliberative agentic systems and care about quality measurement, I'd especially like to hear what you think.*

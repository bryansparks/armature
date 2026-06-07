# Armature — Launch Content Package

**Project:** Armature v0.2.0  
**Author:** Bryan Sparks (bryan@drycanyon.com, drycanyon.com)  
**GitHub:** https://github.com/bryansparks/armature  
**Website:** https://bryansparks.github.io/armature  
**Install:** `pip install armature`

---

## 1. SEO / Traffic Keywords

### Core Concept
- reasoning automation
- reasoning automation framework
- multi-agent workflow engine
- declarative multi-agent workflows
- YAML agent orchestration
- agent execution harness
- agentic workflow YAML
- self-improving AI workflows
- IHR implicit harness rating
- DAG-based agent orchestration
- agent harness Python
- workflow as code YAML AI

### Framework Comparisons
- LangGraph alternative
- CrewAI alternative
- AutoGen alternative
- LangChain alternative Python
- LangGraph vs Armature
- declarative alternative to LangGraph
- no-code agent orchestration Python
- YAML instead of Python agent wiring
- simpler alternative to LangChain
- LangGraph without Python boilerplate

### Use Cases
- multi-agent business process automation
- AI-driven compliance review workflow
- parallel research pipeline AI
- fan-out fan-in agent pipeline
- long-horizon AI workflow
- autonomous business logic AI
- self-improving production AI system
- scheduled AI workflow daemon
- webhook triggered AI workflow
- AI workflow with human approval gate
- AI quality scoring pipeline

### Technical Terms
- guided JSON output mode
- model tier escalation
- fan-out fan-in LLM
- litellm provider abstraction
- SQLite trace store agent
- OpenTelemetry agent observability
- agent safety DSL declarative
- ToolSafetyRule block warn require_approval
- continuation block long-horizon memory
- post-run self-improvement loop
- SelfImproveRunner spec rewrite
- LLM consensus fan-in strategy
- Kahn's algorithm DAG executor
- arXiv agent harness research

---

## 2. Hacker News — Show HN Post

**Title:** Show HN: Armature – multi-agent AI workflows in YAML with a built-in self-improvement loop

---

I kept hitting the same wall.

I'd build a multi-step analysis pipeline in Python — gather data, summarize it, cross-reference it, evaluate the cross-reference, produce a report. It worked. Then I needed a slightly different version. So I copied the Python file and changed the prompts. Then a third version. Now I had three copies of substantially the same retry logic, context management, and LLM wiring — each slightly different, each carrying its own bugs.

This is the most reliable sign something is wrong with an approach: you're copying boilerplate instead of describing the problem.

I spent time with LangGraph, CrewAI, and AutoGen before building this. They're all good libraries. But they're still libraries — they lower the floor on writing orchestration *code*. You still wire up safety checks yourself. You still build quality measurement yourself, or you don't have one at all. The frameworks give you more expressive ways to write the scaffolding. I wanted something where I just describe the workflow.

**Armature is what I built.** You define your agentic team as a YAML spec. The engine handles the rest: DAG execution, parallel fan-out, retries, structured output validation, safety rules, and a full trace store. A minimal workflow looks like this:

```yaml
name: research-brief
version: "1.0"

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  large:
    provider: anthropic
    model: claude-sonnet-4-6

role_type_defaults:
  researcher: large
  judge: large

contracts:
  inputs:
    - name: topic

stages:
  - id: researcher
    role:
      name: Researcher
      type: researcher
      description: "Research this topic thoroughly: {{ topic }}"
    output_mode: text
    depends_on: []

  - id: editor
    role:
      name: Editor
      type: worker
      description: |
        Tighten the researcher's draft into a crisp 3-paragraph briefing.
        {{ researcher.content }}
    output_mode: text
    depends_on: [researcher]
```

```bash
armature validate research-brief.yml
armature run research-brief.yml --input topic="quantum error correction"
```

That's it. No Python. No class subclassing. No explicit edge wiring.

**The part I'm most invested in is the self-improvement loop.** Every run records a `TraceRecord` to SQLite — stage outputs, latency, validation results, retry counts, quorum scores if you're using fan-in consensus. The engine computes an IHR (Implicit Harness Rating) — a weighted composite of output validity, success rate, quorum consensus, latency, and escalation rate. When IHR drops below 0.75, `armature improve` kicks in: it loads your traces, runs a diagnostic analyzer over the failure signatures, calls a medium-tier LLM to propose targeted YAML rewrites, applies the safe changes in-place, and writes the structural ones to a `.pending.yaml` for your review. The spec improves itself between runs.

The `--auto-improve` flag runs this automatically after every execution.

This loop is grounded in actual research — I synthesized 7 arXiv papers (NLAH, MetaHarness, AutoHarness, AgentSpec, Continual Harness, AHE, KYA, ActiveGraph) into the design. The Tsinghua NLAH paper is the architectural foundation; the finding that YAML specs outperform Python harnesses 47.2% vs 30.4% on OSWorld is what convinced me to commit to the declarative approach. The full citations are in the CHANGELOG.

One meta-note: I organized this launch using an Armature launchpad workflow — a spec that drafts platform-specific posts, evaluates them against tone rules, and produces a prioritized launch sequence. I ran it this morning. It worked. That was a good sign.

The project has 1,330 tests (pytest, asyncio), MIT license, and runs on any litellm-supported provider — Anthropic, OpenAI, OpenRouter, Ollama, whatever you have keys for.

`pip install armature`

GitHub: https://github.com/bryansparks/armature  
Docs: https://bryansparks.github.io/armature

**Technical question for the thread:** The self-improvement loop currently uses a medium-tier model to propose YAML rewrites (per arXiv:2605.30621v1, which found ≤3.1pp quality difference vs. frontier models at dramatically lower cost). But the tradeoff is that the medium-tier proposer occasionally misses subtle spec interactions. Has anyone found a good heuristic for when to escalate a spec evolution task to a frontier model, short of just always paying frontier rates?

---

## 3. Twitter/X Thread (8 tweets)

**Tweet 1 (hook):**
The failure mode nobody warns you about with agentic frameworks: you get good at wiring agents together, ship something that works, and then discover three months later you have five slightly different Python copies of the same retry/context/logging scaffolding, each with its own bugs.

The problem isn't the framework. It's that you keep writing the scaffolding.

---

**Tweet 2:**
I built Armature to fix this. You write a YAML spec. The engine handles DAG execution, parallel fan-out, retries, safety, and observability.

```yaml
stages:
  - id: researcher
    role: {type: researcher, description: "Research: {{ topic }}"}
    output_mode: text
    depends_on: []

  - id: judge
    role: {type: judge, description: "Assess: {{ researcher.content }}"}
    output_mode: guided_json
    output_schema:
      type: object
      required: [accept, issues]
      properties:
        accept: {type: boolean}
        issues: {type: array, items: {type: string}}
    depends_on: [researcher]
```

No Python. No class subclassing.

---

**Tweet 3:**
Parallel research pipelines work the same way. You declare a `fan_out` with a `partition_source`, and the engine runs all branches with `asyncio.gather`. Fan-in strategies include `list`, `merge`, `first`, and `consensus` — the last one uses a judge LLM to synthesize conflicting outputs.

The DAG is a consequence of your `depends_on` declarations. You never write the graph directly.

---

**Tweet 4:**
Model tiers let you name capability slots instead of hardcoding models:

```yaml
model_tiers:
  small: {provider: openrouter, model: qwen/qwen3.6-27b}
  large: {provider: openrouter, model: moonshotai/kimi-k2.6}
```

When `guided_json` validation fails, the engine auto-escalates to the next tier. You set the tiers; Armature decides when to use which one.

Works with any litellm provider. Anthropic, OpenAI, Ollama, OpenRouter.

---

**Tweet 5:**
Safety is declarative too:

```yaml
safety_rules:
  - tool: file_write
    condition: {field: path, operator: matches_regex, value: "^/etc/"}
    action: block
  - tool: http_post
    condition: {field: _tool_reversibility, operator: equals, value: NONE}
    action: require_approval
```

`block`, `warn`, `log`, `require_approval`, `allow`. `safety_mode: strict` flips the default to deny-on-no-match. Sub-millisecond evaluation. No Python hook code.

---

**Tweet 6:**
The part I spent the most time on: the self-improvement loop.

Every run writes trace records to SQLite. The engine computes IHR — a weighted composite of output validity, success rate, quorum consensus, latency, and escalation rate.

When IHR drops below 0.75: `armature improve` loads the traces, diagnoses the failure signatures, calls an LLM to propose targeted YAML rewrites, applies the safe ones in-place.

`armature run my_workflow.yml --auto-improve` does this after every run automatically.

The spec gets better on its own.

---

**Tweet 7:**
Armature is one component of a larger platform I am building.

The working name is ElfTech — a stack of AI systems covering reasoning (Armature), deliberation, code generation, deployment, and coordination. The goal is an autonomous-organization platform where AI systems handle end-to-end business processes with minimal human overhead.

Armature is the execution engine. The rest is in progress.

Stay tuned.

---

**Tweet 8:**
Armature v0.2.0 is out now.

pip install armature

GitHub: https://github.com/bryansparks/armature
Docs: https://bryansparks.github.io/armature
HN discussion: [link]

1,330 tests. MIT license. Runs on any litellm provider.

What would you actually use this for? Specifically: what multi-step AI task do you keep rebuilding from scratch every project?

---

## 4. Reddit Posts

---

### r/MachineLearning

**Title:** Armature: A declarative agent harness implementing NLAH, MetaHarness, AgentSpec, and 4 other arXiv papers — self-improving YAML workflows with IHR-driven spec evolution [Show HN]

**Body:**

I spent several months synthesizing seven arXiv papers published between February and May 2026 into a working Python library. The result is Armature — a declarative agent execution harness that runs multi-agent workflows defined as YAML specs and improves those specs automatically from execution traces.

The research foundation:

- **NLAH** (arXiv:2603.25723, Tsinghua) — architectural basis. Key finding: YAML-defined harnesses outperform Python-coded equivalents 47.2% vs. 30.4% on OSWorld. Also defines IHR (Implicit Harness Rating) and the parallel fan-out primitive.
- **MetaHarness** (arXiv:2603.28052, Stanford) — the optimizer. Frontier LLM reads execution traces and proposes targeted harness edits. Full trace access improves optimization accuracy from 41% to 57% vs. pass/fail scores only.
- **AutoHarness** (arXiv:2603.03329) — NL-to-spec synthesis loop and the harness-as-verifier concept behind the judge role.
- **AgentSpec** (arXiv:2503.18666) — declarative safety DSL. Pre/post-tool hooks, composable condition rules, sub-millisecond evaluation.
- **Continual Harness** (arXiv:2605.09998) — two-loop self-improvement (inner: post-run refiner stage; outer: cross-run trace-driven spec rewrite). Failure signature taxonomy: `stage_failed`, `output_invalid`, `low_confidence`, `high_escalation`.
- **AHE** (arXiv:2604.25850) — prediction-verification loop: each proposed spec change must include falsifiable contracts for which failure signatures it expects to fix. Next cycle verifies using precision/recall against observed outcomes.
- **KYA** (arXiv:2605.25376) — static spec risk scoring and the only-tighten safety composition principle (allow rules cannot override block rules).
- **ActiveGraph** (arXiv:2605.21997) — LLM response caching by content hash, behavior rules as trace-triggered reactive hooks, `--auto-improve` post-run gate.

**A minimal spec:**

```yaml
name: compliance-review
version: "1.0"
model_tiers:
  small: {provider: anthropic, model: claude-haiku-4-5-20251001}
  large: {provider: anthropic, model: claude-sonnet-4-6}
role_type_defaults:
  researcher: large
  judge: large
contracts:
  inputs:
    - name: document
stages:
  - id: analyst
    role:
      name: ComplianceAnalyst
      type: researcher
      description: "Identify compliance risks in: {{ document }}"
    output_mode: guided_json
    output_schema:
      type: object
      required: [risks, severity]
      properties:
        risks: {type: array, items: {type: string}}
        severity: {type: string, enum: [low, medium, high, critical]}
    depends_on: []
  - id: judge
    role:
      name: ComplianceJudge
      type: judge
      description: "Validate the analyst's findings: {{ analyst.risks }}"
    output_mode: guided_json
    output_schema:
      type: object
      required: [accept, confidence, issues]
      properties:
        accept: {type: boolean}
        confidence: {type: number}
        issues: {type: array, items: {type: string}}
    depends_on: [analyst]
```

IHR is computed as: `0.35 × output_valid_rate + 0.25 × success_rate + 0.20 × avg_quorum_score + 0.10 × latency_score + 0.10 × happy_path_rate`. When IHR < 0.75, `armature improve` loads traces, runs `DiagnosticAnalyzer`, calls a medium-tier LLM (not frontier — arXiv:2605.30621v1 shows ≤3.1pp quality difference at much lower cost) to propose targeted YAML rewrites, applies safe changes in-place.

Full citations in CHANGELOG.md: https://github.com/bryansparks/armature/blob/main/CHANGELOG.md

`pip install armature` — MIT license, 1,330 tests, Python 3.11+.

---

### r/LocalLLaMA

**Title:** I built a self-improving multi-agent workflow engine that runs on any local model — YAML specs, no Python wiring required (Show HN)

**Body:**

I've been building Armature — a declarative agent execution harness. You write YAML specs describing your multi-agent workflow; the engine runs them, tracks quality metrics, and rewrites the spec itself when performance degrades. It runs on any litellm-supported provider, which means Ollama, LM Studio, llama.cpp, or anything with an OpenAI-compatible endpoint.

```yaml
model_tiers:
  fast:
    provider: ollama
    model: llama3.2:3b
    temperature: 0.1
  capable:
    provider: ollama
    model: qwen2.5:32b
    temperature: 0.2

role_type_defaults:
  worker: fast
  judge: capable
```

That's all it takes to run Armature on local models. The model tier system lets you mix providers — use a local Qwen for workers and a remote Sonnet for judges, or go fully local. The engine escalates to the next tier automatically when structured JSON output fails to validate.

**Why this matters for local model users specifically:**

The self-improvement loop uses a medium-tier model to propose spec rewrites — a deliberate choice based on research showing ≤3.1pp quality difference vs. frontier models. That means you can run the entire loop locally with a capable 32B model and get most of the benefit without an API call.

The fan-out primitive runs parallel research branches with `asyncio.gather`. If you're doing RAG or search pipelines, this gives you N simultaneous local model calls collecting different angles on a question, then a judge stage synthesizing them:

```yaml
  - id: research_branches
    fan_out: 6
    fan_in: consensus
    partition_source: "{{ planner.queries }}"
    partition_key: search_item
    role: {type: worker, description: "Research: {{ search_item }}"}
    depends_on: [planner]

  - id: synthesizer
    signature:
      input:
        research_branches: All research findings
    role: {type: judge, description: "Synthesize: {{ research_branches }}"}
    depends_on: [research_branches]
```

`fan_in: consensus` uses a judge LLM to handle conflicting results. `fan_in: list` just collects them all. No code to write.

Meta-note: I ran a Armature launchpad workflow this morning to organize this launch — a spec that drafted the platform-specific posts and evaluated them against tone criteria. It worked without a single Python change.

`pip install armature` | https://github.com/bryansparks/armature | MIT | 1,330 tests

Curious what local model setups people are running for multi-stage workflows. What's your current go-to for a capable 32B-ish that stays reasonably fast on a single GPU?

---

### r/Python

**Title:** Show HN: I was copying the same retry/context/logging boilerplate across projects — so I built a YAML-based multi-agent workflow engine instead

**Body:**

I built the same Python scaffolding four times. A loop. An LLM call. Output parsing. Error handling. Retry when the model returns garbage. Another retry when the API times out. A check that the output schema is what downstream code expects. Four projects, four slightly different versions, four sets of bugs.

The problem isn't that I was bad at Python. The problem is that I was writing the same harness instead of describing the workflow.

I built Armature to fix this. You write a YAML spec. The engine handles everything else.

```yaml
name: content-pipeline
version: "1.0"

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001

role_type_defaults:
  worker: small
  judge: small

contracts:
  inputs:
    - name: topic

stages:
  - id: researcher
    role:
      name: Researcher
      type: researcher
      description: "Research this topic: {{ topic }}"
    output_mode: text
    depends_on: []

  - id: writer
    role:
      name: Writer
      type: worker
      description: |
        Write a 3-paragraph briefing based on this research:
        {{ researcher.content }}
    output_mode: text
    depends_on: [researcher]

  - id: editor
    role:
      name: Editor
      type: judge
      description: "Tighten and improve: {{ writer.content }}"
    output_mode: text
    depends_on: [writer]
```

```bash
pip install armature
armature validate content-pipeline.yml
armature run content-pipeline.yml --input topic="renewable energy storage"
```

Or from Python:

```python
import asyncio
from armature import Harness

async def main():
    harness = Harness.from_spec("content-pipeline.yml")
    result = await harness.run({"topic": "renewable energy storage"})
    print(result["editor"]["content"])

asyncio.run(main())
```

What you get without writing any Python orchestration code:

- **DAG execution** — `depends_on` declarations are all you need; the engine resolves parallelism automatically
- **Retries with escalation** — `on_fail: loop:` in YAML; `guided_json` auto-escalates to a larger model tier on parse failure
- **SQLite trace store** — every run recorded; `armature dashboard` shows health metrics
- **Self-improvement** — `armature improve` reads traces, diagnoses failure patterns, proposes YAML rewrites; `--auto-improve` does it after every run
- **Safety rules** — declarative `block`/`warn`/`require_approval` on any tool call
- **FastAPI service** — `pip install 'armature[service]'` then `armature serve --specs-dir ./specs`; named workflow API out of the box

No framework dependency. No agent classes to subclass. No prescribed team structure. Just a DAG executor, an LLM adapter, and your spec. 1,330 tests, MIT license.

I used it to organize this launch — a workflow that drafted platform posts and evaluated them against tone rules. That was the final dogfood test.

https://github.com/bryansparks/armature | `pip install armature`

---

## 5. LinkedIn Post

Three years ago I stopped posting here. I got busy building, and LinkedIn started feeling like a place where people performed progress instead of making it.

Today I have something real to share.

I just launched Armature — an open source Python library for building multi-agent AI workflows. The concept: instead of writing Python orchestration code every time you want multiple AI agents to collaborate on a task, you write a YAML spec describing the team and the workflow. The engine handles execution order, parallel fan-out, retries, safety rules, quality scoring, and a self-improvement loop that rewrites the spec when performance degrades.

This is the thing I kept wishing existed for the last two years. I built it.

Underneath the library is something I think matters more than the code: a conviction that the next decade of valuable AI engineering isn't about training larger models — it's about building reliable harnesses that make models do real work consistently. Reasoning automation, not just AI features. End-to-end business processes where multi-agent deliberation replaces brittle rule-based logic, and the system gets measurably better over time.

Armature is the first component of a larger platform I'm building under the working name ElfTech — an autonomous-organization stack where AI systems handle reasoning, deliberation, code generation, and coordination with minimal human overhead. The ambition is big. The first piece is working and shipped.

If you're building with LangGraph, CrewAI, or AutoGen and hitting walls — I'd genuinely like to know what's breaking. That feedback shapes what gets built next.

pip install armature | github.com/bryansparks/armature

What's the most tedious multi-step process in your work that you think AI could handle reliably if the orchestration were solid enough?

---

## 6. Blog Post Draft

**Title:** Why I built Armature (and why your multi-agent system will fail in production)

---

I know where the failure is going to happen, because I've been there.

You built an agentic workflow. It worked in development. Maybe it even worked in the first few weeks of production. Then, slowly, it started returning garbage. A stage that reliably produced valid JSON started returning text. A retry loop that was supposed to catch this started looping past its limit. You found out when a human noticed the output looked wrong — not because any alarm fired, not because a log filled up, but because someone was paying enough attention to notice.

You were flying blind. There was no IHR, no trace store, no signal to watch. Just a Python loop calling an LLM, and silence when it stopped working.

This is the failure I built Armature to prevent.

---

### The wall I kept hitting

I spent two years building multi-agent systems with the frameworks that existed — LangChain, CrewAI, LangGraph. I learned a lot from each of them, and I'm not dismissing them. But every project I built had the same structural problem: the orchestration logic lived in Python code that I had to rewrite every time, the quality measurement didn't exist unless I built it, and the system had no way to know when it was degrading.

The most telling sign was when I needed a second version of a workflow I'd already built. I copied the Python file. Changed the prompts. Struggled with the places where I'd hardcoded assumptions about the prior version's output shape. Then I needed a third version. Now I had three copies of substantially the same retry logic, context passing, and error handling — each slightly different, each accumulating its own bugs.

You're copying boilerplate instead of describing the problem. That's always the sign that something is structurally wrong.

---

### The insight: declarative + self-improving

Two things had to be true at once for a harness to actually work in production.

First, the workflow specification had to be **declarative** — text that describes *what* the workflow does, not *how* to execute it. When the spec is code, only engineers can read or modify it. When the spec is YAML, your domain experts can engage with it, an optimizer can propose changes as a clean diff, and you can version-control the logic without it being tangled up with implementation details.

A Tsinghua research team published results confirming what I'd suspected from practice: YAML-defined harnesses outperform equivalent Python-coded harnesses 47.2% vs. 30.4% on complex task benchmarks. The key finding was that when the specification is readable text, the entire system — including an optimizer — can reason about it. You can't feed Python orchestration code to a model and ask it to improve your workflow. You can feed YAML.

Second, the workflow had to be **self-improving**. Not "you can tune it manually." Self-improving: runs produce traces, traces produce diagnostics, diagnostics drive targeted spec rewrites, rewrites get applied automatically. Every run should make the next run slightly better.

Stanford published a paper showing that a frontier model given access to full execution traces (not just pass/fail scores) could propose harness improvements with 57% accuracy — versus 41% with just scores. The model can reason causally about *why* a run failed when it has the trace. "The output_valid_rate on the analyst stage dropped to 0.4 in the last 5 runs; here is a more constrained output_schema that should fix it" is a different — and more useful — kind of improvement proposal than "the workflow scored 0.71; try again."

---

### How Armature works

You write a YAML spec. The spec declares model tiers (named capability slots, not hardcoded model names), a stage DAG with `depends_on` relationships, and whatever safety rules you need.

```yaml
name: risk-assessment
version: "1.0"
mission: >
  Assess contract documents for legal and financial risk.
  Be conservative — flag ambiguous clauses as medium risk, not low.

model_tiers:
  small: {provider: anthropic, model: claude-haiku-4-5-20251001}
  large: {provider: anthropic, model: claude-sonnet-4-6}

role_type_defaults:
  researcher: large
  judge: large

stages:
  - id: extractor
    role:
      type: researcher
      description: "Extract key clauses from: {{ document }}"
    output_mode: guided_json
    output_schema:
      type: object
      required: [clauses, parties]
      properties:
        clauses: {type: array, items: {type: string}}
        parties: {type: array, items: {type: string}}
    depends_on: []

  - id: assessor
    role:
      type: judge
      description: |
        Assess risk for each clause: {{ extractor.clauses }}
        Parties: {{ extractor.parties }}
    output_mode: guided_json
    output_schema:
      type: object
      required: [risk_level, flagged_clauses, recommendation]
      properties:
        risk_level: {type: string, enum: [low, medium, high, critical]}
        flagged_clauses: {type: array, items: {type: string}}
        recommendation: {type: string}
    depends_on: [extractor]
```

The engine resolves the execution order from `depends_on`, runs stages in parallel when their dependencies are met, handles `guided_json` validation failures by escalating to the next model tier, and records every stage execution to SQLite.

After the run, `armature dashboard risk-assessment.yml` shows a 4-panel health view: IHR trend, success rate, output validity rate, latency percentiles. `armature improve risk-assessment.yml` does the self-improvement cycle.

---

### The self-improvement loop (IHR)

IHR — Implicit Harness Rating — is a single composite score:

```
IHR = 0.35 × output_valid_rate
    + 0.25 × success_rate
    + 0.20 × avg_quorum_score
    + 0.10 × latency_score
    + 0.10 × happy_path_rate
```

When IHR drops below 0.75, the self-improvement cycle fires: `DiagnosticAnalyzer` extracts failure signatures from the traces (`stage_failed`, `output_invalid`, `low_confidence`, `high_escalation`), a medium-tier LLM proposes targeted YAML rewrites based on those specific failure modes, safe changes (prompt rewrites, schema tightening, model tier adjustments) are applied in-place, and structural changes go to `.pending.yaml` for your review.

The v0.2.0 release added `--auto-improve` to run this automatically after every execution. If IHR < 0.75, it fixes itself. If the fix requires review, it queues it.

---

### The research backing

Seven arXiv papers published between February and May 2026 converge on the same insight from different angles: the harness is more important than the model. NLAH (Tsinghua) defined the architectural primitives. MetaHarness (Stanford) proved trace-driven optimization works. Continual Harness formalized the two-loop self-improvement design. AgentSpec gave the safety DSL. AHE introduced prediction-verification to make improvement cycles accountable. KYA added static risk scoring and safety composition rules. ActiveGraph added caching, reactive behavior rules, and the post-run improvement gate.

Every major design decision in Armature traces directly to one of these papers. The citations are in CHANGELOG.md.

---

### The ElfTech vision

Armature is one piece of something larger.

I'm building a platform I'm calling ElfTech — an autonomous-organization stack. Armature handles reasoning workflows. Other components handle deliberation, code generation, deployment, and inter-system coordination. The goal is an organization where AI systems handle end-to-end business processes — not as assistants to human workflows, but as the primary actors in those workflows, with humans reviewing outcomes rather than approving every step.

That's a large claim and a long road. Armature is working and shipped. The rest is in progress.

---

### What to do next

```bash
pip install armature
armature doctor           # verify your environment
armature new              # interactive spec wizard
armature validate my_workflow.yml
armature run my_workflow.yml --input topic="your topic here" --auto-improve
```

The docs are at https://bryansparks.github.io/armature. The full USER-GUIDE.md covers fan-out pipelines, continuation blocks for long-horizon workflows, the safety DSL, memory, and the HTTP service.

If your multi-agent system is failing silently in production, I built this for you.

GitHub: https://github.com/bryansparks/armature  
pip install armature  
MIT license, 1,330 tests, Python 3.11+

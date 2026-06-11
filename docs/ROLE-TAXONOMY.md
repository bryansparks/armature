# Armature Role Taxonomy

The four role types — and why the distinction is not cosmetic.

---

Every stage in an Armature workflow declares a `role` with a `type` field. There are exactly four valid values: `researcher`, `worker`, `judge`, `orchestrator`. This is not a labeling convention — it is a design constraint. The taxonomy encodes two things simultaneously: the **cognitive posture** the model should adopt, and the **cost tier** it should run on by default.

```yaml
role:
  name: DocumentReviewer
  type: worker          # cognitive posture + default model tier
  description: |
    Review this document for compliance issues.
```

---

## The four types

### researcher

A researcher's job is to gather and surface information. It does not make decisions. It does not produce final output. It builds context for everything downstream.

**Default model tier:** `large` — broad knowledge, strong synthesis, good at recognizing what is relevant and what is not.

**Cognitive posture:** Look broadly, retrieve accurately, surface the right signals, do not editorialize.

**Typical tasks:**
- Web search and document retrieval
- Knowledge base queries
- Aggregating background context
- Summarizing source material into structured inputs for downstream stages

A researcher stage returns its output to the shared context. Later stages reference it. The researcher never sees those later stages — it has no authority over what happens with its findings. That separation is intentional.

---

### worker

A worker executes a well-defined task on a well-defined input. It is not deciding anything. It is not summarizing the whole picture. It is doing one thing to one item.

**Default model tier:** `small` — fast, cheap, high throughput. The entire point of a worker is that you can run many of them concurrently without cost anxiety.

**Cognitive posture:** Execute precisely. Follow the schema. Do not interpret beyond the task.

**Typical tasks:**
- Transform, extract, classify, format, translate
- Summarize a single document (not all documents — that is the orchestrator's job)
- Apply a rule to a single item
- Produce a structured JSON object from one input

Workers appear most naturally in fan-out stages, where N items need N executions in parallel. They are the **labor** in the pipeline. High volume, low cost per unit.

```yaml
- id: classify_each
  fan_out: 10
  partition_source: "{{ researcher_stage.articles }}"
  partition_key: article
  role:
    type: worker
    name: Classifier
    description: |
      Classify this article by topic. Article: {{ article }}
      Return {"topic": "...", "confidence": 0.0–1.0}.
```

---

### judge

A judge evaluates. It does not produce the thing being evaluated — it decides whether the thing meets a standard.

**Default model tier:** `frontier` — the most capable model available. Evaluation is harder than production. A judge needs to detect subtle failures, apply nuanced criteria, and make defensible accept/reject/escalate decisions.

**Cognitive posture:** Evaluate against explicit criteria. Score. Decide. Do not hedge — produce a verdict.

**Typical tasks:**
- Quality gates (does this output meet the standard?)
- Consensus voting across N parallel runs
- Escalation checks (does any item in this batch require human review?)
- Scoring and ranking competing outputs

The judge is deliberately separated from the worker because production and evaluation require different capabilities. A worker that is also evaluating its own output is not a judge — it is a worker with a conflict of interest. Separate stages, separate roles.

A judge can also implement the consensus voting pattern via `fan_in: consensus`, running the same evaluation N times and returning the most-agreed-upon verdict.

See `FAN-IN_FAN-OUT.md` for the `fan_in: consensus` pattern.

---

### orchestrator

An orchestrator sees the widest context in the workflow and produces final output or directs what happens next. It is the stage that synthesizes, integrates, and concludes.

**Default model tier:** `frontier` — it receives the most information and must reason across all of it.

**Cognitive posture:** Integrate everything you have seen. Produce a coherent, complete output. You are responsible for the result of the entire workflow.

**Typical tasks:**
- Final report generation
- Executive summary from multiple sub-analyses
- Cross-stage synthesis ("given everything workers found, what is the conclusion?")
- Deciding on a course of action based on judge verdicts and worker outputs

**Researcher vs. orchestrator** — these are easy to confuse. The distinction is directional: a researcher works *into* the pipeline, gathering raw inputs. An orchestrator works *out of* the pipeline, producing final output from processed inputs. A researcher reads the world and returns signal. An orchestrator reads prior stage results and writes the conclusion.

---

## Default tier mapping

These are encoded in `RoleTypeDefaults` in `armature/spec/models.py` and applied whenever a stage does not specify `model_tier` explicitly:

| Role type | Default tier | Rationale |
|-----------|-------------|-----------|
| `worker` | `small` | High-throughput, well-defined tasks; cost dominates |
| `researcher` | `large` | Broad synthesis; needs strong retrieval and reasoning |
| `judge` | `frontier` | Evaluation is harder than production; needs best reasoning |
| `orchestrator` | `frontier` | Sees widest context; final output quality matters most |

These defaults can be overridden at the stage level:

```yaml
role:
  type: researcher
  model_tier: medium    # downgrade if tasks are narrow and retrieval is simple
```

Or globally in the spec:

```yaml
role_type_defaults:
  researcher: medium    # apply to all researcher stages in this workflow
  worker: tiny          # use the smallest available tier for all workers
```

The defaults are not arbitrary — they encode a claim about where intelligence is worth paying for. Workers do repetitive, constrained work; spending frontier-model tokens on them is waste. Judges and orchestrators make the decisions that define whether the workflow's output is good; underpowering them is a quality risk.

---

## The organizational analogy

The four types map directly to roles in a real organization:

| Role type | Organizational analog | What they do |
|-----------|-----------------------|-------------|
| `researcher` | Analyst | Gathers data, surfaces findings, does not decide |
| `worker` | Operator | Executes a defined process on a defined input |
| `judge` | Reviewer / QA | Evaluates output against standards, approves or escalates |
| `orchestrator` | Manager / Author | Synthesizes inputs, owns the final deliverable |

This analogy is not decoration. It is the mental model the taxonomy is built around. When a workflow author declares `type: judge`, they are saying: "this stage is the reviewer, not the doer." That clarity matters when a non-engineer is reading the spec six months after it was written and trying to understand why a frontier-model stage exists in the middle of a pipeline that otherwise uses small models.

---

## Why the taxonomy matters

**It forces explicit purpose.** Every stage must declare what kind of cognitive work it is doing. You cannot accidentally write a stage that is simultaneously gathering context, transforming items, and evaluating quality — those are three separate roles and they run at three different cost tiers.

**It drives automatic cost optimization.** The default tier mapping means that a correctly-typed workflow is cheaply optimized without any explicit configuration. Workers are small by default; they do not need to be told to be cheap. Judges are frontier by default; they do not need to be told to spend.

**It communicates intent to readers.** A YAML spec with explicit role types is self-documenting in a way that raw prompts are not. `type: judge` tells a reader this stage is a quality gate. `type: worker` tells them this stage is doing repetitive transformation, probably inside a fan-out.

**It maps to real organizational patterns.** Teams that build agentic workflows already think in terms of analysts, operators, reviewers, and managers. The taxonomy gives them a direct mapping — no translation required.

---

## A complete example

This workflow uses all four role types in sequence, with fan-out over the worker stage.

```yaml
name: market-research-brief
version: "1.0"
mission: "Produce a market research brief from a list of competitor URLs."

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  large:
    provider: anthropic
    model: claude-sonnet-4-7
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  # --- researcher stage ---
  # Gathers the raw inputs. No decisions made. Returns a structured
  # list of competitor URLs and metadata for downstream stages.
  - id: gather_competitors
    role:
      name: CompetitorResearcher
      type: researcher             # large model by default
      description: |
        Search for the top 8 direct competitors of {{ company_name }}
        in the {{ market_segment }} space.
        For each competitor, return their main URL, primary value proposition,
        and estimated market position.
        Return:
          {"competitors": [
            {"name": "...", "url": "...", "value_prop": "...", "position": "leader|challenger|niche"},
            ...
          ]}

  # --- worker stage (fan-out) ---
  # One worker per competitor, all running concurrently.
  # Each worker processes exactly one competitor profile.
  # Small model: cheap, fast, well-defined task.
  - id: profile_each
    fan_out: 8
    fan_in: list
    partition_source: "{{ gather_competitors.competitors }}"
    partition_key: competitor
    role:
      name: CompetitorProfiler
      type: worker                 # small model by default
      description: |
        Analyse this competitor and produce a structured profile.
        Competitor: {{ competitor }}
        Return:
          {"name": "...",
           "strengths": ["..."],
           "weaknesses": ["..."],
           "pricing_model": "...",
           "target_customer": "...",
           "differentiation_score": 1–10}
    depends_on: [gather_competitors]

  # --- judge stage ---
  # Evaluates the profiles. Flags any that are too thin to include
  # in the brief. Does not write the brief — only approves inputs.
  - id: quality_gate
    role:
      name: ProfileReviewer
      type: judge                  # frontier model by default
      description: |
        Review these {{ profile_each | length }} competitor profiles.
        For each profile, decide whether it meets the bar for inclusion
        in an executive brief (sufficient depth, accurate differentiation score,
        at least 3 concrete strengths or weaknesses identified).
        Flag profiles that fail as requires_revision: true.
        Return:
          {"approved": [...],
           "flagged": [{"name": "...", "reason": "..."}],
           "overall_quality": "acceptable|needs_revision"}
    depends_on: [profile_each]

  # --- orchestrator stage ---
  # Sees all prior stage outputs. Produces the final deliverable.
  # Owns the brief — not a summary of summaries, but the actual output.
  - id: write_brief
    role:
      name: BriefAuthor
      type: orchestrator           # frontier model by default
      description: |
        Write a market research brief for {{ company_name }} in the
        {{ market_segment }} space.

        Approved profiles: {{ quality_gate.approved }}
        Flagged profiles (exclude): {{ quality_gate.flagged | map(attribute='name') | list }}
        Overall quality assessment: {{ quality_gate.overall_quality }}

        Structure the brief as:
        1. Executive summary (competitive landscape in 3 sentences)
        2. Competitor profiles (one section per approved competitor)
        3. Strategic gaps (where {{ company_name }} can differentiate)
        4. Recommended positioning

        Return {"brief": "..."}.
    depends_on: [quality_gate]
```

The execution shape:

```
gather_competitors (researcher, large)
        │
        ▼
profile_each × 8 concurrent (worker, small)
        │
        ▼
quality_gate (judge, frontier)
        │
        ▼
write_brief (orchestrator, frontier)
```

Eight small-model calls for the profiling work. One large-model call to gather context. Two frontier-model calls where quality and synthesis matter. The cost profile follows the taxonomy automatically — no explicit `model_tier` overrides required.

---

## Summary

| Type | Posture | Default tier | Typical position |
|------|---------|-------------|-----------------|
| `researcher` | Gather, synthesize, surface | `large` | Early — feeds context in |
| `worker` | Execute a well-defined task | `small` | Middle — often in fan-out |
| `judge` | Evaluate, score, decide | `frontier` | After workers — quality gate |
| `orchestrator` | Integrate, conclude, produce | `frontier` | Late — owns final output |

The taxonomy is a contract between the workflow author and the harness. Declare the type honestly and the harness handles model selection, cost allocation, and a shared vocabulary for every reader of the spec who comes after you.

---

*Armature — the harness is more important than the model.*

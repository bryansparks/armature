# Subagent Composition in Armature

Hierarchical workflow decomposition — spawn complete child workflows as single stages, reuse them across parents, and fan out N of them concurrently.

---

A stage in Armature normally calls an LLM, a tool, or a gate. A subagent stage does something different: it loads a separate YAML spec and runs it as a full, independent workflow. The parent sees one stage; the child is an entire DAG executing inside it. The result of the child workflow's final context dict becomes the result of the parent stage.

This is compositional architecture for agents. The same principle that makes functions useful in code — naming a unit of work so it can be called from multiple places, tested in isolation, and improved independently — applies to YAML workflow specs.

---

## Single child workflow

The simplest form: one stage delegates entirely to a child spec.

```yaml
stages:
  - id: gather
    role:
      type: researcher
      description: Gather all source documents. Return {"documents": [...]}.

  - id: deep_analysis
    subagent_spec: workflows/deep-analysis.yaml   # spawns a full child workflow
    depends_on: [gather]
```

The parent stage `deep_analysis` blocks until the child workflow completes. Everything inside `workflows/deep-analysis.yaml` — however many stages it contains, whatever DAG shape it has — runs to completion before the parent advances to the next wave.

The child's full result dict (all its stage outputs keyed by stage ID) is returned as the value of the `deep_analysis` stage in the parent context. Downstream parent stages can reference child stage outputs directly:

```yaml
  - id: summarize
    role:
      description: |
        Summarize the deep analysis results.
        Key findings: {{ deep_analysis.findings }}
        Risk score: {{ deep_analysis.risk_judge.score }}
    depends_on: [deep_analysis]
```

`deep_analysis.findings` is the `findings` key from the child's final context. `deep_analysis.risk_judge.score` navigates into a nested child stage output. The parent doesn't know how the child produced these — it only sees the interface.

---

## Context passing

By default, the entire parent context is passed to the child. The child can reference any stage output from the parent, including all Jinja2 template variables. This is the right default for tightly coupled workflows where the child needs full situational awareness.

For clean interfaces, add `isolated: true` and declare `signature.input`. Only the named keys flow to the child:

```yaml
  - id: deep_analysis
    subagent_spec: workflows/deep-analysis.yaml
    isolated: true
    signature:
      input:
        documents: "list"
        analysis_depth: "str"
```

The child workflow receives exactly `documents` and `analysis_depth` — nothing else from the parent context. This makes the child's input contract explicit and enforces that the child doesn't accidentally depend on parent implementation details.

---

## Fan-out of N child workflows

The most powerful pattern: run N complete child workflows concurrently, one per item in a list.

```yaml
stages:
  - id: per_region_analysis
    subagent_spec: workflows/regional-analysis.yaml
    fan_out: 5            # 5 concurrent child workflow runs
    partition_key: region
    partition_source: "{{ regions }}"
    fan_in: list          # collect all 5 results as a list
    isolated: true
    signature:
      input:
        region: "str"
```

Each of the five child workflow runs sees only `region` in its context. They run independently, in parallel. When all five complete, their result dicts are collected under the `per_region_analysis` key as a list — one entry per child run.

The `fan_in` strategies from fan-out/fan-in apply here directly: `list`, `merge`, `first`, and `consensus` all work the same way whether the parallel workers are LLM calls or full child workflows.

### How partitioning works

`SubagentNode._build_contexts` takes the list resolved by `partition_source`, splits it into N chunks (one per `fan_out` slot), and assigns each chunk to a child context under `partition_key`. With `isolated: true` and `signature.input`, only the declared keys travel into each child. Children have no shared state and cannot interfere with each other.

---

## The hierarchy

A parent workflow with subagent fan-out has this structure at runtime:

```
parent workflow
│
├── stage: gather
│     (LLM call)
│
└── stage: per_region_analysis  [fan_out: 5, concurrent]
      │
      ├── child run 0 ─── regional-analysis.yaml
      │     ├── stage: fetch_data
      │     ├── stage: score_metrics
      │     └── stage: write_report       ─── result dict returned
      │
      ├── child run 1 ─── regional-analysis.yaml
      │     ├── stage: fetch_data
      │     ├── stage: score_metrics
      │     └── stage: write_report       ─── result dict returned
      │
      ├── child run 2 ─── regional-analysis.yaml  (identical structure)
      ├── child run 3 ─── regional-analysis.yaml
      └── child run 4 ─── regional-analysis.yaml
            └── stage: write_report       ─── result dict returned
                                                          │
                                          fan_in: list ───┘
                                          [result_0, result_1, ..., result_4]
                                          returned as per_region_analysis value
```

Each child run gets its own session directory — `child_0/`, `child_1/`, and so on — under the parent's session directory. Traces, artifacts, and IHR scores are captured per child, per stage. You get full observability into every level of the hierarchy.

---

## A complete example: multi-region compliance pipeline

```yaml
name: compliance-global
version: "1.0"
mission: "Run compliance review across all operating regions."

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: load_regions
    role:
      type: researcher
      model_tier: small
      description: |
        Return the list of regions to audit.
        Return {"regions": ["NA", "EU", "APAC", "LATAM", "MEA"]}.

  - id: regional_reviews
    subagent_spec: workflows/regional-compliance.yaml
    fan_out: 5
    partition_source: "{{ load_regions.regions }}"
    partition_key: region
    fan_in: list
    isolated: true
    signature:
      input:
        region: "str"
    depends_on: [load_regions]

  - id: global_summary
    role:
      type: orchestrator
      model_tier: frontier
      description: |
        Synthesize compliance results across all regions.
        Region results: {{ regional_reviews }}
        High-risk regions: {{ regional_reviews | selectattr('risk_level', 'eq', 'high') | list }}
        Produce a global compliance report with escalation recommendations.
    depends_on: [regional_reviews]
```

The parent spec has three stages. The child spec (`workflows/regional-compliance.yaml`) may have ten. The parent author doesn't know — and doesn't need to.

---

## Why subagents instead of more stages

### Reuse

A child workflow is a named, versioned file that multiple parent workflows can reference. If three different pipelines all need deep document analysis, they all declare `subagent_spec: workflows/deep-analysis.yaml`. The analysis logic lives in one place. Updating it updates every parent that references it.

With flat stages, you copy and paste. With subagents, you reference.

### Encapsulation

The child workflow's internal stages are invisible to the parent. The parent sees one stage result — a dict keyed by the child's stage IDs. What the child does to produce that result is an implementation detail. You can restructure the child's internal DAG, add judge stages, change model tiers — and the parent spec is unchanged, because the child's output interface is stable.

### Independent testing

Child workflows can be run standalone:

```bash
armature run workflows/deep-analysis.yaml --input '{"documents": [...]}'
```

This means you can develop and debug a child workflow without constructing the full parent context. You can write tests that invoke the child directly. The child is a first-class artifact, not an embedded subroutine.

### Independent self-improvement

The IHR (Iterative Harness Refinement) loop in Armature operates on workflow specs. A child workflow accumulates its own traces — per stage, per run — and can be improved independently:

```bash
armature improve workflows/deep-analysis.yaml
```

The optimizer sees the child's trace history without the noise of the parent workflow. Improvements to the child propagate automatically to every parent that references it. You can run `armature improve` on the parent and child independently, targeting the source of quality issues at the right level of granularity.

---

## Session directories

Each child run gets a dedicated directory under the parent's session:

```
sessions/
└── 2026-06-05T14:22:00/
    ├── stage_gather.json
    ├── stage_per_region_analysis/
    │   ├── child_0/
    │   │   ├── stage_fetch_data.json
    │   │   └── stage_write_report.json
    │   ├── child_1/
    │   ├── child_2/
    │   ├── child_3/
    │   └── child_4/
    └── stage_global_summary.json
```

Each child's traces are self-contained. IHR scoring sees the full tree. `armature improve` on a child spec uses its own `child_N/` trace data, not the parent's.

---

## Error isolation

Child workflow failures behave the same as fan-out worker failures. A child that raises an exception returns `{"_fan_out_error": "..."}` rather than aborting the entire fan-out. Downstream stages can filter these:

```yaml
  - id: global_summary
    role:
      description: |
        Successful regional reviews: {{ regional_reviews | rejectattr('_fan_out_error') | list }}
        Failed regions: {{ regional_reviews | selectattr('_fan_out_error') | list | length }}
```

A single failed region never kills the batch.

---

## Key fields

| Field | Type | Description |
|-------|------|-------------|
| `subagent_spec` | `str` | Path to the child workflow YAML spec |
| `isolated` | `bool` | If true, only `signature.input` keys pass to the child (default: false) |
| `signature.input` | `dict` | Declared input keys when `isolated: true` |
| `fan_out` | `int` | Number of concurrent child workflow runs (omit for single child) |
| `partition_source` | `str` | Jinja2 expression resolving to a list — partitioned across child runs |
| `partition_key` | `str` | Context variable name for each partition in the child (default: `item`) |
| `fan_in` | `str` | How to collect N child results: `list`, `merge`, `first`, `consensus` |

---

*Subagent composition is the unit of reuse in Armature. Stages compose within a workflow; subagents compose workflows into workflows. The harness handles context passing, session isolation, parallel execution, and trace capture at every level of the hierarchy.*

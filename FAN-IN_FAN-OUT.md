# Fan-out and Fan-in in Armature

Parallel map-reduce for agentic workflows — no orchestration code required.

---

## The core idea

Fan-out/fan-in answers the question: **"I have a list of N things — how do I process all of them without writing N stages?"**

A single stage declaration in YAML becomes N parallel executions — one per item in a list. The `fan_in` strategy then decides how to collapse those N results back into one value for downstream stages.

This is the classical **map-reduce** pattern applied to LLM calls:

```
              ┌─── worker (item 1) ───┐
              │                       │
list of N ───►├─── worker (item 2) ───┤───► fan-in ───► downstream
   items      │                       │
              └─── worker (item N) ───┘
                  (all concurrent,
                   bounded by fan_out)
```

The harness handles all the concurrency. The workflow author writes one stage.

---

## Fan-out — splitting work across a list

You declare `fan_out:` on a stage and point it at a list via `partition_source` (a Jinja2 expression). Armature evaluates that expression against the current context, gets a Python list, and runs the stage once per item — all concurrently, bounded by the `fan_out` integer (default 20 in-flight at once via a semaphore).

### Basic example

```yaml
stages:
  - id: fetch_documents
    role:
      type: researcher
      description: |
        List all documents to review. Return {"documents": ["doc1.pdf", ...]}.

  - id: review_each
    fan_out: 5                                          # max 5 concurrent LLM calls
    partition_source: "{{ fetch_documents.documents }}" # Jinja2 expression → list
    partition_key: doc_path                             # name of the per-item variable
    role:
      type: worker
      description: |
        Review this document for compliance issues.
        Document: {{ doc_path }}
        Return {"issues": [...], "risk_level": "low|medium|high"}.
    depends_on: [fetch_documents]
```

Each concurrent worker gets its own **isolated context copy** with `doc_path` bound to one list item. Workers have no shared state and cannot interfere with each other. Per-item failures are caught and reported as `{"_fan_out_error": "..."}` rather than aborting the entire fan-out — one bad document never kills the batch.

### Key fields

| Field | Type | Description |
|-------|------|-------------|
| `fan_out` | `int` | Max concurrent executions (default: 20) |
| `partition_source` | `str` | Jinja2 expression that resolves to a list |
| `partition_key` | `str` | Context variable name for each list item (default: `item`) |
| `fan_in` | `str` | How to collect results (see below; default: `list`) |
| `inject_file_as` | `str` | If set, each item is treated as a file path and its content is injected under this key |

### Reading file content automatically

If each list item is a file path, `inject_file_as` reads the file and injects its text content into the per-item context automatically — no tool call required:

```yaml
  - id: analyse_each_file
    fan_out: 8
    partition_source: "{{ file_list }}"
    partition_key: file_path
    inject_file_as: file_content          # reads each path, injects text as file_content
    role:
      type: worker
      description: |
        Analyse this file for security vulnerabilities.
        File: {{ file_path }}
        Content: {{ file_content }}
```

---

## Fan-in — collapsing N results into one

`fan_in` controls what the stage returns to downstream stages. There are four strategies:

| Strategy | What it does | Use when |
|----------|-------------|----------|
| `list` (default) | Returns all N results as a Python list | You want every output — a downstream stage aggregates |
| `merge` | `dict.update()` merges all results | Each item produces a partial dict with disjoint keys |
| `first` | Returns only the first result | You only care about the fastest / first success |
| `consensus` | Judge-style voting across all results | You run the same prompt N times and want the most-agreed-upon answer |

### `fan_in: list` — collect everything

```yaml
  - id: analyse_each
    fan_out: 8
    fan_in: list              # default
    partition_source: "{{ documents }}"
    ...

  - id: synthesise
    role:
      type: orchestrator
      description: |
        Synthesise these {{ analyse_each | length }} document analyses.
        Analyses: {{ analyse_each }}   # a list of N result dicts
    depends_on: [analyse_each]
```

### `fan_in: merge` — combine partial dicts

Useful when each item owns a disjoint key in the output:

```yaml
  - id: score_each_category
    fan_out: 4
    fan_in: merge
    partition_source: "{{ categories }}"
    partition_key: category
    role:
      type: judge
      description: |
        Score the {{ category }} dimension. Return {"{{ category }}": <score>}.
```

If four items produce `{"pricing": 8}`, `{"support": 6}`, `{"reliability": 9}`, `{"ux": 7}`, the merged result is `{"pricing": 8, "support": 6, "reliability": 9, "ux": 7}` — a single dict downstream stages can reference directly.

### `fan_in: first` — take the fastest result

```yaml
  - id: search_sources
    fan_out: 3
    fan_in: first
    partition_source: "{{ search_endpoints }}"
    partition_key: endpoint
    tool_call:
      name: http_get
      args:
        url: "{{ endpoint }}"
```

The first successful result wins. Useful for racing multiple sources.

### `fan_in: consensus` — vote across N runs

Run the same prompt against the same input N times and let the results vote:

```yaml
  - id: classify_sentiment
    fan_out: 5
    fan_in: consensus
    partition_source: "[1, 2, 3, 4, 5]"   # 5 identical runs
    role:
      type: judge
      description: |
        Classify the sentiment of this text as positive, negative, or neutral.
        Text: {{ customer_review }}
```

The harness picks the most-agreed-upon answer. This is a lightweight quality assurance technique — if all five agree, you have high confidence; if they split, the model is uncertain about this input.

---

## A complete example: document compliance pipeline

```yaml
name: compliance-review
version: "1.0"
mission: "Review all incoming documents for regulatory compliance issues."

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: list_documents
    role:
      name: Collector
      type: researcher
      model_tier: small
      description: |
        List all PDF paths in /incoming that arrived today.
        Return {"documents": ["/incoming/doc1.pdf", ...]}.

  - id: review_each
    fan_out: 10                                           # 10 concurrent reviews
    fan_in: list                                          # collect all results
    partition_source: "{{ list_documents.documents }}"
    partition_key: doc_path
    inject_file_as: doc_content                           # auto-read each PDF
    role:
      name: Reviewer
      type: worker
      model_tier: small
      description: |
        Review the following document for compliance issues.
        Document: {{ doc_path }}
        Content: {{ doc_content }}
        Return:
          {"issues": [{"clause": "...", "severity": "low|medium|high"}],
           "risk_level": "low|medium|high",
           "requires_escalation": true|false}
    depends_on: [list_documents]

  - id: escalation_check
    skip_if: "{{ review_each | selectattr('requires_escalation') | list | length == 0 }}"
    role:
      name: EscalationJudge
      type: judge
      model_tier: frontier
      description: |
        {{ review_each | selectattr('requires_escalation') | list | length }} documents
        require escalation. Review these findings and produce an escalation report.
        Flagged reviews: {{ review_each | selectattr('requires_escalation') | list }}
    depends_on: [review_each]

  - id: final_report
    role:
      name: ReportWriter
      type: orchestrator
      model_tier: frontier
      description: |
        Produce a final compliance summary from {{ review_each | length }} document reviews.
        All reviews: {{ review_each }}
        High-risk count: {{ review_each | selectattr('risk_level', 'eq', 'high') | list | length }}
        Return a structured compliance report.
    depends_on: [review_each]
```

100 documents → 10-at-a-time parallel reviews → optional escalation check (skipped if nothing flagged) → final report. The author wrote four stages in YAML; the harness handled all the parallelism, per-item error isolation, trace capture, and IHR scoring.

---

## Error isolation

Per-item failures never abort the batch. A single document that fails (unreadable file, LLM timeout, parse error) returns:

```json
{"_fan_out_error": "file not found: /incoming/corrupt.pdf"}
```

The rest of the batch completes normally. Downstream stages can filter `_fan_out_error` items out:

```yaml
  - id: aggregate
    role:
      description: |
        Successful reviews: {{ review_each | rejectattr('_fan_out_error') | list }}
        Failed items: {{ review_each | selectattr('_fan_out_error') | list | length }}
```

---

## Concurrency and cost

`fan_out` is both a parallelism limit and a cost-per-minute knob. Higher values finish faster but may hit API rate limits or run up costs on large batches. A value of 5–10 is a safe default for most providers; raise it if you have high-tier API access or are using local models.

| fan_out | 100 items at 2s/item | Effective wall time |
|---------|---------------------|---------------------|
| 1 | sequential | ~200s |
| 5 | 5 concurrent | ~40s |
| 10 | 10 concurrent | ~20s |
| 20 (default) | 20 concurrent | ~10s |

---

## Composing with other Armature features

Fan-out/fan-in composes naturally with every other harness capability:

- **Safety rules** apply to every individual fan-out execution
- **Trace capture** records each item's LLM call separately — you get full IHR scoring per item
- **`on_fail.loop`** retries individual items, not the whole batch
- **`mission:`** is injected into every worker's system prompt
- **`continuation:`** can carry a fan-out result list forward to the next workflow activation

---

*Fan-out/fan-in is the parallel map-reduce of the agentic world. One stage declaration, N concurrent executions, one result — with the full Armature harness applied to every individual call.*

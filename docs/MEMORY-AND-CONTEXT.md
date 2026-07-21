# Memory and Context in Armature

How agentic workflows remember, orient, and build knowledge across calls, stages, and runs.

---

Memory is the central unsolved problem of production AI agents. A workflow that forgets everything on each activation is a stateless function, not an agent. A workflow that remembers too much is brittle, expensive, and slow. Armature addresses this with five distinct memory mechanisms, each operating at a different time horizon and serving a different purpose. Together they give a workflow a working memory for the current run, a rolling episodic memory across recent runs, a persistent knowledge base of extracted facts, active on-demand navigation over stored memory, and shared mission context that keeps every stage pointed in the same direction.

---

## The five memory layers

| Layer | What it stores | Scope | Injected as |
|-------|---------------|-------|-------------|
| **Mission context** | The workflow's goal + prior-stage breadcrumb | In-run, automatic | `[Workflow Mission]` in system prompt |
| **Continuation** | Selected outputs from the previous activation | Cross-run, declared | `prior_run` (configurable) |
| **MemoryStore** | Rolling captures of named stage outputs | Cross-run, rolling window | `_memory` (configurable) |
| **KnowledgeStore** | LLM-extracted entity/fact/confidence records | Persistent, cumulative | `_knowledge` (configurable) |
| **Memory navigation** | Read-only tools that search L0/L1 on demand | Cross-run, opt-in | `memory.*` tools + `_memory_index` |

Each layer is independent. You can use one, some, or all of them in the same workflow.

---

## Layer 1: Mission context — in-run orientation

The simplest form of context is also the most universally applied. Every workflow has a `mission:` field. That string, plus an automatically maintained breadcrumb of prior completed stages, is injected into the system prompt of every LLM call in the workflow.

```yaml
name: market-monitor
mission: >
  Monitor competitor pricing daily and flag any changes
  greater than 5% that require a pricing response.

stages:
  - id: fetch_prices
    role:
      type: researcher
      description: Retrieve current prices from the three competitor APIs.

  - id: analyse_changes
    role:
      type: analyst
      description: |
        Compare today's prices to yesterday's baseline.
        Flag items with delta > 5%.
    depends_on: [fetch_prices]
```

By the time `analyse_changes` executes, its system prompt includes:

```
[Workflow Mission]
Monitor competitor pricing daily and flag any changes
greater than 5% that require a pricing response.

[Prior stages]
fetch_prices: retrieved 47 price points across 3 competitors
```

The breadcrumb is built automatically from completed stage outputs — the analyst knows what the researcher found without receiving a raw data dump. This is the **zero-configuration** memory layer: no spec fields, no database, no configuration beyond the `mission:` string itself.

### Why it works

Multi-stage workflows without shared orientation produce incoherent output. Each stage reinterprets the task independently. The mission string anchors all agents to the same goal. The prior-stages breadcrumb provides awareness of what has already been established — preventing redundant work and enabling genuine reasoning about the current state of the pipeline.

---

## Layer 2: Continuation — cross-run rolling memory

`continuation:` carries specific outputs from the most recent prior activation forward into the current run. This is the mechanism that transforms a one-shot workflow into a recurring agent that accumulates knowledge over time.

```yaml
name: market-monitor
mission: Monitor competitor pricing daily.

continuation:
  carry_forward:
    - key: analyse_changes.baseline_prices   # stage_id.output_key
    - key: report.flagged_items
  inject_as: prior_run                        # default

stages:
  - id: fetch_prices
    role:
      type: researcher
      description: Retrieve current prices.

  - id: analyse_changes
    role:
      type: analyst
      description: |
        Compare today's prices to the baseline from the prior run.
        Prior baseline: {{ prior_run.baseline_prices | default({}) }}
        Previously flagged: {{ prior_run.flagged_items | default([]) }}
        
        Identify new changes and items that have remained flagged.
    depends_on: [fetch_prices]
```

On the **first activation**, `prior_run` is absent from the context — the workflow runs cleanly from scratch. On every subsequent activation, the harness queries the TraceStore for the most recent completed run of this workflow, extracts the declared keys, and injects them as `prior_run` before any stage executes.

### The dotted-key notation

`key: analyse_changes.baseline_prices` means: from the stage with id `analyse_changes`, retrieve the output key `baseline_prices`. The stage must have returned a JSON object containing that key. If the key doesn't exist (stage was skipped, or it returned something different), that entry is simply absent from `prior_run` — no error.

### What continuation is not

Continuation is not a general-purpose conversation history. It carries **structured outputs** — the JSON fields your stages explicitly return. It is not a transcript. This is intentional: structured data is compact, searchable, and semantically clear. A LangChain-style conversation buffer grows unboundedly and mixes intent with execution noise. `continuation:` carries only what you explicitly declare matters.

---

## Layer 3: MemoryStore — rolling episodic memory

`continuation:` carries the previous run's outputs. `MemoryStore` maintains a rolling window of captured stage outputs across many runs — a recency-ranked episodic memory that grows as the workflow runs repeatedly and ages out stale entries automatically.

```yaml
name: content-monitor
mission: Track narrative evolution across daily content ingestion.

memory:
  enabled: true
  db: ~/.armature/memory.db        # default location
  workflow_name: content-monitor   # optional: namespace for stored records
  fresh: false                      # true = ignore prior memories this run
  inject_as: _memory                # default

  capture:
    - stage: summarise
      key: narrative_themes         # capture this key from the stage output
      max_entries: 10               # keep 10 most recent values (default: 5)
    - stage: classify
      key: sentiment_trend
      max_entries: 5

  extract_knowledge: true           # also run KnowledgeExtractor (see Layer 4)
  inject_knowledge_as: _knowledge   # default
```

### Namespacing and cross-spec sharing

Records inside a memory DB are keyed by `workflow_name`. By default this is the spec's top-level `name`, but `memory.workflow_name` overrides it. Use the override when a variant spec (for example, a navigation-enabled version of the same workflow) needs to read and write the same accumulated memory as the original while keeping its own trace/workflow identity.

### What gets injected

At the start of each run, the harness loads existing memories and injects them into the context:

```python
context["_memory"] = {
    "summarise": {
        "narrative_themes": ["newest value", "second newest", ..., "oldest"],
    },
    "classify": {
        "sentiment_trend": ["positive", "mixed", "negative", "positive", ...],
    }
}
```

The list is ordered newest-to-oldest. A downstream stage can access the full history:

```yaml
  - id: trend_report
    role:
      type: orchestrator
      description: |
        Historical narrative themes (newest first):
        {{ _memory.summarise.narrative_themes }}
        
        Sentiment trend (last {{ _memory.classify.sentiment_trend | length }} runs):
        {{ _memory.classify.sentiment_trend }}
        
        Identify shifts and predict what to watch tomorrow.
```

### Quality-ranked eviction

When `max_entries` is exceeded, the oldest captures are evicted — but not purely by timestamp. Captures have an associated `quality` score drawn from the stage's quorum score (if a judge was involved) or 1.0 by default. When the window is full, low-quality captures are evicted before high-quality ones at the same age. A day where the workflow produced a confident, well-scored output is remembered longer than a day where confidence was low.

### Staleness detection

Memories older than 30 days are flagged as stale. The harness injects a `_stale_memory_keys` list into the context:

```python
context["_stale_memory_keys"] = ["summarise.narrative_themes", "classify.sentiment_trend"]
```

A stage can check this and treat old memories with appropriate skepticism:

```yaml
  - id: analyse
    role:
      description: |
        {% if 'summarise.narrative_themes' in _stale_memory_keys %}
        Note: historical themes data is stale (>30 days). Treat as background context only.
        {% endif %}
        Themes: {{ _memory.summarise.narrative_themes }}
```

### `fresh: true` — running without prior memory

Set `fresh: true` to ignore all stored memories for a single run. Useful for baseline comparison runs, testing, or intentional clean-slate analysis. Memory is still captured at the end of the run; it is only the injection at the start that is suppressed.

---

## Layer 4: KnowledgeStore — persistent extracted facts

MemoryStore preserves raw stage outputs. KnowledgeStore distills those outputs into structured facts — entity/fact/confidence triples stored in SQLite with full-text search. This is Armature's embedded RAG layer, purpose-built for agentic workflows.

### Enabling knowledge extraction

Set `extract_knowledge: true` in the memory config. After each run, the harness passes the captured raw memories to a `KnowledgeExtractor` — an LLM call that reads the captures and returns structured facts:

```json
[
  {"entity": "CompetitorA", "fact": "Reduced premium tier price by 12% on 2025-06-01", "confidence": 0.95},
  {"entity": "MarketTrend", "fact": "Three consecutive weeks of downward pressure on enterprise SaaS pricing", "confidence": 0.80},
  {"entity": "RegionApac", "fact": "APAC pricing unchanged; US and EU showing compression", "confidence": 0.75}
]
```

These records are written to `KnowledgeStore` (SQLite + FTS5 full-text index). On subsequent runs, the top-10 most relevant records are retrieved via keyword search and injected as `_knowledge`:

```python
context["_knowledge"] = [
    {"entity": "CompetitorA", "fact": "Reduced premium tier price by 12% on 2025-06-01", "confidence": 0.95},
    {"entity": "MarketTrend", "fact": "Three consecutive weeks of downward pressure...", "confidence": 0.80},
    ...
]
```

A stage can reason over this structured knowledge base directly:

```yaml
  - id: strategy
    role:
      type: judge
      description: |
        Known facts about our competitive landscape:
        {% for item in _knowledge %}
        - [{{ item.confidence | round(2) }}] {{ item.entity }}: {{ item.fact }}
        {% endfor %}
        
        Given today's new pricing data: {{ fetch_prices }}
        Recommend a pricing response.
```

### MemoryStore vs. KnowledgeStore

| Dimension | MemoryStore | KnowledgeStore |
|-----------|-------------|----------------|
| **What is stored** | Raw stage output values | LLM-extracted entity/fact triples |
| **Structure** | `{stage_id: {key: [newest, ..., oldest]}}` | `[{entity, fact, confidence}]` |
| **Growth** | Rolling window (evicts oldest) | Cumulative (appends each run) |
| **Retrieval** | Full injection of all captures | Top-10 by keyword relevance (FTS5) |
| **Staleness** | Automatic detection at 30 days | No built-in expiry |
| **Cost** | Zero LLM calls | One LLM call per run (extractor) |
| **Best for** | Recent time-series data | Accumulated domain knowledge |

Use both together: MemoryStore gives agents access to recent raw signals; KnowledgeStore gives them access to the distilled understanding built up over many runs.

---

## Layer 5: Memory navigation — active, on-demand retrieval

`MemoryStore` and `KnowledgeStore` inject memory passively: every run receives a pre-selected slice of prior captures or facts. For some workflows that is exactly right. For others, the fixed dump is either too noisy or too small, and the agent is better served by **querying memory itself**.

Memory navigation makes stored memory searchable at run time via read-only tools:

- `memory.search_records` — hybrid keyword + semantic search over extracted L1 facts (`KnowledgeStore`).
- `memory.get_records` — fetch specific L1 records by id after a search.
- `memory.search_conversation` — keyword search over raw L0 captures (`MemoryStore`).
- `memory.get_run_trace` — pull a prior run's stage outputs from the trace store.

When `memory.navigation_tools: true` is set, the engine registers these tools for any stage that declares them in `role.tools`, and injects a lightweight `_memory_index` table-of-contents into the stage context so the agent knows what memory is available. Stages that declare a `memory.*` tool no longer receive the full passive `_knowledge` dump for that stage; they pull only what they need. Stages that do not opt in continue to receive `_knowledge` exactly as before.

```yaml
memory:
  enabled: true
  extract_knowledge: true
  navigation_tools: true

stages:
  - id: researcher
    role:
      type: researcher
      tools:
        - memory.search_records
        - memory.search_conversation
      description: |
        {% if _memory_index is defined and _memory_index %}
        Prior memory index: {{ _memory_index }}
        Use memory.search_records / memory.search_conversation to find what is
        already known, then extend it with new information.
        {% endif %}
        Topic: {{ topic }}
```

### Why navigation is opt-in

Navigation trades a small amount of extra latency (one or more tool calls) for a large reduction in passive context. It works best when the agent can make targeted queries — for example, "what do we already know about X?" — rather than needing the full history every time. It is strictly additive: workflows that do not enable it behave byte-for-byte as they did before.

### What we explored and set aside

The original design also proposed two higher layers built on top of L1:

- **L2 topic tracks** — compressed markdown summaries written by a curator stage.
- **L3 team profile** — a single long-running markdown document capturing stable workflow attributes.

We implemented these and ran a cold-vs-warm-vs-navigation evaluation, but the results showed that the curator/write overhead and the extra latency from pulling tracks and profiles canceled the coverage gains we hoped for. The agentic harness got little additional benefit from L2/L3 compared to navigation over the raw L0/L1 layers alone. We therefore removed topic tracks, team profiles, and the curator write tools, keeping only the read-only navigation tools over L0/L1. The documentation for the original four-layer pyramid lives on as a design note; the shipped enhancement is the two-layer navigation model described here.

---

## Context isolation — scoping memory to what a stage needs

All five memory layers inject data into the shared context. In complex workflows, this can create noise: a worker stage deep in a fan-out shouldn't see every key accumulated by every prior stage. `isolated: true` + `signature.input` lets a stage declare exactly which context keys it receives:

```yaml
  - id: review_each
    fan_out: 10
    isolated: true
    signature:
      input:
        - doc_path
        - _knowledge          # structured facts from KnowledgeStore
        - prior_run           # continuation values
      output:
        - issues
        - risk_level
    role:
      type: worker
      description: |
        Review this document against known compliance history.
        Document: {{ doc_path }}
        Prior compliance facts: {{ _knowledge }}
```

This is especially important in fan-out workers. Each worker gets a clean context containing only what it needs — not the accumulated state of the full pipeline. See `CONTEXT-ISOLATION.md` for full details.

---

## Comparing Armature's memory to alternatives

### vs. LangChain ConversationBufferMemory

LangChain's buffer stores the raw text of every prior message. It grows unboundedly, injects the full transcript into every call, and has no concept of quality or relevance. It is designed for conversational chat, not structured multi-stage pipelines.

Armature's MemoryStore stores **structured outputs** (named JSON keys), maintains a bounded rolling window with quality-ranked eviction, and injects only what was explicitly declared worth capturing. The signal-to-noise ratio is orders of magnitude higher.

### vs. external vector databases

A vector database (Pinecone, Qdrant, Chroma) provides semantic similarity search over embeddings. This is powerful for unstructured document retrieval. Armature's KnowledgeStore covers the structured end of the same problem: LLM-extracted entity/fact triples over FTS5 keyword search, embedded in the same SQLite database as traces, with no external infrastructure.

For workflows that need semantic search over large unstructured document corpora, an external vector DB accessed via a tool call is the right approach. For workflows that need to accumulate and query structured knowledge about the domain they are monitoring — competitors, customers, markets, codebases — KnowledgeStore is purpose-built and zero-infrastructure.

### vs. RAG pipelines

RAG (retrieval-augmented generation) retrieves relevant chunks from a corpus at query time and injects them into a prompt. Armature's knowledge layer does the same thing, but the "corpus" is the workflow's own accumulated understanding — not a pre-loaded document store. It is RAG where the workflow itself is both the indexer and the querier, building its knowledge base by running.

---

## A complete memory-enabled workflow

```yaml
name: investment-monitor
version: "1.0"
mission: >
  Track a portfolio of ten technology companies. Maintain running knowledge
  of each company's strategic position. Flag material changes weekly.

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

continuation:
  carry_forward:
    - key: synthesise.portfolio_positions
    - key: synthesise.active_flags
  inject_as: prior_run

memory:
  enabled: true
  capture:
    - stage: analyse_each
      key: company_summary
      max_entries: 8            # 8 weeks of history per company
  extract_knowledge: true       # distill into entity/fact records

stages:
  - id: fetch_filings
    role:
      name: DataCollector
      type: researcher
      model_tier: small
      description: |
        Retrieve this week's SEC filings, earnings calls, and news for the ten portfolio companies.
        Return {"filings": [{"company": "...", "content": "..."}]}.

  - id: analyse_each
    fan_out: 10
    fan_in: list
    partition_source: "{{ fetch_filings.filings }}"
    partition_key: filing
    role:
      name: CompanyAnalyst
      type: worker
      model_tier: small
      description: |
        Analyse the weekly filing for {{ filing.company }}.
        
        Known facts about this company:
        {% for f in _knowledge if f.entity == filing.company %}
        - [{{ f.confidence }}] {{ f.fact }}
        {% endfor %}
        
        Prior position: {{ prior_run.portfolio_positions[filing.company] | default("No prior position") }}
        
        Identify: strategic changes, risk flags, or material events.
        Return {"company": "{{ filing.company }}", "company_summary": {...}, "flags": [...]}.
    depends_on: [fetch_filings]

  - id: synthesise
    role:
      name: PortfolioSynthesiser
      type: orchestrator
      model_tier: frontier
      description: |
        Synthesise this week's analysis across all ten companies.
        Prior active flags: {{ prior_run.active_flags | default([]) }}
        This week's analyses: {{ analyse_each }}
        
        Return {"portfolio_positions": {...}, "active_flags": [...], "weekly_summary": "..."}.
    depends_on: [analyse_each]
```

Run this workflow weekly. After three months:

- `continuation` gives each run awareness of last week's positions and flags
- `MemoryStore` gives the analyst 8 weeks of each company's history
- `KnowledgeStore` gives every stage access to accumulated facts about companies, markets, and risks
- `prior_run.active_flags` lets the synthesiser track which flags have been open for multiple weeks

The workflow builds institutional knowledge automatically, run by run.

---

## Summary: which layer to use

| Question | Layer |
|----------|-------|
| How do I keep all my agents aligned on the same goal? | Mission context (automatic) |
| How do I pass last run's outputs to the next run? | Continuation |
| How do I give my workflow a rolling history of recent runs? | MemoryStore |
| How do I accumulate structured knowledge across many runs? | KnowledgeStore |
| How do I let an agent query memory on demand instead of receiving a fixed dump? | Memory navigation |
| How do I prevent context pollution in fan-out workers? | Context isolation |

These are not competing choices. A production workflow that runs for weeks benefits from all five layers simultaneously — mission for orientation, continuation for immediate prior state, MemoryStore for recent history, KnowledgeStore for accumulated domain understanding, and memory navigation for targeted retrieval.

---

*Memory is what separates agents from scripts. Armature gives you five levels of it, each suited to a different time horizon and a different kind of knowledge.*

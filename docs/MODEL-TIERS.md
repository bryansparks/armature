# Model Tiers in Armature

Cost-aware model selection as a first-class primitive — declare the right capability level per stage, configure the actual model once.

---

The simplest approach to model selection is to hardcode a specific model everywhere. It is also the most expensive, the most brittle to model upgrades, and the approach most likely to produce worse results — because using a frontier model for tasks that a small model handles perfectly wastes capacity that should be reserved for genuinely hard reasoning.

Armature treats model tier as a first-class concept in the spec. Instead of naming a specific model in each stage, you declare a **capability level** — `small`, `large`, `frontier`, and so on — and configure the actual model behind each level once at the top of the spec. The harness resolves the mapping at runtime. Change one line to swap the model behind `frontier`; every stage that uses `frontier` picks it up.

This is the ensemble-of-models pattern: a single workflow runs multiple models at different capability levels, each doing the work it is best suited for, at the price it warrants.

---

## The `model_tiers` configuration block

```yaml
model_tiers:
  tiny:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  medium:
    provider: anthropic
    model: claude-sonnet-4-5
  large:
    provider: openai
    model: gpt-4o
  frontier:
    provider: anthropic
    model: claude-opus-4-7
```

This block lives at the top level of the spec, alongside `name`, `mission`, and `stages`. Every tier entry requires at minimum a `provider` and a `model`. Optional fields let you configure defaults for all stages on that tier:

| Field | Type | Description |
|-------|------|-------------|
| `provider` | `str` | LLM provider: `anthropic`, `openai`, `ollama`, or any supported backend |
| `model` | `str` | Model identifier as accepted by the provider's API |
| `api_base` | `str` | Override the base URL — used for local models, proxies, or self-hosted endpoints |
| `api_key_env` | `str` | Name of the environment variable holding the API key for this tier |
| `temperature` | `float` | Default temperature for all calls on this tier |
| `max_tokens` | `int` | Default max output tokens for all calls on this tier |
| `tool_calling` | `bool` | Force tool calling on or off; `null` auto-detects by provider |
| `adapter_support` | `"dynamic"` \| `"none"` | Load LoRA adapters from the registry and pass them to the provider per request (`dynamic`) or disable adapter loading (`none`) |
| `adapter_path_template` | `str` | Optional path template for locating LoRA artifacts served by this tier |

See `docs/ADAPTER-POWERED-TEAMS.md` for how to select an SLM tier as the host for LoRA adapter-backed skills.

---

## Five built-in tiers

The five canonical tiers encode a capability ladder from cheapest/fastest to most capable:

| Tier | Intended use | Default role type |
|------|-------------|-------------------|
| `tiny` | Token classification, routing, boolean decisions | — |
| `small` | Document processing, extraction, summarization | `worker` |
| `medium` | Light reasoning, structured analysis | — |
| `large` | Research synthesis, multi-step analysis | `researcher` |
| `frontier` | Complex judgment, orchestration, final synthesis | `judge`, `orchestrator` |

You are not required to define all five. Define only the tiers your workflow uses.

---

## Default tiers by role type

Every role type has a built-in default tier. When a stage does not specify `model_tier`, the harness resolves it from the role type:

| Role type | Default tier | Rationale |
|-----------|-------------|-----------|
| `worker` | `small` | High-volume, repetitive work — cost dominates |
| `researcher` | `large` | Synthesis from many sources requires strong reasoning |
| `judge` | `frontier` | Quality decisions should use the best available model |
| `orchestrator` | `frontier` | Orchestration requires planning and complex reasoning |

These defaults encode a deliberate opinion: the harness is built on the assumption that most of your LLM calls are workers, and workers should not run on frontier models unless you have a specific reason.

Override the per-role-type defaults at the spec level with `role_type_defaults`:

```yaml
role_type_defaults:
  worker: small
  researcher: medium   # downgrade if your research task is simpler
  judge: frontier
  orchestrator: large  # downgrade if your orchestration is light
```

---

## Per-stage override

Any stage can override its resolved tier with `model_tier` on the role:

```yaml
stages:
  - id: classify_intent
    role:
      name: Classifier
      type: worker
      model_tier: tiny          # worker default is small; this stage only needs tiny
      description: |
        Classify the intent of this message as one of:
        inquiry, complaint, compliment, other.
        Message: {{ message }}
        Return {"intent": "..."}

  - id: draft_response
    role:
      name: Drafter
      type: worker
      model_tier: medium        # this worker needs more capability than the default
      description: |
        Draft a customer service response to this {{ intent }} message.
        Message: {{ message }}
```

Stage-level `temperature` and `max_tokens` also override the tier-level defaults:

```yaml
  - id: creative_copy
    role:
      name: Copywriter
      type: worker
      model_tier: large
      temperature: 0.9          # override the tier default for this stage only
      max_tokens: 2000
```

The resolution order is: stage-level → tier-level → provider default.

---

## Custom tiers

The five built-in tiers are not a closed list. The `ModelTiers` config uses `extra="allow"`, so you can define any tier name your workflow needs:

```yaml
model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7
  synthesis:                    # custom tier for a specialized synthesis step
    provider: openai
    model: o3
    temperature: 0.2
  local:                        # air-gapped local model
    provider: ollama
    model: llama3.2
    api_base: http://localhost:11434
```

Reference a custom tier by name exactly as you defined it:

```yaml
  - id: final_synthesis
    role:
      type: orchestrator
      model_tier: synthesis
```

---

## Multi-provider workflows

Different tiers can use different providers. A single workflow can span Anthropic, OpenAI, a local Ollama instance, or any other supported backend simultaneously:

```yaml
model_tiers:
  tiny:
    provider: ollama
    model: llama3.2
    api_base: http://localhost:11434    # runs on local hardware; no API cost, no data egress
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  large:
    provider: openai
    model: gpt-4o
  frontier:
    provider: anthropic
    model: claude-opus-4-7
```

The harness routes each stage's LLM call to the correct provider based on the resolved tier. No application code is involved.

### Local/cloud hybrid

The multi-provider design enables a local/cloud hybrid pattern that matters for sensitive data workflows:

```yaml
model_tiers:
  sensitive:
    provider: ollama
    model: llama3.2
    api_base: http://localhost:11434
  cloud:
    provider: anthropic
    model: claude-haiku-4-5-20251001
```

Stages that handle PII, confidential documents, or regulated data use `model_tier: sensitive` — their payloads never leave the machine. Stages that work on anonymized or non-sensitive intermediate results use `model_tier: cloud`. The workflow author controls exactly which stages reach which provider.

---

## The ensemble-of-models pattern

A naively constructed pipeline puts every stage on the frontier model. This is the easiest spec to write and nearly always the wrong cost profile.

The ensemble pattern assigns each stage the cheapest tier that can do the job:

- **Workers** (high volume, repetitive) → `small` or `tiny`
- **Researchers** (synthesizing from sources) → `large`
- **Judges and orchestrators** (final reasoning, quality gates) → `frontier`

The frontier model does not disappear — it handles the stages that genuinely require its capability. It is just not wasted on work that a small model handles correctly at a fraction of the cost.

### Cost comparison: 100-document compliance review

Consider a pipeline with these stages:

| Stage | Role type | Calls | Frontier cost | Tiered cost |
|-------|-----------|-------|---------------|-------------|
| `list_documents` | researcher | 1 | $0.15 | $0.015 (large) |
| `review_each` (fan-out ×100) | worker | 100 | $15.00 | $0.50 (small) |
| `escalation_check` | judge | 1 | $0.15 | $0.15 (frontier) |
| `final_report` | orchestrator | 1 | $0.15 | $0.15 (frontier) |
| **Total** | | 103 | **$15.45** | **$0.815** |

The tiered workflow costs approximately **95% less** than all-frontier and produces equivalent or better results: the frontier model is focused on the judgment and synthesis tasks it does best, rather than performing rote document extraction at 30× the necessary cost.

Actual figures vary by model pricing and token counts. The pattern holds: cost savings are largest in workflows with high-volume fan-out stages — which is where the savings matter most.

---

## No model lock-in

Swapping the model behind a tier is a one-line change in the spec. All stages on that tier pick up the new model automatically:

```yaml
# Before
model_tiers:
  frontier:
    provider: anthropic
    model: claude-opus-4-7

# After — switch to a different frontier model
model_tiers:
  frontier:
    provider: openai
    model: o3
```

This is the same principle as a database connection string: the code (or spec) does not hardcode the address of the thing it depends on. The mapping is configured in one place.

The practical consequence for teams: when a provider releases a better model, or when pricing changes, updating a production workflow is a one-line diff with a clear, auditable history. There is no search-and-replace across dozens of stage definitions.

---

## Progressive upgrade path

Tiers support an iterative improvement workflow:

1. **Start conservative.** Define `small` everywhere, including for judges and orchestrators. Run the workflow on real data.
2. **Observe quality in traces.** Armature records every LLM call with HQS (Intent-Honoring Rate) scoring. If the orchestrator's output quality is low, that shows up in the trace.
3. **Upgrade individual tiers.** Move the offending stage or role type to a higher tier. Re-run. Compare quality and cost.
4. **Stabilize.** Once quality meets the bar, the tier configuration is the documented, reproducible statement of how the workflow is resourced.

This approach avoids the trap of over-provisioning at launch because quality is uncertain. It also avoids the trap of under-provisioning because cost pressure overrides quality — the trace makes quality visible.

---

## Full example

```yaml
name: market-research-digest
version: "1.0"
mission: "Analyse competitor activity and produce a weekly briefing."

model_tiers:
  tiny:
    provider: ollama
    model: llama3.2
    api_base: http://localhost:11434
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  large:
    provider: openai
    model: gpt-4o
  frontier:
    provider: anthropic
    model: claude-opus-4-7

role_type_defaults:
  worker: small
  researcher: large
  judge: frontier
  orchestrator: frontier

stages:
  - id: collect_sources
    role:
      name: SourceCollector
      type: researcher            # resolved to large by default
      description: |
        Collect the top 20 news sources and blog posts about our competitors
        from the past 7 days. Return {"sources": [{"url": "...", "title": "..."}]}.

  - id: filter_relevant
    role:
      name: RelevanceFilter
      type: worker
      model_tier: tiny            # override: classification only needs tiny
      description: |
        Is this source relevant to our competitive analysis?
        Title: {{ title }}
        URL: {{ url }}
        Return {"relevant": true|false, "reason": "..."}.

  - id: extract_each
    fan_out: 10
    fan_in: list
    partition_source: "{{ collect_sources.sources | selectattr('relevant') | list }}"
    partition_key: source
    role:
      name: Extractor
      type: worker                # resolved to small by default
      description: |
        Extract key claims, product announcements, and strategic signals from this source.
        Source: {{ source.url }}
        Return {"claims": [...], "signals": [...], "sentiment": "positive|negative|neutral"}.
    depends_on: [collect_sources, filter_relevant]

  - id: synthesise
    role:
      name: Analyst
      type: orchestrator          # resolved to frontier by default
      description: |
        Synthesise {{ extract_each | length }} source extractions into a structured
        competitive briefing. Identify the three most important strategic signals
        this week. All extractions: {{ extract_each }}
    depends_on: [extract_each]

  - id: score_confidence
    role:
      name: QualityJudge
      type: judge                 # resolved to frontier by default
      description: |
        Score the confidence of the competitive briefing below on a 0–10 scale.
        Identify any claims that are speculative or insufficiently sourced.
        Briefing: {{ synthesise }}
        Return {"confidence_score": <int>, "weak_claims": [...], "approved": true|false}.
    depends_on: [synthesise]
```

Workers read and classify at scale. Researchers gather and synthesize from broad sources. The orchestrator reasons over all extractions. The judge reviews the output before it ships. Each tier does the work it is priced and capable to do.

---

*Model tiers are the cost-control primitive of agentic workflows. Declare once, configure once, override where needed — and the harness handles routing, resolution, and provider dispatch.*

# Armature Spec Reference

Condensed cheatsheet for all YAML fields. For full docs: `USER-GUIDE.md`.
Validate any spec: `armature validate my_workflow.yml`

---

## Top-Level Fields

```yaml
name: my_workflow            # snake_case identifier
version: "1.0"               # string
description: "..."           # one sentence
mission: >                   # optional: injected into every LLM stage's system prompt
  Background context for all agents — tone, domain, constraints.
```

---

## contracts:

```yaml
contracts:
  inputs:
    - name: topic             # every --input key must be declared
      description: "..."      # optional
  max_iterations: 40          # hard ceiling on total stage executions
  max_llm_calls: 200          # total LLM API calls allowed
  timeout_hours: 1.0          # wall-clock limit (fractions OK)
```

---

## model_tiers:

```yaml
model_tiers:
  small:                      # tier name: tiny | small | medium | large | frontier
    provider: openrouter      # anthropic | openai | openrouter | ollama | azure | bedrock
    model: qwen/qwen3.6-27b
    api_key_env: OPENROUTER_API_KEY   # env var holding the API key
    api_base: https://...             # required for ollama / azure / bedrock
    temperature: 0.2          # 0.0–1.0
    max_tokens: 2048
    adapter_support: dynamic  # dynamic | none — see §skill_library
```

Recommended models (OpenRouter):
- `qwen/qwen3.6-27b` — small, fast, reliable guided_json
- `moonshotai/kimi-k2.6` — large context, strong reasoning
- `anthropic/claude-opus-4-7` — frontier

---

## role_type_defaults:

```yaml
role_type_defaults:
  worker: small               # role type → tier name
  researcher: large
  judge: large
  orchestrator: large
```

Built-in fallbacks: `worker=small`, `judge=frontier`, `orchestrator=frontier`, `researcher=large`.

---

## tools:

```yaml
tools:
  - module: my_pkg.tools.web  # must define register(registry: ToolRegistry) -> None
```

---

## adapters:

```yaml
adapters:
  my_cmd:
    name: my_cmd
    type: script              # script | python
    cmd: "echo {{ arg }}"     # Jinja2 allowed
    timeout: 60
```

---

## skill_library:

Declarative skills that can be attached to LLM stages via `role.skills`. Each
skill may declare inline `content`, load text from a `path`, or reference a
registered LoRA adapter via `adapter`.

> Adapter-backed skills implement the Skill-to-LoRA pattern (Zhang & Qi, CUHK,
> June 2026 — [arXiv:2606.16769](https://arxiv.org/abs/2606.16769)): skill
> behavior is distilled into LoRA weights and loaded at runtime instead of
> injecting the full skill text into the prompt.

```yaml
skill_library:
  tdd:
    id: tdd
    description: Test-driven development workflow
    content: |
      Follow test-driven development:
      1. Write a failing test first.
      2. Write the minimal implementation.
      3. Refactor.
    adapter:
      name: tdd
      version: latest          # or a concrete version, e.g. "3"
      fallback: text           # text | none | fail
      inject_metadata: false   # true → append adapter metadata to prompt
```

**`adapter` fields:**

| Field | Type | Description |
|---|---|---|
| `name` | string | Adapter name in the local registry |
| `version` | string \| `latest` | Version to load; `latest` resolves the promoted pointer |
| `fallback` | `text` \| `none` \| `fail` | Behavior when the adapter cannot be loaded |
| `inject_metadata` | bool | Append an "Active via adapter ..." note to the skill prompt |

**`fallback` behavior:**
- `text` — keep the skill's `content`/`path` text in the prompt (default)
- `none` — omit the skill from the prompt entirely
- `fail` — raise an error and abort the run

See also: `model_tiers.*.adapter_support` and `adapter_factory:`.

---

## adapter_factory:

Configuration block for the pluggable LoRA adapter factory used by
`armature adapter create`.

```yaml
adapter_factory:
  backend: local              # mock | s2l | trace | local | modal | together |
                              # runpod | replicate
  base_model: qwen/qwen2.5-7b # must match a configured tier model
  rank: 16
  alpha: 32
  target_modules: [q_proj, v_proj]
  use_dora: false            # Weight-Decomposed Low-Rank Adaptation
  continual_learning:        # C-LoRA-style sequential updates
    enabled: false
    prior_version: latest     # or a concrete version, e.g. "3"
    orthogonality_lambda: 0.01
    freeze_old_routing: true
    init_delta_near_zero: true
  schedule:
    min_new_traces: 100
    max_age_days: 30
    quality_drift_threshold: 0.05
  promotion_policy:
    min_cng: 0.10
    min_hqs_delta: 0.02
    require_manual_review: false
  skills:
    tdd:
      backend: s2l            # per-skill override
      base_model: qwen/qwen2.5-7b
```

**Fields:**

| Field | Type | Description |
|---|---|---|
| `backend` | string | Adapter backend. `mock` writes placeholder artifacts instantly; `s2l` trains from a skill document; `trace` trains from exported SFT/DPO JSONL; `local` runs PEFT/MLX locally; remote backends dispatch to GPU providers. |
| `base_model` | string | Base model the adapter is trained on — must match one configured `model_tiers.*.model` |
| `rank` | int | LoRA rank (default 16) |
| `alpha` | int | LoRA alpha (default 32) |
| `target_modules` | list[string] | Modules to apply LoRA to (default `[q_proj, v_proj]`) |
| `use_dora` | bool | Use Weight-Decomposed Low-Rank Adaptation (DoRA) instead of vanilla LoRA |
| `continual_learning` | `ContinualLearningConfig` | C-LoRA-style continual adapter updates — freeze prior routing matrix, train a near-zero `R_delta`, apply orthogonality regularizer |
| `schedule` | `AdapterSchedule` | Retraining policy |
| `promotion_policy` | `AdapterPromotionPolicy` | Gate for auto-promoting a new adapter to `latest` |
| `skills` | dict[str, `AdapterFactorySkillOverride`] | Per-skill overrides; can override `use_dora` and `continual_learning` per skill |

---

## stages:

Every stage requires:
- `id:` — unique snake_case identifier
- `depends_on:` — list of stage IDs (empty list for start nodes)
- Exactly one execution field: `role` | `tool_call` | `gate` | `adapter` | `subagent_spec`

### LLM stage (role:)

```yaml
- id: analyst
  role:
    name: Analyst             # display name
    type: researcher          # worker | researcher | judge | orchestrator
    model_tier: large         # optional; overrides role_type_defaults
    description: |            # system prompt — this is where quality comes from
      Analyze {{ topic }}.
      {% if focus %}Focus on: {{ focus }}{% endif %}
  output_mode: text           # text | guided_json
  output_schema:              # required when output_mode: guided_json
    type: object
    required: [summary, confidence]
    properties:
      summary: {type: string}
      confidence: {type: number}
  fail_as_value: false        # true = failure stored as value, not abort
  on_fail:
    loop:
      stage: analyst          # stage to retry
      max: 2
  loop:                       # Deliberate iteration (not retry)
    max_iterations: 10        # total iterations including first
    until: "{{ approved }}"   # Jinja2; stop when truthy
    carry_forward:            # dot-paths to carry; null = carry all
      - decide_round.report
      - decide_round.gaps
    iteration_var: "_iteration"  # context var name (default: _iteration)
    backoff_s: null           # initial wait (seconds); doubles each iter
    backoff_max_s: 60.0       # backoff ceiling
  signature:
    input:                    # limit which context keys are visible
      topic: Research topic
      prior_stage: Prior output
  depends_on: [prior_stage]
```

**output_mode: guided_json** — auto-escalates to next tier on JSON parse failure.
Always pair with `output_schema`. Use `medium` or `large` tier (not `small`).

### Tool call stage (tool_call:)

```yaml
- id: run_searches
  tool_call:
    name: web_search          # tool must be registered in tools:
    args:
      query: "{{ search_item.query }}"   # Jinja2 allowed
      max_results: 5
  fan_out: 10                 # max parallel partitions (omit for single call)
  fan_in: list                # list | merge | first
  partition_source: "{{ plan.queries }}"  # Jinja2 expr → list
  partition_key: search_item  # name injected per partition item
  depends_on: [plan]
```

### Human gate stage (gate:)

```yaml
- id: review
  gate: human
  present: |
    Please review: {{ analyst.content }}
  depends_on: [analyst]
```

### Script adapter stage (adapter:)

```yaml
- id: run_script
  adapter: my_cmd             # references adapters: block
  depends_on: []
```

### Subagent stage (subagent_spec:)

```yaml
- id: child_run
  subagent_spec: workflows/child.yml
  fan_out: 3
  fan_in: list
  partition_key: child_input
  depends_on: []
```

### Post-run stage (post_run: true)

```yaml
- id: self_analyst
  post_run: true              # runs after all normal stages complete
  fail_as_value: true         # don't abort the run if this stage fails
  depends_on: []
  signature:
    input:                    # REQUIRED for fan-out workflows; _transcript is huge
      topic: Research topic
      final_stage: The final output
  role:
    name: Director
    type: judge
    description: |
      Review the completed run. Identify quality issues.
```

---

## Fan-out Pattern

```yaml
# Step 1: produce a list
- id: planner
  output_mode: guided_json
  output_schema:
    type: object
    required: [items]
    properties:
      items: {type: array, items: {type: object}}

# Step 2: fan out over that list
- id: worker
  tool_call: {name: some_tool, args: {input: "{{ item.field }}"}}
  fan_out: 5
  fan_in: list                # or merge | first
  partition_source: "{{ planner.items }}"
  partition_key: item
  depends_on: [planner]

# Step 3: consume the collected list
- id: synthesizer
  signature:
    input:
      worker: All collected results
  role: {name: Synthesizer, type: judge, description: "Synthesize {{ worker }}"}
  depends_on: [worker]
```

---

## Jinja2 Context

All context is cumulative — every stage sees all prior outputs automatically.

```
{{ topic }}                    runtime input from contracts.inputs
{{ stage_id }}                 full output dict of a stage
{{ stage_id.content }}         text field of a text stage
{{ stage_id.field }}           named field from a guided_json stage
{{ partition_key.subfield }}   partition variable in a fan-out stage
{{ _iteration.num }}           1-based iteration number (inside a loop: stage)
{{ _iteration.is_first }}      True on iteration 1
{{ _iteration.is_last }}       True on final iteration
{{ _iteration.carry_forward }} selected state carried from previous iteration
{% if x is defined and x %}    guard optional / memory values
{% for item in list %}         loop over a list
```

Missing keys → empty string (ChainableUndefined, no error raised).

---

## memory:

```yaml
memory:
  enabled: true
  capture:
    - stage: analyst
      key: content             # output field name
      max_entries: 5
  inject_as: _memory          # context key; access as {{ _memory }}
```

---

## continuation:

```yaml
continuation:
  carry_forward:
    - key: analyst.summary    # stage_id.output_key dotted notation
  inject_as: prior_run        # access as {{ prior_run.summary }}
```

---

## context_layers:

A named stack of context blocks that `context_policy` can force into stage
prompts. Generalizes the single `mission:` string, which remains and behaves
exactly as before.

```yaml
context_layers:
  - name: principles        # unique; 'mission' is reserved
    precedence: 10          # higher = rendered first among must'd layers
    content: |             # inline text ...
      Never invent numbers.
    never:                  # floor closures — no stage may see these sources
      - raw_pii
  - name: domain
    src: domain.md          # ... or a file path, resolved at load
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | str | required | unique; `mission` is reserved (error) |
| `precedence` | int | `0` | render order among must'd layers (highest first) |
| `content` | str | — | inline layer text — exactly one of `content`/`src` |
| `src` | str | — | path relative to the spec; inlined at load; bundled into packages |
| `never` | list[str] | `[]` | sources closed for ALL stages — non-relaxable floor |

## context_policy:

Workflow-level default and per-stage MUST/NEVER governance over what context
each stage sees. A stage's policy adds to the workflow default; nothing can
relax a closure.

```yaml
context_policy:               # workflow default
  must: [principles]          # layer names forced into every stage's prompt
  never: []                   # sources closed for every stage

stages:
  - id: analyst
    context_policy:
      must: [domain]                # additional forced layers (additive)
      never: [researcher, raw_pii]  # closed stage outputs / inputs / layers / injected keys
```

**Resolution (per stage):**

```
effective_never = ∪ layer.never  ∪ workflow_default.never  ∪ stage.never
effective_must  = (workflow_default.must ∪ stage.must ∪ {mission}) − effective_never
```

A closure at any level always wins over a force. `must` targets layer names
only (plus the reserved auto layer `mission`, which every stage receives by
default and which `never: [mission]` closes). `never` targets:

| target | closes | validated against |
|---|---|---|
| stage id | that stage's output key | `stages` |
| runtime input | that input | `contracts.inputs` |
| layer name | that layer's content | `context_layers` |
| injected key | `_memory`, `_knowledge`, `_transcript`, `prior_run`, `run_id`, … | the harness-injected key set |

The effective policy is recorded on every trace row (`context_policy`).
Known limits: closed content can still flow transitively through intermediate
stage outputs, and tool-mediated reads (memory navigation tools) are governed
by `safety_rules`, not `never:`.

---

## safety_rules:

```yaml
safety_rules:
  - tool: my_cmd
    condition:
      field: cmd              # field to inspect in the tool args
      op: contains            # contains | not_contains | equals | not_equals | matches_regex | truthy
      value: "rm -rf"
    action: block             # block (abort) | warn (continue) | log (silent)
    message: "Dangerous command blocked"
```

---

## self_improvement:

```yaml
self_improvement:
  editable_surfaces:          # surfaces the refiner may modify (default: descriptions, retry_counts, timeouts)
    - descriptions            # role.description on stages
    - schemas                 # output_schema definitions
    - model_tiers             # role.model_tier assignments
    - retry_counts            # on_fail.loop.max values
    - timeouts                # stage.timeout_s values
  target_hqs: 0.90            # optional: HQS threshold below which improvement fires (default: CLI default — 0.90 for `improve`, 0.75 for `run --auto-improve`)
  min_traces: 3               # optional: minimum traces required before analysis (default: CLI default — 3)
  drift_threshold: 0.5        # optional: drift score that fires improvement even when HQS ≥ target (default 0.5)
```

Surfaces NOT listed are locked — the refiner's system prompt explicitly names them as off-limits, and a proposal that touches a locked surface is rejected (not applied).
Default: `[descriptions, retry_counts, timeouts]`. `schemas` and `model_tiers` require human review due to cascading effects.

`target_hqs` / `min_traces` make the spec the single source of truth for *when* self-improvement fires. A CLI flag (`--target-hqs`, `--min-traces`) overrides the spec; when neither is set, the CLI default applies.

`drift_threshold` fires improvement when the system is **oscillating** — previously-fixed failures reappearing — even if HQS is healthy. `drift_score` is the fraction of current failures that are regressions of past `verified_fixes` (0.0–1.0; inert until cycle 2+ when `verified_fixes` first exist). Drift-triggered proposals **always require review** — auto-apply is suppressed to avoid worsening the oscillation (the proposal is written to `<spec>.pending.yaml`). A CLI flag (`--drift-threshold`) overrides the spec.

Used by `armature improve` and `SelfImproveRunner`. Set `n_proposals` on `SelfImproveRunner` to generate multiple candidates and pick the best coverage match.

**Latency-aware selection (#6).** When `n_proposals > 1`, selection is latency-aware: coverage is primary, but among candidates within 1 predicted-fix of the top coverage (an ε-band fuzzy tiebreak), the one with the lowest structural `latency_risk` wins (coverage is the final tiebreak). `latency_risk` is a proxy computed from the spec diff — +1.0 per stage added, −1.0 per stage removed, ±1.0 per tier escalation/demotion, +0.5 per retry-count increase, +0.25 per timeout increase, +0.5 for a `model_tiers` block redefinition; lower is safer for latency. This directly addresses the HQS latency-cancel tradeoff (the max-coverage candidate tends to add the most fix-power and raise latency, netting ~0 HQS). `latency_risk` is logged on each cycle and shown as the `Lat` column on the `armature dashboard` improvement-cycles table (red when > 1.0); on the single-proposal path it is surfaced as an informational flag.

**Leverage-weighted selection.** When sufficient trace history exists, coverage is additionally weighted by **stage leverage** — the Pearson correlation `r` between each stage's per-run signal (quorum score for judge stages, `success × valid` otherwise) and the run's final HQS. A fix targeting a high-leverage stage scores higher than one targeting a low-leverage stage, so the optimizer prioritizes the stages that move HQS the most. The weight map is derived from `compute_leverage(traces)` and is **dormant below a data-sufficiency guard**: leverage is only applied once at least `min_runs` (default 8) runs exist *and* at least one stage reaches `|r| ≥ 0.4`; otherwise coverage is the plain fix count and selection is byte-for-byte the pre-leverage behavior. Leverage reweights coverage only — the ε-band latency tiebreak above is unchanged. The per-stage `r` values and sufficiency status are shown on the `armature dashboard` leverage heatmap panel and recorded on each improvement log entry (`leverage_sufficient`, `leverage_stages`).

---

## Validation Error Codes

| Code | Fix |
|---|---|
| `UNDEFINED_DEPENDENCY` | Check stage ID spelling in depends_on |
| `CIRCULAR_DEPENDENCY` | Remove the cycle |
| `FAN_OUT_MISSING_PARTITION_SOURCE` | Add `partition_source: "{{ list }}"` |
| `PARTITION_SOURCE_MISSING_FAN_OUT` | Add `fan_out: N` |
| `NO_EXECUTION_TYPE` | Add role / tool_call / gate / adapter / subagent_spec |
| `UNDEFINED_MODEL_TIER` | Define the tier in model_tiers: |
| `UNDEFINED_ADAPTER` | Define the adapter in adapters: |
| `SIGNATURE_TYPE_MISMATCH` | Align types between upstream output and downstream input |
| `POST_RUN_TRANSCRIPT_OVERFLOW_RISK` | Add signature.input to the post_run stage |
| `CONTRACT_INPUT_MISSING_NAME` | Add name: to each contracts.inputs entry |
| `UNKNOWN_ADAPTER_BACKEND` | Use a recognized `adapter_factory.backend` |
| `ADAPTER_FACTORY_NO_BASE_MODEL` | Set `adapter_factory.base_model` or ensure it matches a tier |
| `ADAPTER_NO_FALLBACK` | Add `content`/`path` to the skill or change `fallback` to `none`/`fail` |
| `ADAPTER_BASE_MODEL_MISMATCH` | Align `adapter_factory.base_model` with a configured tier model |
| `RESERVED_CONTEXT_LAYER_NAME` | A user layer is named `mission` (reserved for the auto layer from `mission:`) |
| `UNKNOWN_CONTEXT_LAYER` | `context_policy.must` names a layer that doesn't exist |
| `UNKNOWN_CONTEXT_SOURCE` | a `never` names neither a stage id, runtime input, layer name, nor injected key |
| `CONTEXT_POLICY_CONTRADICTS_FLOOR` | a `must` overlaps an applicable `never` (floor, workflow default, or its own) |
| `NEVER_BLOCKS_PARTITION_SOURCE` *(warning)* | a fan-out stage closes its own `partition_source` — it resolves pre-filter |
| `CONTEXT_TRANSIT_LEAK_RISK` *(warning)* | a closed stage's content may flow transitively through a visible stage's output |

---

## CLI Quick Reference

```bash
armature validate spec.yml                         # always run before armature run
armature run spec.yml --input topic="..." --quiet
armature run spec.yml --dry-run                    # validate only, no execution
armature dashboard spec.yml                        # health metrics after runs
armature new spec.yml                              # terminal wizard
armature optimize spec.yml                         # AI-proposed spec improvements
armature adapter create --spec spec.yml --skill tdd --backend local
armature adapter merge skill-a@1 skill-b@1 --name combo
armature adapter eval tdd spec.yml --input topic="..."
```

See `docs/ADAPTER-POWERED-TEAMS.md` for the full end-to-end workflow: choosing
candidate SLM tiers, declaring adapter-backed skills, creating adapters,
periodic retraining from traces, and promotion.

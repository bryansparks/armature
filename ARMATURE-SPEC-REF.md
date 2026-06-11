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
```

Surfaces NOT listed are locked — the refiner's system prompt explicitly names them as off-limits.
Default: `[descriptions, retry_counts, timeouts]`. `schemas` and `model_tiers` require human review due to cascading effects.

Used by `armature improve` and `SelfImproveRunner`. Set `n_proposals` on `SelfImproveRunner` to generate multiple candidates and pick the best coverage match.

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

---

## CLI Quick Reference

```bash
armature validate spec.yml                         # always run before armature run
armature run spec.yml --input topic="..." --quiet
armature run spec.yml --dry-run                    # validate only, no execution
armature dashboard spec.yml                        # health metrics after runs
armature new spec.yml                              # terminal wizard
armature optimize spec.yml                         # AI-proposed spec improvements
```

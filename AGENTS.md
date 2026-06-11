# Armature — AI Coding Guide (Codex / OpenAI CLI)

Armature is a DAG-based multi-agent workflow engine for Python. Developers describe agentic
teams as YAML spec files; the engine resolves execution order, manages context flow between
stages, calls LLMs, and handles retries and fan-out automatically.

## The Primary Path

**In Codex (or any AI coding tool), this is the best way to create a workflow:**

1. Describe the agentic team you want to build in natural language
2. Ask Codex to generate a complete, documented YAML spec
3. Validate: `armature validate my_workflow.yml`
4. Run: `armature run my_workflow.yml --input key=value`

**Working reference:** `docs/ARMATURE-SPEC-REF.md` — all fields and valid values on one page. Load this as context when generating a spec.
**Full documentation:** `docs/USER-GUIDE.md` — consult for advanced features (fan-out, memory, safety rules, lifecycle hooks, deliberative teams).

---

## Core Mental Model

### DAG + cumulative context
Every stage declares `depends_on`. The engine runs them in topological order. Each completed
stage stores its result in a shared context dict under `stage_id`. Every downstream stage
automatically sees all prior outputs — **no explicit data wiring is needed**.

```
researcher ──► analyst ──► judge ──► writer
                  ↑
       (sees researcher's output automatically)
```

### Stage types
| Field | What it does |
|-------|-------------|
| `role:` | LLM stage — has a system prompt, produces text or JSON |
| `tool_call:` | Direct tool invocation — no LLM, deterministic |
| `gate: human` | Pauses execution for human approval |
| `adapter:` | Runs a shell command or Python function |
| `subagent_spec:` | Spawns a child workflow as a nested execution |

### Model tiers
Define named slots (`tiny`/`small`/`medium`/`large`/`frontier`). Stages reference tier names,
not model names directly. Swap all models globally by editing the `model_tiers` block.

### Output modes
- `output_mode: text` → stored as `{"content": "..."}` — use for freeform responses
- `output_mode: guided_json` + `output_schema:` → strongly typed dict; auto-escalates to next
  tier on parse failure — use whenever downstream stages depend on specific fields

---

## Generating a Spec — Checklist

When generating workflow YAML, produce all of these sections in order:

**1. Header**
```yaml
name: my_workflow
version: "1.0"
description: "One sentence describing what this workflow does."
mission: >
  Optional paragraph ALL LLM stages inherit as background context.
  Use for workflow-level invariants: tone, constraints, domain knowledge.
```

**2. Contracts**
```yaml
contracts:
  inputs:
    - name: topic          # every runtime input must be declared here
    - name: focus          # optional inputs too — document them all
  max_iterations: 40
  max_llm_calls: 200
  timeout_hours: 1.0
```

**3. Model tiers**
```yaml
model_tiers:
  small:
    provider: openai
    model: gpt-4o-mini
    temperature: 0.2
    max_tokens: 2048
  large:
    provider: openai
    model: gpt-4o
    temperature: 0.3
    max_tokens: 16000

role_type_defaults:
  worker: small
  orchestrator: large
  judge: large
  researcher: large
```

**4. Tools (if workflow uses custom tools)**
```yaml
tools:
  - module: my_package.tools.web     # Python module path; must define register(registry)
```

**5. Stages** — see patterns below

**6. Post-run self-analyst** — add for any non-trivial workflow:
```yaml
- id: self_analyst
  post_run: true
  fail_as_value: true
  depends_on: []
  signature:
    input:
      # List the key output stages — NEVER use _transcript in fan-out workflows
      topic: Research topic
      synthesizer: The final synthesis output
      judge: Quality assessment
  role:
    name: Director
    type: judge
    description: |
      Review this completed run. Identify quality issues and suggest improvements
      to stage prompts, model tier assignments, or workflow structure.
```

---

## Common Patterns

### Linear pipeline (planner → workers → judge)
```yaml
stages:
  - id: planner
    role: {name: Planner, type: orchestrator, description: "Plan the approach for: {{ topic }}"}
    output_mode: guided_json
    output_schema:
      type: object
      required: [steps]
      properties:
        steps: {type: array, items: {type: string}}
    depends_on: []

  - id: executor
    role: {name: Executor, type: worker, description: "Execute: {{ planner.steps }}"}
    output_mode: text
    depends_on: [planner]

  - id: judge
    role: {name: Judge, type: judge, description: "Assess quality of: {{ executor.content }}"}
    output_mode: guided_json
    output_schema:
      type: object
      required: [accept, confidence, issues]
      properties:
        accept: {type: boolean}
        confidence: {type: number}
        issues: {type: array, items: {type: string}}
    on_fail:
      loop: {stage: judge, max: 2}
    depends_on: [executor]
```

### Fan-out research pipeline (parallel search + synthesis)
```yaml
  - id: plan_searches
    role: {name: SearchPlanner, type: worker, description: "Generate search queries for {{ topic }}"}
    output_mode: guided_json
    output_schema:
      type: object
      required: [queries]
      properties:
        queries: {type: array, items: {type: object, required: [query, intent], properties: {query: {type: string}, intent: {type: string}}}}
    depends_on: []

  - id: run_searches
    fan_out: 10                           # run up to 10 queries in parallel
    fan_in: list
    partition_source: "{{ plan_searches.queries }}"  # must resolve to a list
    partition_key: search_item            # each item injected as this key
    tool_call:
      name: web_search
      args:
        query: "{{ search_item.query }}"
        max_results: 5
    depends_on: [plan_searches]

  - id: synthesize
    signature:
      input:
        topic: Research topic
        run_searches: All search results  # context filter — only these keys visible
    role: {name: Synthesizer, type: judge, description: "Synthesize: {{ run_searches }}"}
    output_mode: text
    depends_on: [run_searches]
```

### Deliberative team (debate → judge)
```yaml
  - id: proponent
    role: {name: Proponent, type: researcher, description: "Make the case FOR: {{ objective }}"}
    output_mode: text
    depends_on: []

  - id: critic
    role: {name: Critic, type: researcher, description: "Challenge the proponent's case: {{ proponent.content }}"}
    output_mode: text
    depends_on: [proponent]

  - id: judge
    role:
      name: Judge
      type: judge
      description: "Adjudicate the debate. OBJECTIVE: {{ objective }}"
    output_mode: guided_json
    output_schema:
      type: object
      required: [decision, reasoning, confidence]
      properties:
        decision: {type: string}
        reasoning: {type: string}
        confidence: {type: number, minimum: 0.0, maximum: 1.0}
    depends_on: [proponent, critic]
```

---

## Anti-patterns to Avoid

| Anti-pattern | Why | Fix |
|---|---|---|
| No `signature.input` on post_run in fan-out workflows | `_transcript` is enormous — overflows context | Add `signature.input` listing only needed outputs |
| `fan_out` without `partition_source` | Validator error: `FAN_OUT_MISSING_PARTITION_SOURCE` | Add `partition_source: "{{ list_var }}"` |
| `guided_json` without `output_schema` | Runtime parse failures, no escalation target | Always pair them |
| `small` tier for complex guided_json | High escalation rate, latency spikes | Use `medium` or `large` for structured output |
| Undefined stage in `depends_on` | Validator error: `UNDEFINED_DEPENDENCY` | Check all stage IDs |
| Undeclared inputs in `contracts.inputs` | Validator warning for `signature.input` keys | Declare every runtime input |
| No `fail_as_value: true` on optional stages | One failure aborts the entire run | Add it to non-critical stages |

---

## Jinja2 in Descriptions and Tool Args

Context values (runtime inputs + upstream stage outputs) are available via Jinja2:

```yaml
description: |
  Research {{ topic }}.
  {% if focus %}Focus: {{ focus }}{% endif %}
  {% if prior_research is defined and prior_research %}
  Prior run findings: {{ prior_research }}
  {% endif %}
  Sub-questions: {{ planner.questions }}

tool_call:
  args:
    query: "{{ search_item.query }}"    # from partition_key in fan-out
    path: "{{ workspace }}/output.txt"
```

- Missing keys render as empty string (ChainableUndefined) — they do not raise errors
- Use `{% if var is defined and var %}` to guard optional inputs
- Fan-out partition items are available via the `partition_key` name you set

---

## Validation Error Codes

| Code | Cause | Fix |
|---|---|---|
| `UNDEFINED_DEPENDENCY` | `depends_on` references unknown stage ID | Check stage ID spelling |
| `CIRCULAR_DEPENDENCY` | Cycle in the DAG | Remove circular depends_on |
| `FAN_OUT_MISSING_PARTITION_SOURCE` | `fan_out` set but no `partition_source` | Add `partition_source: "{{ list }}"` |
| `NO_EXECUTION_TYPE` | Stage has no role/tool_call/gate/adapter/subagent_spec | Add one |
| `UNDEFINED_MODEL_TIER` | Stage references a tier not in `model_tiers` | Define the tier |
| `UNDEFINED_ADAPTER` | Stage references adapter not in `adapters` | Define the adapter |
| `SIGNATURE_TYPE_MISMATCH` | Upstream output type ≠ downstream expected type | Align types |
| `POST_RUN_TRANSCRIPT_OVERFLOW_RISK` | post_run stage has no `signature.input` in a fan-out workflow | Add `signature.input` |

---

## CLI Quick Reference

```bash
armature validate my_workflow.yml          # catch errors before running (always do this)
armature run my_workflow.yml \
  --input topic="..." \                    # pass runtime inputs
  --input focus="..." \
  --quiet                                  # suppress live output
armature run my_workflow.yml --dry-run     # validate spec only, no execution
armature dashboard my_workflow.yml         # health metrics after multiple runs
armature new my_workflow.yml               # terminal wizard (secondary path)
armature optimize my_workflow.yml          # LLM-proposed spec improvements from traces
```

---

## Example: Minimal Working Spec

```yaml
name: topic-researcher
version: "1.0"
description: "Research a topic and produce a structured briefing."

model_tiers:
  small:
    provider: openai
    model: gpt-4o-mini
  large:
    provider: openai
    model: gpt-4o

role_type_defaults:
  worker: small
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
      description: |
        Research the following topic thoroughly.
        Identify key findings, data points, and open questions.
        Topic: {{ topic }}
    output_mode: text
    depends_on: []

  - id: editor
    role:
      name: Editor
      type: worker
      description: |
        Tighten the researcher's draft into a crisp 3-paragraph briefing.
        Preserve all concrete facts. Eliminate repetition.
    output_mode: text
    depends_on: [researcher]
```

```bash
armature validate topic-researcher.yml
armature run topic-researcher.yml --input topic="quantum error correction"
```

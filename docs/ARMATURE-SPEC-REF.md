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
closure:                     # optional: name the stage whose output declares this run's closure
  stage: final_report
mission_source: static       # static | work_unit — where the mission context renders from
requires: [llm, git]         # optional: runner capabilities a server job needs (llm, git, claude-code, platform:ios)
```

---

## contracts:

```yaml
contracts:
  inputs:
    - name: topic             # every --input key must be declared
      description: "..."      # optional
    - name: dry_run
      type: boolean           # string | boolean | integer | number | object | array
      default: false          # applied when the input is absent; must match type
    - name: mode
      type: string
      enum: [fast, thorough]  # the value must be one of these
      required: true          # fail the run when absent and no default
  outputs:                    # typed outputs: what a call: of this workflow returns
    - name: passed
      type: boolean
      from: "{{ verify.passed }}"   # Jinja2 over this run's stage results
  max_iterations: 40          # hard ceiling on total stage executions
  max_llm_calls: 200          # total LLM API calls allowed
  timeout_hours: 1.0          # wall-clock limit (fractions OK)
```

An input with `type` is coerced before anything renders, including load-time
rendering of the spec file: `--input dry_run=false` becomes the boolean
`false`, `"3"` becomes `3`, and `object`/`array` inputs parse as JSON. A value
that does not convert, or falls outside `enum`, fails the run with the input
named in the error. Entries without `type`, `default`, or `enum` behave as
before: values pass through as strings.

`outputs` entries with a `name` are typed outputs. Entries of the older form
(`stage`, `key`, `required`) still check that a stage produced a key.

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
    # reasoning_effort: high  # optional: low | medium | high | xhigh | max; see below
    adapter_support: dynamic  # dynamic | none — see §skill_library
```

`reasoning_effort` goes to litellm, which maps it to the provider's own control
(adaptive thinking plus `output_config.effort` on Claude, `reasoning_effort` on
OpenAI reasoning models). Each model has its own default when it's unset. A
role can override it. A model litellm can't map it for,
including many non-reasoning models, fails the stage on its first call, so omit
it on those tiers.

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
    api_key_env: [TAVILY_API_KEY]  # env vars the module needs; package build
                                   # sweeps these into secrets.yaml so
                                   # fail-closed runners inject them
```

---

## destinations:

```yaml
destinations:                     # run output contract; carried verbatim by
  artifacts:                      # armature package build (an explicit
    - stage_id: writer            # --destinations file still wins; inferred
      name: briefing              # from leaf stages when absent)
      format: markdown            # markdown | json | text
    - stage_id: writer
      name: report
      format: text
      source: research-output/report.html   # opt-in file capture: copy files
                                             # matching this path-or-glob (run
                                             # working dir) into artifacts/
  include_trace: false
```

---

## closure:

```yaml
closure:
  stage: final_report     # a guided_json stage whose output declares the
                          # run's closure (missions design §3)
```

Fields:

| Field | Type | Description |
|---|---|---|
| `stage` | string | ID of the stage whose output is the closure record |

The named stage must use `output_mode: guided_json` with an
`output_schema` satisfying the **built-in closure contract**: a required
`reason` whose enum is non-empty and drawn from `CLOSURE_REASONS`
(`done_no_follow_on` \| `handed_off` \| `blocked_on` \| `escalation`).
Optional: `notes` (string) and `follow_on` (array of objects — mini
work-unit specs the executor upserts into the mission). `follow_on` items
are work units and need a durable address (design §7): each requires
`id`, `title`, and `workflow` (`objective` and `inputs` optional).

```yaml
# inside the closure stage's output_schema
required: [reason]
properties:
  reason: {type: string, enum: [done_no_follow_on, handed_off, blocked_on, escalation]}
  notes: {type: string}
  follow_on:
    type: array
    items:
      type: object
      required: [id, title, workflow]
      properties:
        id: {type: string}
        title: {type: string}
        workflow: {type: string}
```

When `mission run` executes a spec that declares a closure, the stage's
output is extracted into a typed `ClosureRecord` and is **authoritative
for the unit's resting state**:

| `reason` | Resting state | Side effects |
|---|---|---|
| `done_no_follow_on` | `done` | — |
| `handed_off` | `handed_off` | each `follow_on` seeded `pending` (idempotent upsert) |
| `blocked_on` | `blocked_on` | blockers seeded `pending`; the unit's `requires` extended with their ids |
| `escalation` | `escalation` | `notes` carried as the audit reason |

A malformed closure output is loud: the unit lands `failed` (retry while
attempts remain) with the violation named — never silently `done`.

A run whose spec declares no closure yields `done_no_follow_on` — silence is
an explicit default. Validation codes: `CLOSURE_STAGE_UNDEFINED`,
`CLOSURE_STAGE_NOT_GUIDED_JSON`, `CLOSURE_SCHEMA_INVALID`.

---

## mission_source:

```yaml
mission_source: static      # static (default) | work_unit
```

- `static` — the `[Workflow Mission]` context layer renders from the spec's
  `mission:` string (today's behavior).
- `work_unit` — a run executed for a work unit renders the mission context
  from the injected work record instead: the mission's `objective` in
  `[Workflow Mission]`, plus the unit's `title` and `objective` in a new
  `[Work Unit]` layer. The record arrives as the `work_unit` context key
  (governable via `never: [work_unit]`, referenceable in Jinja as
  `{{ work_unit.title }}`). A run without a record — plain `armature run` —
  falls back to the static `mission:` string. `work_unit` is a reserved
  context-layer name.

See [Mission Documents](#mission-documents) below and
`docs/MISSION-AS-WORKFLOW-DRIVER.md`.

---

## Mission documents

A mission document is a second YAML type — the work layer *above* workflows.
It holds a larger objective plus work units, each naming which workflow
accomplishes it. A workflow never references a mission; a work unit
references a workflow. Validated with `armature mission validate`.

```yaml
name: campaign-pretzel
version: "1.0"
description: "..."           # one sentence
objective: |                 # THE context every run of every unit inherits
  Launch the Dangerous Pretzel ad campaign by Nov 15.
posture: human-led            # mission default: executors notify, never act
budget_usd: 200.0             # mission-level allocation ceiling

work:
  - id: hero-headlines       # unique; the durable address (outlives any run)
    title: Write hero headline variants
    objective: 12 variants across 3 tones.   # what "done" means for this unit
    workflow: ../11_iterative_refinement.yml  # path (validated) or registered name (warned)
    inputs: {topic: launch}  # runtime inputs, same grammar as --input
    requires: [brand-approval]  # unit ids that must reach done first
    posture: delegated       # unit override → mission default → human-led
    max_budget_usd: 1.0      # per-run ceiling (metered from the run receipt)
    timeout_hours: 1.0
    max_attempts: 2
```

**Mission fields:** `name`, `version`, `description`, `objective`
(str), `posture` (`human-led` | `delegated`, default `human-led`),
`budget_usd` (num, optional), `work` (list).

**Work unit fields:** `id` (str, unique), `title` (str), `objective`
(str, default `""`), `workflow` (str), `inputs` (mapping, default `{}`),
`requires` (list of unit ids, default `[]`), `posture` (enum or omitted to
inherit), `max_budget_usd` / `timeout_hours` / `max_attempts`
(num, default `max_attempts: 2`).

**Lifecycle** (enforced in `armature/state/work.py`; every writer goes
through the same table):

```
pending → in_progress → done
                    ├→ failed | blocked_on | handed_off | escalation | canceled
```

**Driving a mission locally** — work-unit records persist in a
`LocalWorkStore` (default `~/.armature/work/<mission>/<unit_id>.json` plus a
`transitions.jsonl` audit log; `--store` overrides the base directory):

```bash
armature mission status my_mission.mission.yml    # unit states + recent transitions
armature mission run my_mission.mission.yml unit_a   # run one unit's workflow
armature mission advance my_mission.mission.yml   # pure readiness: what may start now
```

`mission run` refuses without consuming an attempt when the unit's
`requires` are not all `done` or the unit is not in `pending` /
`retry_pending` (a `blocked_on` unit re-enters once its requires — spec ∪
record — are done). Each run increments `attempts`; a failure lands `failed`,
then `retry_pending` only while `attempts < max_attempts` — at the ceiling
the unit stays `failed`. The run id is recorded as the unit's `last_job_id`.

**Budget gates**: `spent_usd` on each record accrues from the run's own
observed LLM cost (the receipt's `cost_usd` field on `ResultsManifest` —
provider-reported per response, `0.00` when the provider reports none,
never a vendor balance check), on success *and* failure. `mission run`
refuses before any attempt is consumed when the unit's `spent_usd` has
reached `max_budget_usd` or the mission's records sum to `budget_usd`.

**`mission advance`** is pure computation: it seeds missing records, reads
the store, and returns a per-unit decision — `launchable` (delegated,
requires done, attempts and budget remaining), `notify_only` (human-led
posture), or `held` with reasons. It never executes anything; executors (slice 4's
dispatch sweep) consume `--json`. When the spec declares a `closure:`, the
run's closure record is authoritative for the unit's resting state (see
[closure:](#closure) above).

Worked example: `examples/missions/campaign-pretzel.mission.yml`. Full
concept doc: `docs/MISSION-AS-WORKFLOW-DRIVER.md`.

---

## adapters:

```yaml
adapters:
  my_cmd:
    name: my_cmd
    type: script              # script | python
    cmd: "./scripts/run.sh"   # only {{ run_id }} may be templated here
    timeout: 60
    parse: json               # optional — stdout parsed as a JSON object becomes the stage result
```

An inline `cmd` may reference no template value except `{{ run_id }}`, because a
rendered value becomes shell syntax. Read inputs and step outputs from the
`ARMATURE_CONTEXT` (JSON) or `ARMATURE_WITH` environment variable instead.
Anything else is the `ADAPTER_CMD_INPUT` validation error. Pack commands are
exempt.

Fields: `name`, `type` (`script` | `python`), `cmd` (script) or `fn` (python),
`args` (static dict merged into context), `timeout`, `parse` (`json` — the
printed JSON object becomes the stage result; non-object top level or non-zero
exit raises, subject to `on_fail` / `fail_as_value`). See
`docs/USER-GUIDE.md` §4.3 for details and a worked example.

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
- Exactly one execution field: `role` | `tool_call` | `gate` | `adapter` | `subagent_spec` | `call` | `agent`

### LLM stage (role:)

```yaml
- id: analyst
  role:
    name: Analyst             # display name
    type: researcher          # worker | researcher | judge | orchestrator
    model_tier: large         # optional; overrides role_type_defaults
    reasoning_effort: high    # optional; overrides the tier default
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
    timeout_s: null           # wall-clock budget across rounds; checked after each round
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
  approval:                   # optional, server runs only
    min_role: team_lead       # project_lead (default) | team_lead | admin
    expires_hours: 24         # default 72, at most 720
    slack_channel: C0123ABCD  # optional: post an Approve and Reject card here
  depends_on: [analyst]
```

From the CLI, a gate asks on stdin. When the server runs the workflow, the
run pauses at the gate after the other stages of its wave finish, and the
question shows on the run page and under **Waiting for you**. Approve or
reject resumes the same run from its checkpoint, with the gate's result set
to `{approved, feedback, decided_by, decided_at}`. A gate nobody decides
before it expires resumes as rejected with `feedback: expired`. A rejected
or expired gate also sets the run's outcome to `blocked`, written by the
server, whatever the gate guarded. Use
`condition: "{{ review.approved }}"` on the next stage to act on the answer.
A gate inside a workflow that a `call:` step runs fails instead of pausing.

With `slack_channel`, or when the run belongs to a Slack thread
(`slack:<channel>:<ts>`), the server posts a card with Approve and Reject
buttons through the team's Slack connection and rewrites it once the gate is
decided. A click counts only from a Slack account that its user linked on
their account page, and only when that user may decide the gate. Anyone else
gets a note only they can see.

Only the engine can pause a run. The server gives it a per-run key over
a pipe that `ARMATURE_ENGINE_FD` names, never in its environment. It reads
and closes the pipe before it starts any step, so no step, script, or agent
session receives the key. The engine signs its `run_suspend` event, including
a digest of the gate's checkpoint and question, and the server opens a gate
only for a signed pause whose step, key, and digest match the gate request.
Who may decide and when it expires come from the stage's `approval:` in the
pinned workflow, never from the request. On Linux,
`armature run` marks itself not dumpable (`PR_SET_DUMPABLE` 0), so a step
running as the same user can't read the engine's memory or environment.
Script steps also can't leave background processes behind, because each
step's process group is killed when the step ends.

Run gated workflows in production on Linux runners with `--job-user`. On a
macOS embedded worker, a step running as the same user with debugger access
could read the engine key from the engine's memory. A double-forked daemon
escapes the step's process group kill, and only the runner's per-user kill as
the job user stops it.

### Script adapter stage (adapter:)

```yaml
- id: run_script
  adapter: my_cmd             # references adapters: block
  depends_on: []
```

### Subagent stage (subagent_spec:)

```yaml
- id: child_run
  subagent_spec: workflows/child.yml   # resolved spec-dir first, cwd fallback
  fan_out: 3
  fan_in: list
  partition_key: child_input
  depends_on: []
```

Carried context renders into the child spec's **string fields only** (the child
is parsed with templates inert first) — multiline upstream values are safe, and
a template may not stand in for YAML structure. Details:
`docs/SUBAGENT-COMPOSITION.md` → *Child spec template semantics*.

### Call stage (call:)

```yaml
- id: fix
  call:
    workflow: pr-fix          # a stored workflow name
    version: 4                # optional; omitted = the version the call bundle pins
  with:                       # the callee's declared inputs; string values may be Jinja2
    repo: "{{ repo }}"
    attempt: "{{ _iteration.num }}"
  loop:
    max_iterations: 3
    until: "{{ passed }}"     # reads the callee's typed outputs
    timeout_s: 1800
  depends_on: [triage]
```

The callee runs as an isolated child run: it sees only its `with:` inputs,
coerced to the types it declares, never the caller's context. The stage's
output is exactly the callee's typed `contracts.outputs`, or the callee's
final stage output when it declares none. Child cost rolls into the caller.

The engine never reads the server database. Called workflows are resolved
before the run into a call bundle beside the top-level workflow file:
`calls/manifest.json` (`{"pins": {"pr-fix": 4}}`) plus one
`calls/<workflow>@<version>.yaml` per callee. `armature package build`
vendors the bundle, and `armature validate` checks `with:` against each
callee when a bundle is present. Details: `docs/SUBAGENT-COMPOSITION.md` →
*Calling a stored workflow*.

### Agent stage (agent:)

An `agent:` mapping runs one Claude Code session. The string form
`agent: <name>` still names an `agent_library` entry.

```yaml
- id: review
  agent:
    harness: claude-code      # claude-code (default) or codex (experimental, can't run yet)
    model: review             # a team provider alias on a server; a model id in a local run
    effort: high              # optional: low, medium, high, xhigh, max
    access: read              # read (default) or write
    prompt: |                 # Jinja2, rendered once at run time
      Review pull request {{ number }} as the {{ specialist.name }} specialist.
    context_files:            # untrusted text goes here, never into prompt
      pr_body: "{{ pr_body }}"
    role: security            # optional: a role document, resolved project, then team, then instance
    skills: [kotlin-review]   # skill documents, resolved team then instance
    repo_skills: null         # true, false, or a list of names; null is true when skills is empty
    repo_rules: []            # more globs, like docs/rules/**, read from the default branch
    allowed_tools: null       # optional; null means the access level's default
    disallowed_tools: []      # adds to the access level's denials
    timeout_s: 1800           # per session; the whole process group is killed past it
    hooks: false              # only false is accepted
  output_mode: guided_json
  output_schema: {type: object, required: [verdict], properties: {verdict: {type: string}}}
  fan_out: 9
  partition_source: "{{ specialists }}"
  partition_key: specialist
  depends_on: [triage]
```

A `role:` names one role document by its key, never a template. The job's
bundle carries its text, without front matter, and the session's system prompt
file opens with it under `# Role`, ahead of the base commit's `CLAUDE.md`. A
role that no scope has fails the job before it starts, as a missing skill does,
and only a runner that reads bundle version 4 claims such a job.

Only `prompt` and `context_files` may hold templates. Inputs never render
into an `agent:` block at load time, so an input holding `{{ ... }}` renders
once. Each context file is written to `ctx/<name>.md` in the session's own
directory, or to `ctx/<name>` when the name ends in `.json`, and the directory
is passed with `--add-dir`. The prompt tells the model to treat
those files as data. The prompt reaches the CLI on stdin, never on its
command line.

Each session runs in a directory of its own with its own `HOME` and
`CLAUDE_CONFIG_DIR`. That directory's `settings.json` sets
`disableAllHooks`, and the CLI gets `--settings` with that file and
`--setting-sources user`, so no repository hook or setting loads. Skills are
written to `CLAUDE_CONFIG_DIR/skills/<name>/SKILL.md`, so a branch can't
shadow one. The environment is an allowlist: `HOME`, `CLAUDE_CONFIG_DIR`,
`PATH`, `TERM`, `LANG`, and the credential variables of the stage's provider.
It never holds the run's event token, the runner key, or other secrets.

On a runner started with `--agent-user`, each session runs as that OS user
through `sudo -n -u <agent user> --preserve-env=<names>`. The run passes the
user as `ARMATURE_AGENT_USER` and a session root outside the run directory as
`ARMATURE_AGENT_DIR`. The agent user's group gets read access to the session's
context files, skills, and read worktree, and write access to its home,
`TMPDIR`, config directory, and write clone. It can't read the run directory, the
bundle, or the run's environment. On Linux, `armature run` also marks itself
undumpable, so other processes of the job user can't read its memory.
`access: write` fails at run time without an agent user, unless the runner
was started with `--same-user`, which runs sessions as its own user with no
isolation. A runner advertises `claude-code` or `codex` only with
`--agent-user` or `--same-user`, and the server's own worker never takes an
agent job.

Every session gets `--permission-mode manual --permission-prompts none` and
`--strict-mcp-config`. `access: read` allows `Read(./**)`, `Grep(./**)`,
`Glob(./**)`, the same three on the context directory, `Skill`, read-only
`git` commands, `cd`, `ls`, and `wc`. It denies `Edit`, `Write`,
`NotebookEdit`, reads of `~/.claude*`, `~/.aws`, `~/.ssh`, `/proc`, `/sys`,
and `/etc/armature`, and git commands carrying `--out`, `--output`,
`--no-index`, `--ext-d`, `--ext-diff`, or `-c`. Its `allowed_tools` may only narrow that list.
`access: write` adds `Bash`, `Edit`, and `Write`, and denies edits under
`.claude/` and `.git/`. Saving a workflow with `access: write` needs a project
lead or higher.

When the run has a `repo` input, the stage clones it once per run and works in
a worktree at the `ref` input. The repo must be an `https://` URL on an
allowed host: `github.com`, or the hosts in the `agent.git_hosts` setting (a
list, the team's over the instance's). Local paths and `file://` are refused,
git may use only https, and redirects are off. Before any session starts,
`.claude/skills`, `.claude/agents`, `.claude/commands`, `.claude/hooks`, and
`.claude/settings*.json` are deleted from the worktree, so a branch can't add
commands or shadow a skill. A read session gets a worktree of the run's clone
that the agent user can only read, removed after the session. A write session
gets its own `git clone --shared` of the run's clone, so nothing it writes
reaches the run's clone. It keeps that clone, and its output carries the path
as `worktree`. The clone is built and shared in a directory no session can
reach, then moved into place, and sharing never follows a symbolic link. The
run's own git commands pass `core.hooksPath=/dev/null`,
`core.fsmonitor=false`, and an empty `credential.helper`, ignore global and
system config, and never run in a git directory a session could write. A `GITHUB_TOKEN` in
the run's environment reaches git as an `http.https://<host>/.extraHeader`
config value in the environment, never on a command line. Without a `repo`
input, the session works in the run's directory, or in its own home under an
agent user.

On a server, `repo` must be the project's configured repo, so a fork's URL
can't become the base. A bare `owner/name` means github.com. A URL must
match on host and path, ignoring case and a `.git` suffix. The `ref` input names one ref or
commit: `:`, `..`, `@{`, whitespace, control characters, and a leading `-`
are refused. The run pins the base commit when it first clones `repo`: the
commit the default branch points at, before any `ref` is fetched. The pin is
a file only the run's user can read, so every stage and the `--resume`
reprompt use the same commit. Reads of the base commit ignore git replace
objects. Two things in a
session come only from that commit, never from the branch under review:

- Rules. Each `CLAUDE.md`, `CLAUDE.local.md`, and `AGENTS.md` at any depth,
  in any letter case, `.claude/rules/**`, and the stage's `repo_rules` globs
  are deleted from the worktree, links included, and replaced with the base
  commit's copies. A link on the way to a base copy is removed too, and a
  copy that still can't be written fails the stage. Every `@` import in the
  worktree copies becomes plain text. In a write session git doesn't see the
  swap, so a commit never includes it. Each base `CLAUDE.md`, root first,
  reaches the session through `--append-system-prompt-file`, up to 200 KB,
  with its `@` imports inlined from the base commit, five levels deep at
  most. An import that is absolute, starts with `~`, leaves the repo, isn't
  a regular file there, or would pass the size budget stays as written.
- Repo skills. With `repo_skills` on, each directory under the base commit's
  `.claude/skills` is copied, scripts included, to
  `CLAUDE_CONFIG_DIR/skills/<name>/`, read-only to the session. A skill is
  refused if it holds a link or submodule, a file over 256 KB, or no
  `SKILL.md`, or if its name isn't a valid skill name. All repo skills
  together stay under 200 files and 5 MB. A `SKILL.md` must start with a
  `---` frontmatter line, after an optional byte order mark and with no
  blank line before it. The frontmatter is parsed as YAML and rewritten with
  only `name` and `description`. A skill whose frontmatter is missing or
  doesn't parse to a mapping is refused. A skill document of the same
  name wins. `repo_skills: true` loads every skill that passes, and a list
  loads only those names and fails the stage if one can't load. The default
  is on for a stage that names no `skills`. The session's log names each
  loaded skill and its source, and each one shadowed or refused.

A repo skill's scripts run with the session's access, so trusting the
default branch trusts everyone who can merge to it.

Read sessions keep the base rules for their whole run. In a write session,
the agent has `Bash`, `Edit`, and `Write`, so it can rewrite or restore a
rule or memory file itself, for example with `git checkout` of the branch's
copy. That's accepted by design: a write session's isolation is its agent
user and its allowlisted environment, not the content of its worktree.

The final `result` line of the stream decides the outcome, whatever the exit
status. With `output_mode: json` or `guided_json`, the reply is parsed,
fences stripped, and checked against `output_schema`. A reply that fails gets
one `--resume` reprompt with the error, then the stage fails. Each session
counts as one call toward `max_llm_calls`, reserved before it starts so a
fan-out can't overshoot, and its cost rolls into the run.
The stream becomes live-view events tagged with the fan-out item's `name` (or
the stage id) as `role`.

On a server, the job bundle carries each skill's current body and, for each
model alias, the model id and the provider's credential, for agent steps in
the workflow and in every workflow it calls. Skills, aliases, and
`agent.git_hosts` resolve project, then team, then instance, and a trigger
secret or setting can't replace them. A run from a server fails when a model
alias isn't in its bundle. Only a local run uses `model:` as a literal model
id with the credential variables of its own environment. A workflow with an
`agent:` step requires the runner capabilities `git` and its harness name.

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

`skip_if` and `condition` see this same cumulative context, so they can read
upstream stage outputs. A skip does not propagate: a skipped stage yields
`{"_skipped": true}` and its dependents still run, so guard them with
`skip_if: "{{ upstream._skipped }}"` when they should skip too.

### Template limits

Every template renders in a sandbox with limits. A render fails with
`TemplateLimitError` when any of the following happens:

- It runs past 1 second (`ARMATURE_TEMPLATE_DEADLINE_S`) or 100,000 loop
  iterations, calls, and lookups.
- Its output passes 1 MiB.
- It builds more than 16 MiB of strings in all.
- A single value or width passes its cap.

An input's value is never rendered as a template, and the server never
renders a spec's templates in its own process. These limits protect runs
against runaway templates that an author writes by mistake. They are best
effort. The hard limit is the memory cap on the process that runs the
workflow: `--memory-mb` on `armature runner`, `runner.memory_mb` for the
server's worker, and `dry_run.memory_mb` for test runs. macOS ignores that
cap.

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
| injected key | always present: `run_id`, `_transcript`, `_diagnostics`, `_stale_memory_keys`, `_memory_index`; conditional (when configured): `_memory`, `_knowledge`, `prior_run` | the harness-injected key set |

The effective policy is recorded on every trace row (`context_policy`).
Known limits: closed content can still flow transitively through intermediate
stage outputs (the `CONTEXT_TRANSIT_LEAK_RISK` warning only checks declared
`depends_on`, not transitive ancestors); tool-mediated reads (memory
navigation tools) are governed by `safety_rules`, not `never:`; and
`skip_if`/`condition`/`until` expressions evaluate against the pre-filter
context (truthiness only — no closed content reaches prompts or trace
previews, but a closed key can still steer control flow).

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
| `NO_EXECUTION_TYPE` | Add role / tool_call / gate / adapter / subagent_spec / call / agent |
| `UNDEFINED_MODEL_TIER` | Define the tier in model_tiers: |
| `UNDEFINED_ADAPTER` | Define the adapter in adapters: |
| `SIGNATURE_TYPE_MISMATCH` | Align types between upstream output and downstream input |
| `POST_RUN_TRANSCRIPT_OVERFLOW_RISK` | Add signature.input to the post_run stage |
| `CONTRACT_INPUT_MISSING_NAME` | Add name: to each contracts.inputs entry |
| `INVALID_REQUIREMENT` | Each `requires:` entry is a capability such as `llm`, `git`, or `platform:ios` |
| `CONTRACT_INVALID_TYPE` | Use string, boolean, integer, number, object, or array for a contracts input or output type |
| `CONTRACT_INVALID_DEFAULT` | Make the default match the declared type and enum |
| `CONTRACT_INVALID_ENUM` | Make enum a non-empty list of values of the declared type |
| `CONTRACT_OUTPUT_MISSING_FROM` | Give each typed output a `from:` template |
| `CALL_SELF` | A workflow can't call itself |
| `CALL_INVALID_WORKFLOW` / `CALL_INVALID_VERSION` | Use a plain workflow name and a version >= 1 |
| `CALL_CONFLICTING_EXECUTION` | Don't combine call: with role, tool_call, adapter, gate, or subagent_spec |
| `CALL_UNRESOLVED` | The called workflow or version isn't in the call bundle |
| `CALL_INVALID_CALLEE` | The called workflow doesn't load |
| `CALL_CYCLE` | Break the cycle of workflows calling each other |
| `CALL_UNKNOWN_INPUT` / `CALL_MISSING_INPUT` / `CALL_INPUT_TYPE` | Make with: match the callee's contracts.inputs |
| `AGENT_CONFLICTING_EXECUTION` | Don't combine an agent: mapping with role, tool_call, adapter, gate, subagent_spec, or call |
| `AGENT_TEMPLATE` | Remove templates from agent.model, role, skills, allowed_tools, and disallowed_tools |
| `AGENT_ROLE_NAME` | Name a role document by its key: letters, digits, `.`, `_`, or `-` |
| `AGENT_READ_TOOLS` | Use `access: write`, or keep `allowed_tools` within the read tools |
| `AGENT_SKILL_NAME` / `AGENT_UNKNOWN_SKILL` | Name a skill document that exists, in lowercase letters, digits, and hyphens |
| `AGENT_SKILL_SHADOWED` (warning) | A name in both skills and repo_skills loads the database skill |
| `AGENT_REPO_RULE` | Give each repo_rules glob as a path inside the repo, without `/`, `~`, or `..` |
| `AGENT_CONTEXT_FILE` | Name each context file with letters, digits, `_`, or `-` |
| `AGENT_SCHEMA_MISSING` | Add output_schema to a guided_json agent stage |
| `AGENT_TIMEOUT` | Set agent.timeout_s above 0 |
| `AGENT_WRITE_FAN_OUT` (warning) | Each fan-out item gets its own writing session and worktree |
| `AGENT_EXPERIMENTAL` (warning) | The codex harness can't run yet |
| `LOOP_INVALID_TIMEOUT` | Set loop.timeout_s above 0 |
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
| `CLOSURE_STAGE_UNDEFINED` | `closure.stage` names a stage the spec doesn't have |
| `CLOSURE_STAGE_NOT_GUIDED_JSON` | the closure stage must use `guided_json` with an `output_schema` |
| `CLOSURE_SCHEMA_INVALID` | closure schema must require `reason` with a non-empty enum ⊆ `CLOSURE_REASONS` |
| `MISSION_NO_WORK_UNITS` *(warning)* | mission validation: no work units — it can never run anything |
| `DUPLICATE_WORK_UNIT` | mission validation: unit id defined more than once |
| `UNKNOWN_WORK_UNIT` | mission validation: `requires:` names a unit id that doesn't exist |
| `MISSION_BUDGET_CONFLICT` | mission validation: unit `max_budget_usd` exceeds mission `budget_usd` |
| `WORKFLOW_NOT_REGISTERED` | mission validation: a path-like `workflow:` doesn't resolve |
| `WORKFLOW_UNVERIFIED_NAME` *(warning)* | mission validation: bare workflow name resolves at the executor |
| `MISSION_FIELD_INVALID` | mission validation: blank unit id, `max_attempts < 1`, or negative `timeout_hours` / `max_budget_usd` / `budget_usd` |

---

## CLI Quick Reference

```bash
armature validate spec.yml                         # always run before armature run
armature mission validate my_mission.mission.yml   # validate a mission document
armature mission status my_mission.mission.yml      # unit states + recent transitions
armature mission run my_mission.mission.yml unit_a  # run one unit's workflow
armature mission advance my_mission.mission.yml    # pure readiness --json for machines
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

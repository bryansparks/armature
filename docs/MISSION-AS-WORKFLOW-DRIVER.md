# Mission as Workflow Driver

One document above the workflows. The workflows become capabilities; the document drives them.

---

[Mission as Context](MISSION-AS-CONTEXT.md) made every agent in *a* workflow see the same goal. But a real objective — *launch the campaign by Nov 15* — is almost never one workflow. It's a human approval, a batch of headline drafts, a refinement loop, a review, all waiting on each other, each run by a different spec, some needing a person's judgment and some fine to leave running unattended. The `mission:` string couldn't span them: it lives inside a single spec.

The missions grammar promotes it. Twice.

1. **The mission becomes a document.** A mission document holds the larger objective plus a list of *work units* — each unit naming which workflow accomplishes it.
2. **The document drives the workflows.** Units declare order (`requires:`), autonomy (`posture:`), and ceilings (`max_budget_usd:` / `timeout_hours:` / `max_attempts:`). In aggregate, the units describe the shape of the whole initiative.

---

## The two YAML types, side by side

| | Workflow spec | Mission document |
|---|---|---|
| What it is | A *capability*: "given inputs, produce this" | The *intent*: "here is the work to be done" |
| File | `my_workflow.yml` | `my_mission.mission.yml` |
| Unit of execution | One run | One work unit, potentially many runs/reattempts |
| References | Stages, adapters — never a mission | Work units, each referencing a **workflow** |
| Validated by | `armature validate` | `armature mission validate` |

The direction of reference is one-way and deliberate: a workflow never references a mission. The same workflow spec then serves many work units of many missions — the workflow is the capability, the work unit is the intent.

The seam between them is the spec's new optional `mission_source: work_unit`. With it, a run executed *for a work unit* renders the `[Workflow Mission]` context layer from the work record instead of the static string: every stage in the run inherits the mission's `objective` plus its unit's `title` and `objective`. That is aggregate coherence across a flock of different workflows, with none of them hardcoding it. (The runtime wiring is slice 2; slice 1 ships the grammar.)

---

## A worked example

`examples/missions/campaign-pretzel.mission.yml`:

```yaml
name: campaign-pretzel
objective: |
  Launch the Dangerous Pretzel ad campaign by Nov 15. Brand voice:
  chaotic-good, never earnest. Total budget is $200 all-in.

posture: human-led          # default for every unit that doesn't override
budget_usd: 200.0

work:
  - id: brand-approval
    title: Human approves brand voice examples
    workflow: ../06_human_in_the_loop.yml
    posture: human-led      # never auto-re-driven

  - id: hero-headlines
    title: Write hero headline variants
    objective: 12 variants across 3 tones, each under 40 characters.
    workflow: ../11_iterative_refinement.yml
    requires: [brand-approval]
    posture: delegated      # this unit may be re-driven by an executor
    max_budget_usd: 1.0
    timeout_hours: 1.0
    max_attempts: 2
```

A work unit **outlives a run**. `hero-headlines` may be attempted, fail, retry, and succeed across runs — the identity is the unit's `id`, not any single run's id. That is the property the whole design buys from OpenRig.

---

## Posture: two levels, deliberately narrow

Precedence is fixed in exactly one place: **unit override → mission default → `human-led`.**

- `human-led` (default): executors may **notify** on this scope's state changes, never act — no re-drive, no resume, no follow-on spawn.
- `delegated`: an executor may re-drive retry-pending units, unblock units whose dependencies completed, and spawn closure-declared follow-ons — always within `max_attempts`, `timeout_hours`, and the remaining `budget_usd`.

The semantics are kept as narrow as OpenRig's `grantsAuthority: false`: delegation is a grant of *execution*, never of authority over the mission itself.

Budget is metered from the runner's observed cost in the run receipt — armature-side and provider-independent, never a vendor balance check.

---

## The lifecycle

Each work unit moves through an armature-owned state machine:

```
pending → in_progress → done
                    ├→ failed        (attempt < max_attempts → retry-pending)
                    ├→ blocked_on    (dynamic: a closure may add/remove dependencies)
                    ├→ handed_off    (closure names follow-on work)
                    ├→ escalation    (resting state: needs a human)
                    └→ canceled
```

Every transition produces an append-only transition record — an audit trail, not just a current-state pointer. **Slice 1 ships the grammar and its static validation** (references, cycles, budgets — the rules you can check before anything runs); transition *enforcement* and persistence arrive with the `WorkStore` in slice 2.

---

## Closures: you cannot silently finish a task

The rule that makes the flock self-coordinating lives in the *workflow* spec, as an optional top-level section:

```yaml
closure:
  stage: final_report     # a guided_json stage; its schema must satisfy
                          # the built-in closure contract
```

The named stage's `output_schema` must require a `reason` drawn from a fixed set — `done_no_follow_on`, `handed_off`, `blocked_on`, `escalation` — and may carry `notes` and a `follow_on` array of mini work-unit specs. One run's judgment becomes more work, as typed output, with the engine doing the bookkeeping: `handed_off` upserts the follow-on units into the mission as new `pending` work; `blocked_on` rewrites dependencies.

A run whose spec declares no closure yields `done_no_follow_on`. Silence is an explicit default — and *declared* silence is the armature-native form of OpenRig's "you cannot silently finish a task."

---

## Validation

`armature mission validate my_mission.mission.yml` reports all issues; warnings never fail the exit code.

| Code | Fix |
|---|---|
| `MISSION_NO_WORK_UNITS` *(warning)* | A mission with no work can never run anything |
| `DUPLICATE_WORK_UNIT` | Unit ids must be unique within the mission |
| `UNKNOWN_WORK_UNIT` | A `requires:` entry names a unit id that doesn't exist |
| `CIRCULAR_DEPENDENCY` | Remove the cycle (self-reference included) |
| `MISSION_BUDGET_CONFLICT` | Unit `max_budget_usd` exceeds the mission's `budget_usd` |
| `WORKFLOW_NOT_REGISTERED` | A path-like `workflow:` doesn't resolve to a file |
| `WORKFLOW_UNVERIFIED_NAME` *(warning)* | Bare registry names resolve at the executor; can't verify locally |
| `CLOSURE_STAGE_UNDEFINED` | `closure.stage` names a stage the spec doesn't have |
| `CLOSURE_STAGE_NOT_GUIDED_JSON` | The closure stage must use `guided_json` with an `output_schema` |
| `CLOSURE_SCHEMA_INVALID` | The closure schema must require `reason` with a non-empty enum ⊆ the closure-reason set |

---

## Compared to alternatives

**A `missions:` section inside a workflow spec** was rejected (design §9 Q1): it can't span workflows, and it would tie the objective to one capability — the exact coupling this design exists to remove.

**OpenRig's queue** is the concept source — the durable work unit, the audited state machine, closure-reason discipline, the operating posture. Its *implementation* (a TypeScript/Node daemon, tmux sessions, a weaker workflow runtime) is explicitly not adopted: armature's grammar carries the concepts, and executors (local run, dispatch) store and schedule what the grammar defines. There is no daemon; the state store and scheduled sweeps replace it.

---

*The mission document is the intent; the workflows are the capabilities; the closure contract is how one run hands work to the next.*
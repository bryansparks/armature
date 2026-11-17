# Resident workflows: long-lived workflow instances and the idle loop

Status: implemented 2026-11-04 — phases 1–5 complete (phases 6–7 remain
open). Proposed 2026-11-03; reviewed and decided 2026-11-03 (see "Decision
record"). Owner: Bryan Sparks.

This document is written to be handed to a planning session. It states the
problem, records what the code already does (with file and symbol names), fixes
the decisions that are settled, and lists the ones still open. A plan built from
it should be phased so each phase ships on its own, as `DESIGN.md` requires.

## Goal

Some workflows should exist for a long time instead of for one run. People have
asked for two things:

1. **Ping a workflow for analytics.** "How many vendor contracts has the intake
   workflow triaged this week, and how many are waiting?" They want an
   addressable thing that answers from what it has accumulated, without
   rerunning anything.
2. **A workflow that wakes on requests.** When a new request arrives (webhook,
   Slack, API, another workflow), a workflow agent that already holds context
   about this stream of work handles it, then goes back to waiting.

An operating system spends most of its time in an idle loop, waiting for work
and scheduling it, and uses spare cycles for housekeeping. Armature should have
the same shape for workflows: an instance that waits, wakes for events, and does
bounded background work when it has nothing else to do.

A **resident workflow** is a workflow version plus a durable **instance**: an
identity, state that survives across runs, an inbox of events, and declared
handlers. Every unit of work it does is still an ordinary, short, ephemeral run.

## What exists today

Armature is already a persistent server. What isn't persistent is the workflow:
nothing outlives one run except trace history, memory, and the database rows
the server keeps about runs. The pieces below are the ones this design reuses.

### The server and the worker

- `armature server` runs the API, UI, event broker and scheduler in one process
  (`DESIGN.md`, "Process layout"). `armature worker` and remote runners execute
  jobs.
- `armature/server/worker.py`: `Worker._loop()` calls `tick()` every
  `self.interval`. `tick()` heartbeats, reaps expired leases, settles finished
  jobs (`finish`), expires and prunes gates, runs `conversations.sweep`, syncs
  gate cards (`gatecards.sync`), then claims queued jobs up to the slot limit
  and launches each as a subprocess (`dryrun.spawn`). **This is already the
  idle loop.** Resident scheduling plugs into it as one more sweep.
- Claims are one compare-and-set `UPDATE ... WHERE status = 'queued'`, so
  several servers can share one database. A job whose lease runs out is settled
  from its run if the run ended, otherwise marked lost, and **never queued
  again**.
- `worker.queue_job(...)` queues a job at the workflow's current version with
  its callees pinned (`call_pins`). It already supports:
  - `key` → `jobs.concurrency_key`: jobs of one workflow with the same key run
    one at a time.
  - `not_before`: debounce.
  - `supersede` / `sticky`: a new job replaces the key's queued ones.
  - `cancel_running`, `thread`, `dry_run`, `rerun_of`, `requested_by`.

### Triggers

- `armature/server/pollers.py`: `KINDS = ("jira_poll", "linear_poll", "schedule")`.
  A schedule is a cron expression with a timezone. `trigger_state.next_run_at`
  is claimed with a compare-and-set, so servers share the polling.
- `armature/server/webhooks.py`: rules are `triggers` rows of kind `webhook`.
  `_decide()` evaluates events and conditions and returns a decision.
  `evaluate()` queues jobs. A rule can already route to a **conversation**
  (`config.conversation`) instead of queueing a plain job; that branch is the
  model for routing to a resident.

### Conversations: a resident workflow specialized for chat

`armature/server/conversations.py` already implements the actor pattern this
design generalizes:

| Conversation piece | What it is | Resident equivalent |
|---|---|---|
| `conversations` row | identity, team/project, `workflow_id`, `state` (`open`/`closed`), `data` JSON, `turns` | `residents` row |
| `conversation_messages` (`seq`, `handled`) | ordered inbox; a turn handles all messages up to `message_seq` | `resident_events` |
| `active_job_id` | the lock: one turn at a time, taken with one conditional UPDATE | same |
| `turn()` | queue one job for the newest unhandled message unless locked or capped | `wake()` |
| `settle()` | when the turn's job ends: mark handled, clear the lock, queue the next turn | same |
| `conversation.*` broker tools (`history`, `reply`, `ask`, `set`, `decide`) | the only way a run touches its conversation, and only while it holds the lock | `resident.*` tools |
| `_set()` with `STATE_KEY`, `STATE_MAX = 4096`, `STATE_KEYS_MAX = 20` | bounded, code-validated state written by the run | instance state |
| `IDLE = 7 days`, `max_turns`, `turns_per_day` | lifetime and cost caps | `lifetime`, `budget` |
| `gated()`: a conversation workflow may not have a `human` gate | a turn must end when it answers | same rule for event turns (see "Gates") |

Workflow inputs for a turn are `{"conversation_id", "message_seq"}`. The run
reads everything else through tools, so the server keeps control of the
conversation. Residents keep that property.

### The "no daemon" principle

`docs/MISSION-AS-WORKFLOW-DRIVER.md`: "There is no daemon; the state store and
scheduled sweeps replace it." This design keeps that principle. A resident is
**not** a process. It is rows plus a sweep. Its runs are ordinary jobs.

### Memory and continuation

- `MemoryConfig` (`armature/spec/models.py`) defaults to a SQLite file at
  `~/.armature/memory/{name}.db`. That is **local to the machine that runs the
  job**. Server jobs land on different workers and runners, so this memory
  can't be a resident's state. See "Questions" (settled: state-as-memory in v1).
- `ContinuationConfig.carry_forward` injects `prior_run` state. It's keyed to
  the workflow, not to an instance.
- `loop.carry_forward` carries state between loop iterations inside one run.

## Why not a process that stays running

The obvious design is an agent process in a `while True` loop that holds its
context and waits on a queue. It fights the current architecture at every point:

- **Grants.** Broker grants are per run and per step, signed with an HMAC, and
  checked against "is this run still open". Per-run event tokens expire after
  48 hours or when the run ends (`runners.py`). A process that never ends would
  need its grants refreshed forever, which weakens the governance model.
- **Crashes.** The lease and reap rules never re-queue a lost run, so nothing is
  paid for twice. A crash would lose whatever lived only in memory.
- **Context.** A context window that keeps growing degrades and gets expensive.
  Short turns over state the server summarizes stay sharp.
- **Cost while idle.** A resident process holds a slot and possibly a model
  session while doing nothing.
- **Upgrades.** Changing the spec would mean migrating a live process. With
  turns, the next turn just runs the new pinned version.

**Decision:** resident = durable instance + ephemeral turns. A warm runner
(process and prompt cache kept hot for a resident) may come later as a latency
optimization. It must not change semantics. Out of scope here.

## Terms

- **Resident workflow:** a workflow whose spec has a `resident:` block.
- **Instance:** one live occurrence of a resident workflow, with an
  `instance_key`. A singleton resident has one instance; a keyed resident has one
  per key (per vendor, customer, repository, Jira epic).
- **Event:** one item in an instance's inbox: `request`, `ask`, `schedule`,
  `idle`, `signal`, or a custom kind.
- **Turn:** one job that handles one or more events for one instance. A turn is
  an ordinary job and run.
- **State:** a bounded JSON document owned by the instance. Turns read it as
  input and change it only through broker tools.
- **Metrics:** the part of state published for `read` queries. Served by the
  server with no run.
- **Idle turn:** a turn the sweep queues because the inbox has been empty long
  enough and the idle policy allows it.

## Design

### Lifecycle of an instance

```
            event arrives (webhook / poller / API / workflow / Slack)
                              │
                              ▼
   ┌──────────┐   create   ┌──────────┐  wake(): lock free, caps ok  ┌──────────┐
   │ (absent) │ ─────────▶ │  idle    │ ───────────────────────────▶ │ running  │
   └──────────┘            └──────────┘                              └──────────┘
                              ▲   │ sweep: inbox empty ≥ idle.after         │
                              │   └──── queue idle turn ─────────────────────┤
                              │                                             │ job ends
                              │      settle(): mark events handled,         │
                              └────── apply state, clear lock, wake again ◀─┘

   any state ──(lifetime exceeded / closed by tool / admin)──▶ closed
   any state ──(admin pause / budget exhausted)──────────────▶ paused ──(resume)──▶ idle
```

Instance states: `idle`, `running`, `paused`, `closed`. `running` means
`active_job_id IS NOT NULL`; it's derived, not stored separately, so a lock and
a state can't disagree.

### Spec grammar

A new optional top-level block on `HarnessSpec` (`armature/spec/models.py`), as
a Pydantic model with `extra="forbid"`:

```yaml
name: vendor-intake
resident:
  instance_key: "{{ event.payload.vendor_id }}"  # omit → singleton. Dotted refs only (stored-template rule).
  concurrency: 1                                  # turns per instance at once; 1 is the default and the v1 limit
  batch: 10                                       # max events one turn handles
  state:
    schema:                                     # optional inline JSON Schema; validated on every write
      type: object
      required: [vendor_id]
      properties:
        vendor_id: {type: string}
    max_bytes: 16384
    publish: [metrics]                            # top-level keys served by `read`
  on:
    request:  { workflow: self }                  # this workflow's own stages handle it
    ask:      { workflow: vendor-intake-answer }  # another stored workflow, pinned like a call
    schedule: { cron: "0 6 * * *", timezone: America/Denver, workflow: vendor-intake-rollup }
    signal:   { workflow: self }
  idle:
    after: 30m                                    # inbox empty this long → idle turn may run
    workflow: vendor-intake-housekeeping
    budget_usd_per_day: 2.00
    max_turns_per_day: 24
    backoff_factor: 2                             # applied when an idle turn reports nothing_to_do
    backoff_max: 12h                              # …and never past this
  lifetime:
    close_after_idle: 30d                         # no REAL events for this long → closed
    max_turns: 5000
  close_on: { signal: pr_merged }                 # a matching named signal/custom event closes the
                                                  # instance with no turn spent (close_reason "close_on")
  budget:
    usd_per_day: 25.00                            # all turns of one instance
```

Validation rules (`armature/spec/validator.py`):

- `on` keys come from a closed set: `request`, `ask`, `schedule`, `signal`, plus
  `custom.<name>`. `idle` is configured under `idle:`, not `on:`.
- `workflow: self` or a workflow name matching `calls.WORKFLOW_NAME_RE`.
  Named handlers resolve and pin exactly as `call:` stages do (`call_pins`), so
  queueing fails early with the same `ValueError` path `queue_job` uses.
- `instance_key` is a stored template and must follow the sandboxed-template
  rule in `DESIGN.md` (a dotted or indexed reference, nothing else).
- `schedule.cron` validates with the same parser `pollers.next_cron` uses.
- A handler workflow for any event kind must not have a `human` gate (reuse
  `worker.gated`). See "Gates".
- Durations parse with one helper (`30m`, `12h`, `30d`).

**Why handlers name workflows.** A stored workflow's `{{ }}` may hold only a
dotted reference, so a single workflow can't branch on
`{{ event.kind == 'idle' }}`. Pointing a handler at its own workflow keeps each
handler simple and testable, reuses the call-pinning machinery, and lets one
resident mix cheap and expensive handlers. For residents that prefer one
workflow, the server also injects precomputed booleans, so
`condition: "{{ _event.is.idle }}"` works with a dotted reference.

### Turn inputs and the context a turn sees

A turn's job `inputs`:

```json
{ "resident_id": "…", "instance_key": "acme", "event_seq_from": 41, "event_seq_to": 44,
  "kind": "request",
  "_event": { "kind": "request", "name": null, "fields": { "number": 7, "sha": "abc123" },
              "is": { "request": true, "idle": false, … } } }
```

`_event.fields` holds the declared scalar fields the batch's webhook rules
mapped (see "Routing events to residents"), merged over the batch in `seq`
order so the newest value wins per key. Events without fields add nothing,
and a batch with none gives `{}`.

As with conversations, the run reads everything else through tools, so the
server stays in control. The engine injects:

- `_resident.state`: the instance state at claim time, read-only in the
  context. Changes go through `resident.state.set` / `resident.state.patch`.
- `_resident.events`: the batch of events, fenced as data under a tag (same
  rule as conversation messages: inputs from outside never read as
  instructions).
- `_resident.meta`: `turns`, `created_at`, `last_turn_at`, `idle_streak`.

### Broker tools

These are added to the broker alongside `conversation.*`
(`armature/server/broker.py`, `_conversation_tool` is the template). They're
usable only by a run whose job is the instance's `active_job_id`.

| Tool | Effect |
|---|---|
| `resident.events` | read the turn's events (and optionally the last N handled ones) |
| `resident.state.get` | read state |
| `resident.state.set` / `resident.state.patch` | replace or JSON-merge-patch state; validated against schema and `max_bytes`; staged, applied by `settle()` only if the turn succeeds |
| `resident.metrics.publish` | shorthand for patching the published keys |
| `resident.reply` | answer the event that asked (API caller, Slack thread, calling workflow) |
| `resident.emit` | queue an event on another instance or resident (bounded fan-out, audited) |
| `resident.idle.report` | `{nothing_to_do: true}` makes the idle policy back off; `{did: "..."}` resets it |
| `resident.close` | close this instance with a reason |

**Decision: state writes are staged and applied atomically in `settle()`, and
only when the job succeeded.** A failed or lost turn leaves state as it was, and
its events go back to unhandled for the next turn (bounded by an attempts count
per event, so a poison event can't loop). This differs from `conversation.set`,
which writes immediately. The plan should decide whether conversations move to
staged writes when they're re-based (see phase 6).

### Data model

New tables in `armature/server/db/schema.py` plus an Alembic migration in
`armature/server/migrations/versions/` (next number after `0026_job_rerun_of`).
SQLAlchemy Core only, with no backend-specific SQL, tested on SQLite, MySQL 8
and Postgres 16+.

```python
RESIDENT_STATES = ("idle", "paused", "closed")
RESIDENT_EVENT_KINDS = ("request", "ask", "schedule", "signal", "idle", "custom")

residents = Table(
    "residents", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False),
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("workflow_id", String(36), ForeignKey("workflows.id"), nullable=False),
    Column("workflow_version", Integer, nullable=False),       # pinned at creation (decision 3)
    Column("instance_key", String(191), nullable=False),        # "" for a singleton
    Column("state", String(8), nullable=False, default="idle"),
    Column("data", JSON),                                        # the instance state
    Column("data_version", Integer, nullable=False, default=0),  # bumped by every applied write
    Column("staged", JSON),                                      # writes from the running turn
    Column("active_job_id", String(36), ForeignKey("jobs.id"), index=True),
    Column("turns", Integer, nullable=False, default=0),
    Column("idle_streak", Integer, nullable=False, default=0),
    _ts("next_idle_at", nullable=True),
    _ts("last_event_at", nullable=True),
    _ts("last_turn_at", nullable=True),
    Column("spent_usd_day", Numeric(12, 4), nullable=False, default=0),
    Column("spent_day", String(10)),                             # UTC ISO date the counter is for
    _ts("created_at"), _ts("updated_at"), _ts("closed_at", nullable=True),
    Column("close_reason", String(32)),
    UniqueConstraint("workflow_id", "instance_key"),
    Index(None, "state", "next_idle_at"),
    CheckConstraint(_in("state", RESIDENT_STATES), name="state"),
)

resident_events = Table(
    "resident_events", metadata,
    _id(),
    Column("resident_id", String(36), ForeignKey("residents.id"), nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("name", String(64)),                                  # for custom.<name>
    Column("payload", JSON),
    Column("source", JSON),                                      # trigger_id / delivery_id / user / workflow run
    Column("reply_to", JSON),                                    # where resident.reply answers
    Column("dedupe_key", String(191)),                           # idempotent ingest (e.g. Slack ts, delivery id)
    Column("handled", Boolean, nullable=False, default=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("handled_by_job", String(36)),
    _ts("created_at"),
    UniqueConstraint("resident_id", "seq"),
    UniqueConstraint("resident_id", "dedupe_key"),
    CheckConstraint(_in("kind", RESIDENT_EVENT_KINDS), name="kind"),
)
```

`jobs.kind` has no check constraint today (default `"workflow"`), so a
`"resident"` value would need no migration. Even so, **add a nullable, indexed
`jobs.resident_id` column**, so `settle()` finds turns with a join instead of
parsing `inputs`, as `conversations.settle` joins on `active_job_id`.

Correction from review: `Numeric` is already imported in `schema.py` (used
for `runs.cost_usd`). Either way, store spend as integer micro-dollars in a
`BigInteger`: it avoids decimal-handling differences between backends.
**Prefer micro-dollars.**

Assigning `seq`: take the instance row lock (`_locked()` pattern, `SELECT ... FOR
UPDATE` where supported, a guarded UPDATE on SQLite) and use `max(seq)+1`,
exactly as `conversations.append` does.

### The module: `armature/server/residents.py`

It mirrors `conversations.py`'s functions:

- `ensure(conn, workflow_id, instance_key, now) -> row`: get or create
  (insert, then catch the unique violation and select).
- `enqueue(conn, resident_id, kind, payload, *, source, reply_to, dedupe_key, now) -> seq | None`:
  append an event (`None` on a duplicate `dedupe_key`), bump `last_event_at`,
  reset `idle_streak` and `next_idle_at`, then call `wake()`.
- `wake(conn, resident_id, now) -> job_id | None`: the equivalent of `turn()`.
  Under the row lock: return if locked, paused or closed. Check `lifetime`,
  `budget.usd_per_day` and `max_turns` (closing or pausing with a reason as
  `turn()` does). Pick the handler for the **oldest** unhandled event's kind,
  take up to `batch` contiguous events of the same handler, then
  `queue_job(..., key=f"resident:{id}", inputs=..., thread=...)` and take the
  lock with a conditional UPDATE. If the UPDATE loses the race, delete the job,
  as `turn()` does.
- `settle(conn, now) -> list[resident_id]`: for each resident whose
  `active_job_id` job left `OPEN_TURN`:
  - On success: apply `staged` to `data` (schema check again), bump
    `data_version`, mark events up to `event_seq_to` handled, and add the run's
    cost to `spent_usd_day`.
  - On failure: discard `staged`, `attempts += 1` on the turn's events, and
    mark events past `max_attempts` handled with an error record.
  - Then clear the lock, set `last_turn_at`, and call `wake()` again.
- `sweep(engine, now)`: called from `Worker.tick()` next to
  `conversations.sweep`, inside its own `try` so a failure never blocks claims.
  It runs `settle`, then the idle scheduler, then closes residents past
  `close_after_idle`.
- `call_tool(engine, *, run_id, job, tool, params, now)`: the `resident.*`
  tools, bound to the run through the job, as `conversations.call_tool` is.

### Ping: `read` vs `ask`

These are two different operations and must stay separate.

- **`read`: no run, no model, no cost.**
  `GET /api/residents/{workflow}/{instance_key}/read` (and a `?keys=` filter)
  returns the published keys of `data`, plus `data_version`, `last_turn_at`,
  `turns`, the inbox depth and `state`. It's served from the row under the same
  RBAC as reading the workflow's runs. This is what dashboards and analytics
  polls should hit; spending a run every time a dashboard refreshes is the thing
  to avoid. Also `GET /api/residents/{workflow}` lists instances with their
  metrics, for the UI.
- **`ask`: a turn.** `POST /api/residents/{workflow}/{instance_key}/ask` with
  `{question}` enqueues an `ask` event with `reply_to` set to the request.
  - Async mode returns `{event_seq, job_id}` at once; the caller polls
    `GET .../events/{seq}` for the reply the turn records, reconciling on
    reload.
  - Sync mode (`?wait=N`, default 30s, capped at 60s) polls the asking
    event's row for the `resident.reply` the turn's tool writes and returns
    the reply — or `null` when the wait runs out, leaving the caller to
    poll the event later.
- **`signal`:** `POST .../signal` with `{name?, payload}` (for a `custom.*` kind
  or a plain signal). Requires contributor access on the workflow.

All three are audited (`audit.record`, action `resident.read|ask|signal`).
`read` should be rate-limited per caller but needs no budget.

### Routing events to residents

- **Webhook rules** (`webhooks._decide`): add `config.resident: true`
  (parallel to `config.conversation`). The decision computes `instance_key`
  from the payload using the resident's `instance_key` template and calls
  `residents.enqueue(kind="request", dedupe_key=delivery_id)` instead of
  `queue_job`. Shadow and dry rules behave as today: shadow records the decision
  only; dry enqueues an event whose turn runs as a dry run (copy `dry_run` onto
  the event and through to the job). A resident rule locks only the delivery
  row, so rule-level `concurrency` config means nothing for residents; the
  instance's own one-turn lock (`active_job_id`) is the real serialization.
  A resident rule's `inputs` declare **fields**: at most 8, each a `$.path`
  into the payload (no bare `$`, no literals). Only a string of at most 200
  characters, a whole number, `true`, `false`, or `null` is kept; anything
  else is dropped whole, never truncated, and named in the decision's
  reason. A missing path is left out. The kept fields ride on the event as
  `source.fields` and reach the turn as `_event.fields`; payloads stay
  fenced. A rule with `signal: <name>` enqueues a `signal` event of that
  name instead of a `request`, deduped on `delivery_id:name` so a request
  rule matching the same delivery can't swallow it. When the pinned spec's
  `close_on` names the signal, the instance closes with no turn and its
  turn ends: a queued one is superseded at no attempt cost, a running one is
  asked to stop unless it emitted the closing signal itself.
  `signal` needs `resident: true` and can't be `cancel_only` or `dry`.
- **Pollers** (`pollers.py`): a `schedule` from a resident's `on.schedule` is
  materialized as a `triggers` row of kind `schedule` with
  `config.resident = true` when the workflow version is saved or activated, so
  it reuses `trigger_state` claiming. When it fires, it enqueues a `schedule`
  event on each **open** instance (keyed residents) or the singleton. Jira and
  Linear polls get the same `config.resident` option, with the issue key as the
  `dedupe_key`. A resident schedule trigger is **spec-owned**: the save path
  (`routes_defs._sync_schedule`) materializes, updates, or removes it whenever
  a version with a `resident.on.schedule` is saved, created, forked, or rolled
  back, so users never create or edit one by hand — and requeue on a resident
  trigger is refused (409), as its issues route to instances, not jobs.
- **Other workflows:** the `resident.emit` tool, and a declarative stage
  `emit: {resident: vendor-intake, key: "{{ vendor_id }}", kind: signal}` if the
  plan finds that cleaner than a tool. Bounded by a per-run emit cap.
- **Slack:** routing a Slack thread to a resident is what conversations already
  do. Leave Slack to conversations until phase 6.

### The idle loop

`residents.sweep` runs every tick and finds idle candidates with one indexed
query:

```
state = 'idle' AND active_job_id IS NULL AND next_idle_at <= now
```

When an instance settles with an empty inbox, `next_idle_at` is set to
`last_activity + idle.after * factor^idle_streak`, capped at `backoff_max`.

For each candidate (claimed with a compare-and-set on `next_idle_at`, as
pollers claim `next_run_at`, so two servers can't both queue it):

1. Skip and push `next_idle_at` to tomorrow when the instance hit
   `idle.budget_usd_per_day` or `idle.max_turns_per_day`.
2. Enqueue an `idle` event (payload: `idle_streak`, time since the last event,
   the inbox high-water mark) and call `wake()`. The idle event is written by
   a separate path from `enqueue()`: it must **not** bump `last_event_at` and
   must **not** reset `idle_streak` — otherwise the backoff could never grow
   past one factor and `close_after_idle` could never fire. Lifetime measures
   real events only.
3. When the idle turn calls `resident.idle.report({nothing_to_do: true})`,
   `settle()` increments `idle_streak` (longer backoff). Any other report, or a
   real event arriving, resets it to 0.

**Priority:** real events always come first. `wake()` picks the oldest
unhandled event; an idle event queued just before a request still runs, but
`wake()` should **drop a pending idle event when real events are waiting**
(mark it handled, `skipped`). Idle work never delays real work by more than one
turn.

**What idle turns are for** (examples for docs and starter packs; the design
doesn't hard-code them):

- Precompute the published `metrics` so `read` is fresh and free.
- Consolidate memory: summarize handled events into state, drop detail,
  reconcile KnowledgeStore facts.
- Self-improvement: when `self_improvement.min_traces` is reached, run the
  improve cycle for the resident's workflow (this is the `watch --tune` daemon
  on the roadmap, done as an idle turn). Proposals still go through review as
  they do today.
- Pick up mission work units in `ready` state (`state/work.py`) when the
  resident drives a mission.
- Check on external things it's waiting for (a PR merged, a ticket moved), for
  sources with no webhook.

Idle turns are read-only toward the outside world: they may fetch external
state and write their own state, memory and metrics, and they may ask the
owner a question; any other outward write is refused (decision 2).

There's also a **server-level idle loop**: a worker with free slots and no
claimable jobs is idle. A plan may later let instance-wide idle work (memory
compaction across residents, trace export) run only then. Note it here; don't
build it in v1.

### Gates

A `request` or `ask` turn must answer, and a `human` gate pauses a run for
hours, holding the instance lock the whole time. v1 rule (same as
`conversations.gated`): handlers for **any** event kind — `request`, `ask`,
`signal`, `idle` and `schedule` — can't have a `human` gate; validation
rejects it. The lock-release-and-reacquire dance a gated turn would need is
deferred until a use case demands it (decision record, question 4). A turn
that needs a human mid-flight asks through the owner channel instead
(decision 2): it ends, and the answer arrives as a new event.

### Version pinning and upgrades

- **An instance runs the workflow version it was created with**
  (`residents.workflow_version`, set by `ensure()`). In-flight instances
  drain on that version and close on their own; only **new** instances pick
  up a newly saved version. This is decision 3: mid-life instances are
  deliberately shielded from spec churn, and short-lived instances (per PR,
  per issue) drain out naturally, so there is no state migration to do.
- The cost of that stability: a spec fix does not reach in-flight instances.
  **Escape hatch:** an admin "upgrade instance" action re-pins one instance
  (or a batch) to the current version; state carries forward unchanged and
  the next turn uses the new version.
- If the current version's `state.schema` rejects an instance's `data` —
  at upgrade-instance time, or at `ensure()` of a new instance reusing a
  still-open key — the instance goes `paused` with
  `close_reason = "state_schema"`, and the UI shows a migrate/reset action.
  Optional `resident.state.migrate: <workflow>` names a one-shot workflow
  that receives the old state and returns the new one; the plan decides
  whether migration is v1 (question 6 says: later).
- Removing the `resident:` block pauses all instances. It never deletes them.
- Server code upgrades are hot-safe for in-flight runs (leases, heartbeats,
  jobs are subprocesses). The engine *package* the worker executes is the
  real version dependency — that is what the dispatch pin discipline
  manages.

### Governance and safety

- Turns are ordinary jobs, so per-step grants, the broker, sandboxing, safety
  rules, strict mode, gates, HQS and traces apply unchanged.
- External input (event payloads) reaches the model fenced as data, never as
  instructions, as conversation messages are.
- State is written only through validated tools, size-capped and
  schema-checked. A run can't write another instance's state; `resident.emit`
  only enqueues an event, and the other instance decides what to do with it.
- Budgets are enforced by the server in `wake()` and the idle sweep, not by the
  model. They fire only when a turn would actually run: a paused or
  over-budget instance holds its events, and nothing fires on an event's
  arrival alone.
- `emit` fan-out is capped per run and per instance per day, to stop residents
  from waking each other in a loop. Record the causal chain in `source`, and
  refuse an event whose chain is deeper than N (default 5).
- Audit: instance created, paused, resumed, closed; every `read`, `ask`,
  `signal`, `emit`; and every applied state write (`data_version`, byte size,
  changed keys, not values).

### UI

- Workflow page: a "Resident" tab listing instances with state, `turns`, inbox
  depth, last turn, today's spend, and the published metrics.
- Instance page: the event timeline (handled/unhandled, which job), turns linked
  to runs, current state as JSON with a diff per `data_version`, and
  pause/resume/close actions.
- Status page: count of open instances and idle turns queued today.

The frontend stays build-free ES modules under `armature/server/static/views/`.

### Local mode (`armature watch`)

`HarnessSpec.triggers` and `armature watch` run without the server. v1 of
residents is **server-only**. A later phase can add a `LocalResidentStore`
(file-backed, like `LocalWorkStore` in `state/work.py`) so `armature watch`
hosts residents on one machine with the same semantics. The store protocol
should be shaped so that's possible: put a `ResidentStore` protocol in front of
the SQL calls the engine-side tools need.

## Phases

Each phase ends with something that runs on a live instance and has a named
rollback, per `DESIGN.md`. Phases 1–5 shipped 2026-11-04; 6–7 remain open.

0. **Spike and decisions (no shipped code).** *Done 2026-11-03* — the decision
   record below settles the open questions; the code anchors were verified
   against the tree during review. Remaining for the plan: the cost-attribution
   audit (question 8) and the `close_on:` / admin-upgrade details. The reading
   list stands for the planning session: `conversations.py`, `worker.py`
   (`tick`, `queue_job`, `finish`), `webhooks._decide`/`evaluate`,
   `pollers.py`, `broker._conversation_tool`, `spec/calls.py`.
1. **Tables, instance and inbox.** Migration, `residents.ensure` /
   `enqueue` / `wake` / `settle` / `sweep`, hooked into `Worker.tick()`.
   The `instance_key` column and uniqueness land here (decision 1);
   singletons are the first test case, `request` events only, enqueued from
   an admin API.
   Rollback: the sweep is behind an instance setting `resident.enabled`, and the
   tables are inert without it.
2. **Spec grammar and handlers.** The `resident:` model, validation, `on:`
   dispatch with named-workflow pinning, injected `_resident` and `_event`
   context, `resident.*` broker tools with staged state writes. Rollback:
   specs without `resident:` are unaffected.
3. **Read, ask, signal, keyed instances.** API endpoints (keyed from the
   start — decision 1), RBAC, audit, the live-broker wait for synchronous
   `ask`, and the UI tab and instance page.
4. **Idle loop.** `next_idle_at`, backoff, idle budgets, drop-idle-when-busy,
   `resident.idle.report`. A starter pack with a housekeeping workflow that
   refreshes metrics.
5. **Routing.** Webhook `config.resident` (computing `instance_key` from the
   payload), poller `config.resident` with issue-key dedupe, `on.schedule`
   materialized as triggers, `resident.emit` with fan-out caps and chain
   depth, `close_on:`. Named test case: a Slack reply resumes a keyed
   instance (decision 2, the beach pattern).
6. **Re-base conversations (optional).** Make a conversation a resident
   specialization: `conversations` rows become residents keyed by thread, and
   messages become events. Only if it removes code; otherwise leave
   conversations alone and share helpers.
7. **Local mode.** `LocalResidentStore` and `armature watch` support.
   Prerequisite: authenticated webhook triggers — `watch`'s listener is
   unauthenticated today, and server-only v1 avoids it by routing through the
   server's rules with RBAC and audit.

## Tests

The invariants the plan should turn into tests (`tests/server/test_residents.py`
alongside `test_conversations.py`), run on all three databases where the suite
does:

- Two workers calling `wake()` concurrently on one instance queue exactly one
  job.
- A duplicate `dedupe_key` enqueues nothing and returns `None`.
- A failed turn leaves `data` and `data_version` unchanged and re-offers its
  events. An event is given up after `max_attempts`.
- A lost turn (lease expired, reaped) behaves like a failed one, and its job is
  never queued again.
- Staged writes over `max_bytes` or failing the schema are refused at the tool
  and again at `settle`.
- `read` never creates a job or run. Assert the counts before and after.
- The idle turn isn't queued before `next_idle_at`, backs off on
  `nothing_to_do`, resets on a real event, and stops at the daily idle budget.
- A pending idle event is dropped when a real event is waiting.
- Two servers running the idle sweep queue one idle turn.
- A handler with a `human` gate fails validation for `request`, `ask`,
  `signal` and `idle`.
- An instance runs the version it pinned until an upgrade re-pins it
  (decision 3). A schema-incompatible version pauses the instance.
- `resident.emit` past the chain depth or fan-out cap is refused and audited.
- A run that isn't the instance's `active_job_id` is refused by every
  `resident.*` tool.
- Closing on `close_after_idle` and `max_turns`.
- An idle event does not bump `last_event_at` and does not reset
  `idle_streak`; `close_after_idle` fires despite a healthy idle cadence.
- The budget counters reset at the UTC day boundary.
- A keyed instance created after a version save runs the new version while
  an older instance drains on its creation version.
- A Slack reply routed by a webhook rule resumes a keyed instance: the
  asking turn ended with state staged and the lock released; the reply is a
  new event and a new turn, and no run was open in between.
- A `close_on` matching signal closes the instance without queueing a turn.

## Decision record (2026-11-03)

Reviewed against the code before deciding (every anchor verified; one
correction folded into the data model). Three owner decisions settle the big
questions; review fixes are folded into the sections above.

1. **Keyed instances are the primary case** (Bryan). Nearly all target
   workflows fan out per work item — one instance per PR (code review), per
   issue (dev work), per commit or PR (translations and ports) — running in
   parallel, each closing when its item closes. Consequences: keys ship with
   the phase-3 API instead of waiting behind routing (phase 1 still builds
   on singletons); the UI and sweeps are sized for hundreds of live
   instances per workflow; and a declarative `close_on:` (a matching signal
   closes the instance without spending a turn) is added for the plan to
   spec, since fleet closures are the common case.
2. **Outward action — event turns act; idle turns ask** (Bryan). `request`,
   `ask` and `signal` turns may act on the outside world (Jira, GitHub,
   Slack). Idle turns may read external sources and write their own state,
   memory and metrics, and may ask the owner a question; any other outward
   write is refused. The ask-from-the-beach interaction is the flagship:
   the turn that asks a question **ends** — state staged, lock released —
   and the answer arrives hours later as a new event (Slack → webhook rule
   → `ask`/`signal`), which starts a new turn from state. No run is ever
   open while waiting, so the 48-hour grant expiry, held slots, and lock
   starvation are all moot. Named phase-5 test case.
3. **Upgrades — pin per instance** (Bryan). An instance runs the version it
   was created with; in-flight instances drain, only new instances pick up a
   newly saved spec. Trade-off accepted: spec fixes don't reach in-flight
   instances; an admin "upgrade instance" action is the escape hatch
   (see "Version pinning and upgrades").

Review fixes folded into this revision:

- The idle scheduler must write idle events on a path that does not bump
  `last_event_at` or reset `idle_streak` (as written before, backoff would
  stall at one factor and `close_after_idle` could never fire).
- `Numeric` correction (see the data model); micro-dollars still preferred.
- Budget-day boundary: UTC.
- No `human` gates on any handler kind in v1, `schedule` included.
- Memory option (a) confirmed for v1: instance state is the resident's
  memory; turns summarize into it. A server-backed MemoryStore behind
  broker tools is the follow-up — and the natural home for memory
  navigation tools in residents.
- Local mode (phase 7) requires authenticated webhook triggers first.
- Naming: "resident" confirmed ("standing" collides with `runner_keys`).

Added 2026-11-05 (owner decisions): fields are declared scalars only, never
the whole payload or any prose, and live in the event's existing `source`
JSON, so there is no migration. There is no supersede. A `close_on` close
also ends the instance's turn. `ensure` re-reads with a locking read after
losing its insert race, so MySQL's REPEATABLE READ snapshot can't hide the
winner's row.

## Questions

Settled 2026-11-03 in the decision record unless noted:

1. **Keyed instances — settled: keyed, and primary** (decision 1). Column
   and uniqueness in phase 1; keys exposed with the API in phase 3;
   routing in phase 5.
2. **Idle turns — settled** (decision 2): read-only toward the world, write
   own state/memory/metrics, ask the owner; no outward actions. Event turns
   act freely.
3. **Memory — settled: (a) state-as-memory for v1**; server-backed
   MemoryStore through broker tools as the follow-up.
4. **Gated `schedule` turns — settled for v1: no `human` gates on any
   handler kind.** Revisit when a use case needs the lock-release dance.
5. **Staged vs immediate state writes for conversations — open**, phase 6
   only, and only if re-basing removes code.
6. **State migration — settled: later.** `paused` + `state_schema` reason +
   UI migrate/reset is the v1 story.
7. **Concurrency per instance — settled: 1 in v1.**
8. **Cost attribution — open; the plan must answer before phase 4.** Audit
   that `cost_usd` populates for every provider kind (including
   `claude-subscription` and `ollama`), and define the unknown-cost policy.
   Recommendation: the USD budget treats unknown cost as 0 while turn caps
   still bind — never fail closed on data we don't have.
9. **Naming — settled: "resident"** (`standing` collides with
   `runner_keys`).

## Out of scope

- A literally long-running agent process, and warm runners.
- Changing how ordinary (non-resident) workflows, triggers, or conversations
  behave, except through phase 6.
- Cross-instance transactions. Instances talk only through events.

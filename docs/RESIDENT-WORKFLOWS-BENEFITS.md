# Resident Workflows — What This Buys You

Companion to `docs/RESIDENT-WORKFLOWS.md` (the design) and `docs/RESIDENT-WORKFLOWS-TECHNICAL.md`
(how it's built). This page is the case for the feature: what changes for a team that turns it on.

## The one-paragraph version

Before residents, an agent workflow was a *run*: it started, spent money, and ended, and anything
it learned died with it. A resident is the other shape — a durable instance of a workflow with an
identity, a bounded state, and an inbox. It costs nothing while it sits. When the world changes
(a webhook lands, a ticket moves, a schedule ticks), the instance wakes for exactly one short
*turn*, does what the event asks, saves what it learned into its state, and goes back to sleep.
Everything downstream — dashboards, budgets, retries, fan-out, human review — is built on that
one idea.

## The benefits

### 1. Reading state is free

`GET /api/residents/{workflow}/{key}/read` serves the instance's published state straight from the
row — no job, no run, no model call. A dashboard that refreshes every ten seconds costs the same
as one that never loads. Before residents, the only way to get a workflow's current view of the
world was to spend a run; now that spend is reserved for work that actually changes something.

### 2. Memory that stays with the thing it tracks

Keyed instances give one resident per real-world object — one per pull request, per ticket, per
customer. Each keeps its own state across however many turns, so a "PR reviewer" instance working
PR #4711 on Tuesday is the same instance, with the same accumulated context, when the PR is
touched Thursday. Nothing re-reads history to reconstruct what it already knew; the knowledge
just sits in the row until the next event.

### 3. The world comes to the agent

Webhook rules (`config.resident`), the Jira and Linear pollers, and cron schedules all deliver
events to instances. A turn happens when something actually changed — not on a poll-everything
cadence, and never a long-running process waiting for input. Each source maps cleanly: a delivery
is a `request`, a ticket event a keyed instance's `request` (deduplicated by issue key), a cron
tick a `schedule` event on every open instance.

### 4. It maintains itself between events

The idle loop runs a turn when the inbox has been quiet for `idle.after`, doubling the gap on
each nothing-to-do report up to `backoff_max`, and stopping for the day at the idle budget. So a
resident can recompute its own metrics, prune its state, or re-check something it was waiting on
— with hard, declarative cost ceilings (`budget_usd_per_day`, `max_turns_per_day`) and no
operator attention.

### 5. Asking is asynchronous and cheap

`ask` enqueues a question as an event; the instance's turn answers it and the reply is recorded
on the event for the caller to poll. The questioner isn't blocked on a run, the instance is only
busy for one short turn, and every answer is kept in the event timeline.

### 6. Fleets are operable

Every instance is pausable, resumable, closable, and upgradable from the admin API — including a
version upgrade that re-pins the instance to a new spec and pauses it (never closes it) if its
existing state fails the new schema. Declarative `close_on` closes a whole fleet from one signal
(a `pr_merged` signal ends every instance of a PR-review resident) and **spends nothing**: the
closing event lands handled, no turn runs. One bad spec can be rolled back without touching live
instances, because each instance runs the version it pinned, not whatever is current.

### 7. Fan-out is safe by construction

`resident.emit` lets one instance start work on another — and it cannot run away. The causal
chain is capped at five links, a run at ten emits, an instance at fifty per day, every refusal is
audited, and a dry turn's emit is dry. This is the primitive for "PR triage resident hands the
build-failure instance to the fix-suggestion resident" without a supervising process.

### 8. Every dollar is governed

Daily budgets (idle, turns, and emits each), per-instance caps, staged state that only applies
when a turn succeeds, an audit row for every meaningful act (`resident.read|ask|signal|close|
emit|resumed`…), and micro-dollar counters on the row. An owner can say "this resident costs at
most $0.50 a day" and the engine enforces it, resuming automatically at the next UTC day.

### 9. Rollout and rollback are one setting

Nothing resident-related runs until `resident.enabled` is set for the server. With it off, the
routes refuse, triggers decide-and-record only, the sweep makes no turns — inert in both
directions, so the feature can ship dark and be disabled without a redeploy.

### 10. It's the same engine underneath

A turn is an ordinary job: grants, sandboxing, safety rules, traces, retries, and the worker all
apply unchanged. Operators already know how to observe, cancel, and audit runs; resident turns
appear there like any other job. No daemon was added — the sweep rides the existing worker tick.

## What it takes

- The `residents` and `resident_events` tables (migration 0027) and one new module
  (`armature/server/residents.py`, ~1000 lines) plus its routes.
- Existing triggers/pollers gain a `resident: true` opt-in per rule — no migration of current
  behavior.
- Cost floor for a resident that only answers events: zero while idle; one short turn per event.

## Where the boundaries are

- Idle turns are read-only toward the outside world (they ask; event turns act) — enforced at
  the one outward-write tool.
- A turn must end when it answers, so no resident handler may run a human-gated workflow; the
  engine refuses to save, create, or dispatch such a spec.
- State is bounded (`max_bytes`, JSON-schema validated, published keys allow-listed) — a resident
  cannot quietly grow into a database of its own.

For the mechanics behind each of these claims, see `docs/RESIDENT-WORKFLOWS-TECHNICAL.md`.

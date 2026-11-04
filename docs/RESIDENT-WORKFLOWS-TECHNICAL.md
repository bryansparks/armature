# Resident Workflows — How It Was Built

Companion to `docs/RESIDENT-WORKFLOWS.md` (design) and `docs/RESIDENT-WORKFLOWS-BENEFITS.md`
(the case for it). This page is for the engineer who has to maintain, extend, or debug the
feature: the architecture, the invariants, where each behavior lives, and how the branch was
produced.

## The architectural bet: rows, not processes

The feature adds **no daemon**. A resident is two rows (`residents`, `resident_events`) and a
`next_idle_at` timestamp. Everything that moves a resident forward is either a request path
enqueueing an event, or the existing worker's tick calling `residents.sweep()`. Durable state
plus ephemeral, ordinary jobs — the engine's existing strengths — instead of long-lived agent
processes. Every hard problem below was solved by choosing that shape up front.

## The data model (migration 0027, `armature/server/db/schema.py`)

- **`residents`** — identity (`workflow_id`, `instance_key`, `workflow_version` — the pin),
  the one-turn lock (`active_job_id`), bounded state (`data`, `data_version`, `staged`,
  `max_bytes`, `publish` via the spec), daily counters (`turns_day`, `idle_turns_day`,
  `spent_usd_day` in micro-dollars, `emits_day`, `spent_day`), the idle schedule
  (`next_idle_at`, `idle_streak`), lifetime (`turns`, `last_event_at`, `last_turn_at`), and
  lifecycle (`state`, `close_reason`, `closed_at`). CHECK constraints share the exact tuples the
  runtime imports (`RESIDENT_STATES`, `RESIDENT_EVENT_KINDS` — defined once in schema.py).
- **`resident_events`** — append-only inbox: `seq` (per-instance, assigned under the instance's
  row lock), `kind` (`request|ask|schedule|signal|idle|custom`), `payload`, `source` (causal
  provenance: delivery ids, `emitted_by`/`chain` for emits), `reply_to`, `dedupe_key`,
  `dry_run`, `handled`, `attempts`.

## The substrate (`armature/server/residents.py`)

Five functions carry the whole lifecycle:

- **`ensure`** — get-or-create an instance, pinning the workflow's *current* version at creation.
- **`enqueue`** — takes the instance's FOR UPDATE row lock, assigns the next seq, dedupes,
  bumps `last_event_at`/clears `idle_streak`/`next_idle_at`, and calls `wake`. All callers
  (routes, webhooks, pollers, emit) get the same path — including the `close_on` hook: a
  matching named signal lands the event handled and closes the instance, **no turn queued**.
- **`wake`** — the one-turn gate: conditionally sets `active_job_id` to a new job only if it is
  NULL (CAS), resolves the handler from the *pinned* spec (`self` → pinned version, named →
  resolved at dispatch, `None` handler → event marked skipped), enforces the Gates rule at
  dispatch time (`worker.gated` backstop → pause `handler_gated`), checks budgets *only when a
  turn would actually run* (a paused instance holds events; caps never fire on arrival alone),
  batches events by target workflow, bakes fenced `_event`/`_resident` inputs, and hands the
  batch's `dry_run` to the job. Unresolvable handlers poison-drain past `MAX_ATTEMPTS` (3).
- **`settle`** — when the turn's run ends: on success, apply the staged state (re-validated with
  the same code the tool used — a write that somehow got past the tool still dies at settle),
  accrue cost, mark events handled, release the lock, and re-wake if more events waited (the
  beach pattern: between turns the lock is free, so the "reply" event simply starts the next
  turn). On failure: staged state is discarded, events gain an attempt, and are re-offered.
- **`sweep`** — the tick's share: settles ended turns, auto-resumes budget-paused instances at
  the next UTC day (audited), and runs the idle scheduler — candidates claimed by a single CAS
  (`UPDATE ... SET next_idle_at = NULL WHERE id = :id AND next_idle_at <= :now AND state = 'idle'
  AND active_job_id IS NULL`), so two servers queue exactly one idle turn. Multi-row scans carry
  `ORDER BY id` so lock acquisition is single-directioned.

**Concurrency discipline, stated once:** every resident-locking path locks exactly one instance
row, always as its first lock; the only multi-row lock is the sweep, which is ordered. Mutual
emit (A emits at B while B emits at A) can in principle interleave; the database kills one
transaction, it rolls back cleanly, and the step retries — accepted as retriable.

## The idle loop

`idle.after` starts the cadence at `ensure`; `settle` schedules the next gap as
`activity + after × backoff_factor ** idle_streak`, capped at `backoff_max` — the streak grows
on each `nothing_to_do` report and resets on any real event. `_enqueue_idle` is deliberately a
*separate* insert path: idle events bump no lifetime counters, so `close_after_idle` measures
real events only. Daily idle budgets push `next_idle_at` to tomorrow rather than dropping work.

## The integration seams

- **API** (`armature/server/app/routes_residents.py`): `read` (free, from the row, `?keys=`
  intersects `publish`), `list` (timeline metrics), `ask` (async `{event_seq, job_id}` or `?wait`
  poll of the event's reply, default 30s capped 60s), `signal`, `events` timeline, and admin acts
  (pause/resume/close/upgrade) under SELECT..FOR UPDATE. RBAC: read/list = viewer, ask/signal =
  contributor, admin = admin; throttled per user; every act audited. `ask`/`signal` are gated by
  `resident.enabled` — as are resume/upgrade's re-wake, so "inert when off" holds on every path.
- **Webhooks** (`armature/server/webhooks.py`): a rule with `config.resident` renders the
  workflow's `instance_key` template against the delivery body in the trigger sandbox
  (sandbox-linted engine-side; regex-anchored to `event.payload` refs), refuses over-length
  renders (191 — the column's width; truncation would silently merge instances), `ensure`s, and
  `enqueue(kind="request", dedupe_key=delivery_id, dry_run=rule.dry_run)`. Shadow rules decide
  only. A resident rule is auditable-but-permissive on config: it locks only the delivery row.
- **Pollers** (`armature/server/pollers.py`): Jira/Linear rules opt in with `config.resident`
  and route issues to keyed instances (dedupe by issue key); requeue on a resident trigger is
  refused with a 409. Schedule triggers with `config.resident` are **spec-owned**: `_sync_schedule`
  (called from the save/create/fork/rollback paths in routes_defs.py) materializes and removes
  the trigger row from the workflow's current resident block, so users never hand-edit them.
- **Tools** (`residents.call_tool` ← broker's `_resident_tool`): nine tools — `events`,
  `state.get`, `state.set`, `state.patch`, `metrics.publish`, `reply`, `idle.report`, `close`,
  `emit` — reached only by the run holding `active_job_id` (409 otherwise), exposed to specs via
  the builtin `resident` pack. `emit` resolves its target by name in-scope, enqueues with
  `chain = incoming + 1`, and refuses past chain 5 / 10 per run / 50 per instance-day — refusals
  are audited, raised *after* the transaction commits so the audit row survives the rollback, and
  an idle turn's emit is refused outright (decision 2), while a dry turn's emit carries its dry
  flag to the target.
- **Spec grammar** (`armature/spec/models.py` + `validator.py`): the `resident:` block, all
  `extra="forbid"`, engine-side only — the server layers its own checks on top (human-gate
  handler refusal on save *and* create, `close_on.signal` grammar, instance_key regex).

## Safety posture

Event payloads reach the model fenced as data (the conversations pattern); `_event`/`_resident`
are the only sandbox-allowlisted private names; state writes are schema- and size-validated
twice; every human-facing act is audited; column widths are enforced at every entry point
(instance_key 191, event name 64, the wake thread label bounded) — the class of bug where MySQL
strict mode turns an over-wide value into a 500 that SQLite silently stores was hunted by
inspection and is pinned by boundary tests.

## How the branch itself was produced

26 commits (`921b126..ac86426`), 34 files, +6,540 lines. A 15-task plan (phases 1–5) was executed
task-by-task with a fresh implementer and a fresh reviewer per task, TDD-first, every review
verdict recorded; 14 controller rulings settled spec-vs-plan conflicts as they surfaced (the
load-bearing ones: the dispatch-time Gates backstop, refuse-don't-truncate on over-length keys,
the idle-streak increment that makes backoff actually back off, and decision-2 enforcement in
code rather than prose). A final whole-branch review on the strongest model found one Critical
(the MySQL-only thread-label overflow above), two Important (the ungated admin re-wake, the
unvalidated `close_on.signal`), and seven minors — all fixed in one wave and re-reviewed clean.
The tree merged to main is the fully verified one: ~4,850 tests passing across core, server,
and transport, with ~90 of them new resident/broker tests, including two-server CAS tests and
the end-to-end beach-pattern test.

## Where to look first

| To understand… | Read |
|---|---|
| The whole substrate | `armature/server/residents.py` (start at `wake`, then `settle`) |
| The HTTP surface | `armature/server/app/routes_residents.py` |
| The schema and day counters | `armature/server/db/schema.py`, `migrations/versions/0027_residents.py` |
| The grammar | `armature/spec/models.py`, `armature/spec/validator.py` |
| Trigger routing | `armature/server/webhooks.py`, `armature/server/pollers.py`, `routes_defs.py: _sync_schedule` |
| A worked example | `examples/residents/housekeeping.yml` |
| The tests as documentation | `tests/server/test_residents.py` (1,877 lines, invariant-per-test) |

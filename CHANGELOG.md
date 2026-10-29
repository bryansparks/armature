# Changelog

All notable changes to Armature are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

## [0.8.0] - 2026-10-29

### Added

- **Workflow renames.** Change `name:` in the editor and save, and the
  workflow takes the new name. Its runs, rules, jobs, kind, and display
  label follow it, and the export drops the old file. A name already used
  in the same scope is refused, and rolling back to a version with the old
  name renames it back.
- **Webhooks and rules.** A team lead creates a webhook on the Webhooks
  page and points GitHub, Slack, or any service that signs with HMAC or a
  bearer token at its URL, `{base}/hooks/{token}`. The server checks the
  signature, drops repeats by delivery id, and logs each delivery for 14
  days. Ordered rules on the webhook match on the event and on payload
  fields (`eq`, `ne`, `in`, `not_in`, `starts_with`, `contains`, `exists`,
  `missing`), pick the project from a repository field, map payload fields
  to workflow inputs, and queue a job for each match. A rule can stop later
  rules or run in shadow mode, and **Test rules** replays a stored delivery
  through the rules. An admin can forward verified deliveries to a legacy
  receiver while workflows move over. Migration 0010 adds `webhooks` and
  `webhook_deliveries` and links triggers and jobs to them.
- **Job worker.** `armature server run` now runs queued jobs. It claims a
  job with a compare-and-set update, runs the workflow version pinned when
  the job was queued, and shows the run live like any other. Each job gets
  the team's `env.*` settings and only the secrets its rule lists. A job
  runs once and is never retried, so a lost run can't double its cost. The
  `worker.slots` setting (2 by default) caps concurrent jobs, and
  `GET /api/jobs` lists a team's jobs.
- **Workflow kind.** Each workflow has a `kind` (review, fix, merge, port,
  work, or other) that decides how its runs count on subject pages and in
  the Overview's reviews today, so renaming a workflow changes only its
  label. The new-workflow form and the workflow page set it, the workflows
  list shows it, and `POST /api/workflows/{id}/kind` changes it with a
  `workflow.kind` audit row. Migration 0009 sets the kind of the workflows
  the server used to classify by name and links runs recorded only by
  workflow name to the workflow their name resolves to in their own team.
  Creating a workflow links the earlier runs that resolve to it, and
  `armature server runs relink --from OLD --to ID-OR-NAME [--dry-run]` links
  the runs of a renamed workflow.
- **Run actions and a Status page.** The server config file (never the UI)
  can list `actions`, each with an `id`, `label`, the `workflows` and
  `statuses` it applies to, a `min_role` (contributor by default),
  `admin_only`, `confirm` text, and a `command`. The run page shows a button
  for each action the viewer may use, asks for confirmation, then runs the
  command with no shell, a minimal environment, and a 120-second timeout. The
  run's fields reach it only as JSON in the file `ARMATURE_ACTION_RUN` names.
  Exit 0 is done, 3 is refused with the command's message, and anything else
  failed. Each run is recorded in the new `operations` table, with its
  message (up to 2 KB) and log (up to 64 KB) redacted, plus an audit row.
  Only one operation runs at a time per run and per action, and an
  `admin_only` action needs step-up. `GET /api/runs/{id}/actions`,
  `POST /api/runs/{id}/actions/{action}`, `GET /api/operations`, and
  `GET /api/operations/{id}` serve them, the last with the log for admins
  only. A `status` command runs every `interval_seconds` on a background
  thread, and `GET /api/status` and the new Status page show its latest
  Markdown, redacted with the instance's secret values. Admins also see the
  recent operations there.
- **Pull request and issue pages.** `GET /api/subjects` lists every subject
  the viewer can see (a pull request such as `owner/repo#123`, or an issue
  key) with its latest title and verdict, a status of `merged` once a
  `pr-merged` run exists, else the latest review verdict, else the latest
  run's status, and counts of runs, reviews, and fixes. It searches subject
  and title with `q`, narrows with `team`, and pages with `before`. Hidden
  workflows stay out of the counts and cost, never out of access or status.
  `GET /api/subjects/{subject}` returns every visible run in order with its
  head, verdict, mode, cost, and report names, the merged links, and each
  finding with its disposition in every review, and answers 404 when the
  viewer sees none of its runs. The new Pull requests page lists them, and a
  subject link anywhere now opens `#/subject/<subject>` with Runs, Findings,
  and Activity tabs.
- **Run artifacts and a tabbed run page.** A step can save a named report
  with `python -m armature.events artifact --name review.md --file PATH` (or
  `--text`, with `--type text/markdown`, `text/plain`, or
  `application/json`), up to 1 MB. The emitter redacts it and sends it in
  parts that each fit one event, over the socket or HTTP, and the server
  redacts the whole artifact again before storing it in `run_artifacts`. A
  later artifact with the same name replaces the earlier one.
  `GET /api/runs/{id}/artifacts` lists them, `GET /api/runs/{id}/artifacts/{name}`
  returns one, and the run stream carries an `artifact` event so a tab appears
  while the run is still going. The run page now has a header with the run's
  title, verdict, subject, links, and a one-line progress strip, then one tab
  per artifact (`review.md` first and default), Output (each step's status,
  duration, error, and stored output, from `GET /api/runs/{id}/outputs`),
  and Log (the live event feed). Markdown artifacts render through a small
  renderer that builds only elements and text nodes: raw HTML shows as text,
  images show their alt text and URL, and only http, https, mailto, and
  same-page links become links. Each artifact has Copy and Download.
- **Overview and Trends pages.** Signing in lands on an Overview of what is
  running (live), what failed or was lost in the last 24 hours, provider
  credential errors, today's spend against the 7-day average, code reviews
  and re-evaluations today, agent time and man hours, and the latest runs.
  Trends charts spend, runs, failure rate, median run time, review verdicts,
  and man hours per day over 7, 30, or 90 days, with the slowest steps and
  per-role review cost. Both read `GET /api/overview` and `GET /api/trends`,
  scoped to the viewer's teams, with day boundaries and percentiles computed
  in the configured time zone so the SQL stays portable. Admins set
  `display.show_cost`, `display.show_man_hours`, `display.human_multiplier`,
  `display.timezone`, and per-workflow labels, colors, and "show in runs by
  default" from a Display section on the Settings page, and every page reads
  them from `GET /api/display`. `GET /api/runs` leaves out hidden workflows
  (`pr-merged` and `build-announce` until an admin saves otherwise) unless it
  gets `workflows=` or `include_hidden=1`.
- **`armature server init`, `migrate`, and `create-admin`** (the new
  `server` extra, with `postgres` and `mysql` extras for those drivers).
  `init` writes `~/.armature/server.yaml` if missing, generates a mode 0600
  master key file, migrates SQLite, MySQL 8, or Postgres to every table in
  the platform design with Alembic, and creates the first admin with a
  prompted password and a TOTP secret. Secrets are sealed with AES-256-GCM
  under HKDF sub-keys of the master key, bound to their team, id, and name.
  The server refuses a non-loopback bind unless `allow_public_bind` is set.
- **`armature server run`** serves a signed-in JSON API and the browser UI
  under `base_path`, and refuses to start without an admin or a master key.
  Local users join through a single-use invite link, valid for 72 hours,
  where they set a password and enroll TOTP. Admins invite anyone, and a team
  lead invites into a team they lead. Sign-in is password plus TOTP, with
  replay blocked by a conditional update, or OIDC (authorization code with
  PKCE, accounts linked on issuer and subject only, roles recomputed from
  `oidc.group_roles` on every sign-in, admin only through
  `oidc.admin_groups`, step-up only through a fresh provider sign-in).
  Sessions are HMAC-keyed rows with 30-minute idle and 12-hour absolute
  limits, a `__Secure-armature` cookie, an Origin and CSRF check on every
  mutation, per-address and per-user throttles, HSTS, and step-up for users
  and secrets. Five roles (admin, team lead, project lead, contributor,
  viewer) are checked per route against the team and project read from the
  database. `create-admin` now confirms a TOTP code before it exits.
  `trust_real_ip` reads `X-Real-IP` from a proxy on the same host
  (docs/platform/DEPLOY.md). `oidc.group_roles` now maps a group to a list of
  `{scope_kind, scope, role}` grants. Names are case-sensitive and usernames
  case-insensitive on every database.
- **Run events and `armature emit`.** A run sends `run_start`, `run_end`,
  `step_start`, `step_end`, `step_failed`, `step_skipped`, `retry`, and
  per-LLM-stage `cost` events, alongside any `on_event` callback. It uses the
  emitter passed to `Harness(emitter=...)`, else `ARMATURE_EVENTS` (`unix:`
  socket or `http(s)://` URL) and `ARMATURE_EVENTS_TOKEN`, else registers
  itself with `ARMATURE_SERVER` using `ARMATURE_RUNNER_KEY`, and runs
  without events if that fails. Subagent runs and runs started inside a
  step tag their events with `parent_step` and send no `run_start` or
  `run_end`, and fan-out branches carry `branch` and `branches`. Script steps
  get the run's `ARMATURE_EVENTS`, `ARMATURE_EVENTS_TOKEN`,
  `ARMATURE_RUN_ID`, and `ARMATURE_STEP_ID`, and never the runner key.
  `armature emit <kind>`, or the faster `python -m armature.events <kind>`,
  sends one event from a script. `--stream-claude` sits in a Claude Code
  `stream-json` pipe, copies every line through unchanged, and emits the
  session's tool calls, results, text, and cost. Emitting never blocks or
  raises: a full queue drops `text`, `log`, and `progress` first and sends a
  `dropped` count, `text` is coalesced to four a second per step, and known
  token patterns and the values of variables named in `ARMATURE_REDACT_ENV`
  are redacted, as are the run's own token, the runner key, and any
  `ark_` key. Every event carries an `eid` that stays the same across
  retries, so the server can drop repeats. HTTP batches stay under 900 KB,
  and a 413 splits the batch instead of dropping events. A running stage
  sends a `progress` heartbeat every 60 seconds. Registration gives up after
  3 seconds and then waits 5 minutes before trying that server again. Event
  endpoints and registration refuse plain `http://` to a non-loopback host,
  and the Unix socket is used only when `ARMATURE_SERVER` is on loopback.
  `import armature` no longer imports litellm or the engine until
  `armature.Harness` or a subpackage is used.
- **Live runs on the server.** A team lead creates runner keys
  (`ark_...`, shown once, stored as an HMAC, step-up to create or delete)
  under `/api/teams/{id}/runner-keys`. A runner registers a run with
  `POST /api/runner/runs` and gets a per-run event token, valid 48 hours or
  until `run_end`, and the endpoints to send to: a mode 0600 Unix datagram
  socket (`events_socket`, default `~/.armature/server-events.sock`) or
  `POST /api/events` with NDJSON up to 1 MB. The server takes the run and
  team only from the token, caps each event at 64 KB and each run at 50,000
  events and 500 a second, redacts token patterns and the exact value of
  every secret the team can see, numbers events per run from 1, and
  broadcasts each one before a single writer stores it, flushing every
  second or 500 events in one transaction. Run, step, cost, finding, and
  end rows follow from the events, and a run silent for
  `lost_after_minutes` (60) is marked `lost` until it speaks again.
  `GET /api/runs/{id}` and `/api/runs/{id}/events` (forward with `after`,
  backward with `before`) read history, and `/api/runs/{id}/stream` and
  `/api/stream/runs` stream it as Server-Sent Events with `Last-Event-ID`
  resume, a heartbeat, and per-user (`sse_per_user`) and per-process
  (`sse_total`) caps. SQLite now runs in WAL mode. Measured with
  `python -m tests.server.live_load` (20 runs at 200 events a second, 100
  viewers): p99 from emit to viewer about 320 ms on SQLite, Postgres, and
  MySQL, with nothing dropped.
- **Instance runner keys.** Migration 0005 lets a runner key belong to the
  instance (`team_id` NULL, scope `*`). Admins manage them under
  `/api/runner-keys` and on the new Runner keys page, with step-up to create
  or delete, and team leads manage their team's keys on its card on the
  Teams page. An instance key's run lands in the team whose project's
  repository matches its `project` without regard to case, then by
  `team-slug/project-name`, else in an admin-only `operations` team created
  on first use. A registering engine sends `ARMATURE_PROJECT`, else the
  run's `project` input, else its `repo` input. DEPLOY.md lists the
  environment variables under "Streaming runs from a host".
- **Instance-level and team-scoped definitions.** Migration 0003 adds
  `teams.slug`, `projects.repo`, instance-level workflows (`team_id` NULL,
  scope `*`), and team-scoped settings keyed by scope and key. `GET
  /api/workflows`, `/api/workflows/{id}`, `/api/documents`,
  `/api/documents/{id}`, `/api/teams/{id}/providers`, and the admin-only
  `/api/settings` read them, and the UI shows workflows, roles and prompts,
  and each team's providers.
- **`armature server export <dir> [--team SLUG]`** writes the instance's
  definitions to files: `workflows/<scope>/<name>.yml` with a first line of
  `# armature: stored-workflow`, `roles/`, `prompts/`, and `missions/`
  Markdown verbatim, `teams.json`, `settings.json`, `providers.json` with
  credential names only, and `manifest.json` with each object's version and
  each file's sha256. A scope is `instance`, `team-<slug>`, or
  `project-<team-slug>-<project>`. Secrets, users, and history are never
  exported. The export is built beside `<dir>` and swapped in whole, and a
  non-empty `<dir>` with no `manifest.json` is refused.
- **`export_on_change`** in the server config (`dir`, `command`,
  `debounce_seconds`) re-exports after every committed workflow, document,
  provider, setting, team, or project write, debounced and one at a time on
  a background thread, then runs `command` as argv in `dir` with only
  `PATH`, `HOME`, `LANG`, and `ARMATURE_EXPORT_DIR` and a 120-second
  timeout. Each run is audited as `export.run`, and the admin-only `GET
  /api/export/status` reports the last run. Only the config file can set a
  command. The armature-ops importers moved to that repository's
  `tools/platform/`.
- **Editing definitions on the server.** Workflows, roles, prompts, and
  missions can belong to the instance, a team, or a project, and a run
  resolves a name project first, then team, then instance. Admins edit
  anything, team leads edit their teams' and those teams' projects'
  definitions, and project leads edit their projects'. Each can fork what
  they can read into a scope they can write, roll back to any version, and
  delete. Every save is validated with YAML line numbers (schema, spec
  checks, template lint, and pack adapter references and `with:` parameters
  against the installed packs), appends a version, is audited, and is
  refused with 409 when its `base_version` is stale. A non-admin may change
  only the description, the mission, and each stage's wiring to pack
  adapters, and may not save a workflow whose adapter commands are
  templates or widen a role's `tools` and `mcp_servers` past the packs'
  allowlists. Template markers outside values, a `{{ }}` that refers to no
  input or step, YAML anchors and aliases, and more than 20,000 nodes are
  refused. Team leads manage their team's model providers, with config
  checked per kind, a credential read only from the team's own secrets, and
  `api_base` held to public https unless an admin saves it. Admins manage
  settings, which refuse secret-looking keys. Everything but a body edit
  needs step-up. `POST /api/workflows/parse` and `render` back the form
  editor, keep comments and unknown keys, and rename a stage in place.
- **`reasoning_effort` on model tiers and roles.** A tier can set
  `reasoning_effort: low | medium | high | xhigh | max`, and a role can
  override it, resolved role → tier → omitted like `temperature` and
  `max_tokens`. Armature passes it to litellm, which maps it to the
  provider's own control (adaptive thinking plus `output_config.effort` on
  adaptive-thinking Claude models). Each model keeps its own default when
  it's unset. An unknown value fails `armature validate`. A model litellm
  can't map it for fails the stage on its first call with
  `UnsupportedParamsError`, without retrying that call or escalating to the
  next tier, though the stage's own `on_fail.loop` still re-runs it.
- The LLM cache key includes the resolved `reasoning_effort` when one is set.
- `~/.armature/defaults.config` and `armature new` keep a tier's
  `reasoning_effort`.
- The ReAct tool loop sends a response's `thinking_blocks` back with its
  assistant turn, so the model keeps its reasoning across tool turns.
- `armature validate` warns with `REASONING_EFFORT_WITH_TEMPERATURE` when a
  stage resolves both a `reasoning_effort` and a `temperature` other than 1,
  which Claude rejects while thinking is on.
- `armature run` also saves the full result to
  `~/.armature/runs/<run_id>/result.json`, so every run keeps its untruncated
  stage outputs instead of only the latest run per workflow.

### Changed

- **Cost display setting covers runs.** With cost turned off in display
  settings, the runs list, run page, pull request pages, and live log show no
  cost, and `GET /api/runs` and `GET /api/runs/{id}` send `cost_usd` as null.
- **Team pages show workflows.** A team's page has a Workflows tab listing
  the workflows that apply to that team, and team managers get an Add
  workflow link that opens the new workflow page with that team chosen.
- **Teams page lists teams.** The Teams page is now a list of the teams you
  can see, and each team opens its own page with Projects, Model providers,
  and Runner keys tabs. Only the open tab loads its data, and Runner keys
  shows only for team managers.
- Script steps now get `ARMATURE_RUN_ID` and `ARMATURE_STEP_ID` in their
  environment. No process Armature starts, whether a script step, the
  `shell` tool, or a docker, pip, or patch call, inherits
  `ARMATURE_RUNNER_KEY`, or a parent run's `ARMATURE_EVENTS` and
  `ARMATURE_EVENTS_TOKEN` unless they belong to the current run.
- The litellm floor is now 1.101.0, the version verified to map
  `reasoning_effort` to adaptive thinking on Claude.

### Fixed

- `~/.armature/last/<workflow>.json` is written atomically (temp file plus
  rename), so concurrent runs of the same workflow can no longer leave a
  corrupt file.

## [0.7.0] - 2026-10-05

### Added

- **Cloud transport** — `armature/transport/` subpackage behind the optional
  `cloud` extra (`pip install armature-agents[cloud]` adds boto3; the default
  install never imports boto3 or the subpackage — enforced by a CI
  import-clean boundary test). Moved from armature-dispatch:
  - `naming` — deterministic sweep job ids (`sweep-<mission>~<unit>~<attempt>`,
    never-2PC) + hostile-id name validation
  - `s3io` — S3 helpers + readers of the engine's `jobs/` results layout
    (`list_run_ids`, `latest_receipt`)
  - `s3work` — `S3WorkStore`, the S3 implementation of the `WorkStore` protocol
  - `settle` — runner inject (step 4.7) + settle (step 7.6): metering, closure
    application, resting states, idempotent redelivery
  - `workops` — submit gates (doc/names/state/requires/attempts/budgets) +
    `start_work_unit` launch bookkeeping
  - `sweep` — the flock sweep with the launch callable **injected**
    (`ARMATURE_SWEEP_LAUNCH`, `module:function`): repair pass (orphan rule),
    readiness → submit/notify/hold, exit-1-on-errors alarm contract,
    `python -m armature.transport.sweep` entrypoint
- Boundary + provider-seam documentation: `docs/TRANSPORT-AND-PROVIDERS.md`
  (policy-is-grammar rule, extras contract, the five provider seams, env
  contracts); README + CLAUDE.md cloud-transport sections.


## [0.6.3] - 2026-10-01

### Added
- **Mission lifecycle (slice 3: closure application, budget metering,
  `mission advance`).** A run's closure is now authoritative for the
  unit's resting state: `armature/state/closure.py` ships
  `extract_closure` (the closure stage's guided_json output → typed
  `ClosureRecord`), `apply_closure` (reason → resting state: `handed_off`
  seeds `follow_on` units `pending` idempotently; `blocked_on` seeds the
  blockers and extends the unit's `requires`; malformed closures fail the
  unit loudly, never silently `done`), and follow-on units are addressable
  work units (`id`/`title`/`workflow` required, validator-enforced).
  Budgets are now metered, not declared: the engine sums the
  provider-reported cost of every LLM response (`total_cost_usd`), the run
  receipt carries it as `cost_usd` (`ResultsManifest`), and each record's
  `spent_usd` accrues on success *and* failure — `mission run` refuses
  before consuming an attempt when a unit hits `max_budget_usd` or the
  mission's records sum to `budget_usd` (armature-side metering, never a
  vendor balance check). New verb `armature mission advance` computes pure
  readiness — `launchable` / `notify_only` / `held` with reasons, `--json`
  for executors — and never executes anything; `mission run` additionally
  re-admits a `blocked_on` unit once its requires (spec ∪ record) are
  done, and `mission status` computes column widths and lists
  closure-seeded units.
- **Mission lifecycle (slice 2: LocalWorkStore + enforcement).** The
  work-unit state machine ships: nine states with a single legality table
  (`armature/state/work.py` — the one place transitions are checked),
  `WorkUnitRecord`/`TransitionRecord` pydantic models, the `WorkStore`
  protocol (the persistence seam a dispatch transport implements against
  S3 later) and `LocalWorkStore` (per-unit JSON records plus an append-only
  `transitions.jsonl` audit; default base `~/.armature/work`, `--store`
  overrides). Two new verbs drive a mission locally: `armature mission run`
  (readiness refusals that never consume an attempt, per-attempt `job_id`
  stamping, and the attempt ceiling — `failed` stays `failed` at
  `max_attempts`) and `armature mission status` (unit states + recent
  transitions). `mission_source: work_unit` is now wired end to end: a run
  for a work unit renders `[Workflow Mission]` from the mission's objective
  and a new `[Work Unit]` layer from the unit's title/objective, with the
  record injectable as `{{ work_unit.* }}` and closable via
  `never: [work_unit]`. Validation hardens with `MISSION_FIELD_INVALID`
  (blank ids, non-positive `max_attempts`, negative ceilings) and
  unit-scoped `stage_id` on every mission error. Budget metering
  (`spent_usd`), closure application, and `mission advance` are slice 3.
- **Mission documents (slice 1: grammar + validation).** A new armature
  document type layers work units above workflows: a mission holds a larger
  objective plus work units, each naming which workflow accomplishes it and
  declaring order (`requires:`), autonomy (`posture:`), and ceilings
  (`max_budget_usd` / `timeout_hours` / `max_attempts`). Ships the
  `WorkUnit`/`MissionSpec` models + loader (`armature/spec/mission.py`),
  `validate_mission` with seven error/warning codes, the `armature mission
  validate` CLI, a shipped worked example
  (`examples/missions/campaign-pretzel.mission.yml`), and the concept doc
  `docs/MISSION-AS-WORKFLOW-DRIVER.md`. Additive and zero-runtime-change:
  workflow specs gain two optional fields — `closure:` (names the
  guided_json stage whose output declares the run's closure, validated
  against the fixed closure-reason enum) and `mission_source: static |
  work_unit` (the seam where a run's mission context renders from the
  injected work record; runtime wiring arrives with slice 2's LocalWorkStore).
- **Research currency check.** New `docs/RESEARCH-CURRENCY-CHECK.md` records the
  September 2026 analysis of harness research since July 2026: the verdict (the
  field converged on what Armature already is), the one adopted item (the resume
  contract), and a register of tracked, deliberately-not-adopted items each with
  its reason and reopening trigger.
- **TypeSafe decision adapter spike (Layer 1).** New example
  `examples/decision-typesafe/` — an A/B benchmark pitting a non-LLM
  decision API (TypeSafe/Jev) against an LLM judge on 20 hand-labeled
  finding statements. Ships `parse: json` script adapters, a fan-out A/B
  spec (`ab-demo.yml`), a labeled benchmark + questions battery, and a
  comparison harness with per-state miss detail. Measured on the live
  API: decision 0.87 vs judge 0.98 dimension accuracy, ~260 ms vs
  ~20.6 s mean latency, $0.0005 for all 20 decision calls. Misses cluster
  at probability boundaries (calibrated uncertainty, not confusion) —
  see the example README for the hybrid-escalation read.
- **Script adapters can emit structured output (`parse: json`).** An adapter
  declared with `parse: json` has its stdout parsed as a JSON object, which
  becomes the stage result directly — downstream stages can reference
  `{{ stage.field }}` and fan-out stages can partition over script-produced
  lists. A non-zero exit, invalid JSON, or a non-object top level raises
  (subject to `on_fail` / `fail_as_value`), making deterministic script
  stages first-class citizens alongside LLM stages.

### Fixed
- **Concurrent runs on a checkpointed session directory now fail loudly.** A
  second run against a session directory whose run is still active raises
  `SessionDirInUse` instead of silently duplicating un-checkpointed stage
  effects (last checkpoint-rename wins, both runs execute the un-checkpointed
  stages). The lock is an `flock` on `session.lock`, held for the run's
  duration and released by the OS on process death — no stale-lock files.
  Runs with `checkpoint: false` take no lock. Alongside the lock, six
  conformance tests (`tests/runtime/test_resume_contract.py`) now pin the
  resume contract in CI — completed stages exactly-once, in-flight stages
  at-least-once, fan-out checkpointed as a unit, concurrent runs rejected —
  and the docs were truth-passed: the FAQ's nonexistent `--resume` flag and
  wrong checkpoint path corrected, and `docs/CHECKPOINT-AND-RESUME.md` gained
  an Effect-delivery contract section.
- **A fan-out branch no longer loses its result when the trace write fails.**
  The success-path trace write ran inside the stage's `try`, so a transient
  `sqlite3.OperationalError` ("database is locked") was indistinguishable from
  the stage itself failing: the branch's finished work was discarded and
  replaced with `_fan_out_error`, and the fan-in list silently came back one
  item short. Trace writes now go through `Harness._record_trace`, which
  swallows telemetry errors the way the failure path and memory capture
  already did. `TraceStore.record` also writes in a single thread hop using
  `BEGIN IMMEDIATE`: a deferred transaction upgrades to the write lock from a
  read snapshot, which SQLite refuses instantly with `SQLITE_BUSY` without
  consulting the busy timeout, and an `await` between the INSERT and the
  COMMIT held that lock for as long as a blocking script adapter stalled the
  event loop.
- **Subagent fan-out no longer duplicates children per item.** A subagent stage
  with both `partition_source` and `fan_out` used to run `fan_out` duplicate
  child workflows *per item* (N items x fan_out=K spawned N×K children, each
  item reviewed K times): the engine's fan-out path ran the stage once per
  item, and then `SubagentNode.execute` fanned out again internally. When
  `partition_source` is set, `SubagentNode` now runs exactly one child and
  returns its result raw — the engine owns concurrency, per-item error
  containment (`_fan_out_error`), and fan-in collection, matching the
  behavior documented in `docs/SUBAGENT-COMPOSITION.md`. The internal
  fan-out path (fan_out without partition_source, e.g. N redundant runs or
  list-sharding via `partition_key`) is unchanged.
- Subagent child session directories are now nested under
  `stage_<stage_id>/child_<n>/` (previously flat `child_<n>/`), matching the
  documented layout and preventing collisions between children of different
  fan-out items or different subagent stages in the same parent.

---

## [0.6.0] - 2026-08-21

### Added

- **Stage credit attribution** — per-stage leverage analysis identifies which stages are most predictive of final HQS. For each stage, `compute_leverage(traces)` computes the Pearson correlation `r` between the stage's per-run signal (quorum score for judge stages, `success × valid` otherwise) and the run's final HQS; stages with high `|r|` are *leverage stages*. Surfaced as a new **leverage heatmap** panel on `armature dashboard` (one row per stage ranked by `|r|`, with signal type, run count, and a `leverage` / `flat` / `noise` verdict). When sufficient data exists (`min_runs` ≥ 8 and a stage reaches `|r| ≥ 0.4`), `armature improve` weights proposal coverage by stage leverage so fixes targeting high-leverage stages are prioritized; below that guard the optimizer is unchanged. Also canonicalizes the HQS trend to formula A (HFR + `avg_latency/5000`) so the dashboard trend and the leverage analysis share one HQS definition.
- Workflow packages: `armature package build|run|verify|inspect` — self-contained,
  portable, verified workflow bundles executed by a generic `armature-runner`
  container; results dir with `receipt.json` + artifacts + optional `trace.jsonl`.
  Reference-only secrets (names in the bundle, values injected at run, fail-closed
  if unresolvable). `manifest.sha256` integrity verified before every run. A package
  with `sandbox.mode: docker` spawns its sandboxed stages as sibling containers on the
  host Docker daemon (DooD) — the runner mounts the socket and runs as root only for
  that case (least privilege otherwise). Three example packages in `examples/packages/`
  (`echo-tool`, `topic-researcher`, `sandbox-shell`) double as the Docker integration
  test corpus (`pytest -m docker`, auto-skipped without Docker). See
  docs/WORKFLOW-PACKAGES.md.

### Security

- Includes all security fixes from [0.5.1], which was bumped on `main` but not separately published to PyPI: enforcement of `safety_rules` / `strict_mode` on every tool dispatch path (the `tool_call:` stage path and the LLM ReAct path), Docker sandbox `file_write` / `file_read` path-traversal containment, and tokenized `classify_shell_command` so indirection/chaining can't bypass the classifier. See the [0.5.1] entry below for full detail.

### Changed

- Bumped version to 0.6.0 (minor bump over 0.5.0; bundles the unreleased 0.5.1 security patch with the stage-credit-attribution feature).

---

## [0.5.1] - 2026-07-28

### Security

- **`safety_rules` / `strict_mode` are now enforced on every tool dispatch path** (Claim 1). Previously the `tool_call:` stage path and the LLM ReAct tool-call path called `registry.dispatch` directly, bypassing the PRE_TOOL safety hook — so a spec author's `block` rule was silently ignored on the two most common tool-execution paths (only the `adapter:`/script path honored it). `ToolRegistry` now holds an optional `HookRegistry` (set via constructor or `attach_hooks`) and `dispatch` calls `run_pre_tool` before the handler (letting `ToolBlocked` propagate) and `run_post_tool` after. The engine attaches its hook registry to the tool registry right after `SafetyHookBuilder.register`, closing both gaps with one chokepoint. The adapter path is unchanged (no double-fire).
- **Docker sandbox `file_write` / `file_read` handlers now contain paths to the host workspace** (Claim 2). They previously did `host_workspace / rel_path` with no containment check, so an absolute `path` (e.g. `/etc/passwd`) or a `..` traversal escaped the workspace and read/wrote host files. Added `_resolve_within` (reject absolute paths, resolve, verify `is_relative_to`). The docker `shell` handler now also refuses destructive commands (returns `exit_code 126`) instead of launching the container, and `wrap_registry` preserves `reversibility`/`postcondition` via `model_copy`.
- **`classify_shell_command` now tokenizes the command so indirection/chaining can't bypass it** (Claim 3). The prefix match (`startswith`) was bypassable via `sh -c "rm -rf …"`, `echo hi; rm …`, `true && rm …`, `FOO=bar rm …`, and `/bin/rm …`. Replaced with shell tokenization: split on `;`/`&&`/`||`/`|`, `shlex`+strip env assignments, `basename`-normalize the command token, and recurse one level into `sh -c`/`bash -c` with a fail-safe depth cap. The `shell` builtin handler (`_shell_run`) now applies the classifier too — it previously ran `subprocess.run(cmd, shell=True)` with no classification, so `tool_call:`/ReAct `shell` with `rm -rf /` executed with zero checks. This is a heuristic denylist backstop, not a complete shell parser.

### Changed

- Bumped version to 0.5.1 (security patch over 0.5.0).

---

## [0.5.0] - 2026-06-29

### Added

- **`CompiledAgent` bundles may carry `safety_rules`** (`list[ToolSafetyRule]`), merged into the referencing workflow's `safety_rules` at load. Agent `block` rules are a non-overridable floor: workflow `allow` on a blocked tool is dropped and the agent's block is ordered to fire first. Enables `armature-cabinet` 0.2.0 to enforce `brakes.forbidden_actions` from the bundle.
- **`ToolSafetyRule.condition` is now optional** (`None` = applies to every call of the tool). Previously an unconditional rule required a condition that always matched, and the only practical spelling (`{field:"_", op:"truthy"}`) never matched at all.

### Changed

- `_resolve_agent_references` merges bundle `safety_rules` into the spec.
- Bumped version to 0.5.0 (additive minor bump over the released v0.4.0) and reconciled the stale `armature/__init__.py` (`__version__` was `0.3.5`) with `pyproject.toml`.

---

## [0.4.0] — 2026-06-29

### Added

- **Pluggable adapter factory + LoRA adapter skills** — `skill_library` entries can reference a registered LoRA adapter via `skill.adapter`. On tiers with `adapter_support: dynamic`, the adapter artifact is passed to the provider at runtime and the skill text is omitted from the prompt to save context; on `none` tiers the configured `fallback` policy applies. Includes `mock`, `s2l`, `trace`, `local`, `remote`, and `merged` backends; `AdapterRegistry` for versioned local storage; `MergedAdapterFactory` for parameter-space adapter merging; CLI commands `armature adapter create/promote/merge/eval`; and `watch --tune` skeleton. The pattern is developed from the **Skill-to-LoRA** paper (Zhang & Qi, CUHK, June 2026 — [arXiv:2606.16769](https://arxiv.org/abs/2606.16769)).
- Example `examples/07_lora_adapter.yml` demonstrating a mock-backed TDD skill adapter end-to-end.
- `--registry` option on `armature run` and `Harness.from_spec(...)` so workflows can use a custom adapter registry without relying on the default `~/.armature/adapters`.
- **`use_dora` + `continual_learning` adapter training options** — `adapter_factory.use_dora` enables DoRA; `adapter_factory.continual_learning` enables C-LoRA-style sequential adapter updates with frozen `R_old`, near-zero `R_delta`, and orthogonality regularizer (Zhang et al., [arXiv:2502.17920](https://arxiv.org/abs/2502.17920)). Flags propagate through all adapter backends and are persisted in `adapter_config.json` / `AdapterMetadata`.
- **Real mathematical LoRA adapter merging** — `MergedAdapterFactory` now loads source `adapter.safetensors` files and adds corresponding `lora_A`, `lora_B`, and (for DoRA) `lora_magnitude_vector` tensors. Supports optional per-source weights and enforces strict compatibility checks (same base model, rank, alpha, target modules, DoRA setting).
- **Continual adapter learning lifecycle** — all training backends resolve a prior adapter version automatically when `continual_learning` is enabled, validate compatibility, and pass the prior artifact directory to the trainer as a warm start. A production trainer can load the prior LoRA and add a near-zero delta with an orthogonality regularizer (C-LoRA).
- **Trace preprocessing for adapter training** — `PreprocessConfig` and `preprocess_examples` support score filtering, maximum conversation length, exact-message deduplication, and example caps. Configure via `request.extra["preprocess"]` or the `armature adapter create`/`update` pipeline.
- **Promotion policies** — `PromotionPolicy` abstraction with `AlwaysPromotePolicy`, `ThresholdPromotionPolicy`, and `CompositePromotionPolicy`. `AdapterRegistry.register()` and `AdapterRegistry.promote()` consult a policy before advancing the `latest` pointer. CLI gains `--min-score` and `--force` flags on `armature adapter promote`.
- **`armature adapter update` command** — single-shot command that trains a new adapter version from traces, optionally evaluates it against a workflow spec, and promotes it only if the configured policy passes. Derives hyperparameters from the prior `latest` version so continual updates are one command.
- **Agent + skill attribution on `TraceRecord`** — each LLM-stage trace now carries `agent_id`, `agent_version`, and `active_skill_ids`, persisted as new SQLite columns with an additive, per-column migration guard (existing databases upgrade in place). The engine populates them from the resolved `agent_library` bundle role (`x_source` → `agent_id`, `x_agent_version` → `agent_version`, `skills` → `active_skill_ids`); they are absent for inline `role:` stages. Per-skill tool attribution is derivable by joining `active_skill_ids` × the skill library's per-skill tools × the recorded `tools_called`.
- **`armature loop <spec>` outer-loop driver** — new CLI subcommand that runs a workflow back-to-back under a central budget (`--max-iterations`, `--max-llm-calls`, `--max-wallclock`, `--max-tokens`), with carry-forward between passes (`--carry-forward`, `--inject-as`), a `--until` Jinja2 stop predicate, `--converge` early stop on identical consecutive results, and `--interval` pacing. Each iteration is a fresh `Harness.run()` with its own run_id; the driver accounts the budget from the TraceStore and writes one `__loop__` summary trace row (run_id = the loop session id, so per-iteration HQS is not inflated). `--output` writes the full `LoopResult` as JSON. New `armature/loop/` package (`carry.py`, `logic.py`, `runner.py`) + an additive `loop` command in `cli.py`; **no changes to the engine, spec models, or validator**. Closes the per-iteration trace/report fragmentation, missing central budget, and runaway-loop risks called out in `ROADMAP.md` without the engine changes `IterateConfig` would require.

### Docs

- README, `docs/USER-GUIDE.md`, `docs/ARMATURE-SPEC-REF.md`, `docs/ARCHITECTURE.md`, and `docs/ARMATURE-PHILOSOPHY.md` now cite the Skill-to-LoRA and C-LoRA papers as the research foundation for adapter-backed skills and continual adapter learning.
- New guide `docs/ADAPTER-POWERED-TEAMS.md` explains end-to-end how to set up candidate SLM tiers, declare adapter-backed skills, create adapters, and run periodic/continual retraining from traces.
- `armature loop` documented in README §CLI, `docs/USER-GUIDE.md` §18 (Running workflows → CLI reference), and `CLAUDE.md` CLI Quick Reference. A new *Agent attribution fields* subsection in `docs/USER-GUIDE.md` documents `agent_id` / `agent_version` / `active_skill_ids`. `ROADMAP.md` notes `armature loop` as a no-engine-change option that addresses the outer-loop budget and merged-report gaps.

---

## [0.3.5] — 2026-06-17

### Added

- **`agent_library` + `Stage.agent`** — specs can now reference pre-built agent bundles instead of inlining a role. Declare bundles in `agent_library` (each entry points to an `agent.yaml` file with a `role` and optional `skill_library`); stages reference them via `agent: <key>`. At load time, `loader._resolve_agent_references` copies the bundle's role onto the stage, merges bundle skills into `spec.skill_library` (existing keys win), normalises skill paths to absolute, and clears `stage.agent`. Zero engine changes — the resolved spec is indistinguishable from a hand-authored one. This is the first building block for Armature Cabinet, a future registry of shareable, versioned agent definitions.

---

## [0.3.4] — 2026-06-15

### Fixed

- **`armature improve` / `armature optimize` crashed for non-Anthropic users** — both commands hardcoded `claude-sonnet-4-6` as the refiner model, raising an auth error for anyone without `ANTHROPIC_API_KEY`. `SelfImproveRunner` now resolves the refiner model lazily from the spec's own top tier (`frontier` → `large` → `medium` → `small` → `tiny` → first custom tier), so improvement runs on whatever provider the workflow already uses. The `ARMATURE_REFINER_MODEL` env var overrides this for users who want a dedicated refiner model. `OptimizerRunner` receives the same treatment via an `ARMATURE_REFINER_MODEL` env var that patches the optimizer's internal spec at runtime. Both commands also gain `--model` CLI flags for explicit override.
- **Missing friendly-error handling on `improve` / `optimize` / `--auto-improve`** — auth and connectivity failures in these commands emitted a raw traceback instead of the friendly `✗ No valid API key` message that `armature run` already showed. All three paths now route through `_print_provider_error`.

---

## [0.3.3] — 2026-06-15

### Fixed

- **Checkpoint + loop resume bug** — when a `loop` stage was checkpointed, `_execute_stage_with_recovery` would return the cached result from iteration 1 on every subsequent iteration, short-circuiting the loop. Fixed by writing per-iteration checkpoint keys (`stage_id__iter_N`) and loading them into a separate `_checkpoint_loop_iters` dict; completed loops also write the final result under the plain `stage.id` key so downstream stages can reference it. Added 6 new tests in `tests/runtime/test_checkpoint.py` covering full execution, mid-loop resume, and full-loop skip.

---

## [0.3.2] — 2026-06-15

### Changed

- **Renamed the run-quality metric from IHR (Implicit Harness Rating) to HQS (Harness Quality Score)** throughout the codebase, CLI/dashboard output, and documentation. The formula and components are unchanged — only the name. This removes an acronym collision with the unrelated "Intelligent Harness Runtime (IHR)" from the NLAH paper, since Armature's metric is its own composite quality score and was never derived from that work. Public symbols renamed: `IhrResult` → `HqsResult`, `compute_ihr` → `compute_hqs`, the `ihr` field → `hqs`; the `--auto-improve` threshold is now described as "HQS < 0.75".

---

## [0.3.1] — 2026-06-15

### Changed

- `armature run` now translates common LLM-provider failures (missing/invalid API key, unreachable provider, rate limit) into a concise, actionable one-line message instead of dumping a raw multi-hundred-line litellm traceback. Unexpected errors still surface a full traceback.

### Docs

- README: documented the `[wizard]` extra (required by `armature new`); corrected the `armature improve` flags (`--apply`/`--no-apply`); made the no-API-key Ollama quickstart self-contained so it works for pip-only installs; added a provider-key note to the Quick start.

---

## [0.3.0] — 2026-06-13

### Added

- `loop` configuration on `Stage` for deliberate iteration, distinct from `on_fail.loop` retry
- `IterationConfig` model: `max_iterations`, `until`, `carry_forward`, `iteration_var`, `backoff_s`, `backoff_max_s`
- `_iteration` context variable: always defined (1-based), includes `num`, `is_first`, `is_last`, `carry_forward`
- `carry_forward` for selective state passing between iterations; `null` carries entire previous result
- `loop_iteration` trace event type and `TraceRecord.loop_iteration` field (distinct from `retry_attempt`)
- Validator checks for `IterationConfig`: `max_iterations >= 1`, valid Jinja2 `until`, non-empty `carry_forward` paths, valid `iteration_var` identifier, warning when both `loop` and `on_fail.loop` are set
- Risk scoring: +4 per `loop` stage, +2 per `loop` stage with `max_iterations > 10`
- Example workflow `examples/11_iterative_refinement.yml` demonstrating the `loop` feature

### Self-Harness-inspired ([arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1)

- **Causal 3-tuple failure attribution** — `DiagnosticResult` now carries a `causal_attribution: CausalAttribution` field with three orthogonal dimensions: `terminal_cause` (execution_error / schema_validation / low_confidence / postcondition / prompt_weak), `causal_status` (spec_problem / model_problem / tool_problem), and `mechanism` (timeout / runtime_error / schema_too_strict / model_underpowered / judge_uncertain / tier_insufficient / tool_violation / prompt_missing_instruction). The refiner can target fixes at the mechanism rather than guessing from the surface symptom.
- **Declared editable surfaces** — new `self_improvement:` top-level spec field with `editable_surfaces` list. The five surfaces are `descriptions`, `schemas`, `model_tiers`, `retry_counts`, `timeouts`; defaults are `[descriptions, retry_counts, timeouts]`. `SpecRefiner` system prompt is dynamically generated to restrict the model to the declared surfaces and explicitly name locked ones.
- **K-proposal diversity** — `SelfImproveRunner` accepts `n_proposals: int` (default 1). When `n_proposals > 1`, `SpecRefiner.refine_many()` generates candidates in parallel via `asyncio.gather` with rotating diversity hints (minimize changes / fix output format / adjust model tier / tighten schema). `_pick_best_proposal()` selects the candidate whose `predicted_fixes` most overlap the active diagnostics.
- **Held-out trace-split regression gating** — `_healthy_stage_ids()` identifies stages that appear in traces but carry no diagnostics. `_proposal_regression_risk()` flags proposals that modify those healthy stages. Risky proposals are filtered before selection; if all candidates are risky the best of the risky set is used as a fallback. `ImprovementReport` gains `n_proposals_generated` and `regression_risk_count` fields (also written to the JSONL audit log).

### Tests

- 1,388 tests passing (up from 1,286 at v0.2.0)

---

## [0.2.0] — 2026-06-07

### ActiveGraph-inspired ([arXiv:2605.21997](https://arxiv.org/abs/2605.21997))

- **LLM response caching** — `LLMCache` stores responses by SHA-256 content hash (model + messages + kwargs); `--no-cache` flag on `armature run` bypasses the cache for a clean run. Subsequent runs with identical prompts are instant and free.
- **`armature replay <run_id>`** — reads TraceStore records and renders a stage-by-stage execution table (stage id, role, model, latency, success, quorum score, HQS contribution) with a per-run HQS summary. Enables post-mortem debugging of any historical run without re-executing.
- **`BehaviorRule` / `BehaviorRegistry`** — trace-triggered reactive hooks. Registered rules receive the recent trace list and fire a handler when their pattern matches. Built-in `hqs_feedback` behavior: after runs where rolling HQS drops below 0.75, the engine prints a Rich-formatted hint suggesting `armature improve`.
- **`--auto-improve` flag on `armature run`** — after execution, if HQS < 0.75, automatically calls `SelfImproveRunner.analyze()`. Safe changes are applied in-place to the spec; structural proposals that require review go to `{spec}.pending.yaml`.

### KYA-inspired ([arXiv:2605.25376](https://arxiv.org/abs/2605.25376), Veldt Labs)

- **Static spec risk score** — `compute_spec_risk(spec)` scores a HarnessSpec on five weighted factors (tool-call stages, no-judge penalty, require_approval rules, fan-out stages, strict mode credit); returns a `SpecRiskResult` with `score` (0–100), `tier` (LOW / MEDIUM / HIGH / CRITICAL), and `factors` list. Displayed automatically by `armature validate`.
- **Rogue signal counter** — `RogueSignalCounter` dataclass wired into `SafetyHookBuilder.register()`; incremented on every `ToolBlocked` event at runtime. Count appears in the `run_summary` event and in the CLI run output as `"N blocked"`.
- **Only-tighten safety rule validation** — `validate_spec()` now raises `CONFLICTING_SAFETY_RULES` when an `allow` rule targets a tool (or wildcard) that an existing `block` rule already covers. Enforces KYA's composition principle: safety rules may only tighten constraints, never loosen them.

### Long-horizon focus

- **`mission:` field on HarnessSpec** — a single-line or multi-paragraph statement of the workflow's overall goal. Automatically injected into every LLM stage's system prompt as a `[Workflow Mission]` block, followed by a `[Prior stages]` breadcrumb (compact JSON preview of each completed stage's output). Keeps agents anchored to the stated goal across long-running workflows (hours or days) without any per-stage configuration. Non-LLM stages are unaffected.

### Low-latency / streaming

- **`response_stage: true` on a Stage** — designates a single text-mode LLM stage as the streaming response. When the HTTP service executes the workflow, tokens from that stage are forwarded to the SSE event stream in real time as `{"type": "token", "content": "..."}` events. A `{"type": "response_stage_complete", "stage_id": "...", "content": "<full text>"}` event fires as soon as the response is assembled, before background stages finish. Clients can render the response immediately without waiting for `run_complete`. JSON-mode stages (`output_mode: json` / `guided_json`) silently ignore `response_stage: true` and use the normal non-streaming path.

### Named workflow registry

- **`WorkflowRegistry`** (`armature/service/registry.py`) — in-memory registry of named `HarnessSpec` objects. `load_dir(path)` scans a directory for `*.yaml`/`*.yml` specs and keys each by `spec.name`, silently skipping malformed files. `register(spec)` adds a single spec; `get(name)` returns a spec or `None`; `list_all()` returns a list of `{name, description, stages}` dicts for API enumeration.
- **`build_app(registry)` factory** — `armature/service/app.py` is refactored so the FastAPI application is constructed from an injected `WorkflowRegistry`. The module-level `app = build_app()` default is preserved for backward compatibility with existing deployments.
- **`GET /workflows`** — list all registered workflows (name, description, stage count).
- **`GET /workflows/{name}`** — workflow metadata (name, description, version, stages list).
- **`POST /workflows/{name}/run`** — synchronous run; accepts `{"inputs": {...}}`; returns `{run_id, status, result}`.
- **`POST /workflows/{name}/run/async`** — async run; returns `{job_id, status}`; poll or stream results via the existing `GET /run/{job_id}` and `GET /run/{job_id}/events` endpoints.
- **`armature serve --specs-dir <path>`** — new flag that loads all YAML specs from the given directory into a `WorkflowRegistry` at startup and prints the registration count. Existing `armature serve` (no flag) is unaffected.

### Long-horizon state & triggers

- **`continuation:` spec block** — enables long-horizon workflows that remember outputs across activations. `carry_forward` lists stage keys from a prior run to pull forward; `inject_as` names the context key they appear under (default: `prior_run`). `Harness._load_prior_context()` resolves the last completed run via `TraceStore.get_run_outputs()` and merges the values into the initial context before the DAG executes.
- **Output truncation cap raised (200 → 2000 chars)** for carry-forward stages — ensures meaningful summaries survive the round-trip without being silently clipped.
- **`triggers:` spec block** — `CronTrigger` and `WebhookTrigger` models added to `HarnessSpec` with full Pydantic validation. Model and validation layer only; firing is handled by `armature watch`.
- **`armature watch <spec>`** — daemon command that blocks until Ctrl-C. `TriggerDispatcher` runs a `_cron_loop` (powered by `croniter`) for scheduled triggers and a Starlette-backed `_webhook_server` for HTTP triggers; both fire `Harness.run()` on each event. `croniter>=2.0` added as a core dependency.

### Research-backed improvements ([arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1)

- **Cheap-evolver for `SelfImproveRunner`** — spec refinement now explicitly uses a medium-tier model (not the frontier). [arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1 shows ≤3.1pp quality difference between frontier and medium-tier evolvers; the cost savings are substantial. `SpecRefiner`'s docstring updated to reflect this intentional design choice.
- **Harness-Following Rate (HFR)** added as a fifth HQS component (10% weight). HFR = fraction of trajectories where the model adheres to harness instructions on the first attempt (`escalation_count == 0`). HQS formula updated from `0.40/0.30/0.20/0.10` to `0.35/0.25/0.20/0.10/0.10` (output_valid / success / quorum / latency / hfr). Both `TraceStore.compute_hqs` and `SelfImproveRunner._compute_hqs` updated.
- **Skill-Load Rate (SLR) diagnostic** — new `low_skill_activation` `DiagnosticCode` fires when a stage declares tools in `role.tools` but the model never invokes any. `TraceRecord` gains `tools_declared` and `tools_called` fields; `LLMNode` collects called tool names during the ReAct loop and passes them to the engine for trace recording. `SpecRefiner` system prompt updated to advise strengthening role descriptions when `low_skill_activation` is detected.

### Tests

- 1,286 tests passing (up from 1,202 at v0.1.0 release)

---

## [0.1.0] — 2026-05-26

Initial public release.

### Core execution

- YAML workflow spec format with full Pydantic validation
- Async DAG executor (Kahn's algorithm, `asyncio.gather` for parallel stages)
- Four role types: `worker`, `orchestrator`, `judge`, `researcher`
- Model tier routing with automatic escalation on validation failure
- `on_fail` retry loops with context enrichment (`_retry_attempt`, `_last_error`)
- Guided JSON output via litellm `response_format`
- Human approval gate (`gate: human`)
- Script and Python adapter nodes for deterministic steps
- LLM retry with exponential backoff (rate limits, service unavailability)
- Context management and compaction

### Parallel execution

- `SubagentNode` fan-out/fan-in with `asyncio.gather`
- Fan-in strategies: `list`, `merge`, `first`, `consensus`
- `fan_in: "consensus"` — LLM judge synthesizes conflicting parallel outputs
- `partition_key` to split a context list across N child agents
- `Stage.isolated` — strip parent context to declared inputs only

### State and observability

- `TraceStore` (SQLite) — structured per-stage trace records
- `SessionLog` (JSONL) — append-only event log, crash-safe
- `ArtifactStore` — file-backed output persistence
- HQS (Harness Quality Score) — 4-component quality metric
- `TraceRecord.inputs_hash` — SHA-256 fingerprint of stage inputs (tamper-evident)
- `TraceRecord.policy_version` — SHA-256 of active safety rules
- `TraceRecord.inputs_provenance` — per-key origin labels for every context value
- OpenTelemetry instrumentation (optional, zero overhead if SDK absent)
- `armature report --run-id <id>` — per-run text report with failure signatures
- `armature dashboard <spec>` — Rich 4-panel aggregate health dashboard

### Safety and governance

- `ToolSafetyRule` + `SafetyCondition` declarative YAML DSL
- Six operators: `contains`, `not_contains`, `equals`, `not_equals`, `matches_regex`, `truthy`
- Five actions: `block`, `warn`, `log`, `require_approval`, `allow`
- `ToolBlocked` non-retryable exception
- `safety_mode: strict` — fail-closed (deny on no-match)
- `Reversibility` enum (`FULL / PARTIAL / NONE`) on every tool
- `_tool_reversibility` pseudo-field queryable in safety rule conditions
- `ToolDescriptor.postcondition` — callable to verify tool side effects
- `PostconditionFailed` exception and `POSTCONDITION_FAILED` diagnostic code

### Memory

- `MemoryStore` — cross-run persistent memory (SQLite)
- `MemoryConfig.fresh` — opt-out of prior memories for a clean-slate run
- `MemoryStore.staleness_threshold_days` — flag aged memory entries
- `_stale_memory_keys` context injection when stale entries are detected
- `KnowledgeStore` — post-stage structured knowledge extraction

### Self-improvement

- `DiagnosticAnalyzer` — 4-code failure signature taxonomy
- `Stage.post_run` — in-run refiner stage receives `_transcript` + `_diagnostics`
- `SelfImproveRunner` — outer cross-run improvement loop (`armature improve`)
- `SpecRefiner` — frontier LLM rewrites targeted YAML sections
- `ImprovementReport` — improvement audit with drift score and governance classification
- `_load_all_verified_fixes()` — cross-cycle regression detection (drift score)
- `_classify_changes()` — auto-apply vs. review-required classification; writes `.pending.yaml`
- `RefinerResult` — carries `predicted_fixes` and `predicted_regressions`
- `_verify_predictions()` — set-math verification of prior predictions
- `ProposalStore` + `OptimizerRunner` — multi-iteration meta-harness optimizer
- `PromptBootstrapper` — injects high-quality trace examples as few-shot prompts
- `SpecDrafter` + `AutoHarness` — NL-to-spec synthesis loop
- `TraceExporter` — SFT and DPO training data export (`armature export-traces`)

### Ecosystem

- LangFuse adapter (auto-activates from env vars)
- LangSmith adapter (auto-activates from env vars)
- MCP server support (`mcp_servers:` spec section, stdio/http/sse transports)
- FastAPI HTTP service — sync (`POST /run`) and async (`POST /run/async`, SSE stream)
- LangGraph sidecar template (Docker Compose)
- Messaging channel connectors (`armature channels start`)
- Docker sandbox isolation (`sandbox: mode: docker`)
- Hermes emitter for agent bundle generation

### Developer experience

- `armature validate <spec>` — validate without running
- `armature new` — interactive spec creation wizard
- `armature doctor` — environment health check
- Skills system (`skill_library:` in spec, injected per-stage)
- `armature serve` — HTTP service
- `armature optimize <spec>` — single-shot optimizer
- `armature improve <spec> [--apply-pending]` — self-improvement loop
- `armature export-traces` — trace export for fine-tuning
- `armature dashboard <spec> [--watch] [--format json]` — health dashboard
- 1,202 tests

[0.3.3]: https://github.com/bryansparks/armature/releases/tag/v0.3.3
[0.3.0]: https://github.com/bryansparks/armature/releases/tag/v0.3.0
[0.2.0]: https://github.com/bryansparks/armature/releases/tag/v0.2.0
[0.1.0]: https://github.com/bryansparks/armature/releases/tag/v0.1.0

# Armature platform: web UI, config store, and run history

Status: accepted 2026-10-23. Owner: Stephan Heilner, with Bryan Sparks.

## Goal

Anyone who installs Armature can run `armature server`, sign in, and manage
everything from a browser. That covers workflows, roles, prompts, model
providers, secrets, teams, projects, users, and run history. The same code runs
a company instance with Okta SSO, a small instance for a client, and a personal
instance at home. Nothing instance-specific lives in git.

Today the only deployment that does this, armature-ops, keeps that data in
files edited through Claude Code. Its Node dashboard is tied to that one
deployment. This design moves the UI into Armature, ports it to Python, and
puts the data in a database.

## Constraints

- One runtime. The server is FastAPI, already Armature's `service` extra. The
  frontend stays build-free ES modules, as the existing dashboard is.
- Any SQL database. SQLAlchemy 2 Core, with no ORM, and Alembic migrations, tested on
  SQLite, MySQL 8, and Postgres 16+. No backend-specific SQL. JSON columns use
  `sqlalchemy.JSON`, and aggregation such as percentiles happens in Python.
- Workflow definitions never carry shell commands a browser user typed.
  `ScriptNode` runs `cmd:` with `shell=True`, so an editable `cmd:` would be
  remote code execution for anyone with an editor role. Commands come only from
  installed packs (see "Workflow packs").
- Templates can't run code. Every Jinja environment in Armature today is the
  unsandboxed `jinja2.Environment`, so an editor who writes
  `skip_if: "{{ ''.__class__.__mro__ ... }}"` reaches code execution without
  touching `cmd:`. All rendering moves to `ImmutableSandboxedEnvironment`, and
  a stored workflow's `{{ }}` may hold only a dotted or indexed reference to an
  input or step output, with no calls, filters, or `{% %}`.
- The armature-ops instance keeps running through the migration. Each phase
  ends in a state the live instance can run on, with a named rollback.

## Workflow format

### Requirements

The format has to stay readable in a diff, be editable from a form and from a
raw editor, be validated before it's saved, be easy for an LLM to write
correctly, and keep long prose readable.

### Survey of the current workflows

The nine specs in armature-ops total about 1,200 lines of YAML. About half of
that is `adapters:` blocks that each wrap one script: name, `cmd:`, `parse:
json`, and a timeout. The other half is stages: id, adapter or inline role,
`depends_on`, fan-out, `skip_if`, and `fail_as_value`. The long text lives
elsewhere: 27 Markdown prompt files, and `config/roles.json`, which ties each
prompt to its platforms, tools, MCP servers, and per-platform models.

So the workflows have two parts with different needs. Structure, meaning which
steps run in which order with what limits, is short and must validate.
Prose, meaning role prompts, the team lead brief, and missions, is long and
must read well.

### Decision: YAML for structure, Markdown for prose

- **Workflows are YAML**, as Armature specs already are. The pydantic model
  (`HarnessSpec`) gives the JSON Schema that drives the form editor, the
  validator, and the LLM assistant's structured output. No second format or
  converter is needed, and existing specs load unchanged.
- **Roles and prompts are Markdown with YAML front matter**, the format Claude
  Code uses for agents and skills. One document replaces a `roles.json` entry
  plus its prompt file.
- **Steps reference adapters from the catalog by name.** `adapter:
  github-review/team-lead` resolves to an installed pack's adapter. In the UI,
  adding a step means picking a step type, filling in its declared parameters,
  and wiring its inputs. Raw `cmd:` strings never appear in a stored workflow.

Markdown alone was considered and rejected for structure. It can't express a
dependency graph, fan-out, or limits without inventing a grammar that has no
schema to validate against. JSON was rejected for prose because long prompts in
JSON strings are unreadable.

A stored workflow keeps Armature's own field names. Every adapter comes from
the installed `github-review` pack, and `{{ }}` values come from run inputs
and step outputs, never from webhook text:

```yaml
name: pr-role-review
description: Review one pull request with specialist roles, then a team lead.
mission: mission/mobile-team          # a document id
contracts:
  inputs:
    - name: job_file
stages:
  - id: resolve
    adapter: github-review/resolve-pr
  - id: incremental
    adapter: github-review/incremental-check
    depends_on: [resolve]
    fail_as_value: true
  - id: assignments
    adapter: github-review/assign-roles
    depends_on: [resolve, incremental]
  - id: reviews
    adapter: github-review/role-review
    depends_on: [assignments]
    partition_source: "{{ assignments.jobs }}"
    partition_key: assignment
    fan_out: 9
    fan_in: list
  - id: verify
    adapter: github-review/build
    depends_on: [assignments]
    fail_as_value: true
  - id: team_lead
    adapter: github-review/team-lead
    depends_on: [reviews, verify]
    with:
      prompt: prompt/team-lead-pr
      model: lead
  - id: post
    adapter: github-review/post-review
    depends_on: [team_lead]
```

A friendlier second spelling (`uses`, `after`, `for_each`) was considered and
dropped. It needs a compiler whose errors map back to lines, and two formats to
document, for a gain the form editor already gives. `with:` is the one
addition: parameters validated against the adapter's schema and passed to it
through `ARMATURE_CONTEXT`, never rendered into a command.

A role looks like this:

```markdown
---
name: security
description: Secrets, insecure storage, data leaks, and authorization gaps.
applies_to: [ios, android, backend, dotnet]
model: review            # a model alias the team maps to a provider
tools: [Read, Grep, Glob, "Bash(git -C:*)"]
mcp_servers: []
---

You are the security reviewer. ...
```

Front matter is code too: `tools` can grant `Bash(*)` and an `mcp_servers`
entry names a command. A project lead may choose only tools and servers the
installed pack allowlists, and anything wider needs an admin. Adding a role is
a new document. A role runs in every workflow that selects
roles by platform, with no workflow edit. This matches how armature-ops already
treats `roles.json`.

### Model aliases

Workflows and roles name an alias, such as `review`, `lead`, or `fast`, never
a model id. Each team maps aliases to a provider and model. FamilySearch maps
`lead` to Opus 5.5 on Bedrock, and a personal instance can map it to the same
model through an API key. This lets the same pack run anywhere.

### Workflow packs

A pack is a versioned directory of code, installed from git or PyPI:

```
packs/github-review/
  pack.yaml          # name, version, adapters with parameter schemas
  adapters/          # the scripts
  workflows/         # default workflows, copied into the DB on install
  roles/             # default role documents
  prompts/
```

`pack.yaml` declares each adapter's `cmd`, parameters (a JSON Schema),
outputs, timeout, and which secrets it needs. Install refuses a `cmd` that
contains `{{`, and an install or upgrade needs an admin (with a fresh step-up when TOTP is on),
is pinned to a commit or wheel hash, and is audited. The UI builds the step form from
that schema. Only admins install or upgrade packs, because a pack is code.
Project leads compose workflows from installed adapters and edit prompts and
roles.

A run resolves `pack/adapter` refs on the host that runs it, from
`ARMATURE_PACKS_PATH` and then `~/.armature/packs`, the server's default
`packs_dir`, so every runner host needs the same packs installed.

armature-ops becomes three packs: `github-review` (PR role review, incremental
review, discuss, fix loop, and daily review), `dev-work`, and `mobile-port`.
Its scripts move with little change, because packs still run through
`ScriptNode`.

## Data model

Every table except `users` and `settings` belongs to a team. Ids are UUIDs
stored as strings, which keeps them portable across backends. Tables are
SQLAlchemy Core definitions with one module of query functions per area.

| Area | Tables |
| --- | --- |
| Tenancy | `teams`, `projects` (a repository with its `team_id`, platform, enabled workflows, and branch filters) |
| People | `users` (with OIDC `iss` and `sub` columns), `memberships` (user, team or project, role), `sessions`, `audit` |
| Definitions | `workflows`, `documents` (roles, prompts, missions), `versions` (append-only history of both), `packs` |
| Config | `providers` (per team: kind, region, model aliases), `secrets` (per team or instance), `settings`, `triggers` |
| Execution | `jobs` (queued, claimed, heartbeat) |
| History | `runs` (with trigger, cancel, and re-run actors, cost, and duration), `run_steps`, `run_events`, `findings` |

Adapters are read from each installed `pack.yaml`, not stored. Each edit of a
workflow or document appends a `versions` row with author and time, which
gives an audit trail and one-click rollback. Export and import as a directory
keeps a git copy possible for anyone who wants one.

## Users, roles, and sign-in

| Role | Scope | Can |
| --- | --- | --- |
| Admin | Instance | Everything, including packs, providers, secrets, and users |
| Team lead | One or more teams | Manage the team's projects, members, model aliases, and workflows |
| Project lead | One or more projects | Edit the project's workflows, roles, and prompts. Re-run and cancel runs |
| Contributor | Projects | Trigger, re-run, and cancel runs. Comment |
| Viewer | Projects | Read runs and history |

Sign-in supports two methods, and an instance enables either or both:

- **OIDC** with Okta or any compliant provider through Authlib. A group claim
  maps to roles, such as `armature-admins` to admin.
- **Password**, ported from the dashboard: scrypt and rate limits. TOTP
  (RFC 6238 with replay protection) and step-up for sensitive changes are
  off by default, and `require_totp: true` turns them on.

OIDC uses the authorization code flow with PKCE, and checks `state`,
`nonce`, issuer, audience, and the JWKS algorithm. Accounts link on (`iss`,
`sub`) only, never on email. Roles are recomputed from groups on every login,
so a group removed in Okta revokes the role, and only groups named in config
grant anything. Admin comes from a claim only through an explicit
`admin_groups` setting.

Sessions are server-side rows keyed by an HMAC of the token, ported from the
dashboard: 30-minute idle and 12-hour absolute limits, a `__Secure-` cookie,
an origin check plus a CSRF token on every mutation, and per-IP and per-user
throttles. One FastAPI dependency per route enforces the role and loads the
object to check its team or project, never trusting an id in the body. The
same check covers live streams, exports, re-runs, cancels, and the assistant.
With `require_totp: true`, changes to packs, providers, secrets, users, and
sign-in settings need step-up.

## Secrets

Secrets are encrypted in the database with AES-256-GCM, as the dashboard
already encrypts TOTP secrets, under a master key that never enters the
database. The key comes from `ARMATURE_MASTER_KEY`, a key file, or AWS KMS.
HKDF derives separate sub-keys for secrets, sessions, and TOTP, and a key id
in each value's prefix allows rotation. The team, secret id, and name are
bound as GCM additional data, so a ciphertext copied into another team's row
fails to decrypt. Encryption at
rest is worth it even on a trusted host, because database backups and
replicas leave the machine. The UI never shows a secret again after it's
saved, only that one is set and when.

A run receives only the secrets its pack declares, in the stage environment.
Workers start without the master key or the OIDC client secret, and sessions
keep starting under `env -i`, as armature-ops does today. Every secret
decrypted for a run is redacted by exact value from logs, events, and error
bodies.

## Model providers per team

| Kind | Used for | Credential |
| --- | --- | --- |
| `bedrock` | litellm stages and Claude Code (`CLAUDE_CODE_USE_BEDROCK=1`) | Bearer token or IAM role |
| `anthropic` | litellm stages and Claude Code | API key |
| `claude-subscription` | Claude Code sessions only | OAuth token from `claude setup-token` |
| `openai-compatible`, `ollama` | litellm stages | API key or none |

A subscription token works only for Claude Code sessions. A team that has only
a subscription can't run the inline LLM stages (the litellm path), so the
validator flags a workflow whose stages need a provider kind the team lacks.
Anthropic's terms for subscriptions also govern automated use, so check that
an unattended pipeline is allowed under the plan before you rely on one.

## Run history and live activity

### Today

The dashboard polls every 5 seconds. It reads `traces.db`, `session.jsonl`,
and five log files, and parses Python reprs out of trace outputs. Claude Code
sessions run with `--output-format json`, so nothing is visible until a role
finishes. That is why live activity is slow and shows little.

### Design

1. **Wrappers emit events, sessions don't.** Each run gets an
   `ARMATURE_EVENTS` endpoint: a mode 0600 Unix datagram socket, or a
   loopback HTTP URL for remote workers, with a per-run token sent in a
   header. Only an HMAC of the token is stored, and it expires when the run
   ends. The server takes the run and team from the token, never from the
   payload. The engine's `on_event`, the runner, and an `armature emit`
   command write to it with a 50 ms send timeout, so a down server never
   stalls a run. Session wrappers emit on a session's behalf, so a session
   under `env -i` never holds the token.
2. **Claude Code streams.** Wrappers switch to `--output-format stream-json
   --verbose` and emit each tool call, a tool-result summary capped at 2 KB,
   assistant text, and token usage. The final result is still the last
   message, so wrapper outputs don't change.
3. **Some events must be kept and some can be dropped.** Step changes, cost,
   findings, tool calls, and results are never dropped. Assistant text is
   coalesced to four updates a second. A full per-run queue drops text first
   and records a `dropped` marker, so the UI shows the gap.
4. **Broadcast first, store second.** The server assigns each event a per-run
   `seq`, pushes it over Server-Sent Events (SSE) to subscribers of that run
   or team, then a single writer flushes every second or 500 events in one
   transaction for all runs together. A slow subscriber's bounded queue
   overflows, the subscriber is dropped, and it resumes with `Last-Event-ID`.
   With several server processes, a Postgres `NOTIFY` of (`run_id`, `seq`) or
   Redis wakes the others.
5. **Completion writes the summary.** When a run ends, the server writes
   `runs`, `run_steps`, and `findings` in one transaction. Console output
   stays in log files and is linked, not stored. Events for finished runs are
   deleted after a retention period, and summaries are kept.

Event text is redacted before storage, because stream-json carries tool
results from hostile diffs, and the UI renders it as text, never HTML.

Latency drops from 5 to 10 seconds to under one second, and the database
receives one batched insert per second in total. `run_events` has only its
primary key (`run_id`, `seq`), and `runs` is indexed on (`team_id`,
`started_at`). A load harness proves it: 20 concurrent runs at 200 events a
second with 100 viewers, p99 under one second, on all three databases.

## Authoring with an LLM

The workflow editor has an assistant panel that uses the team's `lead` alias:

- **Draft.** Describe a change in words. The assistant returns a patch to the
  YAML or the role document, using structured output against the JSON Schema,
  and the editor shows it as a diff to accept.
- **Validate.** Every save runs `validate_spec`, the pack adapter parameter
  schemas, the provider check, and a lint of `{{ }}` references against step
  outputs. Errors point at the line.
- **Test.** Run the workflow in dry-run mode against a recorded trigger
  payload. Adapters run with `ARMATURE_DRY_RUN=1`. Most writing adapters in
  armature-ops already have a `*_DRY_RUN` flag that this maps to, and packs
  must honor it for every adapter that declares `writes: true`. A
  stub mode replays recorded step outputs, so the graph can be tested without
  spending money.

## Process layout

`armature server` runs the API, the UI, the event broker, and the scheduler
in one process. Runs execute in worker processes (`armature worker`) that
take jobs from a `jobs` table, so a long run survives a server restart, as
armature-ops's detached runners do today. A worker claims a job with one
compare-and-set `UPDATE ... WHERE status = 'queued'` and checks the row count,
which works the same on all three databases.

The webhook gateway stays a thin, separate process that verifies signatures
and inserts jobs. An instance doesn't need to expose the API port, and
`POST /run` with an arbitrary spec path goes away.

## Migrating armature-ops while it runs

| Phase | Change | Live instance | Rollback |
| --- | --- | --- | --- |
| 0 | Commit current state | Unchanged | armature-ops `3fc5218`, armature `ops-live` |
| 1 | Server, database, auth, RBAC, secrets, and UI shell in Armature | Unchanged. New server runs on its own port and database | Stop the new server |
| 2 | Event pipe, live view, and history. Importer for `traces.db` and the dashboard Postgres | Runs post events to the new server too. Old dashboard keeps working | Unset `ARMATURE_EVENTS` |
| 3 | Workflow, role, prompt, and provider editors. Assistant. Dry-run testing | Read-only mirror of the ops config | Not needed |
| 4 | Import ops config into the database. The server renders `teams.json`, `roles.json`, `prompts/`, and `specs/` from it | Scripts read rendered files, unchanged | Restore files from git |
| 5 | Repackage ops scripts as packs. Runners take jobs from the database | Cut over one workflow at a time, starting with `build-announce` | Point the runner back at the file queue |
| 6 | Retire the Node dashboard and the file-based config | Apache proxies `/dashboard` to the new server | Re-enable the old vhost |

Phase 4's render step is the key to zero downtime. It's the same pattern
`teams.json` already uses, where the dashboard owns the data and writes the
file the scripts read.

## Out of scope for this change

- Multi-tenant hosting of several companies in one database. Each company
  runs its own instance.
- Moving `state/` coordination files (locks, queues, and marks) into the
  database, except where Phase 5 replaces a queue.
- Script authoring in the browser.

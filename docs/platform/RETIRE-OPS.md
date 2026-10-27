# Retiring armature-ops

Status: draft for review, 2026-10-26. Extends phases 5 and 6 of
[DESIGN.md](DESIGN.md).

Goal: Armature is a service that someone can install and run on a machine at
home, and FamilySearch is one instance of it. The UI, the service, the worker,
the webhook receiver, the scheduler, and the database access all live in this
repository. Teams, projects, workflows, roles, prompts, model providers,
webhooks, triggers, settings, and secrets all live in the database and are
editable in the UI. A user adds a project (a git repository) and defines,
runs, and triggers workflows without changing code or files on disk.

Every FamilySearch workflow then runs from the server plus one private pack
repository, and the armature-ops repository is deleted. Packs are text too:
every pack file, including adapter scripts, is stored in the database as a
versioned, hashed text file that you can view and edit in the UI. Workers
fetch a pack version from the server, so no runner host needs a checkout. Git
is an import and export format only.

## Self-hosted install

These requirements apply to every slice, and the FamilySearch cutover is the
first customer of them, not an exception to them.

- One command sets up and one command runs. `armature server init` already
  writes a config, creates a SQLite database and master key, and creates the
  first admin. `armature server run` then serves the UI and API, runs an
  embedded worker, runs the scheduler, and receives webhooks on `/hooks/...`,
  all in one process. Separate `armature worker` and gateway processes are
  optional, for hosts that want them.
- The database is SQLite by default, or an external PostgreSQL or MySQL
  instance through `database_url` and the `postgres` or `mysql` extra. Every
  slice's migrations and tests run on all three, and nothing depends on one
  backend's features.
- `server.yaml` holds only bootstrap values: the database URL, the master key
  location, the listen address, the public origin, and sign-in options. It
  never names a script, a command, or a workflow. `export_on_change`,
  `actions:`, and `status:` go away in S10, and nothing new is added there.
- A workflow defined in the UI can run without any pack. The built-in step
  kinds (LLM roles, `claude:`, `tool:`, `template:`, gates) cover the common
  cases, so packs are needed only for custom code.
- A project is a git repository plus a connection. The user enters a
  repository URL and picks a connection (a GitHub App, a token, or an SSH key
  held in `secrets`), and the worker clones and caches it in its work
  directory. Enabling a workflow on a project and adding its trigger happen
  in the project's page.
- A webhook is a database row with its own URL, a source type that picks the
  signature check (GitHub, Slack, or generic HMAC), a sealed secret, filters,
  and a mapping from payload fields to workflow inputs. Adding one needs no
  restart.
- Core code names no team, workflow, repository, or channel. Anything
  FamilySearch-specific is a setting, a database row, or the `familysearch`
  pack.
- A service file for launchd and systemd ships with the CLI
  (`armature server install-service`), plus a container image, so the
  service starts on boot without hand-written plists.

## Where things stand

- The stored specs are rendered from the database by
  `tools/platform/render.py`, but every adapter still runs
  `cmd: "$OPS_HOME/scripts/..."`. None uses a `pack/adapter` reference, and
  `~/.armature/packs` holds only the `hello` pack.
- `code-review` is the database name of `pr-role-review`.
  `specs/pr-role-review.yml` differs only in `name:`, `bin/pr-review` runs
  `code-review.yml`, and history says `pr-role-review`. `specs/pr-review.yml`
  is only a rollback target.
- Core hardcodes FamilySearch workflow names: `REVIEW_WORKFLOWS` in
  `armature/server/stats.py` and the fix, merge, port, and work lists in
  `armature/server/subjects.py`.
- The server can't start a run. It has a dry-run `test` endpoint but no Run
  action, so every run in the database came from an ops script posting
  events. A workflow defined in the UI has no way to execute.
- Projects store a `repo` string but no connection or credentials, and
  nothing clones a project's repository.
- `server.yaml` on the ops host points `export_on_change`, `actions:`, and
  `status:` at scripts in the ops checkout.
- Armature already has unused `jobs` and `triggers` tables, runner keys, run
  tokens, step-up, the secrets keyring, the event pipe,
  `runtime/checkpoint.py`, and croniter. `armature server init` already sets
  up a SQLite instance end to end.
- Gaps: `HumanGateNode` reads stdin, `AdapterDef` has only `cmd:`, packs live
  only on disk, and Claude sessions use `--output-format json`, so
  `events/claude_stream.py` isn't wired to the live view.
- Scripts call side-effect helpers inline rather than as stages, for example
  `slack_ask.sh` from `jira_work_approve.sh` and `intake_chat.py`, and
  `as_repo.sh` or `gh_app_token.sh` from many scripts. Built-in tools need a
  run-scoped CLI (`armature tool ...`) as well as a stage form, or secrets stay
  in script environments.
- `gateway/app.py` holds domain routing as well as signature checks: fix
  commands, discuss replies, port checks, pull request cancel, Slack replies
  that wake waits, intake turns, and `/ci/build`.
- The queues are more than FIFOs. `bin/pr-review` keys by pull request, waits
  for a quiet period (10 minutes, 2 for discuss), detects stale jobs after
  150 minutes, and runs 3 at a time. jira-work runs 1 at a time and parks jobs
  that wait for an AWS path.
- `config/validate.json`, the tool allowlists in `config/jira-work.json`,
  `config/workers.json`, and `config/github-apps.json` hold commands or host
  paths, so they're code or host config, not team data.
- `bin/jira-watch` runs from a LaunchAgent that isn't in `launchd/`.
- `state/` is about 14 GB, almost all of it runner cache. The coordination
  state, such as `pr-findings`, marks, waits, and per-issue state, is small
  JSON.
- The legacy `armature serve` triggers in `armature/service/` have no
  signature checks and keep jobs in memory. New triggers live in
  `armature/server/`, and `serve` is deprecated.

## What each part becomes

Core is an Armature feature (slice numbers below). Tool is a built-in tool. FS
is the `familysearch` pack. AWS is the `aws-runners` pack in the deployment
repository.

| ops path | Destination |
| --- | --- |
| `specs/*.yml` | Database workflows using `pack/adapter`, `claude:`, `tool:`, and `template:` steps. Defaults ship in the FS pack. |
| `specs/pr-role-review.yml`, `specs/pr-review.yml` | Delete. Their runs are relinked to `code-review` (S0). |
| `specs/jira-work.yml` | Database workflow `dev-work`, former name `jira-work`. |
| `prompts/*.md`, `config/roles.json`, `teams.json` | Database documents, teams, and projects. Already imported. |
| `config/{jira-work,ios-port,pr-review,pr-authors,linear-intake}.json` | `familysearch.*` settings, validated by a JSON Schema in `pack.yaml`. |
| `config/validate.json`, tool allowlists | FS pack code, because they're commands. |
| `config/workers.json` | AWS pack settings. |
| `config/github-apps.json` | Core connections of kind `github-app` (S4), keys in `secrets`. |
| `config/ops.env`, `config/secrets/*.pem` | Tokens to `secrets`. Flags and limits to workflow execution settings, triggers, model aliases, or settings. |
| `templates/*.txt` | Database documents rendered by a sandboxed `template:` step (S4). |
| `bin/pr-review`, `pr-fix`, `jira-work`, `ios-port`, `intake-chat`, `lock.sh` | Core job queue and worker (S2). |
| `bin/jira-work-resume`, `rerun-reviews`, `resynthesize`, `retry_job.sh`, `pr_rerun.sh` | Core rerun and resume (S8). |
| `bin/review`, `bin/jira-watch` | Core schedule and Jira poll triggers (S3). |
| `bin/mac-prime` | AWS adapter on a schedule. |
| `bin/disk-clean` | Core worker cleanup (S2), plus an FS adapter for Gradle and Xcode pruning. |
| `gateway/` | Core webhook receiver (S3) with declarative filters. |
| `slack_post.sh`, `slack_ask.sh`, `render.sh` | Tools `slack.post` and `slack.ask` (S4, S6), and the `template:` step. |
| `jira_upsert.sh`, `jira_transition.sh`, `linear.sh` | Tools `jira.*` and `linear.*` (S4). |
| `gh_app_*.sh`, `gh_identity.sh`, `as_repo.sh`, `pr_status.*`, `pr_threads.py`, review posting | Tools `github.*` (S4). Tokens are minted on the server. |
| `claude_*.sh`, `jira_work_implement.sh` | Core `claude:` step (S7). |
| `review_report.py`, `run_reports.py`, `digest_report.sh` | Core run reports and digest (S9). |
| Review, fix, dev-work, iOS port, intake, and build validation scripts | FS adapters. Dev-work scripts are renamed `dev_work_*`. |
| `worker_*`, `session_*`, `mac_worker_*`, `mac_host_ctl.sh` | AWS adapters. |
| `tools/platform/*` | Delete after S10, once the history import runs a last time. |
| `launchd/`, `deploy/`, `infra/aws/` | Deployment repository. |
| `dashboard/`, `scripts/dashboard.*`, `*.before-*` backups | Delete. |
| `state/` queues, locks, marks, per-subject state | Core queue (S2), pack state (S5), and waits (S6), moved by import scripts at each cutover. |
| `state/` caches (repos, mirrors, worktrees, primes) | Worker work directory, never the database. |
| `tests/` | Pack repository tests. Gateway checks move to core (S3). |

`timetrack-sync` and `tools/author_value.py` move into the `familysearch` pack.

## Armature slices

Each slice is one pull request with an Alembic migration where needed, tests
on SQLite, Postgres 16, and MySQL 8, and the feature off until a workflow uses
it, so `main` ships after each one.

| Slice | Adds |
| --- | --- |
| S0 | A workflow `kind` (review, fix, merge, port, work, other), set in the workflow editor, that replaces the hardcoded name lists. Runs are grouped by `workflow_id` only, so renaming a workflow in the UI changes only its label. A migration links name-only imported runs to the workflow of the same name, and `armature server runs relink` links runs recorded under an old name. |
| S1 | Packs defined as text and stored in the database: a `pack.yaml` manifest plus text files, each version immutable and hashed. A Packs page to view, edit, and diff files, where saving creates a draft and publishing a version needs an admin with step-up and shows the declared adapters, secrets, and tools. Import from a directory or git commit and export back to one. Jobs pin pack versions. Workers materialize a version into a private directory, refusing absolute paths, traversal, and links. |
| S2 | Job queue and worker: compare-and-set claims, per-workflow slots, per-key concurrency, quiet-period debounce, leases and a reaper, cancel, and defer. The worker runs embedded in `armature server run` by default and as `armature worker` on other hosts. Each job runs detached with its own heartbeat, gets only the secrets its steps declare, and starts from a clean environment. A Run button with a form built from the workflow's declared inputs. Queue and Workers pages. |
| S3 | Triggers and webhooks, defined in the UI: cron schedules, Jira and Linear polling that enqueues each newly matching issue once, and webhook rows with per-webhook URLs under `/hooks/` served by the main process. GitHub, Slack, and generic HMAC signature checks, replay protection, declarative filters, payload-to-input mapping, and a shadow mode that records decisions without enqueuing. A separate `armature server gateway` process stays optional. |
| S4 | Connections (GitHub App, token, SSH key, Slack, Jira, Linear) with credentials in `secrets`. Projects gain a connection, default branch, and repository cache in the worker. Built-in tools for Slack, Jira, Linear, and GitHub, callable as a `tool:` step or as `armature tool` from an adapter, executed on the server so tokens never reach scripts. A sandboxed `template:` step. Every writing tool honors dry run. |
| S5 | Pack state store, scoped by team and pack, with compare-and-set and daily counters, replacing per-subject state files and daily caps. Amends the DESIGN.md out-of-scope note on `state/`. |
| S6 | Slack human gate: `slack.ask`, a `gate: slack` step, and thread bindings that route replies on finished threads to a follow-up workflow. Later, a waiting job frees its slot. |
| S7 | `claude:` step: read or edit mode, worktree, prompt and role from the database, tool and MCP allowlists, sandbox profile, model alias, timeout, output schema, stream-json for the live view, and an executor hook the AWS pack implements. |
| S8 | Rerun from a step and retry of failed fan-out branches, through the existing checkpoint. |
| S9 | Run reports from sandboxed Markdown templates, and the daily digest as a server job. |
| S10 | Remove the bridge: `export_on_change`, `actions:`, and `status:` from `ServerConfig`, and the render pipeline. Native rerun and cancel, and a Status page for queues, workers, and triggers. `armature server install-service` and the container image. |

Order: S0, S2, S4, S3, S7, S1, S5, S6, S9, S8, S10. S2 comes first because a
home install is useless until the server can run a workflow, and the
embedded worker resolves packs from disk until S1 lands. S4 and S3 follow, so
a user can add a repository and trigger workflows from it. `build-announce`
needs only S2 and S4 on the ops host, where its pack is installed on disk.

## Pack repository

```
packs/familysearch/   pack.yaml, adapters/{review,fix,dev_work,port,build,intake}/,
                      lib/, sandbox/, default workflows, roles, prompts, reports, templates
packs/talentstorehouse/  pack.yaml, the TalentStorehouse intake and its settings
tests/                current armature-ops tests, with gh, jira, and claude stubbed
migrate/              one-off importers for state/, and a verify script
```

The `aws-runners` pack and `infra/aws/` live in a deployment repository, per
the boundary rule in `CLAUDE.md`.

Each adapter declares its `cmd` (a path inside the pack), parameters,
outputs, timeout, whether it writes, its secrets, and the built-in tools it
may call. Adapters read settings from the job's definitions bundle and state
through `armature state`. The database holds the released pack. The repository
is where its tests run, and an admin imports a tested commit as a new version
or edits small changes in the UI, then rolls back by activating the previous
version.

## Cutover order

The ops gateway gets a flag per workflow, `ROUTE_<WF>=server`. With `server`,
it forwards the verified event to the server's job queue instead of the file
queue. Rolling back unsets the flag. Webhook URLs move last.

| Step | Workflow | Needs | Verify | Data to move |
| --- | --- | --- | --- | --- |
| A | build-announce | S1, S2, S4 | A dry-run CI post, then one real build | None |
| B | pr-merged | S4 Jira and GitHub tools | Replayed merge payloads in dry run, then one real merge | None |
| C | slack-intake | S4 | A test post in the intake channel | None |
| D | talentstorehouse-intake | S5, S6, S7 | New threads on the server, old threads finish on the old path | `state/intake-chat` after 7 idle days |
| E | repo-review (daily) | S3 schedule, S7, S9 | A dry-run run compared with the last file run's digest | None |
| F | code-review | S2, S3, S4, S5, S7, S9, build adapters | Canary on one repository, then 3 or more pull requests with matching reviews, one status comment, and findings continuity | `pr-findings`, `pr-marks`, `pr-status`, `pr-discuss`, `pr-context`, `pr-auto-fix`. Relink `pr-role-review` history to `code-review`. |
| G | pr-fix | F, S7 edit mode | A dry-run patch, then one real push | None, the fix queue is empty |
| H | ios-port | AWS Mac sessions, defer | One designer pull request in dry run | `ios-port-marks`, `port-parked` |
| I | dev-work | S3 Jira poll, S6, S7, AWS sessions | The poll trigger in shadow for 2 or more days against `logs/jira-work.log` | Rename `jira-work` to `dev-work`, `jira-marks`, per-issue state and threads, the `ops.jira-work` setting |
| J | Webhook URLs for the GitHub org, Slack apps, and CI | Every flag on for a week, native triggers in shadow | A week of matching enqueue decisions | None |

While a legacy run holds a subject, the server's job for that subject
defers, so the two paths never run the same pull request or issue at once.

## Teardown

1. Confirm 14 days with no job on a file queue.
2. Remove the ops LaunchAgents, including `jira-watch`, which isn't in
   `launchd/`. Install the server, gateway, and worker agents.
3. Drop the old gateway and dashboard proxies from Apache, and add the
   `/hooks` paths.
4. Move `ops.env` tokens into `secrets`, rotate the webhook secrets, signing
   secrets, CI token, and personal access tokens, and delete `ops.env` and
   `config/secrets/`.
5. Run the history import a last time, reconcile counts, then archive and
   drop the old dashboard database.
6. Archive `logs/` (2.5 GB) to S3 with a one-year retention and delete it
   locally.
7. Move caches into the worker work directory and delete the rest of `state/`.
8. Move `infra/aws/` to the deployment repository.
9. Delete the `ops.*` settings and the bridge config in `server.yaml`.
10. Archive the ops repository as read-only, tagged `final`, and delete the
    directory after 30 days.

## Risks

- A lease bug could requeue a long Claude review and double its cost, so
  Claude-heavy workflows default to one attempt.
- The gateway needs signing secrets, so it stays loopback-only behind Apache.
- A blocking `slack.ask` holds a slot until S6's second phase, which is
  acceptable at current slot counts.

## Decisions

Recorded 2026-10-26.

| Question | Decision |
| --- | --- |
| How are packs stored? | As text in the database, editable in the UI. Git is import and export only. |
| Is TalentStorehouse its own team and pack? | Yes, with its own secrets, settings, and Slack channel. |
| Keep `timetrack-sync` and `author_value.py`? | Keep both and port them into the pack. |
| Linear polling in scope? | Yes. S3 builds Jira and Linear polling together. |

Recorded 2026-10-27.

| Question | Decision |
| --- | --- |
| Who is the target user? | Someone running one instance on a home machine. Everything is set up in the UI, and FamilySearch is one instance. |
| Several processes? | One process by default, with the worker, scheduler, and webhook receiver embedded. Separate workers and gateway are optional, so job claims stay compare-and-set and the scheduler takes a leader lease when more than one server runs. |
| Where do webhooks live? | Database rows with UI editing, received by the main process. |
| Generic workflows | Generic capabilities, such as pull request review on a GitHub repository, are built-in steps and default workflows that ship with Armature. Only FamilySearch-specific scripts go in the `familysearch` pack. |

## Open questions

- Accept the S5 pack state store as an amendment to DESIGN.md?
- One `familysearch` pack, or the three packs DESIGN.md names? Given the
  generic-workflows decision, the three DESIGN.md packs mostly become
  built-in defaults, which leaves one `familysearch` pack.
- Who schedules the GitHub org and Slack app admin window for step J?

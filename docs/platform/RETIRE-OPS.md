# Retiring armature-ops

Status: draft for review, 2026-10-26, revised 2026-10-27 for remote execution
(see [Remote execution](#remote-execution) and the 2026-10-27 decisions).
Extends phases 5 and 6 of [DESIGN.md](DESIGN.md).

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

- One command runs. On a fresh checkout, `armature server run` starts in setup
  mode: it listens on loopback only, prints a one-time setup link, and a
  browser wizard walks through the database, the first admin, the AWS
  connection, model providers, teams, repositories, and webhooks. After
  setup, the same process serves the UI and API, runs the scheduler and the
  job queue, and receives webhooks on `/hooks/...`. `armature server init`
  stays as the scripted path.
- The server host only runs the service and reaches the database. It never
  runs workflow work and makes no LLM calls. Runners do the work (see
  [Remote execution](#remote-execution)). If the host can run Claude Code, an
  admin may start a runner on it, but nothing assumes one.
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
- S12's server side is built: the AWS connection, environment types, the
  per-job environment dispatcher and reaper, one-time enrollment, ephemeral
  keys bound to one job, `armature runner --enroll-token-env` and
  `--job-user`, the warm runner, and Mac host allocation. It stays off until
  a connection verifies, so the ops reapers and the new install never touch
  each other's instances. The instance-side git checkout as `armature-git`
  isn't wired yet.
- S7's `agent:` step is built: Claude Code sessions with an allowlisted
  environment, a home and config directory per session, hooks off,
  database skills as user skills, untrusted text only in context files,
  read and write access levels, one `--resume` reprompt for structured
  output, and process-group kill on timeout. A `skill` document kind
  (migration 0017) stores skills, and the job bundle carries skills and
  provider credentials. Codex has an experimental argv builder only. The
  minimal clone and worktree run inside the session's run, as the job user
  when the runner has `--job-user`, and sessions run as `armature-agent` when
  it has `--agent-user`. Pushing, MCP allowlists, roles from the database as
  prompts, and the real-CLI checks below are still open.
- Known limit, S14 auto_fix: the fix session runs the repository's own
  `.armature/build`, so pull request code runs with the session's model
  credential in its environment. Any write session that runs repository code
  has this exposure. It never sees team secrets, the event token, or a tool
  grant. Prefer short-lived per-job model credentials, such as Bedrock session
  tokens, over long-lived API keys.
- Manual real-CLI follow-up for S7, on an image with `armature-agent`:
  - Put a hook in the checked-out branch's `.claude/settings.json` that
    touches a marker file, and confirm the marker never appears.
  - Put an `@/etc/armature/runner.env` and an `@/proc/self/environ` import in
    the branch's `CLAUDE.md`, and confirm neither file's content reaches the
    session.
  - Confirm a skill under the branch's `.claude/skills` never loads with
    `--setting-sources user`, apart from the worktree cleanup that deletes it.
  - Check whether `CLAUDE.md` in the working directory loads at all under
    `--setting-sources user`. The base commit's `CLAUDE.md` files also go in
    through `--append-system-prompt-file`, so confirm the session follows
    them, and drop the file if the working-directory copy loads too.
  - Run a repo skill with a script from the base commit, such as nephi-bot's
    `run-android.sh`, and confirm the session runs it from
    `CLAUDE_CONFIG_DIR/skills` and can't edit it.
  - Confirm the CLI honors a skill's `allowed-tools` frontmatter, which the
    copy removes, and that a skill's `hooks` frontmatter stays off under
    `disableAllHooks`.
- Decision for S23: a write session starts from the base commit's rule and
  memory files, but anything it writes or restores itself, such as the
  branch's `CLAUDE.md`, is accepted. A git-command denylist can't hold on top
  of unrestricted `Bash`, `Edit`, and `Write`. A write session is isolated by
  the agent uid and the allowlisted environment. Read sessions keep the full
  guarantee.
  - Check whether `total_cost_usd` on the result line of a `--resume` session
    is cumulative or for that call alone. The node adds both lines, which
    double-counts if it's cumulative.
  - Start a Bash background task (`sleep 600 &`, `nohup`, `setsid`) in a
    session that then times out, and confirm the group kill as the agent user
    stops it. Anything that escapes is stopped by the runner's
    `kill -KILL -1` as `armature-agent` when the job ends.
  - Confirm `sudo --preserve-env=<names>` passes the names under the image's
    `env_keep` without `SETENV`, and that `HOME` and `TMPDIR` survive sudo's
    `env_reset` instead of becoming the agent user's own.
  - Confirm `Defaults>armature-agent !use_pty` keeps sudo from allocating a
    pty, so the session's stdin prompt and stream-json stdout pass through
    unchanged and the group kill reaches the CLI.
- Manual follow-up for S6 gates, on the real Linux runner image with
  `--job-user armature-job`:
  - Run a workflow with a `gate: human` stage. Confirm the run pauses, the
    gate opens on the run page, and approving it resumes the same run. That
    shows the engine key arrived on stdin through
    `sudo -u armature-job` (with `Defaults>armature-job !use_pty`), since a
    pause without a signed `run_suspend` never opens a gate.
  - From a script step in that run, read `/proc/<engine pid>/environ`, and
    confirm it holds `ARMATURE_ENGINE_FD` but no key.
- The legacy `armature serve` triggers in `armature/service/` have no
  signature checks and keep jobs in memory. New triggers live in
  `armature/server/`, and `serve` is deprecated.

## What each part becomes

Core is an Armature feature (slice numbers below). Tool is a built-in tool. FS
is the `familysearch` pack. AWS is the environment provider in
`armature/transport/` behind the `cloud` extra, with its CloudFormation and
image templates in `deploy/aws/` (S12).

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
| `worker_*`, `session_*`, `mac_worker_*`, `mac_host_ctl.sh` | AWS environment provider (S12) and `armature runner` (S11). |
| `tools/platform/*` | Delete after S10, once the history import runs a last time. |
| `infra/aws/` | `deploy/aws/` in this repository, deployed by the setup wizard and built by `armature env build` (S12). |
| `launchd/`, `deploy/` | Deployment repository. |
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
| S2 | Job queue: compare-and-set claims, per-workflow slots, per-key concurrency, quiet-period debounce, leases and a reaper, cancel, and defer. Jobs are claimed by runners (S11), not by the server process. Each job gets only the secrets its steps declare and starts from a clean environment. A Run button with a form built from the workflow's typed inputs (`type`, `default`, `enum`, defaults from project settings). Queue and Runners pages. |
| S3 | Triggers and webhooks, defined in the UI: cron schedules, Jira and Linear polling that enqueues each newly matching issue once, and webhook rows with per-webhook URLs under `/hooks/` served by the main process. GitHub, Slack, Jira, Linear, and generic HMAC signature checks, replay protection, approval of each new webhook with its sender pinned, declarative filters, payload-to-input mapping, and a shadow mode that records decisions without enqueuing. A separate `armature server gateway` process stays optional. |
| S4 | Connections (GitHub App, token, SSH key, Slack, Jira, Linear) with credentials in `secrets`. Projects gain a connection, default branch, and repository cache in the worker. Built-in tools for Slack, Jira, Linear, and GitHub, callable as a `tool:` step or as `armature tool` from an adapter, executed on the server so tokens never reach scripts. A sandboxed `template:` step. Every writing tool honors dry run. |
| S5 | Pack state store, scoped by team and pack, with compare-and-set and daily counters, replacing per-subject state files and daily caps. Amends the DESIGN.md out-of-scope note on `state/`. |
| S6 | Human gates and conversations. A gate suspends the run, saves its checkpoint to the database, frees its runner, and resumes on any runner when someone answers in Slack or the UI. A conversation is a stored thread plus one short run per incoming message, which replies with a question or reaches a decision that code enforces. The web workflow assistant becomes a conversation, so the host makes no LLM calls. |
| S7 | `agent:` step: harness `claude-code` or `codex`, a model alias, `effort`, `access: read-only` or `write`, skills, a prompt or role from the database, tool and MCP allowlists, timeout, output schema, and stream-json for the live view. Sessions load the repository's `CLAUDE.md`, its docs, and `.claude/skills`, and never its hooks. The string form `agent: <name>` keeps its existing agent-library meaning. A `skill` document kind stores skills. |
| S11 | Runners and dispatch. `armature runner` claims jobs whose requirements match its declared capabilities (`llm`, `claude-code`, `codex`, `git`, `platform:<name>`), heartbeats, receives a sealed bundle (the pinned workflow with its `call:` children, inputs, and the secrets and provider credentials its steps declare), streams events, and reports a result. A whole run executes on one runner. A warm runner in AWS takes LLM-only work. Runner keys are per team. |
| S12 | AWS environments. An encrypted AWS connection with a narrow role, environment types (`ios` on an EC2 Mac host VM, `android`, `dotnet`, and `node` on EC2 Linux), a type per project, `armature env build <type>` for images, and a provider that launches one environment per run, mints an ephemeral runner key, checks out the repository, and terminates the instance at the end. Large artifacts go to S3. The runner runs as a separate OS user from the workflow processes it starts, so a run can't read the runner key or other jobs' bundles. |
| S13 | Grammar: `call:` a stored workflow by name with typed `with:` inputs and typed outputs, resolved and pinned when the job is bundled, scope-checked, and loopable with a time budget. Model aliases resolve by project, then team, then instance. |
| S14 | Review results. A `risk` step in `code-review` (deterministic floors, then the model's level, rubric stored as a document), decisions and outcomes as typed records, the "Armature review" check run bound to the head commit, and run and thread pages that lead with the outcome, the live log, and links to the pull request, issue, or Slack thread. Threads are set explicitly by each trigger and carried by every follow-on run. |
| S15 | Setup wizard on top of S11 and S12, and the Projects page: git URL, connection, environment type, workflows, and webhooks on one screen. |
| S8 | Rerun from a step and retry of failed fan-out branches, through the existing checkpoint. |
| S9 | Run reports from sandboxed Markdown templates, and the daily digest as a server job. |
| S10 | Remove the bridge: `export_on_change`, `actions:`, and `status:` from `ServerConfig`, and the render pipeline. Native rerun and cancel, and a Status page for queues, workers, and triggers. `armature server install-service` and the container image. |

Order, revised 2026-10-27: S3 webhooks (in progress), S14 run and thread
pages (in progress), S2 and S11 together (in progress: claims, leases,
cancel, the runner API, `armature runner`, and the Runners page are in; slots
per workflow, debounce, defer, the Run button, and the Queue page are not),
S12, S13, S7, S4, S1, S6, S14 risk
and outcomes, S15, S5, S9, S8, S10. S0 is done. The queue and runners come
first because nothing runs without them, and a runner on the host proves the
protocol before AWS exists. Environments follow, so every run moves off the
host. The grammar and `agent:` step make the workflows definable in the UI.
The setup wizard comes last because it wraps all of the above.

## Remote execution

The server queues work and records results. Runners do the work.

- **One runner program.** `armature runner` connects with a runner key,
  declares its capabilities, and claims only jobs whose requirements it meets.
  A job's requirements are the union of what its workflow's steps declare: an
  `agent:` step needs `git`, its harness, and the project's platform, and an
  LLM-only workflow needs only `llm`. A job with no requirements, such as one
  queued before this change, matches any runner.
- **Three places a runner runs.**
  - A warm runner in AWS, such as an ECS Fargate service, takes LLM-only work:
    conversation turns, planning, and triage. Replies take seconds.
  - A per-run environment takes work that needs the source. The server
    launches it from the project's environment type, it runs one job, and the
    server terminates it when the job settles or times out.
  - Optionally, the server host, when an admin starts a runner there.
- **Whole runs, not split runs.** A run executes on one runner, so the engine,
  its checkpoint, and its cost meter stay in one process, and a coding
  session stays next to its checkout. A workflow that needs the source runs
  every step in the environment.
- **Bundles.** On claim, the server returns a bundle: the pinned workflow body
  and its `call:` children, the inputs, the secrets and provider credentials
  the steps declare, settings, the run ID, and a run token. The runner never
  reads the database.
- **Leases.** Runner heartbeats extend the job lease. A job waiting for an
  environment to boot is `provisioning`, with its own longer lease, so a cold
  Mac start isn't reaped as lost.
- **AWS connection.** The setup wizard deploys `deploy/aws/stack.yaml`, which
  creates a narrow role that can launch only from the stack's launch
  templates, terminate only instances it tagged, pass only the worker roles,
  and send commands only to tagged instances. The server stores that
  credential sealed with the master key, and every use is audited.
- **Environment types.** Each is a database row: kind (EC2 Linux or EC2 Mac
  with Tart), image, instance types, disk, timeouts, the capabilities it
  provides, and a health check. `armature env build <type>` builds and
  verifies an image with Packer or Tart and registers it. A project picks its
  type when it's added. Every image includes git, Claude Code, Codex, and
  `armature runner`.
- **Trust in a session.** A coding session loads the repository's
  `CLAUDE.md`, docs, and skills, but never its hooks or settings, because a
  pull request branch can change them and the session holds credentials.

## Code review, development, and risk

- **Code review.** The specialists that apply to the project's platform run
  in parallel as read-only `agent:` sessions in one environment, each
  returning findings. The team lead decides each finding: blocking, should
  fix, or dropped, with a reason. When `auto_fix` is on, a write session
  applies only the accepted findings, the build runs, the fix is committed to
  the pull request's branch, and the review runs once more. Fixes are pushed
  only for authors on the allowlist, and never for a high-risk change.
- **Development.** Triggered by Jira, Linear, or a Slack intake conversation.
  The environment checks out the latest default branch and creates a branch,
  a write session implements the issue with the named skills, the
  `pr-review` skill checks correctness and scope, and the workflow calls
  `code-review` in a loop until the lead approves or the budget runs out. A
  high-risk change stops for approval before the pull request opens.
- **Reviewed mark.** An approved review sets the "Armature review" check run
  on the exact head commit. The pull request trigger skips a commit that
  already has one, and any new push needs a new review.
- **Risk.** Every change a workflow touches gets a level. Code computes
  floors first, such as migrations or authentication files meaning at least
  medium, and the model can raise the level but not lower it. A first pass
  before the specialists picks the panel (low risk runs a smaller one), and a
  final pass after them sets the level. If the final level is higher, the
  skipped specialists run before the lead decides. The rubric is a document
  that people edit in the UI.
- **What people see.** Each run page and thread page leads with one line of
  outcome (pull request opened, commits pushed, changes recommended), the
  risk level, and links to the pull request, issue, or Slack thread. Next
  come the live log while it runs, the lead's decisions, and each
  specialist's findings as readable text. Earlier runs in a thread fold
  away as superseded.

## Models per team and project

A step names a model alias, such as `review` or `translate`, plus an optional
`effort`. The alias resolves through the project's providers, then the
team's, then an instance default an admin sets. A provider's credential is a
secret in the same scope, so one team can never use another team's models or
keys. Provider kinds cover Bedrock, Anthropic, a Claude subscription, any
OpenAI-compatible endpoint (for example Kimi or a translation model), Ollama,
and OpenAI for Codex. A translation service that isn't a chat model, such as
DeepL, is a pack adapter.

## Pack repository

```
packs/familysearch/   pack.yaml, adapters/{review,fix,dev_work,port,build,intake}/,
                      lib/, sandbox/, default workflows, roles, prompts, reports, templates
packs/talentstorehouse/  pack.yaml, the TalentStorehouse intake and its settings
tests/                current armature-ops tests, with gh, jira, and claude stubbed
migrate/              one-off importers for state/, and a verify script
```

The AWS environment provider lives in this repository, in `armature/transport/`
behind the `cloud` extra, so a new install can deploy it from the setup
wizard. The default install still never imports `boto3`.

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
- Production runners that run gated workflows should be Linux with
  `--job-user`. On Linux, `armature run` is not dumpable, so a step can't read
  the engine key from the engine's memory. On a macOS embedded worker, a step
  running as the same user with debugger access could. A double-forked daemon
  that a step leaves behind escapes the step's process group kill, and only
  the runner's per-user `kill -KILL -1` as the job user stops it.

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
| Teams | Two: FamilySearch (formerly Mobile, slug `familysearch`) and TalentStorehouse. The other teams were removed and their runs moved to FamilySearch. |
| Where does work run? | Never on the server host. Every run executes on a runner in AWS: a warm runner for LLM-only work, and a per-run environment for anything that needs the source. A runner on the host is optional. |
| How does the server reach AWS? | With a stored, narrowly scoped role from a stack the setup wizard deploys. The provider and its templates live in this repository behind the `cloud` extra. |
| Webhook trust | A new webhook holds deliveries until someone approves one, and the sender is then pinned. Jira and Linear are first-class sources. |
| Auto fix | A workflow input defaulting from a project setting. Fixes go to the pull request's own branch after the build passes, then the review runs once more, only for allowlisted authors. |
| Risk | High blocks auto-fix pushes and stops development for approval before the pull request. Low runs a smaller specialist panel. |
| Models | Defined per project or team, never shared with other teams. Steps name aliases. |
| Coding sessions | Load the repository's `CLAUDE.md`, docs, and skills, never its hooks. |
| Workflow sharing | A workflow belongs to one project. Nothing is shared across projects for now. Copying a workflow to another project is one action, so a new project starts from an existing workflow, and `call:` resolves only within the caller's project. |
| AWS isolation from armature-ops | Separate stacks, a separate project tag, and separate key and bucket names, so the ops reapers and IAM conditions, which match `project=armature`, never touch the new install's instances while both run. |
| Mac hosts | A Mac host bills for at least 24 hours, so hosts aren't per run. An admin starts and stops hosts in the UI, the server refuses release inside the minimum, and an idle host is released on a schedule. Per run, only the VM is created and deleted. |
| Repository access in an environment | A short-lived installation token scoped to one repository, available only to the checkout and push steps, which run as a separate user from the coding session. |
| Images | One Linux base image plus per-type runtime installs pinned by checksum in the environment type, as ops does today. A type may name a fully baked image instead. |
| Model credentials in an environment | Minted per run by the server: an assumed role with a session policy for Bedrock, or the team's API key. Never the instance role, so the session can't reuse it. |

## Open questions

- Accept the S5 pack state store as an amendment to DESIGN.md?
- One `familysearch` pack, or the three packs DESIGN.md names? Given the
  generic-workflows decision, the three DESIGN.md packs mostly become
  built-in defaults, which leaves one `familysearch` pack.
- Who schedules the GitHub org and Slack app admin window for step J?
- Should an adapter's `with:` stop rendering input values at load? Today they
  render at load and again at run time, so an input holding `{{ ... }}` is
  evaluated twice. A `call:` stage's `with:` already renders once.

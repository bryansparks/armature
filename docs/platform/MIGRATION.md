# Moving armature-ops workflows to the server

This page covers each armature-ops workflow that the server now runs: what it
replaced, the dry-run test that proves it acts like ops, and the shadow and
cutover steps you run. The order and the open gaps are in
[RETIRE-OPS.md](RETIRE-OPS.md).

## Importing a workflow

`armature server import-ops` writes one ops workflow into a project as
database rows: the workflow body, any role, prompt, or skill documents it
needs, the team webhook it listens on, and its webhook rule. Prompt and
template text is copied into the rows, because the server has no workflow
files.

```bash
armature server import-ops --workflow pr-merged --project familysearch/mobile-ios --dry
armature server import-ops --workflow pr-merged --project familysearch/mobile-ios
```

- `--project` takes `team/project`, by the team's slug or name, or the
  project's repository, such as `fs-eng/mobile-ios`.
- `--dry` validates everything and prints what would change, then rolls the
  transaction back.
- Every write goes through the same definition and webhook code as the UI, so
  validation and the audit log apply.
- The import is idempotent. It upserts by name, saves a new workflow version
  only when the body changed, and never deletes a row.
- A new rule starts in shadow and dry. A later import keeps a rule's shadow,
  dry, and enabled flags, so it never undoes a cutover.
- A new rule's order comes after every rule already on its webhook, so no two
  rules tie. A later import keeps the order, including one you changed.
- A job takes its rule's dry flag when it's queued, so changing the flag later
  never changes a job that's already waiting.
- Each import records a hash of what it wrote: in the version note for a
  workflow or document, and in the rule's `import_hash`. If you edited a row
  in the UI since the last import, and the import would change it, the import
  is refused with "edited since import, pass --force to overwrite". Copy your
  edit into the ops module, or pass `--force` to replace it.
- Settings the import reads, such as `slack.build_channel` or
  `jira.transition_qa`, are copied into the rows. After you change one, import
  again.
- A new webhook starts pending approval. Set its secret, and approve its first
  delivery on the webhook page.

## The dry-run test

`tests/server/test_migrate_ops.py` replays recorded deliveries from
`tests/fixtures/ops/<workflow>/` against the imported rows. For each fixture,
the test does the following:

1. Runs the delivery through the team's webhook rules.
2. Runs each matched workflow under the real engine, with a stub tool client
   that answers as the broker does in a dry run.
3. Runs each recorded tool call through the real built-in tool against fake
   Slack and Jira APIs.
4. Compares the writes with the `ops` block in the fixture.

`tests/fixtures/ops/capture.py` recorded each `ops` block by running ops'
own scripts on the same delivery, with fake `jira` and `curl` commands. It
copies only ops' scripts, templates, and specs, so it never reads `ops.env`.
Run it again when you add or change a fixture:

```bash
python tests/fixtures/ops/capture.py ../armature-ops pr-merged
```

The server escapes `&`, `<`, and `>` in Slack text, which ops didn't, so a
mention or link in posted text shows as literal text. The test expects the
escaped text. Ops sent no unfurl flags, so Slack previewed links, and the test
expects both unfurl flags on. A Jira comment is compared as paragraphs of
lines, as Jira shows it. Every other difference fails the test.

## Shadow and cutover

Each rule goes through the same four stages, in this order. You change both
flags in the rule form on the webhook page.

1. **Shadow.** The rule starts with shadow and dry on. It records whether it
   would match, and queues nothing. Check the decisions on the webhook's
   deliveries page.
2. **Dry live.** Turn shadow off and leave dry on. Jobs run, and each tool
   call that writes is recorded with its parameters on the run page, without
   reaching GitHub, Slack, or Jira. A tool that only reads, such as
   `review.marked`, `review.risk`, or `jira.get-issue`, runs for real so
   later steps see real data. Compare the recorded calls with what ops did
   for the same events. Dry applies to that rule's runs only.
3. **Stop the ops side.** Each workflow below names the step.
4. **Live.** Turn dry off, and watch the first real event.

To roll back, turn the rule's shadow on and undo the ops step.

## build-announce

Replaces ops' `POST /ci/build` route, `scripts/render.sh` with
`templates/build_announce.txt`, and `scripts/slack_post.sh`.

The workflow has one step: `slack/post` renders the ops message from the CI
payload's `app`, `version`, `build`, `track`, `notes`, and `url`. The team
webhook `ci-build` is a generic webhook with bearer authentication, and its
secret is named `CI_WEBHOOK_TOKEN`, as in ops.

Before you import, set the team or project setting `slack.build_channel` to
the channel ID. Without it, the import is refused, just as ops leaves
`SLACK_BUILD_CHANNEL` blank so that it fails closed. The channel is copied
into the rule, so a channel change needs another import. The post sets
`unfurl: true`, so links preview as they did from ops.

Dry-run fixtures: `ios-testflight` (multi-line notes with an ampersand) and
`android-play-beta` (a numeric build and no notes).

Steps:

1. Run the import into the project that owns announcements, such as
   `familysearch/mobile-ios`.
2. Set the `CI_WEBHOOK_TOKEN` secret on the `ci-build` webhook to the value ops
   uses, so CI needs only a new URL.
3. Shadow: in the CI build job, send the same payload to both the ops
   `/ci/build` URL and the `ci-build` webhook URL. Approve the first held
   delivery.
4. Dry live: turn the rule's shadow off. For one build of each app, compare
   the recorded `slack.post` text with the message ops posted.
5. Stop the ops side: remove the ops `/ci/build` URL from the CI job. Ops
   posts nothing once no build reaches it.
6. Live: turn the rule's dry off, and check the next build's message.

## pr-merged

Replaces the merge branch of ops' `POST /gh/pr`, `scripts/render.sh` with
`templates/pr_merged_comment.txt`, and `scripts/jira_transition.sh`.

Import it once into each FamilySearch project: `fs-eng/mobile-ios`,
`fs-eng/android-tree`, and `fs-eng/mobile-api-nodejs`. Each copy has its own
rule on the team's `github` webhook. A rule matches `pull_request` events with
`action: closed` and `pull_request.merged: true`, so a plain close never
matches. Its `project_from: repository.full_name` sends a merge only to the
copy of the merged repository's project, so each merge runs once. The
workflow has one step, `jira/transition`, with the `ops.env.example` values
written into it:

- It finds the issue key in the head branch, then the title, then the body.
- It moves the issue only from `In Progress`, to `In Test`, or to the
  project setting `jira.transition_qa` when it's set at import.
- It moves a Defect through `Resolve Issue` with resolution `Fixed`.
- It moves only `MEMTEMDIS` issues.
- It adds the ops comment after a move.

To change a value, edit the workflow in the UI.

Ops' per-platform override, such as `JIRA_TRANSITION_QA_IOS`, becomes the
project setting `jira.transition_qa` on that platform's project. Set it before
the import if ops.env defines one.

Dry-run fixtures: a Story keyed in the branch moves with a comment, a Defect
keyed in the title resolves as Fixed, an issue already in `In Test` and one
still in `To Do` stay put, a `MOB` key is skipped without reading Jira, a
merge with no key does nothing, a plain close matches no rule, and an iOS merge
moves to the iOS QA status.

Steps:

1. Run the import once per project: `familysearch/mobile-ios`,
   `familysearch/android-tree`, and `familysearch/mobile-api-nodejs`.
2. Add the `github` webhook URL to each watched repository's GitHub webhooks,
   with the `pull_request` event, next to the ops `/gh/pr` hook. Set the
   webhook's secret, and approve its first delivery.
3. Shadow: for a few merges, check that the rule matched every merge and
   skipped plain closes.
4. Dry live: turn each rule's shadow off. For a few merges, compare the
   recorded `jira.transition` parameters, and the run's result, with ops' log
   for the same merge.
5. Stop the ops side: on the ops host, move `specs/pr-merged.yml` out of
   `specs/`, so ops' dispatch of `pr-merged` fails and is logged. The ops
   `/gh/pr` hook stays, because code review, fix, and the iOS port still use
   it.
6. Live: turn each rule's dry off, and watch one real merge move its issue.

## code-review

Replaces the review branch of ops' `POST /gh/pr`, `bin/pr-review`'s queue,
and `specs/code-review.yml`. The import copies S14b's `code-review` and
`review-pass` starters into the project, and seeds S14b's ten review roles on
the instance when they're missing. Import it once into each FamilySearch
project.

The rule on the team's `github` webhook matches `pull_request` events with
the actions ops reviewed (`opened`, `reopened`, `synchronize`, and
`ready_for_review`). It skips drafts and bots, and matches only the
repository's review branch from ops' `teams.json`: `master` for
`fs-eng/mobile-ios` and `fs-eng/mobile-api-nodejs`, and `dev` for
`fs-eng/android-tree`. `project_from: repository.full_name` routes each pull
request to its project's copy. `key` is the repository and pull request
number, with `concurrency: supersede` and `cancel_running`, so the newest
head wins as it did in ops' queue.

Dry-run test: `tests/server/test_migrate_code_review.py`.

- The rule matches each golden in exactly one project, and skips what ops
  skipped.
- Each ops specialist session carried its S14b role's instructions.
- With the golden's team lead decision, the recorded review posts the same
  event and the same inline comments as ops.

Steps:

1. Run the import once per project: `familysearch/mobile-ios`,
   `familysearch/android-tree`, and `familysearch/mobile-api-nodejs`.
2. Shadow, then dry live, as described under "Shadow and cutover".
3. The import writes ops' `pr-authors.json` lists as each app project's
   `github.push_authors`, with `team:<slug>` entries kept, and an empty
   `github.push_exclude`, so `auto_fix` pushes only for those authors.
   `fs-eng/mobile-api-nodejs` gets no list, so nothing is pushed there.
   TODO: ops also reviewed only those authors, and never reviewed
   `fs-eng/mobile-api-nodejs`. No rule condition checks team membership, so
   every author is reviewed. Keep `fs-eng/mobile-api-nodejs` in shadow until
   there's a decision.
4. A second rule matches a pull request closed unmerged, with the same key
   and `cancel_only: true`. It ends the key's queued review and asks its
   running one to stop, as ops' `pr_cancel.sh` did, and queues nothing, so a
   closed pull request never gets a review.
5. TODO, no replacement yet: discuss mode, and thread resolution on later
   pushes.
6. Stop the ops side: set `pipelines.pr_review` to `false` for the
   repository in ops' `config/teams.json`, then turn the rule's dry off.

## slack-intake

Replaces the channel branch of ops' `POST /slack/events` and
`specs/slack-intake.yml`. The rule on the team's `slack` webhook is a
conversation rule (S6B) for `message` events in the intake channel. Each
person's message is a turn: `conversation/history` reads the thread, an
`agent:` classifies the newest message with ops' instructions and principles,
`jira/create` files a new issue, or `jira/comment` adds the message to the
named issue. Then `conversation/reply` posts the issue's link in the thread. The message reaches the model only as a context
file, never in the prompt.

Before you import, set the team settings `slack.intake_channel` to the
channel ID, `jira.project` to the project key that new issues go in (ops'
`JIRA_PROJECT`, `MEMTEMDIS`), and `jira.site_url`. `jira.create` sends only
the issue type, summary, description, and labels, and only into the
`jira.project` project. Both are copied into the rows, so a change to
either needs another import. The classify session uses the `lead` model alias,
since ops judged with its large model.

Dry-run test: `tests/server/test_migrate_slack_intake.py`.

- A create makes ops' new issue, and a comment makes ops' Jira comment, each
  with its thread reply.
- An ignored message and a low-confidence one do nothing, as in ops.
- Bot posts, edits, and other channels match no rule.

Steps:

1. Run the import into one project, such as `familysearch/mobile-ios`.
2. Shadow, then dry live.
3. The conversation stays open, so a reply in the thread is its next turn and
   is classified as ops classified every message. The S6B turn caps and the
   idle close apply on top.
4. Stop the ops side: unset `SLACK_INTAKE_CHANNEL` in ops, point the Slack
   app's event Request URL at the `slack` webhook URL, and then turn the
   rule's dry off.

## repo-review

Replaces ops' launchd job at 06:30 and `specs/repo-review.yml`. Import it
once into each FamilySearch project. Each copy runs the specialist roles
that ops' `roles.json` gave its platform, as read-only `agent:` sessions.
Every role runs for `fs-eng/mobile-ios` and `fs-eng/android-tree`, and every
role but `beauty` runs for `fs-eng/mobile-api-nodejs`. Each session gets
ops' daily preamble, and its role document leads its instructions. The team
lead and the digest get ops' instructions, and read the reviews and the
lead's decisions from context files. The import adds an S23 `schedule`
trigger at `30 6 * * *` America/Denver, created disabled. Its inputs are the
repository and its review branch.

Dry-run test: `tests/server/test_migrate_repo_review.py`.

- The panel per platform matches ops' `roles.json`.
- With a recorded day's reviews, the lead and the digest receive what ops'
  did.
- The run makes no tool calls.

Steps:

1. Run the import once per project.
2. A schedule has no shadow or dry mode. Run the workflow by hand once from
   its page, and compare its digest with the day's ops digest.
3. TODO, no replacement yet: ops reviewed the range since its last mark
   (`state/marks`). Without S5 state, each run reviews the last 24 hours, and
   a quiet day still runs the panel.
4. TODO, no replacement yet: the S14b roles open with the pull request
   preamble, which the daily preamble then has to override. The roles need a
   variant without the preamble.
5. TODO, no replacement yet: ops attached `digest.md` as the run's report.
   Without S9, the digest is the `digest` step's output.
6. TODO, no replacement yet: ops' post-run `self_analyst` stage.
7. Stop the ops side: unload ops' launchd review job, then enable the
   schedule.

## pr-fix

Replaces the fix branch of ops' `POST /gh/pr` and `specs/pr-fix.yml`. The rule
on the team's `github` webhook takes a new reply in a review thread that starts
with `/armature fix`, routes it with `project_from`, and queues one fix per pull
request at a time. The body is built from S14b's starters, in ops' order:

1. `github/permission` asks GitHub, through the installation, for the
   commenter's access. Anything but write or admin gets a reply on the pull
   request, and nothing else runs, as ops declined.
2. `review-pass` reviews the head, and posts its findings.
3. `review/pushable` checks the push preconditions, and `code-review`'s write
   session fixes the findings and commits.
4. `github/push` pushes the fix commit to a new
   `armature/fix-<number>-<commit>` branch, which the push gate allows without
   a risk, since it's a new `armature/` branch with no pull request.
5. `review-branch`, a copy of `review-pass` that reviews that branch and posts
   or marks nothing, reviews the fix. Its `review/risk` records the branch's
   head.
6. Only when that review approves at low or medium risk does `github/push`
   push the same commit to the pull request's branch. The gate allows it,
   because a `review/risk` followed the run's last push. Then
   `review/decisions` records the fixes, and `review-pass` reviews and marks
   the new head.

The import copies `review-pass` and `review-branch` into the project. The
`armature/fix-` branch stays after the run. Delete it by hand, or leave it as
the record of what was reviewed.

Dry-run test: `tests/server/test_migrate_pr_fix.py`.

- The rule takes the goldens' requests.
- A requester without write access is refused with a reply.
- An unapproved fix never moves the pull request's head.
- An approved fix pushes the reviewed commit to the pull request.

Steps:

1. Run the import once per project.
2. Shadow, then dry live. In dry mode the push gate records both pushes and
   sends nothing.
3. TODO, no replacement yet: ops also took `/armature fix` as a pull request
   comment (`issue_comment`, the form all three goldens record) or as a review
   body. Those payloads carry no head commit or branch, and no rule reads them
   from GitHub yet.
4. Difference: ops refined up to three rounds, and reused its stored findings
   instead of reviewing the head again first.
5. Stop the ops side: set `PR_FIX_ENABLED=0` in ops, then turn the rule's dry
   off.

## development

Replaces ops' `bin/jira-watch` poll and `specs/development.yml`. Import it
once into each FamilySearch project. The import also copies S14b's
`review-pass`. Each copy's S23
`jira_poll` runs ops' JQL through the team's Jira connection, narrowed to the
components that ops' `development.json` mapped to that repository: `Android`,
`iOS` and `Mobile iOS`, or `Mobile API`. The poll starts disabled and in
shadow, and seeds on its first poll, so it doesn't queue every open issue.

Each job asks a project lead first, through a `gate:`, as ops asked in Slack.
`jira/get-issue` reads the issue. An `agent:` write session gets the summary,
description, and comments as context files, implements the issue with ops'
`jira_work.md` instructions, and commits it. `github/push` pushes the commit
to a new `armature/<issue>` branch. Then come `development-review`'s stages,
copied into the body rather than called, so its gate pauses the job:

1. A session rates the pushed branch. Its `agent.ref` checks out that branch,
   while the implement session worked on the base.
2. `review/risk` records the risk.
3. A high-risk branch waits at a `gate:` for a project lead.
4. `github/open-pr` opens the pull request with the session's title and body.
5. `call: review-pass` reviews it.

Dry-run test: `tests/server/test_migrate_development.py`.

- Each project polls ops' JQL for its own components, and a re-import keeps
  the poll's shadow flag.
- A declined start does nothing.
- The gates pause the job itself, and a high-risk branch waits at its gate
  before any pull request opens.
- A low-risk branch opens its pull request and reviews it.
- An already-fixed issue pushes nothing and opens no pull request, as in ops.
- The session gets ops' instructions, and the issue as context files.

Steps:

1. Add the team's Jira connection, then run the import once per project.
2. Enable each poll in shadow, and compare the issues it records with ops'
   `logs/jira-work.log` for two days. Then turn shadow off.
3. TODO, no replacement yet: ops' four specialist consults and the tech
   lead's brief wait for S14b roles without the pull request preamble.
4. TODO, no replacement yet: ops picked a platform with a model when an issue
   had no component. No project's poll matches such an issue.
5. TODO, no replacement yet: ops posted to the issue's Slack thread and let
   the session ask questions there. S6B conversations don't reach a
   development run yet.
6. Stop the ops side: set `DEVELOPMENT_ENABLED=0` in ops, then turn the poll's
   shadow off.

## ios-port

Replaces ops' `_wants_port` in `POST /gh/pr`, `config/ios-port.json`, and
`specs/ios-port.yml`. Import it only into `familysearch/android-tree`, where
it writes. The rule on the team's `github` webhook matches pull requests in
`fs-eng/mobile-ios` that are opened, reopened, or ready for review, by an
author in ops' list (`RyanPlumb`), skipping drafts and bots. It supersedes by
pull request. `github/pr-diff` reads the iOS pull request's changed files
and patches, which it can do for any repository of a project in the team,
capped at 400 KB. An `agent:` write session gets them as context files, ports
the change with ops' `ios_port.md` instructions, and commits it.
`github/push` pushes the commit to `armature/ios-port-<number>`. Then
development's copy of `development-review`'s stages rates the branch, holds a
high-risk port at a `gate:`, opens the pull request, and reviews it.

Dry-run test: `tests/server/test_migrate_ios_port.py`.

- The rule takes what ops ported, into android-tree, and skips other authors,
  drafts, and pushes.
- The session reads the iOS diff as data, and a port that already matches
  pushes nothing, as in ops.
- The approval gate is in the job's own workflow, so it pauses.

Steps:

1. Run the import into `familysearch/android-tree`.
2. Shadow, then dry live.
3. TODO, partly replaced: ops also gave the session each changed file after
   the change, and the whole iOS app at its head. `github/pr-diff` returns
   only the patches.
4. TODO, no replacement yet: emulator screenshots, which no environment can
   take yet, and a tool to push them.
5. TODO, no replacement yet: ops also ported a pull request when it merged
   unported, and checked its port marks first.
6. Differences to accept or close: ops opened a draft pull request, after
   four specialists reviewed the port and a tech lead had it refined.
   `development-review` opens a ready pull request and reviews it with the full
   panel. Ops' Slack thread for questions has no replacement yet.
7. Stop the ops side: set `IOS_PORT_ENABLED=0` in ops, then turn the rule's
   dry off.

## localization

Replaces ops' `bin/localization` and `specs/localization.yml`. Import it into
`familysearch/mobile-ios` and `familysearch/android-tree`. As in ops, it runs
by hand. Start it from the workflow's page, with the repository, the pull
request's number, its head branch as `ref`, and its base branch as `base`. An
`agent:` write session with ops' translation instructions finds the strings
that changed, edits every language file, and commits the result. Its model
is the project's `translate` alias. With `mode: draft`, ops' default, nothing
is pushed. With `mode: preview_branch`, `github/push` pushes the commit to
`armature/l10n/pr-<number>`, as ops did.

Dry-run test: `tests/server/test_migrate_localization.py`.

- A draft run translates with ops' placeholder, escape, and markup rules, and
  pushes nothing.
- A preview run pushes to ops' branch name.

Steps:

1. Run the import into each app project, and set the `translate` model alias.
2. Run it on a pull request that ops translated, and compare the commit with
   ops' patch.
3. TODO, no replacement yet: ops detected the strings, pruned deleted keys,
   and checked placeholders with FamilySearch pack scripts
   (`localization_strings.py` and its siblings), and ran one session per
   language. With no FamilySearch pack on the server, one session does all
   of it, and a pull request with nothing to translate still costs a
   session.
4. TODO, no replacement yet: ops saved a draft as a patch file. Without S9,
   the commit stays in the run.
5. TODO, no replacement yet: ops' `push` mode, which pushed to the pull
   request's own branch.

## Groundwork for the remaining workflows

The workflows below don't have bodies yet. Their bodies depend on S14b
(review roles and review tools), S6B (conversations), and S23 (schedule
and poll triggers). What's ready now:

- **Captured goldens.** `tests/fixtures/ops/<workflow>/` holds two to four
  deliveries per workflow, each with ops' recorded behavior. `capture.py`
  records every `gh`, `jira`, `curl`, `aws`, and git network call, and each
  Claude session's prompt, flags, and the files those flags name. Each session
  answers with a canned result, so a new agent stage can be compared on its
  inputs and on the tool calls that follow. A role stage records its rendered
  prompt.
- **Documents.** `armature/server/migrate_ops/ported.py` lists each
  workflow's missions and prompts, which are copied from ops under
  `armature/server/migrate_ops/documents/` and checked against
  `MANIFEST.json`. `tests/fixtures/ops/port_documents.py` makes the copies
  again. The ten review roles and the PR team lead aren't copied, because
  S14b seeds them. Repository skills, such as `nephi-bot` and `pr-review`,
  load from the base ref. Ops has no skills of its own to port.
- **Mapping notes.** For each ops step, the tables below name the platform
  primitive that replaces it. "None yet" marks a gap.

Primitives: `agent:` is an S7 Claude Code session. Tools are S1 built-ins
such as `slack.post`, `jira.*`, and `github.*`. `call:` runs another stored
workflow. `gate:` is an S6 approval. Review tools are S14's
`github.post-review`, check run, and risk. Triggers are S23's schedule and
poll triggers. Conversations are S6B.

### slack-intake

| Ops step | What it does | Replacement |
|---|---|---|
| gateway `/slack/events` | Verifies the signature, drops bots, edits, retries, and other channels | Slack webhook, with rule conditions on `event.type`, `event.subtype`, `event.bot_id`, and `event.channel` |
| `classify` (role) | Judges intent, confidence, summary, type, and key | LLM stage with `prompt/slack_intake_classify` and the `principles` layer |
| `created` | `jira_upsert.sh create` | Tool `jira.create` |
| `commented` | `jira_upsert.sh comment` | Tool `jira.comment` |
| `reply` | `render.sh` with `slack_intake_reply.txt` | Template inlined in the `slack.post` text |
| `notify` | `slack_post.sh` in the thread | Tool `slack.post` with `thread_ts` |
| confidence below 0.7 | `skip_if` on each step | `skip_if`, or a `gate:` to approve each ticket |

### repo-review

| Ops step | What it does | Replacement |
|---|---|---|
| launchd at 06:30 | Starts the daily run | S23 schedule trigger |
| `repos` | Watched repos with `daily_review` on | Project setting, or one copy per project |
| `sync` | Fetches, resets, and reads the range since the last mark | Environment checkout, plus S5 state for the last reviewed SHA: none yet |
| `assignments` | Roles per platform from roles.json | The workflow's role list (`ported.ROLES`) |
| `reviews` | Eight read-only Claude sessions with `_shared.md` | `agent:` read sessions as the starter roles. The roles carry the PR preamble, so the daily preamble needs either S14b roles split into preamble and body, or role bodies kept here: open |
| `team_lead` (role) | Decides what changes | LLM stage with `prompt/repo_review_team_lead` |
| `digest` (role) | Writes the Slack post | LLM stage with `prompt/repo_review_digest`, then `slack.post` |
| `report` | Attaches `digest.md` | S9 report: none yet |
| `self_analyst` | Post-run review | `post_run` stage with `prompt/repo_review_director` |

### code-review

| Ops step | What it does | Replacement |
|---|---|---|
| gateway `/gh/pr` and `bin/pr-review` queue | Filters, debounces, applies newest-head-wins, and cancels on close | GitHub webhook rule, plus S2 per-key concurrency and debounce: none yet |
| `resolve` | Reads the PR, applies the author allowlist, and sets up the worktree, range, and findings continuity | `github.pr` read tool, environment checkout, `review.authors` setting, and S5 state: partly none yet |
| `status` | Status comment updated in place | S14 check run, or the `github.pr-comment` upsert |
| `incremental` | Small-diff Claude session with the incremental schema | `agent:` with `prompt/incremental_pr` and `schemas/incremental.json` |
| `assignments` | Panel by platform | `ported.ROLES["code-review"]`, plus S14 `risk` for a smaller panel |
| `reviews` | Nine read-only sessions | `agent:` read sessions as the starter roles |
| `verify` | `pr_validate.sh` builds the branch | A build adapter in the FamilySearch pack. The Mac VM build has none yet |
| `team_lead` | Claude session with the team-lead schema | `agent:` as starter `team_lead` with S14b's schema |
| `post` | Inline review, status, and auto-fix follow-up | S14 `github.post-review`. Thread resolution and discuss mode have none yet |

### pr-fix

| Ops step | What it does | Replacement |
|---|---|---|
| `/armature fix` comment | Checks write access, then queues | GitHub webhook rule on `issue_comment`, plus the permission check in the S1 push gate |
| `prepare` | PR, threads, last review, and worktree | `github.*` read tools and environment checkout |
| `fix` | Write session with `pr_fix.md` or `pr_fix_merge.md` | `agent:` write session with `prompt/pr_fix` or `prompt/pr_fix_merge` |
| `review` | Re-review loop, at most three rounds | `call: code-review` in a loop with a budget (S13) |
| `push` | Pushes, resolves threads, and opens a fix PR from a fork | Tool `github.push` (gated, capped), and `github.open-pr` for a fork. Thread resolution has none yet |

### development

| Ops step | What it does | Replacement |
|---|---|---|
| `bin/jira-watch` poll | Issues assigned to me in the open sprint | S23 Jira poll trigger |
| `prepare` | Reads the issue | Tool `jira.get-issue` |
| `platform` (role) | Picks the platform when the component doesn't | LLM stage with `prompt/development_platform` |
| `approve` | Asks in Slack, then waits | `gate:` with a Slack prompt (S6), and S6B for thread replies |
| `setup` | Worktree on the platform's base | Environment checkout on the platform's environment type |
| `consult_assignments` and `consults` | Read sessions with `_shared_jira_consult.md` | `agent:` read sessions as the starter roles, plus `prompt/_shared_jira_consult` (same preamble question as repo-review) |
| `brief` (role) | Merges the advice into a brief | LLM stage with `prompt/development_brief` |
| `implement` | Write session with `jira_work*.md`, the platform build, and devices | `agent:` write session with `prompt/jira_work` or a variant. iOS hardware gating needs S12 Mac hosts |
| `review` | Local review loop | `call: code-review` in a loop (S13) |
| `publish` | Opens the PR, moves the issue, and posts in Slack | `github.open-pr`, `jira.transition`, `slack.post`, and the S14 check run |

### ios-port

| Ops step | What it does | Replacement |
|---|---|---|
| gateway `_wants_port` | mobile-ios PRs from ios-port.json authors | GitHub webhook rule with an author condition |
| `prepare` | Reads the PR and asks in Slack | `github.*` read and `slack.post`, or a `gate:` |
| `setup` | Android worktree | Environment checkout on the android environment type |
| `source` | Source diff from mobile-ios | `github.*` read of the other repo's diff: none yet (cross-repo read) |
| `implement` | Write session with `ios_port.md` | `agent:` write session with `prompt/ios_port` |
| `review_assignments` and `reviews` | Four read sessions | `agent:` as starters `beauty`, `conventions`, `simplicity`, and `correctness`, or `call: code-review` |
| `decide` (role) | Decides what to refine | LLM stage with `prompt/ios_port_decide` |
| `refine` | Second write session | `agent:` with `resume` |
| `publish` | Draft PR | `github.open-pr` (draft) |
| `screens` | Emulator screenshots pushed to a branch, then a PR comment | None yet: no Android emulator in an environment, and no tool that pushes a file |

### localization

| Ops step | What it does | Replacement |
|---|---|---|
| PR trigger | Strings changed in a PR | GitHub webhook rule |
| `prepare` | PR and worktree | `github.*` read and environment checkout |
| `detect` | Finds added, changed, and deleted strings | FamilySearch pack adapter (`localization_strings.py`) |
| `translate` | One session per language | `agent:` sessions with `prompt/localization_translate`, using a per-project model alias |
| `prune` and `check` | Removes deleted keys and checks placeholders | FamilySearch pack adapters |
| `publish` | One commit, by default only a patch on the run | `github.push` behind its gate, or a run artifact |

### Open items for the capture tool

`tests/fixtures/ops/builders/` holds the scripts that write each fixture's
inputs. Run a builder with `OPS_CHECKOUT` pointing at an ops checkout, and
then run `capture.py`. These are known limits of the capture tool:

- **Binary files.** Fixture `files` and `repos` hold text only, so ios-port's
  `screens` step always posts its no-screenshots comment.
- **Session edits.** A fake Claude session can't change files. The pr-fix and
  development fixtures put their change in `fixture/session-edit.patch` and
  apply it with `git apply` in front of the repo's lint-fix command. A
  migrated engine has to run that command the same way, or those three
  goldens diverge. ios-port and localization build the change into the
  repository instead. A fake answer that applies a patch would remove this
  workaround.
- **Parallel sessions.** The call counter in `used.json` isn't locked, so
  parallel review roles can corrupt it. The pr-fix and development fixtures
  set `PR_FIX_REVIEW_FAN_OUT=1` and `DEVELOPMENT_REVIEW_FAN_OUT=1`.
- **Commit dates.** Commits the scripts make are pinned only through each
  fixture's `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE`. The tool should pin
  them itself.
- **Standard input.** The `gh` fake reads standard input when the real `gh`
  wouldn't, so inside a `while read` loop it swallows the rest of the input.
  ios-port's `ported-draft-pr` keeps to one iOS file for that reason.
- **AWS paths.** Sessions that run on AWS or a Mac VM aren't captured. Their
  files come back from S3, and the AWS path sleeps for real. The fixtures set
  `IOS_PORT_AWS=0`, `DEVELOPMENT_AWS=0`, and `AWS_WORKER_FALLBACK=1`.
- **Timing fields.** A few outputs, such as `verify.seconds` and the digest's
  `attached`, depend on the machine. Compare them loosely.

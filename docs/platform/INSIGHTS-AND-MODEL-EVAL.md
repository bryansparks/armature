# PR insights and model evaluation

Status: in progress, 2026-11-13. Built on branch `pr-insights`, one phase at a time.

This design adds two features to the Armature server that share one data
store:

- **PR insights** records every pull request in the repositories you choose,
  including pull requests Armature doesn't review. It measures where
  development slows down, which mistakes recur, and how much each person
  contributes, and it opens fix pull requests for recurring problems behind an
  approval gate.
- **Model evaluation** records the exact model and effort behind every step
  and finding. It runs shadow copies of a code review with a different model
  or effort, and it learns which settings are worth their cost.

Both features feed the same loop. Every run adds evidence, a weekly job turns
the evidence into proposed changes, and a person approves the changes. That
loop extends the trace-driven tuning in
[HQS and self-improvement](../HQS-AND-SELF-IMPROVEMENT.md) from single specs
to the review process and the developers it serves.

## Goals

1. Track every pull request in a chosen set of GitHub repositories, whether or
   not Armature reviews it.
2. For each pull request, record its author, change type, every push and what
   that push was, review rounds until approval, time lost to merge conflicts,
   CI results, merge time, and any later revert or bug fix traced back to it.
3. For each person across repositories, measure what they ship, the quality
   of what they ship, and the review work they do for others.
4. Keep every specialist finding, including findings the lead dropped, along
   with the lead's decision on each one.
5. Group recurring findings into categories, report them, and fix recurring
   problems automatically behind an approval gate.
6. Record the model ID, provider, tier, and requested and reported effort for
   every step and finding.
7. Compare models and effort levels on real reviews without touching the real
   review, and recommend changes to tier and effort settings from the results.

## Non-goals

- A single combined score per person. The person view shows separate scores
  for shipping, quality, review contribution, and unblocking, so tradeoffs
  stay visible.
- A second database or Postgres schema. See [Storage](#storage).
- Applying any change automatically. Fix pull requests, tier changes, and
  effort changes all wait for a person's approval.

## What exists today

The scout and the `data` and `correctness` specialists mapped the current
code. These facts shape the design.

| Area | Current behavior | Consequence |
| --- | --- | --- |
| Findings | `findings` (`armature/server/db/schema.py:592`) stores only the lead's decisions. Raw specialist output lives in `run_steps.output`, which events over the size limit cut down (`armature/events/emitter.py:141`). | Findings the lead dropped silently have no row. |
| Prior decisions | `armature/server/tools/context.py:184` feeds the latest `findings` row with a decision for a `(project_id, finding_key)` on the same pull request to the live lead as "Last decision". That includes rows a runner-registered run recorded on that pull request. | Any new row with a decision in `findings` reaches the real review. Specialist candidates and shadow-arm output must go elsewhere. |
| Finding identity | `finding_key` is the first 12 hex digits of a SHA-1 of the path and claim (`armature/server/tools/review.py:82`). It isn't scoped to a pull request. | Reworded claims get new keys, and the same claim on two pull requests collides. |
| Finding lineage | `findings.lineage_key` holds the `finding_key` of the earliest finding on the same pull request and project that a finding restates in an earlier round: the same key, else the same path within 15 lines, or either one without a line, and enough shared words (`armature/server/rounds.py`). Otherwise it holds the finding's own key. The server derives it, and never reads it from a payload. | A reworded claim keeps one lineage across rounds, while `finding_key` stays the GitHub marker ID. |
| Webhook deliveries | Deleted after 14 days. Payloads over 1 MiB are stored as NULL (`armature/server/webhooks.py:64`, `:83`, `:1093`). | Pull request history must be pulled out into durable rows when it arrives. |
| PR author | Read only from the delivery payload (`armature/server/subjects.py:137`). | Lost after 14 days. |
| Review rounds | `review_marks` keeps one row per approved head, and a later run overwrites it (`armature/server/tools/marks.py:221`). | Rounds have to be counted from posted reviews. |
| Model attribution | The cost event records the starting tier's model (`armature/runtime/engine.py:845`). The fallback order can retry a failed `large` stage on `tiny` (`armature/nodes/llm.py:549`). Ingest drops `model` (`armature/server/ingest.py:709`). | A step's recorded model can be wrong. |
| Effort | Passed to the CLI (`armature/nodes/harness.py:90`) and never reported back. Review steps set no effort, so the CLI default applies. | No run records its effort. |
| Shadow mode | Today "shadow" means a rule records that it would have matched and queues nothing. Dry mode skips `review.decisions` (`armature/server/broker.py:442`). | No mode exists that runs a variant and stores its output. |
| Environments | A job's environment type comes from its project (`armature/server/environments.py:801`). | A shadow arm needs another way to choose its environment. |
| Fix-PR guards | `may_open` allows a low- or medium-risk run to open a pull request with no gate (`armature/server/tools/marks.py:356`). The daily push cap counts per branch. | Self-healing needs its own gate and its own cap. |

## Storage

All new data goes in the Armature database as new tables with prefixes
(`gh_`, `eval_`), in the same SQLAlchemy `MetaData`. The server has one
engine and no schema translation (`armature/server/db/__init__.py:23`).
SQLite has no schemas, and `tests/server/test_migrations.py` compares one
`MetaData` against the migrations. A separate Postgres schema or a Supabase
instance would add a second connection path for no benefit, because the
server already provides authentication, team scoping, and the UI.

Every new table follows these rules:

- `team_id` is `NOT NULL`, so `rbac.scope_clause` (`armature/server/rbac.py:108`)
  can filter it.
  `daily_reports` is the one exception. It holds a single instance-wide
  report per day, and only an admin reads it, as with `settings`.
- GitHub database IDs are `BigInteger`, and every column in a unique
  constraint is `NOT NULL`, because NULLs never collide.
- Rows are keyed on GitHub's immutable IDs, never on a delivery, so a webhook
  and a backfill that describe the same object produce one row.
- Inserts copy the insert, catch `IntegrityError`, then update pattern at
  `armature/server/tools/marks.py:325`. Updates apply only when the incoming
  `gh_updated_at` is at least the stored value, so a late webhook can't roll
  back a newer backfill.
- Indexed strings are 191 characters or fewer, and timestamps use the UTC
  `_ts` helper.

The first migration is `0029`. It creates the tables and adds only nullable
columns to existing tables through `batch_alter_table`, with a full
downgrade.

### Tables

GitHub history, written from webhooks and from backfill:

| Table | Unique key | Holds |
| --- | --- | --- |
| `gh_repos` | `(team_id, gh_repo_id)` | A tracked repository, its optional `project_id`, and whether tracking is on |
| `gh_pull_requests` | `(team_id, gh_pr_id)` | Number, author's GitHub ID and login at the time, base and head refs, state, created, merged, and closed times, and the merge commit |
| `gh_pushes` | `(pr_row_id, after_sha)` | `before_sha`, `base_sha`, pusher, push class, the evidence for the class, and its confidence |
| `gh_reviews` | `(team_id, gh_review_id)` | Reviewer, state, head SHA, submitted time, comment count, and body length |
| `gh_review_comments` | `(team_id, gh_comment_id)` | Author, path, line, head SHA, body length, and URL. The text stays on GitHub. |
| `gh_check_runs` | `(team_id, gh_check_run_id)` | Name, head SHA, conclusion, and duration |
| `gh_pr_commits` | `(team_id, commit_sha)` | Which pull request landed each commit on the base branch |
| `gh_pr_links` | `(from_pr, to_pr, kind)` | A revert or fix linked to the pull request it corrects, with evidence, confidence, and detector version |
| `gh_conflict_samples` | `(pr_row_id, sampled_at)` | Polled `mergeable` state, so conflict time is measured from samples |
| `gh_sync_state` | `(team_id, repo_id, kind)` | The backfill cursor and watermark |

Review data:

| Table | Unique key | Holds |
| --- | --- | --- |
| `finding_candidates` | `(run_id, step_key, ordinal)` | Every finding a specialist raised, with the server-computed `finding_key`, path, line, severity, claim, body, and an optional `arm_id` |

A candidate joins to the lead's decision on `(run_id, finding_key)`. A
candidate with no match was dropped silently or reworded, and the matcher
records which.

People:

| Table | Unique key | Holds |
| --- | --- | --- |
| `people` | `id` | Display name and an optional link to `users` |
| `person_identities` | `(team_id, kind, value)` | A GitHub numeric ID, a GitHub login (dated, because logins change), a commit email, or a Jira account |

Commit emails of the form `<id>+<login>@users.noreply.github.com` map
automatically. Other emails are confirmed by hand on the people page.

Labels and categories:

| Table | Unique key | Holds |
| --- | --- | --- |
| `taxonomies` | `(team_id, version)` | A set of categories as JSON |
| `labels` | `(subject_kind, subject_id, taxonomy_version)` | One label for a pull request, push, candidate, finding, or cluster, with confidence and the labeler that produced it |
| `clusters`, `cluster_members` | Keyed by taxonomy version | Groups of recurring findings |

Labels never live in columns or `CHECK` constraints. A new category set is a
new taxonomy version and new label rows, and the reports choose a version.
Relabeling costs a classification pass and no migration.

Model attribution and evaluation:

| Table | Unique key | Holds |
| --- | --- | --- |
| `step_configs` | `(run_id, step_key)` | What the server configured when it bundled the job: alias, model ID, provider, tier, and requested effort |
| `llm_calls` | `(run_id, seq)` | What each call actually used: model ID, tier, attempt index, tokens, and cost, from cost events |
| `eval_experiments` | `id` | What is being compared, the sampling rate, and the daily budget |
| `eval_arms` | `(experiment_id, head_sha, label)` | One arm's run, its overrides, and whether it is the control |
| `eval_matches` | `(experiment_id, candidate_id)` | Which findings match across arms, with the score and method |
| `eval_scores` | `(arm_id, scorer_version)` | Recall, precision, unique valid findings, cost, and latency per arm |

## PR insights

### Collecting events

A server-side step runs for every delivery from a tracked repository, before
the 14-day prune. It pulls the delivery's facts into the `gh_` tables and
doesn't keep the payload. This step is code in the server, not a workflow,
because a workflow run per event is too slow and costly for an insert.

It handles `pull_request`, `pull_request_review`,
`pull_request_review_comment`, `check_run`, `check_suite`, and `push`.
Tracking a repository requires the GitHub App installed with read access to
pull requests, contents, and checks. A repository doesn't need an Armature
project to be tracked, so `gh_repos.project_id` is nullable.

A nightly job compares stored rows with the GitHub REST API to fill gaps from
dropped deliveries.

### Backfill

The server's sync service backfills each tracked repository. It pages
through pull requests with the GitHub GraphQL API, writes each pull request
and its children in one transaction, and saves its cursor in
`gh_sync_state`, so it can stop and resume. It never deletes a child row
because a page left it out. See decisions 7 and 8.

### Classifying pushes

GitHub's `synchronize` event carries `before` and `after` but no `forced`
flag, and nothing in the event says why someone pushed. Each push is
classified when it arrives, and the evidence is stored with the class:

| Class | Rule |
| --- | --- |
| Merge from base | The compare status is "ahead", and the new commit has two parents, one of them on the base branch. |
| Rebase | The compare status is "diverged" or the old head is gone, and the patch IDs match. Checking patch IDs needs a clone in a runner job. |
| Force push with new content | "Diverged", and the patch IDs differ. |
| Armature fix | The head matches a `github.push` tool call from an Armature run. |
| Fix after review or new scope | A heuristic: commits after a review that touch the flagged files count as a fix. Stored as a label with a confidence score. |

The person who clicks GitHub's **Update branch** button is the push's sender,
so the sender never identifies the class.

Merge-conflict time can't come from events, because GitHub sends nothing
when a base-branch change creates a conflict, and `mergeable` stays null
until a background job finishes. A poller samples `mergeable` for open pull
requests and stores intervals in `gh_conflict_samples`. Conflict time counts
against the codebase and the process, never against the author.

### Counting review rounds

One round is one distinct pull request head that received a posted review.
For Armature, that is a live `github.post-review` or `review.record-post` tool
call with `posted: true`. A run that registers through a runner key and posts
on its own counts once its `post` step output names a `review_id` that GitHub
sync stored on the run's own pull request, in its project's repository, under
a login Armature posts as. For people, it is a `pull_request_review` submitted
event, counted per reviewer per head. Reruns, superseded jobs, and canceled
jobs count as attempts, not rounds. Heads that Armature pushed count as
automatic fix rounds. Those arrive with `record-push` in PR 2, so `rounds.py`
doesn't count them yet.

`armature/server/rounds.py` stores the result on each run: `review_round`, the
round of the last head it posted, `prev_round_run_id`, the first run of the
round before, and `review_kind` (`full`, `incremental`, `discuss`, or
`resolve_declined`), read from its step outputs. A run that posts several heads
opens a round for each new one. A run that reposts a head gets that head's
round. The server recomputes a pull request's rounds whenever one of its runs
ends and whenever sync or a webhook stores its reviews or pushes, each time in
its own transaction after that write commits, so a round can change after it
is written. On Postgres an advisory lock per team and pull request serializes
these passes. These columns classify runs and never authorize a review or a
mark. The same pass sets `head_sha` on an ended run that has none,
from the synced review its `post` step names when no other run claims that
review, or from the head its `resolve` step claims when that is the pull
request's latest synced push at the run's start. It never replaces a head that
`review.risk` or `review.record-risk` wrote. `armature server backfill-rounds`
derives existing runs.

### Change type

The linked Jira issue type decides the change type when there is one. A
classification stage labels the rest as a bug fix, enhancement, new feature,
code maintenance, or platform maintenance, with a confidence score.

### Escaped defects

When a pull request merges, `gh_pr_commits` records which commits it landed.
For a squash merge, that is the merge commit. For a rebase merge, it is the
range of the base-branch push.

A later revert links back directly. A later bug fix is blamed against its
parent commit with `git blame -w -M -C` and an ignore-revisions file, so moved
and reformatted code doesn't point at the wrong pull request. For fixes that
only delete lines, the deleted lines are blamed in the parent. Blame results
are stored as candidates with a confidence score. Only an explicit revert or
issue link marks one as confirmed.

### Per-person view

| Area | Measures |
| --- | --- |
| Shipping | Merged pull requests by change type, weighted by size |
| Quality | Review rounds per pull request, findings the lead kept per round, and confirmed escaped defects, all adjusted for size and type |
| Review contribution | Reviews submitted, diff size reviewed, response time to review requests, substantive comments, and an estimated review time |
| Unblocking | Fixes to hotspot files and platform maintenance work |

GitHub doesn't record time spent reviewing. The estimate combines the span
from a review's first comment to its submission with the size of the diff.
A classifier separates substantive comments from nits. A reviewer whose three
comments each catch a real bug ranks above one who approves many pull
requests with no comments.

Per-person views require the `team_lead` role or higher
(`armature/server/rbac.py:55`).

### Recurring findings

The weekly `finding-patterns` workflow labels new candidates and human review
comments with the current taxonomy, clusters them by category and code area,
and writes a report with these sections:

- The most frequent categories per repository and per person, and how they
  change over time
- Hotspot files, ranked by findings, conflicts, and churn
- Each specialist's overrule rate: how often the lead dropped its findings
- Findings the lead dropped that later turned into an escaped defect

### Self-healing

Armature proposes fixes on its own. Each Monday it picks each team's biggest
recurring patterns and runs the built-in `self-heal` workflow on each one.
The workflow chooses the most permanent fix that works:

1. A deterministic check, such as a SwiftLint or Detekt rule or a CI check
2. A one-time sweep that fixes existing instances
3. A change to the review prompt, a specialist, or the repository's
   `CLAUDE.md`
4. Guidance for authors

A team lead approves every fix on the Fixes screen before anything reaches
the repository. Unreviewed agent commits never reach GitHub, because CI on a
push could run them with the repository's secrets. The run goes in this order:

1. `insights.fix-context` gives the agent the pattern's counts and up to ten
   examples, fenced as data.
2. The agent commits the fix in its worktree. Nothing is pushed.
3. `insights.fix-patch`, a step with no agent, takes the commit as a patch
   against the commit the session started from and stores it, with that
   base commit and its first 12 changed lines, on the proposal. A patch over
   200 KB, a binary change, or one that isn't UTF-8 fails the proposal as
   too large to review. A symlink, a submodule, a `.gitmodules` change, or
   any mode but a plain file's fails it as changing more than text files.
   No change fails it as nothing to fix.
4. A rating agent reads the patch as data, and `insights.fix-risk` records
   the higher of its level and the server's path floors on the patch's own
   paths.
5. An unconditional human gate waits up to 168 hours for a team lead, with
   step-up on approve. Approving is refused before the gate moves when the
   repository already reached its daily cap of opened fixes. The gate is
   skipped when no patch was stored.
6. On approval, only steps with no agent run: a fresh checkout of the base
   commit, `insights.fix-apply` applies the stored patch and commits it,
   `github.push` sends it to `armature/fix-<proposal id>`, and
   `github.open-pr` opens a draft pull request. A patch that no longer
   applies fails the proposal and pushes nothing. A branch whose pull
   request didn't open is deleted by the run, and the insights daemon
   deletes any the run left behind, for example when it was lost after its
   push.

Before the pull request exists, the Fixes screen links the stored patch as
plain text. A decline or an expiry leaves nothing on GitHub.

A pattern qualifies when its cluster has at least `insights.fix_min_flags`
flags (default 5) over 28 days, in a tracked repository that belongs to a
project whose providers define the `develop` and `review` aliases, and that
reaches GitHub through a GitHub App whose installation has contents and pull
request write. A gh CLI login never qualifies, because its token sits with
the user an unattended agent session could run beside. A self-heal run also
needs a runner whose agent sessions run as a separate agent user.

The server enforces these guards, whatever the workflow says:

- `github.push` and `github.open-pr` from a self-heal run need its
  proposal approved and a team lead's gate approved after the latest
  rating, at any risk.
- The broker forces `draft` and pins every push, pull request, and branch
  delete to the run's own proposal's repository, branch, and base. The
  agent never chooses them. Any other run is refused a push to, or a pull
  request from, an `armature/fix-` branch.
- A proposal binds only the job the weekly slot queued for it.
- `github.open-pr` from self-heal runs is capped per repository per day by
  `insights.fix_daily_pr_cap` (default 2), checked at approval and again,
  under the same lock as pushes, when the pull request opens.
- One proposal per pattern at a time, keyed on
  `(taxonomy version, category, repository, area)` because cluster rows are
  rebuilt weekly, and none within 60 days of the last one.
- At most `insights.fix_weekly_per_team` proposals a week (default 3), one
  per repository, and none once the team's self-heal spend this week
  reaches `insights.fix_weekly_budget_usd` (default 40 USD). A week starts
  Monday 00:00 UTC.
- `github.delete-branch` deletes only an `armature/fix-` branch with no
  open pull request, whose proposal was declined, expired, or failed, or
  whose own approved run pushed it and couldn't open the pull request.
- `self-heal` is a read-only built-in, so nobody can edit the gate out.

A merged fix shows how often its pattern was flagged in the 30 days before
the merge and since.

### Daily report

Each morning an admin can read what Armature learned the day before, across
every team and workflow, on the Insights Daily report tab and as a file on
disk. The insights jobs daemon builds it on its first tick at least 2 hours
after the previous day ends in `display.timezone`, so the day's last runs
have finished. Every query is bounded by that day's start and the next day's
start, so a day that changes daylight saving time is 23 or 25 hours.

The `daily_reports` row for the day is the only marker that the day is done.
A failed build stores nothing and retries on the next tick, and a second
server's insert loses on the unique day. The row records how many of the
day's runs were still open at build time.

The report has these sections:

- Headline: runs, cost, and failures against the day before and the 7-day
  average, by workflow, plus a per-model breakdown of `llm_calls`. Cost comes
  from `runs.cost_usd` only, and it shows even when `display.show_cost` is
  false, because only an admin reads the report.
- Findings: step errors grouped by their first line, failing workflows, the
  slowest and costliest runs, dry runs that left no finding and no artifact,
  and review churn from the round and lineage columns. A late catch is a
  finding first raised in round 2 or later. A repeat restates an earlier one.
- Learned: plain takeaways from those numbers, each citing them. No model is
  called.
- Changes: deploy log lines and the live commit, workflow and document
  versions with author and note, the names of changed trigger, webhook rule,
  and setting keys (never their values), and which configured files changed.

Free text is redacted before it's stored. Comparison runs count nowhere.

`reports` in the server config file sets where the files go and what the
report reads from the host. Only the config file sets it.

| Key | Default | Meaning |
| --- | --- | --- |
| `reports.dir` | `~/armature-reports` | Folder for `YYYY-MM-DD.html`. It must be a directory this user owns with mode 0700. Each file is written 0600. |
| `reports.roots` | none | Directories whose files are fingerprinted by sha256. Symlinks, `ops.env`, `secrets/`, `.git/`, and `__pycache__/` are skipped. |
| `reports.deploy_log` | none | A deploy log whose lines start `YYYY-MM-DD HH:MM:SS` in the display zone |
| `reports.live_sha` | none | A file holding the live commit |

The file is self-contained HTML with no script, written once when the row is
stored, and kept. Each fingerprint is compared with the newest earlier
report's, and the first report is the baseline.

## Model evaluation

### Recording what actually ran

These changes make every step record the model and effort that produced its
output:

1. `_execute_with_escalation` returns the tier, model, and effort that
   succeeded, plus every attempt with its own tokens.
2. The cost event carries model, tier, and provider. Ingest writes one
   `llm_calls` row per call.
3. The stream parser keeps the CLI's reported `effort` from its init message
   and `modelUsage` from its result. `modelUsage` gives per-model totals that
   include subagents.
4. At bundle time (`armature/server/worker.py:518`), the server writes
   `step_configs` with the resolved model ID and provider for each alias.
5. A finding links to the model that raised it through `raised_by`, which
   names the specialist's step. `findings.step_id` names the decisions step,
   which doesn't help here.

Review steps should also set effort explicitly, so the recorded requested
effort isn't null.

### Shadow arms

An arm is a second run of the review on the same pull request head with one
setting changed. Reusing the code-review workflow unchanged would break the
real review in three ways:

- The arm's `marked` step ends it as soon as the primary marks the head.
- `review/context` gives the arm the primary's findings and decisions, so the
  arms aren't independent.
- The arm's job shares the code-review concurrency key, so it cancels the
  real job.

Arms therefore run in a new job-level arm mode that the broker checks next to
dry mode. In arm mode:

- Reads are allowed.
- `review.decisions` and `review.risk` write rows tagged with the `arm_id`,
  to `finding_candidates` and the `eval_` tables, never to `findings`.
- Every GitHub and Jira write is refused and logged.
- The job's concurrency key is the code-review key plus the arm ID.
- `ref` is pinned to the primary's head, and `marked` is skipped.
- The arm reads the primary's stored `context` output and doesn't fetch it
  live.
- The primary's risk level and specialist panel are pinned, so the arms
  differ only in model and effort.

An arm job declares a `requires` capability that only the arm environment
type provides, so it runs in that environment, away from the primary.
`context._decided`, `subjects.py`, and the stats queries exclude arm rows.

### Scoring

Every experiment includes a control arm that reruns the primary's exact
settings, pinned by model ID and not by alias. The variant and the control
run together, after the primary, under the same conditions, so the effect of
the change is the variant minus the control, taken per pull request. A
measure counts only when the mean of those differences is more than twice
its standard error and the experiment compared at least `min_prs` pull
requests. The scorer applies this to recall, precision, unique valid
findings, and cost. A cheaper variant also needs precision that isn't
significantly worse. The control minus the primary is the environment shift:
what running later, as a comparison, changes on its own. It's reported and
never decides a result. See decision 10.

Scoring against the lead's decisions favors the primary, because the lead
only saw the primary's findings. The scorer uses three sources instead:

1. A matcher pairs findings across arms. It requires the same path and a
   line in the same diff hunk, then checks that the two findings describe the
   same problem, and pairs them one to one. Each arm is matched to the
   primary first, then the variant's unmatched findings are matched to the
   control's, so an issue only the two comparison runs found counts once. It
   stores each match's score and method.
2. A blinded judge labels the union of all arms' findings in a shuffled order
   with the arm hidden.
3. Later evidence, such as an escaped defect, confirms or overturns labels.

Each arm gets recall, precision, unique valid findings, cost, and latency.

### Experiment design

- Vary one setting at a time, per specialist. Two models times four effort
  levels times nine specialists is 72 combinations, which is too many to
  learn from.
- Start with the most expensive question for each kind of specialist. For
  `concurrency` and `correctness`, test whether xhigh beats high. For
  `simplicity` and `conventions`, test whether Sonnet matches Opus.
- Sample a share of reviews at random, so easy pull requests aren't
  overrepresented.
- Each experiment has a daily budget. Arms stop starting when it's spent.

### Comparing a model on another provider

A model variant can name an alias in place of a model id, in the `alias`
field of the create request. The variant is stored as the alias and the
model id it names, and the variant arm runs on that alias, so it gets that
provider's credential and environment. Without an alias, a model variant
runs on the baseline's provider with the baseline's credential.

The alias resolves in each review-pass workflow's project, as the baseline
does, and the variant is refused unless every project resolves it to the same
provider and model. An alias on a provider with an `api_base` needs a price
on that provider first, so its cost can be compared.

When the alias resolves to another provider than the baseline's, only an
admin can create the experiment, because the variant arm sends reviews to
that provider. A team lead can still name an alias or a model id that the
baseline's provider serves. The Add comparison form offers an admin each
alias the team can use on another provider, labeled with that provider's
kind.

When both sides run the same model, the question and the recommendation
name each side's alias, such as "Claude Opus 4.6 (mirror) instead of Claude
Opus 4.6 (review)".

Approving a cross-provider recommendation sets the specialist's `model:` to
the variant's alias. It never adds the variant's model id to the baseline's
provider. It needs an admin, like any model change, and it refuses when the
alias names another model than the one compared. See [Provider prices](DESIGN.md#provider-prices).

### Recommendations

The weekly job reads `eval_scores` and writes recommendations in this form:
"Over 40 pull requests, `concurrency` at high found everything xhigh found at
60% of the cost." Each recommendation is a proposed change to the tier
configuration behind an approval gate. The playbook's run log applies the
same rule to the interactive agent team.

## Workflows and server work

| Piece | Kind | Trigger |
| --- | --- | --- |
| Event collection | Server code | Every delivery from a tracked repository |
| Conflict poller and nightly gap fill | Server code | Schedule |
| `pr-insights` | Workflow | Webhook rule on `pull_request` opened, synchronized, or closed |
| GitHub sync and backfill | Server code | Scheduler loop, per tracked repository |
| `finding-patterns` | Workflow | Weekly schedule |
| `self-heal` | Built-in workflow | Weekly, for each team's biggest patterns |
| `model-eval` | Server code plus arm mode | Sampled code-review runs |
| Daily report | Server code | Insights jobs tick, once per local day, 2 hours after the day ends |
| Insights pages | Server UI | Extends the subject and trends pages (`armature/server/static/views/subject.js`, `trends.js`) |

One copy of each workflow serves every repository, with the repository as an
input. Separate copies would drift apart and would need separate edits for
every taxonomy change.

## Phases

| Phase | Delivers | Why first |
| --- | --- | --- |
| 0. Attribution fixes | Correct model on escalated calls, effort and `modelUsage` reported, `step_configs` and `llm_calls`, explicit effort on review steps | Every later comparison depends on it, and each piece is small |
| 1. Candidates and categories | `finding_candidates` from a new per-finding event, a backfill from untruncated `run_steps.output`, taxonomy v1, labels on existing findings | Shows whether existing findings cluster into anything useful |
| 2. GitHub collection | `gh_` tables, collection at ingest, backfill workflow, people and identities, push classification | Data for every pull request, reviewed or not |
| 3. Reports and pages | `finding-patterns`, the insights pages, and per-person views | The first thing you read every week |
| 4. Shadow arms | Arm mode, arm environment type, matcher, scorer, and recommendations | Needs phase 0 attribution |
| 5. Self-healing | `self-heal` with its gate and caps, and the Fixes screen | Needs trusted clusters from phase 3 |

## Existing bugs found during the design

These defects affect current runs, so they're worth fixing independent of
this design:

- The fallback order in `armature/nodes/llm.py:549` retries a failed stage on
  lower tiers first, so a failed `large` stage can produce `tiny` output.
- A `finding_key` collision across two pull requests in one project feeds the
  wrong "Last decision" to the lead (`armature/server/tools/context.py:167`).
- `total_cost_usd` may count a resumed session's earlier spend twice. This
  one is unconfirmed.

## Decisions

1. **Comment text isn't stored.** GitHub keeps review comments on merged
   pull requests, and `gh api` reads them. The `gh_review_comments` and
   `gh_reviews` rows keep the GitHub ID, author, path, line, head SHA, time,
   body length, and URL, and the labels live in `labels`. The classifier
   reads the text from the webhook payload when the comment arrives, and a
   relabel refetches it with GraphQL, about 100 comments per request. No
   retention rule is needed, and the database holds no one's comment text.
2. **Repositories without a project.** A repository is tracked through
   `gh_repos` and needs no Armature project. Requiring a project would mean
   creating projects for teams Armature doesn't serve.
3. **Arm environment.** Arm jobs declare the `eval-arm` runner capability.
   Any environment type or runner that provides it can run arms. The intended
   setup is a dedicated AWS environment type, which keeps arms independent of
   the primary review. The capability also works with a local runner for
   testing.
4. **Experiment budget.** Each experiment has a `daily_budget_usd` field
   with a default of 25 USD. Arms stop starting for the day once the spend
   recorded in `llm_calls` reaches it.
5. **Effort on review steps.** Phase 0 leaves review effort unset and records
   the effort the CLI reports. Setting it explicitly would change production
   reviews before there is evidence for any value. Experiments vary it
   instead.
6. **Escalation order.** A failed tier escalates to higher tiers first,
   nearest first. Only when every higher tier fails does it fall back to
   lower tiers, nearest first. The old order jumped from a failed tier to
   the lowest configured tier. Keeping a downward fallback as the last
   resort means a stage on the top tier still recovers, and the recorded
   attribution shows which tier produced the output.
7. **Polling is the source of truth for GitHub data.** A server sync service
   polls each tracked repository with GraphQL every few minutes, newest
   updates first, down to its saved watermark, and backfills history from a
   saved cursor. Webhook deliveries are collected as they arrive, but only
   to cut latency. GraphQL's timeline reports force pushes, so a missing
   webhook subscription costs freshness, not data. A repository tracked by
   two teams is synced separately for each team. Backfill runs in this
   service and not as a workflow, because runners can't write to the
   database.
8. **Backfill depth and rate limits.** Backfill reaches back 18 months by
   default, set by `insights.backfill_since`. Check runs are kept for each
   pull request's final head and for heads that received a review. Sync
   pauses until the rate-limit window resets whenever fewer than 1,500
   GraphQL points remain, so live reviews that share the token keep
   working.
9. **People are team-scoped.** `people` carries `team_id`, like every other
   new table.
10. **The effect is the variant minus the control.** An earlier rule
    counted a variant when its gap from the primary exceeded the control's
    gap from the primary. With no real effect, the variant and the control
    are two draws from the same distribution, so the variant's gap is the
    larger one about half the time, and the rule reports a change that
    isn't there. Both comparison runs also share conditions the primary
    lacks: they run later, read the pull request after the primary's review
    is posted, and run only one specialist. Comparing the variant with the
    control removes those shared conditions. The per pull request
    difference, against twice its standard error, measures whether a gap is
    larger than the runs' own disagreement.
11. **Fixes are proposed automatically, and held until approved.** The
    weekly job proposes fixes without anyone picking a cluster first, within
    the caps above, because the goal is for Armature to find and fix
    recurring problems on its own. The fix stays on the server as a patch
    until a team lead approves it, and only then is it pushed and opened as
    a draft. Unreviewed agent commits never reach the repository, because
    CI on a push could run them with secrets.

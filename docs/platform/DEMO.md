# Platform demo walkthrough

This page walks you through a local demo of the Armature platform: what to
click, in what order, and what each screen shows.

> **The runs are replayed sample data.** No LLM, AWS, GitHub, Slack, or Jira
> call happens. Each run's events were replayed through the server's own
> ingest, so the pages draw them exactly as they draw real runs. Running real
> work needs a real AWS connection, model provider, and vendor apps. See
> [DEPLOY.md](DEPLOY.md).

## Start it

```bash
scripts/demo.sh --reset   # fresh data, then serve
scripts/demo.sh           # reuse the data from last time
```

Open <http://127.0.0.1:8197/> and sign in as `demo` with password
`demo-armature`. Ctrl-C stops the server. Use `--reset` on the day, so the run
times read as recent. To pick a Python, set `ARMATURE_DEMO_PYTHON` to one with
the `server` extra installed.

## 1. Setup checklist

Open **Setup** in the menu. The checklist shows each step of bringing up a
server: database, first admin, models, AWS, runners, connections, repos, and
webhooks. The demo has already finished most of them, so the checklist reads
as a server partway through setup.

## 2. Projects

Open **Projects**. Team FamilySearch has three projects:
`fs-eng/mobile-ios`, `fs-eng/android-tree`, and `fs-eng/mobile-api-nodejs`.
Each one names its repository, its GitHub connection, and the AWS environment
type its jobs run on: `macos-xcode`, `android-gradle`, and `linux-node`.

## 3. Connections and Verify

Open **Teams**, then FamilySearch, then the **Connections** tab. GitHub,
Slack, and Jira show as verified, with the
identity each one reported, such as the GitHub App's bot login. Click
**Verify** on any of them to show the check. In the demo, Verify answers from
the seeded identity instead of calling the vendor.

Open **AWS**. The connection shows region, subnets, launch templates, budget
limits, and the three environment types. The AWS loop is off in the demo, so
nothing launches or gets reaped.

## 4. Webhooks held for approval

Open **Webhooks**, then `github-mobile`. A new webhook starts pending, so its
first delivery, a `pull_request` event for PR #486, is held instead of
queuing work. Show the held delivery and its **Approve** button. The rule
under it is in shadow, so approving it records the decision and queues
nothing.

## 5. The code-review run

Open **Runs**, then the `code-review` run on `fs-eng/mobile-ios#482`.

- The header shows the **Medium risk** chip. Hover it to see why: a
  dependency file, `Podfile.lock`, changed. The server, not the model, sets
  risk.
- The outcome reads **Changes recommended: 1 blocking, 3 should fix**.
- The location reads **AWS macOS**, the `mac2-m2.metal` host the review ran
  on.
- The **Pull request** and **Jira FSMOB-1423** links go out to GitHub and
  Jira.
- The team lead's decision groups the findings: **Blocking** (a data race in
  `PortraitLoader`), **Should fix**, **Fixed** (already fixed on the branch),
  and **Dropped**, each dropped finding with the lead's reason.
- **Specialists** lists all nine reviewers (simplicity, correctness,
  security, performance, beauty, test adequacy, concurrency, conventions, and
  compatibility) with what each one found.
- The **Log** tab shows every step, tool call, and finding in order.

For live logs, open the other `code-review` run, on
`fs-eng/android-tree#219`. It's still running: four specialists have
finished, and new tool calls from the other five arrive every few seconds.

## 6. The gate approval

Open **Waiting for you**. The `development` run for FSMOB-1431 paused at its
`approve_plan` gate before writing any code. Its risk is **High**, because the
plan touches `FamilySearch/Auth/SessionStore.swift`. Open it to read the plan,
then click **Approve** or **Reject**, with an optional note.

Approving queues the run's resume job. In the demo it then waits for a Mac
environment that never launches, because AWS is a stand-in.

## 7. Threads

Open **Pull requests**, then the FSMOB-1423 thread. One thread holds
everything about the issue: the development run that opened PR #482, then the code review of that
pull request. A pull request joins its issue's thread through the `[KEY]` its
title starts with, or through the link the development run reported.

## 8. Ops workflows in dry mode

On **Runs**, the `PR merged` run for PR #479 and the `Build announce` run for
TestFlight build 6.42.0 are dry runs. Their logs say what they would have
done, such as moving FSMOB-1398 to Ready for QA or posting to
`#mobile-ios-builds`, and nothing was sent.

## Not in this demo yet

Pollers and the other trigger kinds aren't merged yet. After they merge,
rebase this branch and run `scripts/demo.sh --reset`. The seed skips any step
whose feature is missing and prints a note, so it keeps working before and
after other slices land.

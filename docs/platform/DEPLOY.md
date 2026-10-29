# Deploying the Armature server behind a reverse proxy

`armature server run` listens on loopback by default (`host: 127.0.0.1`,
`port: 8098`). Put a TLS-terminating reverse proxy on the same host in front of
it, and set `public_origin` to the URL people type, such as
`https://armature.example.com`.

## First run

On a fresh install, `armature server run` starts in setup mode instead of
failing. It prints a one-time link and serves only the setup page until an
admin exists:

```shell
$ armature server run
Armature needs a first admin. Open this link on this computer to set it up. It works once:
  http://127.0.0.1:8098/setup#<token>
```

The page has two screens:

1. **Database.** Use the built-in SQLite database, or paste a PostgreSQL or
   MySQL URL and test it. The server writes `server.yaml` (mode 0600),
   migrates the database, and creates the master key. Back up
   `~/.armature/master.key`. Without it, no saved secret can be read.
2. **First admin.** A username and a password of at least 12 characters.

PostgreSQL is the production backend. On MySQL, the per-key job claim guard
and the trigger scheduler's claim are unverified under its default
REPEATABLE READ isolation. Run the database test matrix against a real MySQL
server before you use it in production.

The server then signs you in and opens **Setup** in the app, a checklist for
your team's models, AWS, runners, connections, repos, and webhooks. Each
step can be skipped and done later. The server never starts a runner itself.
The Runners step makes a runner key, shows the `armature runner` command with
a copy button, and shows the runner when it first checks in. The Runners and
Webhooks steps show `public_origin`, the address runners and webhooks use. It
lives in `server.yaml` only, so change it there and restart the server. The Overview shows "Finish setup" until every step is
done or skipped, or you hide the checklist.

Setup mode is locked down:

- It binds `127.0.0.1` whatever `host` says, and refuses any peer that isn't
  loopback. It reads no proxy header.
- Every setup call needs the token from the console, compared in constant
  time. The token lives only in the process's memory.
- The link prints to stderr, and only when stdin is a terminal. Under a
  service manager, `run` says to run `armature server run` in a terminal and
  exits, so the link never lands in a service log.
- Once an admin exists, every setup route is gone. An admin made another
  way, such as `armature server create-admin`, ends setup mode too.

When `public_origin` is the setup address, `http://127.0.0.1:<port>`, the
full app takes over the same socket with no restart. Otherwise setup mode
stops after the admin step and the server starts on the configured `host`
and `public_origin`, where you sign in.

With `require_totp: true`, setup mode is off. Run `armature server init`
instead, which does the same steps on the terminal and enrolls an
authenticator.

## Client addresses

Sign-in throttles count failures per client address. Behind a proxy, every
request arrives from `127.0.0.1`, so without the real address one attacker's
failures lock out everyone.

The server reads the `X-Real-IP` header only when both of these hold:

- `trust_real_ip: true` is set in `server.yaml`.
- The request comes from a loopback address, which is the proxy on this host.

It never reads `X-Forwarded-For`. Set `trust_real_ip` only when a proxy on this
host always overwrites `X-Real-IP`, because otherwise any client can choose its
own address and slip past the throttle.

### nginx

```nginx
location / {
    proxy_pass http://127.0.0.1:8098;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### Caddy

```caddy
armature.example.com {
    reverse_proxy 127.0.0.1:8098 {
        header_up X-Real-IP {remote_host}
    }
}
```

### Apache httpd

```apache
ProxyPass        / http://127.0.0.1:8098/
ProxyPassReverse / http://127.0.0.1:8098/
RequestHeader set X-Real-IP "%{REMOTE_ADDR}s"
```

Each of these overwrites any `X-Real-IP` the client sent.

## A base path

To serve the server under a path such as `/armature`, set
`base_path: /armature` and proxy that path without stripping it:

```nginx
location /armature/ {
    proxy_pass http://127.0.0.1:8098;
    proxy_set_header X-Real-IP $remote_addr;
}
```

## Sign-in

By default, people sign in with a username and password only. Invites ask
for a new password, and no change asks you to sign in again first.

To require an authenticator app, set `require_totp: true` in `server.yaml`
or `ARMATURE_SERVER_REQUIRE_TOTP=true`. With it on:

- Sign-in asks for a code from an authenticator app.
- `armature server create-admin` and invites enroll an authenticator.
- Changes to secrets, users, packs, webhooks, AWS, and other settings ask
  for the password and a fresh code, or a fresh single sign-on, at most
  every five minutes. This is called step-up.

The password throttles, CSRF checks, session limits, roles, and audit log
work the same either way. Turning the setting off keeps each user's enrolled
authenticator, so turning it back on needs no new enrollment. A user who
first signed in while it was off has no authenticator, so issue them a new
invite from the Users page after you turn it on.

## Headers the server sets

Every response carries a strict Content-Security-Policy, `X-Frame-Options:
DENY`, and `Cache-Control: no-store`. Unless `public_origin` is a plain-HTTP
localhost address, it also sends `Strict-Transport-Security:
max-age=31536000`, so serve the origin over HTTPS only.

## Streaming runs from a host

A host that runs workflows with `armature run`, such as an armature-ops
machine, streams each run to the server when these environment variables are
set:

- `ARMATURE_SERVER`: the server's origin, such as
  `https://armature.example.com`. Plain HTTP works only to a loopback address.
- `ARMATURE_RUNNER_KEY`: an `ark_` runner key. A team lead creates a team key
  on the Runner keys tab of the team's page, and its runs land in that team. An
  admin creates an instance key on the Runner keys page, and each of its runs
  lands in the team whose project matches the run's repository.
- `ARMATURE_PROJECT`: the project the run belongs to. With a team key, this
  is a project name or repository in that team. With an instance key, this is
  a repository slug, such as `fs-eng/android-tree`, matched without regard to
  case, or `team-slug/project-name`. When it's unset, the run's `project`
  input is used, then its `repo` input. An instance key's run with no
  matching project lands in the admin-only "Operations (unassigned runs)"
  team.
- `ARMATURE_ACTOR`: who started the run. The default is `USER`.
- `ARMATURE_TRIGGER_KIND`: what started the run, such as `webhook` or
  `schedule`. The default is `cli`.

Deleting a runner key stops new registrations with it and ends the event
streams of the runs it registered.

## Starting a runner

A runner is a host that runs the jobs the server queues, such as the jobs
webhooks start. It claims a job over HTTPS, runs it with `armature run` from
a private work directory, streams the run's events back, and reports how the
job ended. The runner never reads the database: each job arrives as a bundle
with the pinned workflow, its inputs, the `env.*` settings, the secrets the
trigger lists, and a token for the run's events.

To start one:

1. Create a runner key. A team key runs only that team's jobs. An instance
   key runs any team's jobs.
1. On the host, install Armature with the base install. The runner doesn't
   need the `server` extra.
1. Put the key in an environment variable. The runner reads the key from the
   environment only, so it never shows up in a process list.
1. Start the runner, naming each capability the host has:

   ```shell
   export ARMATURE_RUNNER_KEY=ark_...
   armature runner --server https://armature.example.com \
     --capability llm --capability git --slots 2
   ```

The runner takes only jobs whose workflow's `requires:` list is covered by its
capabilities. A job whose workflow lists nothing, including every job queued
before runners existed, goes to any runner. Capabilities are short names such
as `llm`, `git`, `claude-code`, `codex`, and `platform:ios`.

Other options:

- `--key-env NAME`: read the key from `NAME` instead of
  `ARMATURE_RUNNER_KEY`.
- `--name`: the runner's name on the Runners page. The default is the host
  name. A runner that restarts with the same key and name keeps its row.
- `--label NAME=VALUE`: a note shown on the Runners page. Repeat it for each
  label.
- `--work-dir`: where the runner keeps its jobs. The default is
  `~/.armature/runner`. Each runner uses its own subdirectory named after
  the runner, and each job gets a `job-<id>` directory there that only the
  runner's user can read and that's removed when the job ends. On start, a
  runner removes `job-*` directories a crashed run left in its own
  subdirectory and touches nothing else. A second runner with the same name
  and work directory on one host refuses to start.
- `--enroll-token-env NAME`: read a one-time enrollment token from `NAME`
  and trade it for a runner key at `POST /api/runner/enroll`. An AWS
  environment's boot script starts the runner this way. The runner keeps the
  key in `--key-file` (default `/etc/armature/runner.env`, mode 0600), so a
  restart reuses it instead of enrolling again. A second enrollment with the
  same token gets `410 Gone`.
- `--memory-mb MB`: cap each run's address space (`RLIMIT_AS`) at `MB`
  MiB. The default is 8192, and 0 turns the cap off. macOS ignores it,
  because it won't lower that limit. The server's own worker takes the same
  cap from `runner.memory_mb` in its config file, and a test run takes
  `dry_run.memory_mb`, which defaults to 2048.
- `--job-user USER`: run each workflow as `USER` through `sudo`, so the
  workflow can't read the runner key or another job's bundle. It needs
  `--slots 1`, because when a job ends the runner stops every process `USER`
  left running. The runner
  doesn't need root. It must belong to `USER`'s group, and sudoers must let
  it run commands as `USER` without a password, as the base image's
  `armature-runner ALL=(armature-job,armature-git) NOPASSWD: ALL` does. The
  runner starts the job with `sudo -n -u USER --preserve-env=NAMES`. Only the names of the job's
  variables reach the command line, never their values. The workflow file is
  copied to `/tmp/armature-job-<id>`, which only the job user's group can
  read.
- `--agent-user USER`: with `--job-user`, run each `agent:` session as
  `USER`, so a Claude Code session can't read the run directory, the bundle,
  or the run's secrets. The job user must belong to `USER`'s group, `USER`
  must belong to no group of the job user, and sudoers must let the job user
  run only the agent CLIs and `/bin/kill` as `USER`, keeping only the
  session's allowlisted variables, as the base image's
  `/etc/sudoers.d/armature-job` does. The runner must be able to run
  `/bin/kill`, `/bin/rm`, and `/usr/bin/find` as `USER`, to stop what
  sessions leave, remove `/tmp/armature-agent-<id>`, and delete `USER`'s
  files in `/tmp`, `/var/tmp`, `/dev/shm`, and its home when the job ends. Sudo must not
  allocate a pty for `USER` (`Defaults>USER !use_pty`).
- `--same-user`: run each `agent:` session as the user that started the
  runner, without `sudo`, so you don't need to create the job and agent
  users. This is meant for a runner on your own Mac. The trade-off is
  isolation: a session can read and change everything that user can,
  including the runner key, the job's bundle and secrets, your home
  directory, and your SSH and cloud credentials. Each job still gets its own
  work directory, which the runner removes when the job ends. Each live
  session records its process group in that directory, and when the job ends,
  is canceled, or the runner stops, the runner stops every recorded group
  before removing the directory. It never stops processes by user, because
  that user is your login. The engine key
  still reaches the run on a pipe. `--same-user` can't go with `--job-user`
  or `--agent-user`. The runner prints a warning when it starts, and the
  Runners page and each run's location chip show "same user".
  `--capability claude-code` or `codex` needs `--job-user` and
  `--agent-user`, or `--same-user`, and the server's own worker never runs
  agent jobs. A workflow with an `agent:` stage pushes only from a runner
  with `--agent-user`, or from a runner that registered with `--same-user`
  and still holds the run's job. The server checks what the runner reported
  when it connected, never what the run says about itself.

To run Claude Code sessions from a runner on your own Mac, as your own user:

```shell
export ARMATURE_RUNNER_KEY=ark_...
armature runner --server http://127.0.0.1:8080 --same-user \
  --capability llm --capability git --capability claude-code
```

The runner refuses plain HTTP to anything but a loopback address, because the
claim carries the job's secrets. It heartbeats each job every 30 seconds, which
extends the job's two-minute lease. If a runner stops heartbeating, the server
marks the job lost and never queues it again, so a run is never paid for
twice. Ctrl-C or `SIGTERM` stops the runner, ends its runs as failed, and
reports them.

A user with the contributor role or higher on a job's team can cancel the job
from its run page. A queued job is canceled at once. A running job's runner
sees the request at its next heartbeat, ends the run as canceled, and stops
the run's processes.

A team runner key never receives an instance secret. A team runner skips a
job whose trigger names a secret that's set only for the instance, and the job
waits for an instance runner, as it would for a capability no runner has.
Instance runner keys and the server's own worker still get instance
secrets.

The Runners page, for admins, lists each runner with what it can do, how many
jobs it's running, and when the server last heard from it.

### The server's own worker

The server still runs jobs itself unless you turn that off, so an install with
no runner keeps working. Instance settings control it:

- `worker.enabled`: `false` stops the server from claiming jobs. Leases still
  expire and finished runs still settle their jobs.
- `worker.slots`: how many jobs the server runs at once. The default is 2.
- `worker.capabilities`: a list such as `["llm"]`. The server's worker
  claims only jobs that list no more than these. When unset, it claims only
  jobs that list nothing, which includes every job queued before `requires:`
  existed.

The server's worker and a runner build the same bundle, so a job runs the
same either way.

## AWS environments

With the `cloud` extra installed, the server can start one EC2 instance per
job for jobs that need the source and that no runner can take. The AWS page,
for admins, holds the connection, the environment types, the Mac hosts, the
environments, and the warm runner. With `require_totp: true`, every change on
the page needs step-up.

Nothing launches until the connection verifies. A server without the
`cloud` extra never imports boto3, and the Status page says
"AWS not configured" until a connection verifies.

### The connection

The connection stores the region, the subnets, the security group, the
instance profiles, the Linux and Mac launch templates, the Mac image
repository, the artifact bucket, the allowed instance types, and the
credentials. `deploy/aws/README.md` maps each stack output to its field. Credentials are static access keys, a role the server assumes
with an external ID, or both. They're sealed in the database, never in
`secrets`, and the page shows only whether they're set and the last four
characters of the access key ID. Leave the credential fields blank to keep
the stored ones.

Every instance, volume, and host the server creates carries the project tag,
`armature-platform` by default, and an `armature-install` tag. The server
lists, terminates, and releases only what carries both. The project tag
can't be `armature`, which armature-ops owns and reaps by. Changing the
credentials, the role, the external ID, the region, the install ID, or the
project tag makes the connection unverified until you verify it again. The
server refuses to change the install ID or project tag while any environment
is running or any Mac host isn't released, because it finds its instances by
those tags. Other changes take effect at once.
While the connection isn't verified, nothing new launches, but the server
keeps reaping what it already launched with the credentials that last
verified.

Every launch passes its instance type, `HttpTokens=required`, and tags for
the instance, its volumes, and its network interface, plus the Spot request
on a Spot launch, because the IAM policy checks the request itself, not the
launch template. The allowed instance types copy the host-role stack's
`AllowedInstanceTypes`. Saving an environment type or allocating a host with
a type outside that list fails with a message that names it, and Verify
lists every enabled type that uses one. With the host-role stack's name set,
Verify also reads the stack and reports a type the connection allows but the
stack doesn't. The connection's list stays the one the server uses.

### Environment types and projects

An environment type is an AMI for `ec2-linux`, or a Tart image for
`ec2-mac`, with the instance types to try, the market, the capabilities it
provides, a job timeout, and an hourly cost for the budget guard. A project
lead picks a project's type on the project page.

The Projects page lists every project you can see. Each project holds its
repository as `owner/name`, the team's GitHub connection it uses, and its
default branch. Paste the repository's address and the page keeps
`owner/name`. Webhooks belong to the team, and a delivery for a project's
repository starts that project's workflows.

A queued job gets an environment when all of these hold:

- It has waited at least 10 seconds, and no online runner, busy or idle, can
  take it.
- It needs more than `llm`. A job that needs only `llm` waits for a runner,
  and the Queue on the Runners page says to start the warm runner.
- Its project has an enabled environment type that provides everything the
  job needs. When the type lacks a capability, the job fails and says which.
- The cost guards allow it: the connection's most environments at once, its
  most launches in 24 hours, its daily budget, and the team setting
  `environment.max_concurrent`, which caps environments per project. After
  three launch failures in a row that aren't capacity errors, launches pause
  for 10 minutes.

The job moves to `provisioning` and the instance boots with only a one-time
enrollment token in its user data. The token expires with the boot timeout.
The runner trades it for an ephemeral runner key that can claim only that
job. Spot is tried in every subnet and instance type before on-demand. The
instance requires IMDSv2, terminates itself on shutdown, and shuts down
after the connection's longest lifetime. The workflow runs as
`armature-job`, and agent sessions as `armature-agent`. Neither can reach the
instance metadata service.

### Building images

`armature env build <type>` bakes a type's image with the templates in
`deploy/aws/images`. It writes the type's installs to a temporary JSON file
and runs `packer build` on `base.pkr.hcl` for an `ec2-linux` type, filling
the region, subnet, security group, instance profile, install ID, tags, and
instance type from the connection. For an `ec2-mac` type it runs
`ios-vm/build.sh` with the type's base image, and needs `--key-file` and
`XCODE_VERSION`. Pass `--base-installs` for the installs every image gets,
and `--var-file` for anything the connection doesn't hold.

```shell
armature env build android --base-installs base-installs.json
```

### The reaper

Every 10 seconds the server:

- Relaunches an environment it recorded but didn't launch, with the same
  idempotency token, so a crash never starts two instances.
- Terminates an environment that didn't boot in time and requeues its job
  once, without spending one of the job's attempts. A second boot failure
  fails the job.
- Terminates the environment of a finished or canceled job and revokes its
  runner key.
- Terminates an environment past its lifetime and marks its job lost.
- Terminates instances with this install's tags and an `armature-env` tag
  that the database doesn't know, after 10 minutes, and fails jobs whose
  instance is gone. Mac host instances and image builds carry no
  `armature-env` tag, so the sweep never touches them.
- Releases an idle Mac host after its idle time, once its 24 hours are up.

The server's own job worker also requeues, once, or fails a `provisioning`
job that's 10 minutes past its lease with no live environment, so a job
never waits forever when the AWS connection is gone. When an environment is
still booting 10 minutes past its boot timeout, as after a restart with no
verified connection, the worker marks it failed and revokes its key. The
orphan sweep terminates its instance once a connection verifies again. A
connection that isn't verified never relaunches anything.

An environment's runner key can only claim its own job. It can't register
runs of its own, and neither can the warm runner's.

The warm runner is one on-demand instance with the `llm` capability and a
standing key. Start and stop it on the AWS page. The reaper never touches
it.

### Mac hosts

A Mac dedicated host bills for at least 24 hours from allocation. The AWS
page allocates one only when the connection allows host allocation and you
confirm the cost. Otherwise it says to ask the account owner. The server
then starts the host's macOS instance from the Mac launch template. Without
a Mac launch template, record the instance ID on the host yourself. Each
host runs up to two Tart VMs at once. The server refuses to release a host
before its 24 hours are up.

## Receiving webhooks

Each webhook gets its own URL under `/hooks/`, after any base path. The
proxy must pass the request body unchanged and keep the signature headers:
`X-Hub-Signature-256` for GitHub, `X-Slack-Signature` and
`X-Slack-Request-Timestamp` for Slack, `X-Hub-Signature` for Jira, and
`Linear-Signature` for Linear. A Linear delivery more than 60 seconds old,
or a Slack delivery more than 5 minutes old, is rejected, so keep the server
clock in sync.

A new webhook starts pending. Deliveries that pass the signature check are
held. They queue nothing and aren't forwarded until a team lead opens a held
delivery and approves it. Approving turns on sender pinning with that one
sender: the GitHub App installation, or the repository when there is none,
the Jira site or cloud id, the Linear workspace, or the Slack workspace. A
delivery with no sender can't be approved. With pinning on, a delivery from
a sender that isn't on the list is held. Approving it adds the sender to the
list, and dismissing it leaves it unrun. A team lead can remove a sender
from the list, or turn pinning off. Rejecting a pending webhook deletes it
and its team secret, unless another webhook or a rule in the team uses that
secret. Generic webhooks also need approval, but are never pinned.

An approved or replayed delivery goes to the forward URL once, signed again
with the webhook's secret as its source signs it, with a fresh Slack
timestamp. Linear signs a timestamp inside the body, so a replayed Linear
delivery is forwarded unsigned.

Webhooks that existed before this upgrade stay approved with sender pinning
off, so any delivery with a valid signature is let through as before. To
turn pinning on, a team lead reviews the senders the webhook has let through
in the last two weeks and confirms that list.

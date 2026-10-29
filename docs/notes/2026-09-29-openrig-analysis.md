# OpenRig Analysis — Fit as a "Flock of Workflows" Layer for Armature

**Date:** 2026-09-29
**Repo:** https://github.com/mvschwarz/openrig (primary sources only — repo files, GitHub API metadata, releases)
**Question:** Once there are many Armature workflows running under a larger mission/initiative, would OpenRig help configure and manage that "flock"?

---

## What OpenRig Is

OpenRig is a **local multi-agent harness orchestrator** written in TypeScript (npm package `@openrig/cli`). Its own tagline: *"Multi-agent harness that runs Claude Code and Codex together as one system"*, and its framing sentence: **"A harness wraps a model. A rig wraps your harnesses. Define your agent team in YAML, boot it with one command."** ([README](https://github.com/mvschwarz/openrig))

It turns a scattered set of *interactive AI coding agent sessions* (Claude Code, Codex CLI) into a persistent, named, coordinated "team" that survives reboots. Concretely it is:

- A **daemon** (Hono HTTP, SQLite-backed) + **CLI** (`rig`, ~80 subcommands) + **TUI** + **web UI** + **MCP server** (so agents can manage their own topology)
- Built on **tmux** as the terminal transport; macOS/Linux only, Node 22/24, no native Windows
- Local-first: single maintainer's machine (or a small set of hosts via cross-host coordination), Apache-2.0 licensed

This is the crucial orientation point: OpenRig orchestrates **long-lived interactive agent processes** (resident harness sessions you can attach to and type into), not declarative workflows of one-shot LLM calls. Its own docs call it "a local control plane for multi-agent work" ([docs/DESIGN.md](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/DESIGN.md)).

## Core Concepts and Vocabulary

Its config grammar is two YAML files, mirrors of Armature's single spec:

### 1. RigSpec (`rig.yaml`, version "0.2") — the topology
([docs/reference/rig-spec.md](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/rig-spec.md))

Real example, the shipped demo ([demo/rig.yaml](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/demo/rig.yaml)):

```yaml
version: "0.2"
name: demo-rig
culture_file: culture.md
pods:
  - id: orch
    label: Orchestration
    members:
      - id: lead
        agent_ref: "local:agents/lead"
        profile: default
        runtime: claude-code
        cwd: .
    edges: []
  - id: dev
    label: Development
    members:
      - id: impl
        agent_ref: "local:agents/impl"
        profile: default
        runtime: claude-code
        cwd: .
      - id: qa
        agent_ref: "local:agents/qa"
        profile: default
        runtime: codex
        cwd: .
    edges:
      - kind: can_observe
        from: qa
        to: impl
edges:
  - kind: delegates_to
    from: orch.lead
    to: dev.impl
```

Key fields: `pods` (grouping), `members` (each requiring the quadruple `agent_ref`, `profile`, `runtime` ∈ {claude-code, codex, terminal}, `cwd`), `edges`, `culture_file`, `permission_policy` (`builtin:locked|standard|open|yolo` — "YOLO is off by default"), `services` (Docker Compose with health-gated `wait_for`), `startup` (files + idempotent actions with `applies_on: [fresh_start|restore]`), and pod-level `continuity_policy` (sync triggers, restore protocol).

A member's stable address is a **Seat**: `dev-owner@first-project`. Session names are derived `{podId}-{memberId}@{rigName}`.

### 2. Edge kinds — declared intent, mostly unenforced
([docs/reference/edge-types.md](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/edge-types.md))

Five kinds: `delegates_to`, `spawned_by` (these two constrain launch order via topological sort), `can_observe`, `collaborates_with`, `escalates_to` (visualization/identity only). The doc is explicit that edges do **not** route messages, enforce permissions, or restrict communication — the vocabulary "captures design intent even before the runtime enforces it."

### 3. AgentSpec (`agent.yaml`, version "1.0") — per-agent resources
([docs/reference/agent-spec.md](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/agent-spec.md))

`name`, `profiles` (named bundles a rig member selects via `profile:`), `resources` in five categories — `skills`, `guidance` (merged into CLAUDE.md), `subagents`, `hooks`, and `runtime_resources` (fragments merged into `.claude/settings.local.json`, `.mcp.json`, or Codex `config.toml`), plus `imports` (other AgentSpecs whose resources become selectable as `namespace:id`). Lifecycle field `execution_mode` accepts only `interactive_resident` in v1. The real demo lead agent is nearly empty:

```yaml
name: lead
version: "1.0.0"
resources:
  skills: []
profiles:
  default:
    uses:
      skills: []
```

### 4. The durable coordination layer ("hot-potato" queue)
([docs/as-built/architecture/coordination-primitive.md](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/as-built/architecture/coordination-primitive.md))

Work moves through SQLite tables: `stream_items` (append-only audit intake), `queue_items` (8-state lifecycle: pending → in-progress → done/blocked/failed/denied/canceled/handed-off), `queue_transitions` (append-only), plus inbox/outbox mailboxes. The load-bearing rule: closing a queue item as `done` **requires a closure reason** from a fixed set — `{handed_off_to, blocked_on, denied, canceled, no-follow-on, escalation}` — enforced inside the daemon's transaction boundary. You cannot silently finish a task.

### 5. Workflow Runtime (WF) — linear handoff chains, not a DAG
([docs/as-built/architecture/workflow-runtime.md](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/as-built/architecture/workflow-runtime.md))

Three SQLite tables: `workflow_specs` (read-through cache of authored YAML/markdown specs keyed by `(name, version)` with content hash), `workflow_instances` (survive daemon restart), `workflow_step_trails` (append-only). The **transactional-scribe contract** commits packet closure + next-step creation + trail + events in one transaction ("lost handoffs are impossible by design"). Features: `next_hop.on: {handoff|waiting|done|failed: step-id}` branching, per-step `harness:` and `host:` pins, human `gate:` objects that park the instance `waiting`, a `max_hops` loop guard, and keepalive watchdogs that nudge stuck owners after crashes. Resume ("WF-5") redrives a failed instance in one transaction with re-resolved owners. This is a **single-chain handoff state machine** — roughly a linear pipeline with bounded remediation loops. There is no fan-out, no typed output schemas, no model-tier management.

### 6. Mission Control and operating posture — the operator layer
([docs/as-built/architecture/mission-control.md](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/as-built/architecture/mission-control.md), [docs/reference/scoped-operating-posture.md](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/scoped-operating-posture.md))

Mission Control is a phone-friendly observability surface: **seven views** (`my-queue`, `human-gate`, `fleet`, `active-work`, `recent-ship`s, `recently-active`, `recent-observations`) returning a 9-field content model (name, phase, status, next-action, pending-human-decision, read-cost, last-update, confidence, evidence link), and **seven atomic write verbs** (`approve`, `deny`, `route`, `annotate`, `hold`, `drop`, `handoff`) — each verb commits queue mutation + audit row + event in one transaction, with append-only audit and bearer-token auth. Notifications via ntfy/webhook "for the operator's phone via tailnet."

**Operating posture** is a per-scope binary — `human-led` (quiet by default) vs `delegated` — resolved through the precedence chain *qitem → workstream → mission → project → rig → global host*, with `grantsAuthority: always false`. Delegated posture is required before agents may interrupt the operator with new findings. Work is organized on disk as **missions/slices** (`rig scope`), distinct from runtime coordination (`rig workflow instantiate`).

### 7. Continuity — snapshot/restore with "honesty"
Stable addresses plus restore machinery: `restore_policy` per member (`resume_if_possible`, `relaunch_fresh`, `checkpoint_only`), snapshot/restore of whole topologies, restore-packet parsers for Claude/Codex transcripts, and an explicit "restore honesty" architecture rule (restore-check probes; resume vs rebuild vs fresh is reported truthfully). There is also a `docs/reference/legacy-topology-migration.md`, i.e. the topology grammar already broke once.

## Architecture and How It Works

([docs/as-built/architecture.md](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/as-built/architecture.md) — itself a redirect map into 14 thematic modules)

- **Processes:** one local daemon (`createDaemon` composition root, Hono HTTP routes, SQLite with ~39+ migrations), launched on demand by `rig up`. The daemon nudges tmux; it does not exec agents itself.
- **Agent execution:** a five-method **RuntimeAdapter** contract with concrete Claude/Codex/Terminal adapters. Agents live in tmux panes as normal CLI sessions; the daemon injects prompts (`rig send`), captures transcripts, and relays activity-hook events (minimal payload, no prompt text/tool args) to `/api/activity/hooks`.
- **Data flow:** operator (or agent, via MCP) emits work → stream item → absorbed into a queue item owned by a seat → owner closes with a closure reason, possibly handing off → next owner. Events flow to clients over SSE; a 73-member `RigEvent` union.
- **Storage:** SQLite is the single durable store (chat, queue, workflows, audit, mode bindings); missions/slices and CULTURE.md are plain files.
- **Cross-host (v0.4.6+):** only create and handoff cross host boundaries — "origin-owns-the-record, at-least-once + idempotent, message-passing closure (never 2PC)"; successor IDs derived deterministically so retries deduplicate on the primary key.
- **Bundles:** `rig bundle` packages a rig (RigBundle) for sharing — analogous to Armature's workflow packages.
- **Discovery:** `rig adopt` can adopt existing stray tmux sessions into a managed rig.

Typical operator flow ([getting-started](https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/getting-started.md)):

```sh
rig specs preview first-project
rig up first-project --cwd . --plan   # dry-run resolution
rig up first-project --cwd .
rig ps --nodes --rig first-project    # seat readiness, not just daemon health
rig send dev-owner@first-project 'Improve the CSV import error...'
rig queue list --limit 1000
```

## Maturity Assessment

**It is a working system, not a spec or prototype — but a young, single-maintainer, very-high-velocity one.**

- **Repo metadata** ([GitHub API](https://api.github.com/repos/mvschwarz/openrig)): created 2026-04-01 (six months old); 2,175 stars, 160 forks, 56 open issues, 28 MB; TypeScript; Apache-2.0; last push 2026-09-29 (today).
- **Releases** ([releases API](https://api.github.com/repos/mvschwarz/openrig/releases?per_page=10)): 40+ releases in ~6 months; v0.5.17→v0.6.1 shipped within four days (Sept 25–29). v0.6.0 is literally titled "works for strangers" — first-use hardening is still the current theme. v0.6.1 release notes state it "does not claim hardened public-network deployment."
- **Contributors** ([contributors API](https://api.github.com/repos/mvschwarz/openrig/contributors?per_page=100&anon=true)): effectively one author — mvschwarz with 1,496 commits — plus a build bot (`v-openrig-build`, ~1,534 commits), meaning a large fraction of the codebase is agent-authored (the project dogfoods itself heavily; there is even a `factory-rsi` recursive-self-improvement workflow). A handful of external contributors (Wu Shuwen, a few 1-commit contributors) appeared only in the most recent commits.
- **Tests/CI:** genuinely strong for the age — a large `packages/cli/test/` suite (100+ test files visible in the tree), `.github/workflows/tests.yml` with eight jobs including "a seeded lost-baton failure scenario — a checker that misses that seeded failure cannot pass," a `portability-report.yml`, and a Docker testbed with L0–L6 runbooks ([docker/testbed](https://github.com/mvschwarz/openrig/tree/main/docker/testbed)).
- **Docs:** unusually extensive but sprawling — an `as-built/` tree kept synchronized with code (rare and good), a `reference/` tree whose scope has drifted into product-SDLC territory (`wave-sdlc.md`, `product-journey-sdlc.md`, `planning-dial.md`, `knowledge-maturity.md`, `lore-routing.md`), release notes per version, and a website (openrig.dev). The topology grammar has already required a `legacy-topology-migration.md`.
- **Risk markers:** 0.x versioning with rapid breaking change; bus factor of one; concepts still being invented weekly (mission control, postures, workflow runtime are all recent phases PL-004/PL-005/WF-2).

## Mapping to Armature's "Flock of Workflows" Problem

### Different layer, different noun

The first thing to be clear-eyed about: **OpenRig and Armature are not the same category of system.**

| | Armature | OpenRig |
|---|---|---|
| Unit of execution | A workflow run: DAG of one-shot LLM calls | A seat: a long-lived interactive Claude Code/Codex session |
| What the user defines | One YAML spec: stages, roles, model tiers, schemas | Two YAML files: a topology (who exists) + per-agent resources |
| Data flow | Cumulative shared context dict; `depends_on` | Message passing: `rig send`, queues with closure reasons |
| Orchestration grammar | Topological DAG, fan-out, fan-in, loops, gates | Topological *launch order* only; edges are documentary; workflows are single handoff chains |
| Model management | Model tiers, guided_json escalation | None — delegates to the harness's own model config |
| Runtime | Own Python engine calling provider APIs | Wraps external harness CLIs via tmux |

OpenRig's agents are **humans-with-a-harness** peers: they're given outcomes, they use tools, they hand off. Armature stages are deterministic nodes in a plan. OpenRig's Workflow Runtime (its closest analog to Armature) is strictly *weaker* than Armature's DAG: no fan-out, no typed outputs, no budget terms, no context governance.

### Where OpenRig is genuinely ahead of Armature — the concepts worth borrowing

For the flock problem specifically, OpenRig has a mature *operator control-plane* vocabulary that Armature currently lacks:

1. **Stable addresses (seats) + a persistent daemon.** Armature runs are ephemeral; OpenRig's daemon makes agents addressable (`dev-owner@first-project`) across restarts, with snapshot/restore and honest resume. An Armature "flock" layer would need exactly this: a long-lived supervisor process, not per-run CLIs.
2. **Mission/slice scope layer.** `rig scope` puts durable work records (what exists, what's next) on disk, separate from runtime coordination (`rig workflow instantiate`). This is the missing layer above Armature: today an Armature workflow is the top of the hierarchy; there's no mission → slice → workflow chain. OpenRig's precedence chain (qitem → workstream → mission → project → rig → host) is a worked model.
3. **Mission Control's shape.** Seven views over one queue primitive, a 9-field phone-friendly row model, and seven *atomic audited write verbs* (approve/deny/route/annotate/hold/drop/handoff). Armature's `dashboard` is read-only health metrics; it has no operator action surface, no audit trail of interventions, no notification dispatch (ntfy "operator's phone via tailnet").
4. **Operating posture (human-led vs delegated).** A per-scope switch controlling *when agents are allowed to interrupt the human*, with explicit `grantsAuthority: false`. Armature's `gate: human` is per-stage; it has no workflow-level or mission-level posture concept.
5. **Closure-reason discipline.** "You cannot silently finish a task" — every completion declares where the work went next (`handed_off_to` / `blocked_on` / `escalation` / …), enforced transactionally. For a flock of Armature workflows handing work to each other, this is the single best idea in the repo: a typed inter-workflow handoff contract.
6. **Watchdog keepalives with boot-sweep re-arm.** Instances survive daemon restart; stuck work gets nudged; crash recovery is a first-class loop. Armature's resume work (PR #27) covers one run; OpenRig models the supervisor's crash recovery across all runs.
7. **Cross-host handoff design.** "Origin-owns-the-record, at-least-once + idempotent, message-passing closure (never 2PC)" with deterministically derived successor IDs — directly relevant to the parked Armature Dispatch work.

### What overlaps with what Armature already has

- `armature loop` (repeat runs under a central budget) ↔ OpenRig has **no budget management at all** — no token budget, no cost ceiling, no usage governance (the v0.6.1 release shows only rate-limit *telemetry*). Armature is ahead here; OpenRig would not import this.
- `armature dashboard` ↔ Mission Control, but OpenRig's is far richer (write verbs, audit, notifications, phone model).
- Workflow packages ↔ RigBundle (portable rig packaging) — same idea, both recent.
- `subagent_spec` (child workflow as nested execution) ↔ OpenRig's `spawned_by` edge + subagent resources — same instinct; OpenRig's is looser (unmanaged by the runtime).
- Human gates ↔ OpenRig's `gate:` workflow objects and `human-gate` Mission Control view — comparable.

### What does not fit

- **Stack:** TypeScript/Node + tmux vs Python. An Armature fleet layer adopting OpenRig as a component would run Node 22/24 alongside the Python engine and depend on tmux as a transport — a heavy, awkward dependency for something that manages batch workflow runs, which don't need panes at all.
- **Harness coupling:** OpenRig's adapters are Claude Code and Codex *CLI* sessions. Armature's stages are API calls. Wrapping an Armature workflow as an OpenRig member would mean running it inside a tmux pane and talking to it through `rig send` text — a regression versus Armature's typed, structured stage I/O.
- **Orchestration depth:** for anything Armature would delegate *to*, OpenRig's workflow runtime cannot express fan-out, guided JSON contracts, model tiers, or context governance. The delegation arrow can't point from Armature to OpenRig; OpenRig cannot host Armature-grade workflows.
- **Churn risk:** six months old, 0.x, bus factor ~1, grammar already migrated once.

## Honest Verdict

**Borrow the concepts; do not adopt the system.**

OpenRig is real, tested, and thoughtfully documented, but it solves a different problem — orchestrating *interactive resident coding agents* on one operator's machine — at a different layer than Armature's batch DAG workflows. As a concrete maturity layer *for* Armature it fails on stack (Node/tmux), on grammar (no fan-out, no typed contracts, no budgets), and on stability (0.x, single maintainer, breaking changes).

What OpenRig genuinely offers the flock question is a **worked design for the operator control plane**: a persistent daemon with SQLite state; stable seat addresses; a mission → slice → workflow scope hierarchy on disk; a queue primitive with mandatory closure reasons as the inter-agent contract; an operator surface with atomic, audited write verbs and phone-friendly notifications; a per-scope human-led/delegated posture switch; and watchdog-driven crash recovery. Those seven concepts map one-to-one onto Armature's actual gaps above the workflow layer, and they are the parts of OpenRig that are concept-mature despite the codebase's youth. An Armature-native "flock" or "missions" layer would do well to copy the *shapes* — the closure-reason set, the seven-verb operator contract, the posture precedence chain, the keepalive boot sweep — implemented in Python against Armature's existing run/results store, rather than embedding or calling OpenRig.

Secondary observation worth recording: OpenRig is also a live existence proof that a single maintainer with agent leverage can ship a production-shaped control plane in six months, and its `as-built` doc convention (architecture docs split into frontmatter-tagged modules regenerated alongside code) is itself a practice worth stealing.

---

### Sources read

- Repo/README: https://github.com/mvschwarz/openrig
- RigSpec reference: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/rig-spec.md
- AgentSpec reference: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/agent-spec.md
- Edge types: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/edge-types.md
- Demo topology and agent: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/demo/rig.yaml, https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/demo/agents/lead/agent.yaml
- Architecture map: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/as-built/architecture.md
- Workflow runtime: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/as-built/architecture/workflow-runtime.md
- Coordination primitive: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/as-built/architecture/coordination-primitive.md
- Mission Control: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/as-built/architecture/mission-control.md
- Design language: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/DESIGN.md
- Getting started: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/getting-started.md
- Operating posture: https://raw.githubusercontent.com/mvschwarz/openrig/HEAD/docs/reference/scoped-operating-posture.md
- GitHub API: repo metadata, releases, contributors, commits, git tree (fetched 2026-09-29)
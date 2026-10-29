# Pi (earendil-works) — a Minimal, Extensible Coding-Agent Harness vs. Armature's Declared Workflows

**Date:** 2026-10-26
**Repo:** https://github.com/earendil-works/pi (primary sources only — repo files read from a shallow clone at v1.4.0 packages, 2026-10-26)
**Question:** The user calls it "a new approach to a harness." What is it, how real is it, and does anything move the needle past what Armature already has?

---

## What Pi Is

A production-grade, MIT-licensed **coding-agent harness** — the same product class as Claude Code / Codex CLI / OpenClaw — built as a TypeScript monorepo (~182k lines of src across 11 packages, 706 test files, `packages/*/package.json` all at v1.4.0). By **earendil-works** (Austria; "strengthen human agency / craft software and open protocols") — the org includes Armin Ronacher (Flask), David Brailovsky, and Mario Zechner (badlogic, libGDX). The main repo carries ~118k stars. Pi is not a research prototype; it ships via npm, a pi.dev installer, Nix flakes, and standalone binaries.

Its stated philosophy is the opposite of kitchen-sink: *"Pi is a minimal, extensible agent harness that you can make your own… Pi ships with powerful defaults but skips features like sub-agents and plan mode. Ask Pi to build what you want, or install a package that does it your way."* The repo's `AGENTS.md` (8.4K) is itself a serious agent-behavior contract — conversational rules and code-quality rules for both humans and agents.

Package layout (`packages/`):

| Package | LOC (src) | Role |
|---|---|---|
| `coding-agent` | 76k | The CLI: sessions, tools, interactive/print/JSON/RPC/SDK modes |
| `ai` | 28k | Unified multi-provider LLM API (OpenAI, Anthropic, Google, …) |
| `durable` | 28k | **Durable agent harness** (experimental): commit-before-show, resume |
| `tui` | 20k | Terminal UI with differential rendering |
| `chord` | 15k | Typed host runtime: hot-reloadable objects, principals, RPC |
| `agent` | 2.5k | Agent loop core (tool calling, state) |
| `evals` | 1.4k | Behavioral evals (vitest-evals), docs-lift paired containers |
| `mcp`, `telemetry`, `codemode`, `env` | 1.5k–3.3k | MCP client, vendor-neutral telemetry, sandboxed scripting, env |

Maturity evidence: 706 `*.test.ts` files; behavioral evals with paired-container runs; supply-chain hardening treated as a first-class feature (below); honest security docs (`packages/coding-agent/docs/security.md`) that say plainly *"Watching the transcript, using project trust, and reviewing changes do not create a security boundary."*

## The Mechanisms

### 1. pi-durable — write-ahead durability for the agent loop
`packages/durable/README.md`: *"All changes go through one line of atomic commits, and nothing is shown before its commit is stored."* Every transcript **entry is immutable**; a **task** is a durable state machine that "saves a checkpoint at every step, so a restarted process continues from the last one." Generation (`pi.generation`) and tool calls (`pi.tool`) are themselves built-in tasks. Storage is SQLite or JSONL, and the harness runs on Node **or Cloudflare Durable Objects** (`src/cloudflare.ts`, `declarations.ts`). If the process dies mid-turn, reopening storage resumes the turn — not just the conversation.

### 2. codemode — agent-authored orchestration in a sandbox
`packages/coding-agent/docs/codemode.md`: the model writes raw JavaScript (not JSON, not a fenced block) that runs as the body of an async function inside a **QuickJS sandbox with no Node APIs, no filesystem, no network, no timers**. Scripts reach the outside world *only* through tool handles and a `models` handle exposing three types: `chat`, `image`, and **`classifier`**. Only the script's output reaches the model. Consequences the docs state honestly: a script can parallelize tool calls and **filter large results before the model sees them**; tool calls before a script failure are not undone (no transactional rollback of effects); output is bounded (`max_output_tokens`, hard char limits).

### 3. chord — objects with principals
`packages/chord/README.md`: applications built from **objects** (state + code at an address) created on demand, addressed by **references** that survive hot reload; **every object knows its principal — an owner and a guest get different objects, with different methods**; state is observable JSON synced as snapshot + deltas over in-memory/IPC/WebSocket links; contracts are runtime TypeBox values. Hot reload keeps in-flight work on the code it started with and migrates incompatible state at a safe point.

### 4. Extensions / skills / packages — the customization plane
Extensions are TypeScript modules loaded **inside the Pi process with full OS permissions** (docs warn accordingly) registering tools, commands, path protection, dangerous-command confirmation, session-event handlers, context modification, and UI. Skills follow the **agentskills.io spec**: advertised by name and description, full instructions loaded **on demand**. All of it bundles into npm/git-distributable packages (`pi install npm:…`, `pi install git:…`).

### 5. Security by containment, not by permission system
Pi deliberately has **no built-in permission system** ("By default, it runs with the permissions of the user and process that launched it"). Instead, `docs/containerization.md` documents three isolation patterns, the most interesting being **Gondolin** (separate repo, ~2.3k stars): a local Linux micro-VM where *pi and its file-based provider credentials stay on the host* while built-in tools and `!` commands execute inside the VM — and the docs flag its own weakness (commands inherit host env vars, so env-var credentials leak into the VM).

### 6. Supply-chain hardening as a product feature
`README.md` "Supply-chain hardening": direct deps pinned exact (`.npmrc` `save-exact=true`, `min-release-age=2` to refuse same-day releases), `package-lock.json` ground truth with pre-commit blocking accidental lockfile commits, an **install-lock with an explicit allowlist for dependency lifecycle scripts** (new lifecycle-script deps fail checks until reviewed), `--ignore-scripts` everywhere including CI, scheduled `npm audit` workflows, and the same tarball packer for release smoke tests and publication.

### 7. Sessions as trees
`docs/how-pi-works.md`: session entries form a tree; each path is a branch; forking clones selected history; compaction inserts a summary entry while originals remain in the tree; model context is reconstructed from the active branch.

## Mechanism-by-Mechanism Armature Mapping

| Pi mechanism | Armature status | Evidence / concrete slice |
|---|---|---|
| **codemode sandbox** | **Missing — most valuable import.** | Armature's fan-out/partition is *spec-declared* orchestration; codemode is *agent-authored* orchestration inside one turn, with cheap `classifier` models callable from the sandbox — the Jev pre-filter bet, embedded in the loop. Slice: an adapter type whose `cmd` is a model-authored script run in a sandboxed interpreter (QuickJS via PyMiniRacer, or Python via RestrictedPython/Pyodide) exposing (a) the ToolRegistry as a call handle, (b) a decision/classifier handle (the TypeSafe API), returning only the script's filtered output as stage value. Directly addresses H4-v2's latency pain: filter-before-the-model-sees is the operational form of that lesson. |
| **pi-durable commit-before-show, checkpoint-per-step** | **Partial.** | Armature persists stage *results* after completion (context dict) and has the resume contract (PR #27 SessionLock) at stage granularity; pi-durable checkpoints *inside* a turn and commits transcript+state+task atomically. Slice for the dispatch runner: a work-unit WAL — write the unit's receipt before the runner reports success, so a Fargate task death mid-stage resumes rather than re-bills. Feeds the July currency-check finding that the resume contract was "the one real gap." |
| **chord principals** | Missing, but deployment-side by our boundary rule. | Capability-style "owner and guest get different objects" is app-runtime IAM; for Armature it belongs to the governance arc's PDP conversation (Octopus's formed-policy + pi's principals are two answers to the same question), not the engine. |
| **Skills: name+description advertised, loaded on demand** | **Already built — 4th independent validation.** | The Memory Pyramid's P2 agent-pulled-context bet: our 3-arm eval → AKV Cache-As-Tool → Octopus's bound artifacts → pi's on-demand skills. Armature needs nothing; record the citation. |
| **Extensions in-process, full permissions, trust required** | Already covered differently. | Armature's ToolRegistry.dispatch chokepoint + lifecycle hooks + `api_key_env` sweeping are the same plane with a stricter default (Armature declares tools in the spec; pi loads them from disk at runtime). |
| **No permission system; containerize instead** | Already covered differently, converging. | Pi's three-pattern table + Gondolin (creds on host, tools in VM) is a concrete input to the governance brief's P1 egress/secret-broker items — and pi's own caveat (env-var inheritance breaks the boundary) is the reason the brief says "inject only narrowly required secrets." |
| **Supply-chain hardening** | Partial. | Armature packages already have manifest.sha256 sealing and `armature package verify`; pi adds: lifecycle-script allowlists for bundled deps, min-release-age pinning, lockfile ground-truth discipline. Slice: fold two or three of these checks into `armature package verify` (governance brief P2 made concrete). |
| **Docs-lift evals** (paired without/with-docs containers) | Missing, minor. | A neat methodology for measuring whether documentation actually helps the agent; could inform Armature's self_analyst, but low priority. |
| **Session trees / compaction-preserves-originals** | Already covered differently. | Armature's context model is cumulative-DAG, not conversation-tree; the compaction-preserves-originals detail rhymes with the memory pyramid's provenance discipline. |

## Convergence With the Prior Notes

- **Agent-pulled context (4th source):** skills loaded on demand joins our 3-arm eval, AKV's Cache-As-Tool (`2026-10-26-cache-engineering-for-agents.md`), and Octopus's memory-as-bound-artifacts (`2026-10-26-octopus-si-os-analysis.md`). The P2 bet is now about as externally validated as a design can get.
- **Cheap decision models at the edge:** codemode's `classifier` type is the same bet as JevAgentHarness (`2026-10-26-jev-agent-harness-decision-models.md`) — but pi embeds it *in the orchestration layer* rather than as a gate, a composition Armature's deferred Layer 2 hadn't considered.
- **Credentials outside the execution boundary:** Gondolin + Octopus's process-authority separation + the governance brief's P1 secret-broker are three statements of one rule.
- **Durability:** pi-durable (and the org's `absurd`/`dquickts` experiments — QuickJS with durable execution, showing this is the org's central research theme) align with the July harness-literature currency check that flagged resume as the one real Armature gap.
- **Filter-before-model-sees:** codemode operationalizes what the AKV paper argued (context cost) and what H4-v2 measured (latency cancels quality).

## Verdict

**SHELVE — with codemode as the one genuinely novel import candidate, and two smaller design inputs.** Pi is a mature, production-grade *interactive coding agent* — a different problem from Armature's declared, reproducible workflow fleet (same class difference as OpenClaw and Octopus: complements, not competitors). Most of its surface (agent loop, TUI, session trees, MCP, telemetry) is the interactive harness Armature deliberately isn't.

What survives the "moves the needle" bar:

1. **Codemode** — the only mechanism here Armature has no equivalent for: agent-authored, sandboxed, output-only orchestration with cheap classifier models callable inside the loop. It composes three of our parked arcs (typesafe-decision Layer 2, H4 latency, tool-chokepoint enforcement) into one adapter design. Worth a spec-level design discussion when any of those arcs reopens.
2. **pi-durable's commit-before-show** — the durability bar for the dispatch runner's work units.
3. **Pi's supply-chain checklist** — three concrete additions to `armature package verify`.

Maturity: high — unlike the Octopus repo (17 days old, zero evals), pi is tested (706 test files), evaluated (vitest-evals suites), honestly documented, and maintained by named engineers with deep OSS track records.
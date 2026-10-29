# NVIDIA OpenShell Analysis — a Per-Request Authorization Runtime vs. Armature's Declared Workflows

**Date:** 2026-10-28
**Primary sources:** https://docs.nvidia.com/openshell/about/overview (+ /about/architecture, /how-it-works/policies/overview, /how-it-works/policies/prover, /how-it-works/providers/overview, /how-it-works/inference, /how-it-works/gateways/authentication, /security/best-practices, /observability, /about/support-matrix) and the repo at https://github.com/NVIDIA/OpenShell — all doc pages read at v0.1.6.
**Question (Bryan's, two-part):** What might OpenShell bring to Armature — and, more interestingly, *where* could Armature workflows run so that running them there *adds to AI Governance principles*?

---

## What OpenShell Is

**OpenShell is an open-source runtime for fleets of autonomous AI agents: every agent runs in a sandbox whose filesystem, process, network, and credential access are kernel-enforced from outside, and every outbound network request passes a per-request policy check that also decides whether a real credential gets injected.** Apache-2.0, primarily Rust (`crates/`) with Python/TypeScript/Go SDKs, published by NVIDIA. Repo facts (2026-10-28): **20.5k stars, ~2k forks, 1,890 commits**, 387 open issues, weekly stable releases (Tuesdays), current v0.1.6 — a *very* young project with unusually strong adoption and unusually serious docs. No NIM/NGC/GPU dependency anywhere in the required stack; the only "NVIDIA" in the runtime is the default Ubuntu base image (`nvcr.io/nvidia/base/ubuntu:24.04`) and an example provider profile.

What it runs: agent workloads — Claude Code, OpenCode, Codex, Copilot CLI are the named examples — as owned child processes inside a sandbox, on Docker, Podman (rootless), Kubernetes (Helm, requires a NetworkPolicy-enforcing CNI), or a libkrun MicroVM. The host needs Linux 6.2+ with Landlock ABI 3 enabled and seccomp user-notification; macOS runs it inside the Docker Desktop Linux VM; launch fails closed if the kernel features are missing.

## The Architecture (about/architecture)

A control plane plus a per-sandbox split that puts every decision on the trusted side of the boundary:

- **Gateway** — control plane: lifecycle, user auth (mTLS, OIDC with PKCE/client-credentials/device-grant), state, policy delivery, and *it issues all credentials*.
- **Supervisor** — the trusted decision-maker, one per sandbox, outside the sandbox's network fence: "checks requests against policy, supplies credentials, resolves DNS, opens approved connections." It is the workload's *only* egress path; the outer fence denies everything else.
- **Sandbox** — inside the boundary with the agent; owns the agent's processes, identifies the calling program from trusted `/proc` data ("the real program behind each request, not a path the agent claims"), and forwards TCP/DNS to the supervisor. It "never makes policy decisions."
- **Policy prover** — an SMT solver in the gateway that formally checks policy changes (below); ships standalone as `openshell-prover`.

Isolation is Landlock (filesystem, locked at creation) + seccomp user-notification (staged network ops) + non-root identity with no Linux capabilities + network fencing per runtime (Docker/Podman: workload networking disabled; K8s: NetworkPolicy allows only the supervisor; MicroVM: guest has no network device, supervisor talks over authenticated vsock). The docs are honest about the limit: this is **container-grade defense in depth, not a hypervisor boundary** — user-namespace root-mapping mitigates container escape but is "disabled by default; GPU compatibility unverified."

Identity: per-sandbox JWTs minted by the gateway, **bound to one sandbox and one "generation" (run)** of it; restarting invalidates prior tokens and TLS certs. The sandbox side holds "nothing worth stealing: no gateway signing key, no gateway JWT, and no provider credentials." On Kubernetes the supervisor bootstraps from the pod's projected ServiceAccount token, and the gateway re-verifies the live pod UID before minting anything — fail-closed throughout.

## The Mechanisms That Matter for Governance

### 1. Declarative deny-by-default policy (how-it-works/policies/overview, security/best-practices)

A YAML policy with five sections — `filesystem_policy` (Landlock), `landlock` compat mode, `process` (uid/gid), `network_policies` (per-destination, **per-binary** rules with L7 access modes like `read-only` vs `read-write`), `network_middlewares`. "OpenShell denies anything the policy does not allow." Precedence: global policy (which *replaces* per-sandbox policy — a fleet lockdown switch) > saved policy > image policy (an invalid one blocks launch rather than falling back) > built-in default. Filesystem/process are fixed at creation; network rules hot-reload at runtime. The policy file is explicitly pitched as "version-controlled security controls that can be reviewed and audited."

### 2. Per-request authorization with credential injection (how-it-works/providers/overview, inference)

**This is the headline mechanism.** Credentials never enter the sandbox. Each attached provider puts an *opaque placeholder* in the agent's environment; the supervisor resolves it in-flight — in headers, Basic auth, query params, URL path segments, opted-in request bodies, WebSocket text, and **AWS SigV4 re-signing** — but only after two independent gates pass: the network policy must allow this binary to this destination, *and* the provider profile's endpoint binding must cover this host/port/path. Mismatches fail closed with `credential_endpoint_mismatch` (HTTP 403). Refresh material (OAuth2 refresh tokens, client credentials, Google SA JWTs, AWS STS via `sts:AssumeRole`) is stored gateway-side, separate from injectable values; the gateway mints short-lived tokens and rotates them. Break-glass raw-credential export exists as a gRPC RPC restricted to **mTLS operators** (OU=operator certs) — an ordinary user or OIDC admin cannot call it. Provider types require imported **profiles** — YAML access contracts naming authorized hosts/ports/paths/binaries — and "provider creation rejects profileless types."

### 3. The policy prover: SMT-verified proposals (how-it-works/policies/prover)

Two checks, explicitly independent: a **boundary check** ("a policy allows no access beyond a boundary policy" — the enterprise maximum) and a **proposal risk check** (a proposed network rule "adds no risky access compared with the sandbox's current policy, such as access to cloud metadata addresses"). Soundness-first: unmodeled features (GraphQL, MCP, WebSocket, JSON-RPC, symlink-ambiguous paths) return `unsupported` with a stable `reason_code` rather than silently passing; "Treat every other result as a failure." The standalone CLI "is primarily intended for agents — a parent agent can check that a policy it writes for a subagent stays within the parent's own maximum allowed policy," needs no gateway, and is exit-code scriptable for CI.

### 4. Agent-proposed rules with human approval (policies/advisor, overview)

The **Policy Advisor** is the channel by which an agent can "propose narrow network rules for your review" — each proposal automatically triggers the prover's risk check, and flagged changes (new credentialed reach, new HTTP methods) route to human review in the TUI. Approvals persist across restarts *within a sandbox instance* but reset on re-creation ("Destroy and recreate the sandbox to reset all operator-approved endpoints") — approvals are bound to the sandbox generation, not global sticky grants.

### 5. OCSF evidence stream (observability)

Three signal kinds: OTLP traces (gateway, drivers, supervisors), Prometheus metrics (gateway), and **OCSF-schema security events** — "network and HTTP policy decisions, process lifecycle, SSH sessions, and configuration changes," plus gateway-side events "such as approving or rejecting draft policy rules." Export as always-on shorthand (`NET:OPEN [MED] DENIED /usr/bin/curl(2187) -> github.com:443 [reason:transparent_tcp_policy_denied]`), JSONL per sandbox for a log shipper, or OTLP. This is a machine-readable, structured audit trail of every allow/deny decision with binary attribution.

### 6. Trust-on-first-use binary pinning (security/best-practices)

Authorized binary paths are pinned to the first observed **SHA256 digest**; "a later digest mismatch triggers an immediate deny," and missing integrity evidence fails closed. Broad globs like `/**` are flagged as mistakes.

## Mechanism-by-Mechanism Armature Mapping

Per the boundary rule (policy is grammar in the engine repo; mechanism lives in the deployment), almost all of OpenShell is *mechanism* — and that is the point: it is the first credible off-the-shelf implementation of the governance brief's P0 column.

| OpenShell mechanism | Armature today | Verdict |
|---|---|---|
| Per-request network authorization, deny-by-default | `safety_rules` enforced at ToolRegistry.dispatch (PR #16); sandbox.mode: docker gives *container* containment, no egress policy; dispatch runners have `allow_all_outbound` on public subnets | **Missing — the single biggest governance gap OpenShell closes.** Our live weak boundary (egress) is exactly what OpenShell fences |
| Credentials outside the execution boundary, placeholder injection | Package secrets injected from `--profile` .env at run time (values do reach the runner's environment) | **Partially covered.** OpenShell's supervisor-side resolution is strictly stronger — secrets never enter the workload at all. 4th independent statement of the credentials convergence (Octopus, pi/Gondolin, our brief) |
| Per-run identity (sandbox ID + generation JWTs; K8s pod-UID re-verification) | Per-package runner task roles in storage_stack.py, but fleet-wide S3 statements; run ids are engine-level, not identity-level | **Design input for the governance arc's P0 #1** — "identity bound to one run of the thing" is the concrete pattern |
| Bound approvals (prover + advisor + generation-scoped persistence) | `gate: human` pauses execution; approvals are a pause, not an access grant | **Design input for P0 #3** — approval-as-scoped-grant that resets on re-creation, with an SMT check *before* a human ever sees it |
| OCSF evidence ledger | Stage transcripts, receipts with sha256 (destinations), `~/.armature/last/`, dispatch job records | **Design input for P0 #4** — schema-compatible allow/deny rows with binary attribution; ours record *what happened*, not *what was refused and why* |
| SMT policy prover, `unsupported`-not-silent | `armature validate` is structural (DAG, tiers, schema checks), not access-boundary reasoning | **New class.** Nothing on our shelf does formal "candidate grants no more than the boundary" checks — and it generalizes: the improve arc's candidate specs are policies too |
| Binary SHA256 pinning (TOFU) | Package manifest.sha256 verifies *files*, not which binary may exercise which grant | **Missing, minor slice** — the dispatch runner could pin interpreter/tool digests per workflow package |
| Landlock/seccomp per-agent sandbox | Docker DooD (namespaces + seccomp defaults, no Landlock, no per-binary process identity) | **Covered-ish.** Same container-grade tier OpenShell itself occupies; OpenShell's addition is *kernel-enforced least privilege inside* the container plus program identity |

## The Two-Part Answer

### Part 1 — What OpenShell brings to Armature

Import as **deployment mechanism, not engine grammar**. Concretely, in the governance-brief's priority-table language:

1. **P0 #2 (central action authorization)** — OpenShell's supervisor *is* the in-process deterministic PDP the Octopus analysis argued for, generalized to network+credentials: policy as data, decision at request time, never inside the untrusted boundary. Armature adopts by *running inside it* rather than reimplementing it.
2. **P0 #3 (bound approvals)** — the advisor flow (agent proposes → prover risk-checks → human approves → grant lives one sandbox generation) is the first concrete, shipped implementation of "approvals that can't be replayed." Note the convergence: Octopus forms grants as pure functions (nothing stored to replay); OpenShell scopes approvals to a generation (stored, but worthless after restart). Same property, two mechanisms.
3. **P0 #4 (evidence ledger)** — OCSF events for allow/deny/credential decisions are a standard-schema version of what our governance brief called for; the armature-dispatch runner's `results/` layout could carry them without a new format.
4. **Grammar-side imports (small, real):** (a) an `audit`-before-`enforce` posture for new `safety_rules` — log violations, don't block, until the rule has soaked; (b) `unsupported`-not-silent semantics for anything Armature verifies but can't fully check (a validator rule that refuses to guess); (c) profile-style *access contracts* for tools: today a tool declares `api_key_env`; the OpenShell-shaped next step is declaring the endpoint the key is valid at, so an injected secret that drifts to a different host fails closed. That last one is the one grammar change worth making regardless of deployment.

### Part 2 — Where to run Armature to add governance value

Three concrete compositions, in ascending order of fit:

1. **The package runner as an OpenShell sandbox (near-term, local).** `armature package run` executes a generic armature-runner image; under OpenShell the runner becomes the sandbox workload: Landlock confines it to the package directory, `network_policies` derive from the spec's declared tools (each tool's host becomes an allowed destination for the runner's binaries), and the secrets profile becomes *provider attachments* — placeholders resolved only at each tool's declared endpoint. The workflow spec (grammar) → OpenShell policy (mechanism) derivation is mechanical because Armature specs already declare tools, their env keys, and now (per import 4d above) their endpoints. This turns "sandbox.mode: docker" from container-shaped containment into per-request authorized containment with an evidence stream — the whole P0 column for the packages path, with zero engine rewrite.
2. **Dispatch runners as OpenShell-managed workloads on Kubernetes (the real fit).** armature-dispatch already runs ECS Fargate runners with per-package task roles but a **fleet-wide S3 policy and open egress** — the two gaps the governance analysis found in the live IaC. Running the dispatch runner image inside OpenShell (K8s driver, Helm) gives per-work-unit egress fencing, credential injection that never places raw values in the task environment, and OCSF rows per request — closing both gaps at the mechanism layer where they belong. The S3 statements problem (fleet-wide GetObject/PutObject) is *not* solved by OpenShell (S3 access is IAM, not egress) — that stays a storage_stack.py fix; but exfiltration-via-egress and credential theft are squarely OpenShell's claims.
3. **The claude_code adapter and ReAct stages (the interactive-agent case).** The headless `claude -p` stage spike and any long-horizon agent stages are exactly OpenShell's named workload class; running them OpenShell-sandboxed is where "per-request authorization" stops being about the workflow engine and starts being about the agent's own loop — the pi codemode composition (deterministic filtering before the model sees material) at the OS layer instead of the tool layer.

**The "governance principles" answer in one line:** OpenShell is the first substrate we've analyzed where an Armature run's *effects* (network, credentials, files) are authorized per-request from outside the trust boundary, evidenced in a standard schema, and approvable in grants that die with the run — i.e., the P0 table stops being a design and becomes a deployment choice.

## Honest Caveats

- **Maturity mismatch risk:** v0.1.x, weekly releases, experimental interfaces may break in patches. 20.5k stars says interest; 387 open issues and a 0.1.6 version number say early. Do not make it a hard dependency of the dispatch path yet; compose behind the existing Docker sandbox so OpenShell can be unplugged.
- **Container-tier isolation, stated honestly:** Landlock/seccomp are kernel LSM features, not a hypervisor; the docs never claim VM-grade isolation (their MicroVM runtime is the exception). `best_effort` Landlock compat mode "may skip unavailable policy paths" and emits a High-severity finding — a misconfigured host silently degrades the filesystem fence. The governance brief's "container ≠ isolation" non-goal stands.
- **Not an IAM replacement:** S3/IAM-scoped access stays ours to fix; OpenShell governs network reach and credential injection, not cloud resource policies.
- **Linux-kernel-bound:** Linux 6.2+/Landlock ABI 3 required for native enforcement (macOS only via the Docker Desktop VM); ECS Fargate kernel support for Landlock/seccomp-user-notify needs verification before composition #2 is assumed to work.
- **TLS-termination consequence:** placeholder rewriting happens at the supervisor's terminating proxy; `tls: skip` (client-cert mTLS, non-HTTP protocols) disables credential injection — some real provider integrations fall outside the strongest guarantee.
- **Marketing vs mechanism:** nearly everything cited above is specified mechanically in the docs (the security pages are unusually concrete, including their own limits section); the one soft spot is "formally verified policy changes" — the prover verifies *policy containment against a boundary you write*, not end-to-end runtime behavior, and it says so plainly.

## Verdict

**Not a needle-mover for the engine's grammar — a needle-mover for the governance arc's *deployment* story.** Every capability it offers lives on the mechanism side of the boundary rule, which means Armature adopts it rather than reimplements it. Of the whole literature shelf, this is the first item where the right move is not "extract a design pattern" but "run our existing artifacts inside it and inherit the properties." Import the one grammar change that is independently worthwhile (endpoint-scoped `api_key_env` → access contracts for tools), record the advisor/prover flow as the shipped reference for bound approvals, and evaluate composition #1 (package runner in an OpenShell sandbox) as the first live experiment when the governance arc un-parks.

Position on the shelf: **4th statement of credentials-outside-the-execution-boundary** (Octopus, pi/Gondolin, our brief — now with a production-shaped implementation); **2nd shipped existence proof** for the Octopus "formed grants" property (generation-scoped approvals); and the **missing P0 #2/#4 substrate** the governance brief specified but didn't name.
# Workflow Packages

Self-contained, verified workflow bundles a generic `armature-runner` container
can execute anywhere — spec, inputs, vendored tools, a deps manifest, secret
*names*, and output destinations in one directory. Build once, run anywhere,
queue for a pool of worker containers.

---

## Why packages exist

A workflow spec plus its Python tool modules is not enough to move a workflow
between machines. The spec references tool modules by import path; the tool
modules have their own dependencies; the run needs API keys; and the outputs
need somewhere deterministic to land. Reconstructing all of that on a fresh
host — or on a fleet of hosts — is error-prone.

A **workflow package** is the unit of portability. `armature package build`
takes a spec and bundles everything the runner needs into one self-contained
directory, then verifies it is complete. `armature package run` executes that
directory in a generic `armature-runner` container and writes artifacts plus a
run receipt to a results dir. The package carries secret *names*, never values
— so it is safe to log, queue, and ship.

The package is **data, not an image**. One runner image serves every package.
A pool of identical runner containers can pull packages from a queue with no
changes to the package or the runner.

```
            ┌──────────────────────────────────────────────────┐
            │  one generic image: armature-runner              │
            │                                                  │
   queue ──▶│  ┌───┐ ┌───┐ ┌───┐ ┌───┐   (N identical workers) │
  packages  │  │run│ │run│ │run│ │run│  each pulls a package,  │
   (data)   │  └─┬─┘ └─┬─┘ └─┬─┘ └─┬─┘   mounts it read-only,    │
            │    ▼     ▼     ▼     ▼     runs to completion,    │
            │  results/<run_id>/receipt.json + artifacts/       │
            └──────────────────────────────────────────────────┘
```

---

## Features

| Feature | What it gives you |
|---|---|
| **One-directory bundle** | spec + inputs + vendored tools + deps manifest + secret names + destinations + integrity checksums — everything to run, nothing else to fetch |
| **Build-time verification** | Eight completeness checks (V1–V8) abort a bad build before it ships |
| **Integrity guarantee** | `manifest.sha256` (standard `sha256sum` format) over every file; the runner re-verifies on every run and fails closed on tamper, missing, or extra files |
| **Reference-only secrets** | The bundle carries *names* only; values injected at run from the owner's `--profile .env`; fail-closed if any are missing; values never written to package, results, or logs |
| **Generic executor image** | One `armature-runner` image runs every package — the package is data, not an image |
| **Deterministic results dir** | `results/<run_id>/{receipt.json, result.json, artifacts/, trace.jsonl, logs/}` — the same layout every run, every host |
| **Run receipt** | `receipt.json` is the ready-made payload for push sinks (webhook, S3, Slack) — downstream consumers never parse Armature internals |
| **Optional trace** | `--include-trace` writes a full `trace.jsonl` (LLM dialogs) alongside artifacts for replay/debugging |
| **Nested sandbox (DooD)** | When a packaged workflow declares `sandbox.mode: docker`, the launcher mounts the host Docker socket and runs the runner as root so `DockerSandboxProvider` can spawn the workflow's shell/file stages as sibling containers — isolation identical to a non-packaged run. Non-sandbox packages get neither (least privilege) |
| **Four CLI verbs** | `armature package build | run | verify | inspect` — one tool for the whole lifecycle |
| **Pool-ready by construction** | No per-host config, no image per workflow, secret-free bundle, machine-readable receipt — the format was designed for queue + fleet execution, not just single runs |

---

## Use cases

### 1. Move a workflow to a fresh machine
Build the package on your laptop; copy the single directory (or its tar/zip
archive) to a server with Docker; run it there. No need to reproduce the tool
modules, their pip deps, or the spec's model wiring on the target host — the
runner image and the package carry all of it.

### 2. Run the same workflow on many hosts identically
A package is immutable data with an integrity checksum. Every host that runs
it executes the exact same bytes. Diff two runs' `receipt.json` to compare
outputs without trusting the hosts to be configured alike.

### 3. Queue packages for a pool of worker containers
The format is designed for this. Packages are small, secret-free, self-describing
data blobs; a queue of packages can be drained by N identical `armature-runner`
containers. Each worker pulls a package, mounts it read-only, injects that
owner's secrets, runs to completion, posts `receipt.json` to a sink, and ships
the matching `artifacts/` files — then picks up the next package. No changes to
the package or the runner per worker. (See [Future path](#future-path-pool-of-worker-containers).)

### 4. Hand a workflow to an untrusted executor
Because the bundle is secret-free, you can hand a package to a third-party
runner (or store it in version control, or attach it to a ticket) without
leaking credentials. The executor injects values from its own `--profile` at
run; if it lacks a declared secret, the run fails closed.

### 5. CI gate on workflow quality
`armature package build` runs the eight completeness checks and aborts on any
failure. Wire `build` + `verify` into CI to block merges that produce a
non-runnable package (undeclared inputs, dangling artifact refs, missing tool
modules).

### 6. Replay and debug a run
Run with `--include-trace`. `results/<run_id>/trace.jsonl` captures the full
LLM dialog trace; `receipt.json` records status, duration, exit code, and
artifact paths. Reproduce a failure by re-running the same package with the
same inputs.

### 7. Fan out a parameter sweep
Build one package with a runtime input declared; run it N times with different
`--input key=value` overrides. Each run lands in its own
`results/<run_id>/` with its own receipt. The package is built once; only the
input override varies per run.

---

## Package layout

```
<name>.pkg/
  package.yaml          # manifest: api_version, name, version, spec, inputs, requirements,
                        #   requirements_lock, tools_dir, secrets, destinations, runtime_inputs,
                        #   armature_version, created_at, created_by, integrity
  workflow.yaml         # the validated Armature spec
  inputs.yaml           # bundled default runtime inputs (overridable at run)
  requirements.txt      # armature-agents + custom-tool deps
  requirements.lock     # optional pinned hashes
  tools/                # vendored custom tool source
  secrets.yaml          # required secret NAMES (api_key_env list) — NEVER values
  destinations.yaml     # output contract: artifacts[], include_trace, results_layout
  manifest.sha256       # checksums of every file (standard sha256sum format)
  README.md             # what this package does
```

### `package.yaml` — the manifest

| Field | Description |
|---|---|
| `api_version` | Package manifest schema version (`armature.package/v1`) |
| `name`, `version` | From the spec's `name` / `version` |
| `spec` | Path to the bundled spec (default `workflow.yaml`) |
| `inputs` | Path to bundled default inputs (default `inputs.yaml`) |
| `requirements` | Path to `requirements.txt` (or `None`) |
| `requirements_lock` | Optional pinned-hashes lock file |
| `tools_dir` | Vendored tools directory (or `None` if no custom tools) |
| `secrets` | Path to `secrets.yaml` (secret *names*) |
| `destinations` | Path to `destinations.yaml` (output contract) |
| `runtime_inputs` | Input names the caller must supply at run (not bundled) |
| `armature_version` | Armature version constraint |
| `created_at`, `created_by` | Provenance |
| `integrity` | Path to `manifest.sha256` |

### `secrets.yaml` — secret names, never values

```yaml
required:
  - name: ANTHROPIC_API_KEY
  - name: OPENROUTER_API_KEY
```

Auto-generated at build time from every `model_tiers[*].api_key_env` referenced
in the spec. The bundle carries *names* only — values are injected at run from
the owner's `--profile` `.env` file.

### `destinations.yaml` — the output contract

```yaml
artifacts:
  - stage_id: writer
    name: briefing
    format: markdown      # markdown | json | text
include_trace: false      # set true to emit trace.jsonl
results_layout: by_run_id
```

The runner extracts each artifact's content from the matching stage's output and
writes one file per entry to the results dir.

### `inputs.yaml` — bundled defaults

Default values for runtime inputs, supplied at `build` time via
`--input key=value`. Any value here can be overridden at `run` time with
`--input key=value`.

---

## `armature package build`

```bash
armature package build \
  --spec my_workflow.yml \
  --out my_workflow.pkg \
  [--tools ./my_tools]            # directory of custom tool source to vendor
  [--requirements requirements.txt]
  [--destinations destinations.yaml]
  [--runtime-inputs topic,focus]  # input names the caller supplies at run
  [--profile ~/.armature/secrets.env]  # verify secrets resolve against this .env
  [--archive tar]                 # tar | zip — also archive the package
  [--input topic="default value"] # bundled default inputs
```

The builder validates the spec, vendors tools, auto-generates `secrets.yaml`
from the spec's `api_key_env` references, infers a default `destinations.yaml`
from the spec's leaf stages when none is given, writes the manifest, and then
runs the eight completeness checks below. If any check fails the build aborts.

Any `context_layers:` entry with a `src:` file is bundled too, copied into the
package at the same path relative to the spec that the source spec used —
same posture as vendored tools. A `src:` that would escape the package
directory (`../` traversal, an absolute path) fails the build closed
(`SRC_PATH_ESCAPE`) rather than bundling it. Bundled layer files are covered
by `manifest.sha256` like every other file in the package.

### The eight completeness checks

| Check | What it verifies |
|---|---|
| **V1 SPEC_VALID** | The bundled spec loads and passes validation |
| **V2 INPUTS_COMPLETE** | Every declared `contracts.inputs` name is either bundled in `inputs.yaml` or listed in `runtime_inputs` |
| **V3 SECRETS_DECLARED** | Every `api_key_env` in `model_tiers` is declared in `secrets.yaml` (and resolvable against `--profile` if given) |
| **V4 TOOLS_RESOLVABLE** | Every `tools:` module is either vendored under `tools/` or listed in `requirements.txt` |
| **V5 SANDBOX_IMAGE** | If the spec uses `sandbox.mode: docker`, warn if the image isn't local (warn-only — it will pull at run) |
| **V6 ARTIFACTS_VALID** | Every `destinations.artifacts[].stage_id` exists in the spec and produces output |
| **V7 DEPS_RESOLVE** | `requirements.txt` is parseable |
| **V8 INTEGRITY** | `manifest.sha256` is written for every file |

`armature package verify <pkg>` re-runs all eight checks (V1–V8) without
rebuilding — V8 recomputes and rewrites `manifest.sha256`.
`armature package inspect <pkg>` prints the manifest read-only.

> The verifier's `write_integrity` flag controls V8. The builder and
> `armature package verify` use the default (`True`, write). The runner's R2
> re-verify passes `write_integrity=False` so V8 **validates** the existing
> manifest instead of rewriting it — the package mount is read-only in the
> container.

---

## `armature package run`

```bash
armature package run my_workflow.pkg \
  [--results ./results]          # results dir (default ./results)
  [--profile ~/.armature/secrets.env]  # .env with secret values
  [--input topic="override"]      # input overrides (key=value)
  [--include-trace]              # write trace.jsonl alongside results
```

By default `run` launches a generic `armature-runner` container that mounts the
package and a results volume, installs `requirements.txt`, injects secrets from
`--profile`, and executes the workflow. The container is the same regardless of
which package it runs — the package is data.

> `--direct` is an internal in-process mode the container entrypoint uses; users
> run `armature package run <pkg>` and get a container.

### What the runner does (R1–R8)

The in-process core (`PackageRunner`) runs a fixed eight-step sequence; the
container entrypoint calls the same core via `--direct`:

1. **R1 integrity** — re-verify `manifest.sha256`; fail closed on tamper/missing/extra.
2. **R2 re-verify** — re-run the completeness checks with read-only integrity (`write_integrity=False`); V8 validates the manifest rather than rewriting it, since the package is mounted `:ro`.
3. **R3 secrets** — inject values from `--profile` into the environment; compute the missing set *before* injection; fail closed (`SecretMissingError`) if any declared name has no value.
4. **R4 deps** — install `requirements.txt` and put the vendored `tools/` dir on `sys.path` (so tool modules import without being pip-installed).
5. **R5 inputs** — load bundled `inputs.yaml`, apply `--input` overrides.
6. **R6 run** — construct the real `Harness` and run the workflow.
7. **R7 capture** — write `results/<run_id>/{receipt.json, result.json, artifacts/, trace.jsonl}`.
8. **R8 exit code** — `0` on `complete`, non-zero on `failed`.

### Results directory layout

```
results/<run_id>/
  receipt.json       # run receipt (status, run_id, duration, artifacts[], trace, error)
  result.json        # engine's final result dict
  artifacts/         # one file per destinations.artifacts[] entry (md/json/txt)
  trace.jsonl        # if --include-trace: full trace (dialogs); written even if 0 records
  logs/
```

The engine's live session directory (TraceStore DB etc.) is written to
`results/_pending/session/` during execution and is not relocated into the
per-run output dir. `trace.jsonl` is delivered into `<run_id>/` regardless.

### `receipt.json` — the run receipt

```json
{
  "package_name": "topic-researcher",
  "package_version": "1.0",
  "run_id": "a1b2c3d4",
  "status": "complete",
  "started_at": "2026-08-21T12:00:00Z",
  "finished_at": "2026-08-21T12:00:42Z",
  "duration_s": 42.1,
  "exit_code": 0,
  "armature_version": ">=0.6.0",
  "artifacts": [{"name": "briefing", "stage_id": "writer", "format": "markdown",
                 "path": "artifacts/briefing.md"}],
  "trace": {"included": false, "path": null},
  "error": null
}
```

The receipt is the ready-made payload for push sinks — webhook, S3, Slack. A
runner can post `receipt.json` on completion and ship the matching `artifacts/`
files; downstream consumers never need to parse Armature internals.

---

## The secrets model — reference-only, fail-closed

The bundle is **secret-free**. It carries *names* (`ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`) in `secrets.yaml`, never values. This means a package is
safe to log, queue, store in version control, or hand to an untrusted executor.

At run time:

1. The owner provides a `.env` file via `--profile` (gitignored, never committed).
2. The runner injects those values into the container environment.
3. If any declared name has no value at run, the run **fails closed** — it does
   not silently proceed with missing credentials.

```
build time:  spec → secrets.yaml (names only) ──► bundle
run time:    --profile secrets.env (values) ──► injected into container env
             missing value for a declared name ──► fail-closed
```

Secret values are never written to the package, the results dir, the receipt,
the trace, or the logs — only to the running process's environment.

---

## Nested sandbox

When a packaged workflow declares `sandbox.mode: docker`, the runner container
(Docker-in-the-container) mounts the Docker socket from the host
(DooD — Docker-outside-of-Docker). `DockerSandboxProvider` runs the workflow's
shell and file stages as **sibling containers** on the host daemon, not as
nested children. This keeps sandbox isolation identical to a non-packaged run:
resource limits, network isolation, and image-digest tracing all apply, and the
sandbox containers are siblings of the runner, not descendants.

The launcher only mounts the socket and runs the runner as root when the
package's bundled spec declares `sandbox.mode: docker` (it peeks at the spec
before launch). Every other package runs as the image's non-root `armature`
user with no socket — least privilege. Root is required for socket access on
Orbstack (which rejects `--group-add`); on Linux a `docker`-group member would
also work, but root is the portable choice for a trusted runner that
deliberately mounts the socket.

---

## A worked example

Build and run the minimal spec from the README as a package.

**1. The spec** (`topic-researcher.yml`):

```yaml
name: topic-researcher
version: "1.0"
description: "Research a topic and produce a structured briefing."

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
    api_key_env: ANTHROPIC_API_KEY

role_type_defaults:
  worker: small
  researcher: small

contracts:
  inputs:
    - name: topic

stages:
  - id: researcher
    role:
      name: Researcher
      type: researcher
      description: |
        Research the following topic thoroughly.
        Topic: {{ topic }}
    output_mode: text
    depends_on: []

  - id: writer
    role:
      name: Writer
      type: worker
      description: |
        Turn the researcher's notes into a crisp 3-paragraph briefing.
        Notes: {{ researcher.content }}
    output_mode: text
    depends_on: [researcher]
```

**2. Build the package** (with a destinations contract so we get an artifact):

```yaml
# destinations.yaml
artifacts:
  - stage_id: writer
    name: briefing
    format: markdown
include_trace: true
```

```bash
armature package build \
  --spec topic-researcher.yml \
  --out topic-researcher.pkg \
  --destinations destinations.yaml \
  --runtime-inputs topic
```

The builder validates the spec, auto-generates `secrets.yaml` (declaring
`ANTHROPIC_API_KEY`), writes the manifest, and runs all eight checks. On
success:

```
Built package: topic-researcher.pkg
```

**3. Inspect it:**

```bash
armature package inspect topic-researcher.pkg
```

**4. Verify it without running:**

```bash
armature package verify topic-researcher.pkg
```

**5. Run it** (provide the runtime input and a secrets profile):

```bash
armature package run topic-researcher.pkg \
  --profile ~/.armature/secrets.env \
  --input topic="quantum error correction" \
  --include-trace \
  --results ./results
```

**6. Read the results:**

```
results/a1b2c3d4/
  receipt.json           # status, run_id, duration, artifacts[], trace
  result.json            # full engine result dict
  artifacts/
    briefing.md          # the writer stage's output
  trace.jsonl            # full dialog trace
```

---

## Future path: pool of worker containers

The package format is designed for pool execution, not just single runs. The
shape of the system at steady state:

- **A queue of packages** (data blobs) — local disk today, SQS tomorrow. Each
  entry is a package reference plus the owning profile and any input overrides.
- **A pool of N identical `armature-runner` containers** — same image, no
  per-worker config. Each worker pulls a package, mounts it read-only, injects
  the owner's secrets from `--profile`, runs to completion, and posts
  `receipt.json` + ships `artifacts/` to a sink.
- **Push sinks** — `receipt.json` is the ready-made payload for webhook, S3, and
  Slack notifications on completion. Downstream consumers need no Armature
  knowledge; they read the receipt and fetch the named artifact files.

None of this requires changes to the package or the runner. The same package
you build and run locally today is the unit a fleet executes tomorrow. The
first slice (this release) delivers build + run-one-package locally; the pool,
the SQS source, and the sink adapters are the next layer on top of this stable
contract.

---

## Testing

The packaging feature has **35 tests** in `tests/packaging/` (plus the shared
`conftest.py` fixtures) and **3 example packages** in `examples/packages/`.
They run as part of the full repo suite (`python -m pytest tests/`); the whole
suite is green (1870 passed at the time of writing). Three of the tests are
**real-Docker integration tests** that build the `armature-runner` image and
run the example packages in actual containers; they skip automatically when
Docker is not available, so `pytest tests/` stays green on any machine without
Docker. Run them explicitly with `pytest -m docker`.

> **Live local run.** All three example packages were run end-to-end in real
> Orbstack containers (`armature package run <pkg>`), which surfaced a
> host-path bug the unit suite missed: the container-mode CLI passed
> *relative* paths to `docker run -v`, and Docker silently treats a relative
> path matching its volume-name charset as an empty named volume (rejecting
> names that start with `_`, which is how `_inputs-override.yaml` surfaced it).
> The fix absolutizes every host path that reaches a bind-mount and writes the
> override to a unique temp file; a regression test
> (`test_package_run_container_mode_absolutizes_paths`) locks it in.

### Shared fixtures (`conftest.py`)

| Fixture | What it provides |
|---|---|
| `tiny_spec` | A minimal LLM `role:` spec (`echo-demo`, one declared input `topic`, one `writer` stage, `openrouter` small tier with `api_key_env: OPENROUTER_API_KEY`). Used by builder/verifier/runner/CLI tests. |
| `no_llm_pkg` | A no-LLM `tool_call` spec (`echo-tool`) plus a vendored `echo_tool` module that registers a real `READ_ONLY` tool. Returns `(spec_path, tools_dir)`. Used by the e2e test so the run needs no API key and no network. |

### Manifest models — `test_pkg_manifest.py` (3)

| Test | Asserts |
|---|---|
| `test_package_manifest_defaults` | `api_version == "armature.package/v1"`; default paths (`workflow.yaml`, `secrets.yaml`, `destinations.yaml`, `manifest.sha256`); `runtime_inputs == []` |
| `test_secrets_and_destinations_roundtrip` | `SecretsFile`/`Destinations`/`ArtifactSpec` field round-trips |
| `test_results_manifest_serializes` | `ResultsManifest.model_dump_json()` emits compact JSON with `"status":"complete"` and `"trace":{"included":true` (Pydantic v2 compact form) |

### Integrity — `test_integrity.py` (4)

| Test | Asserts |
|---|---|
| `test_write_then_verify` | `write_manifest_sha256` writes `manifest.sha256`; `verify_integrity` returns `True` on a clean tree |
| `test_tamper_fails` | Modifying a file after manifest write → `verify_integrity` is `False` |
| `test_missing_manifest_fails` | No `manifest.sha256` → `False` |
| `test_extra_file_fails` | A file present but not in the manifest → `False` |

### Completeness verifier — `test_verifier.py` (6)

| Test | Asserts |
|---|---|
| `test_verifier_pass` | A well-formed package: `report.ok` is `True`; all eight check names present; V8 wrote `manifest.sha256` |
| `test_v2_missing_input_fails` | An input declared in `contracts.inputs` absent from `inputs.yaml` and `runtime_inputs` → V2 `fail` |
| `test_v3_undeclared_secret_fails` | An `api_key_env` with no entry in `secrets.yaml` → V3 `fail` |
| `test_v3_profile_resolves` | With a matching `profile_env`, V3 `pass` |
| `test_v6_dangling_artifact_fails` | A `destinations.artifacts[].stage_id` that doesn't exist in the spec → V6 `fail` |
| `test_v1_invalid_spec_fails` | A spec missing the required `stages` field → V1 `fail` |

### Builder — `test_builder.py` (4)

| Test | Asserts |
|---|---|
| `test_build_assembles_full_tree` | All eight expected files exist in the output package dir |
| `test_build_auto_generates_secrets` | `secrets.yaml` contains `OPENROUTER_API_KEY` (inferred from the spec's `api_key_env`) |
| `test_build_default_destinations_infers_leaves` | With no `--destinations`, the leaf `writer` stage is inferred as an artifact |
| `test_build_aborts_on_invalid_spec` | A spec missing `stages` raises (build aborts) |

### Results writer — `test_results.py` (3)

| Test | Asserts |
|---|---|
| `test_results_layout_and_receipt` | `receipt.json` + `result.json` + `artifacts/brief.md` written; receipt's artifact path and `trace.included` correct |
| `test_trace_omitted_when_disabled` | `include_trace=False` → no `trace.jsonl`; receipt `trace.included` is `False` |
| `test_json_artifact` | A `format: json` artifact is written as valid JSON with the right content |

### PackageRunner (in-process core) — `test_runner.py` (3)

Uses a `FakeHarness` (no real LLM) so the runner's R1–R8 glue is tested without network:

| Test | Asserts |
|---|---|
| `test_runner_complete` | A clean run → `status == "complete"`, `receipt.json` and `artifacts/writer.md` exist |
| `test_runner_secrets_fail_closed` | `OPENROUTER_API_KEY` unset → `SecretMissingError` raised (fail-closed) |
| `test_runner_input_override` | `inputs_override={"topic":"override"}` reaches the stage output (the artifact contains `"override"`) |

### Docker launcher — `test_docker_runner.py` (2)

Command-construction only (stubs out the subprocess runner — does not shell out):

| Test | Asserts |
|---|---|
| `test_sandbox_pkg_mounts_socket_and_runs_as_root` | A `sandbox.mode: docker` package: `build_command` mounts `/package:ro` + `/results` + the Docker socket, adds `--user 0:0`, and appends only `/package --results /results` + option flags (no doubled `package run --direct` — the image ENTRYPOINT supplies it) |
| `test_non_sandbox_pkg_omits_socket_and_root` | A non-sandbox package: no socket, no `--user 0:0`, no secrets/override flags — least privilege |

### CLI — `test_cli.py` (5)

| Test | Asserts |
|---|---|
| `test_package_help_lists_subcommands` | `armature package --help` lists `build`, `run`, `verify`, `inspect` |
| `test_package_build_via_cli` | `armature package build` via the Typer runner produces a package dir with `package.yaml` |
| `test_package_inspect_via_cli` | `inspect` on a built package prints the spec name (`echo-demo`) |
| `test_package_verify_via_cli` | `verify` on a built package exits 0 |
| `test_package_run_direct_exits_nonzero_on_failure` | A tampered package (`workflow.yaml` byte appended → R1 integrity fails) run via `--direct` exits **non-zero** (R8 contract; regression test for the final-review blocker) |

### End-to-end — `test_e2e.py` (1)

| Test | Asserts |
|---|---|
| `test_build_and_run_direct_no_llm` | Builds the `no_llm_pkg` via `PackageBuilder`, runs it via `PackageRunner(skip_deps_install=True).run_sync(...)` against the **real `Harness`** (not a fake), and asserts `status == "complete"`, `receipt.json` exists, `trace.jsonl` exists, and an artifact with `stage_id == "echo"` was produced. This is the one test that exercises the real engine tool_call path end-to-end — no LLM, no network, no Docker. |

### Docker integration — `test_docker_integration.py` (3)

Real-container tests (marked `docker`; skipped without a Docker daemon). A
module fixture builds the `armature-runner` image from `Dockerfile.runner`
once; each test builds an example package via the CLI and runs it with
`armature package run <pkg>` (the real container path, not `--direct`):

| Test | Asserts |
|---|---|
| `test_echo_tool_runs_in_container` | The `echo-tool` example builds + runs in a container; host `results/<run_id>/` has `receipt.json` (status `complete`), `trace.jsonl`, and `artifacts/echo.md` containing the `--input msg=…` override value. Closes the container round-trip, the container-mode CLI, and the host `--input`→container `--inputs-override` path. |
| `test_tampered_package_exits_nonzero_in_container` | A tampered package (a byte appended to `workflow.yaml` → R1 integrity fails) run in a container exits **non-zero** (R8). Proves the failure path through the real container. |
| `test_sandbox_shell_runs_in_sibling_container` | The `sandbox-shell` example (`sandbox.mode: docker`) runs in a container that mounts the Docker socket as root and spawns an `alpine:3.20` **sibling** container (DooD); the sibling's `echo` stdout is captured into `artifacts/result.json`. Closes the nested-sandbox path. |

These tests surfaced (and fixed) three real bugs the unit suite missed: the
`Dockerfile.runner` image failed to build (missing `README.md` at install time,
and an incomplete wheel from a partial source copy), the launcher duplicated
the image's `package run --direct` ENTRYPOINT, and R2's re-verify tried to
rewrite `manifest.sha256` on the read-only package mount.

### Gaps — what is *not* yet tested

The first slice is now well covered, including the real-Docker round-trip.
These seams remain — each is a candidate for a new test:

**Closed by the Docker integration tests and the live local run:**
- ✅ Real Docker round-trip (build image → run package in container → host results + receipt).
- ✅ Container-mode CLI (`armature package run <pkg>`, not just `--direct`).
- ✅ Host `--input` override reaching a stage inside the container (the `--inputs-override` container flag).
- ✅ Nested sandbox (DooD) — a `sandbox.mode: docker` package spawning a sibling container.
- ✅ Read-only integrity at run time (`write_integrity=False` on the `:ro` package mount).
- ✅ Bind-mount path absolutization — the live run surfaced that relative host
  paths were silently treated as empty named Docker volumes; the CLI now
  absolutizes every host path, with a regression test.
- ✅ Concurrent-run override clobber — overrides now go to a unique temp file
  instead of a shared `_inputs-override.yaml` in the package's parent dir.

**Still open:**
1. **`--secrets` container flag end-to-end.** The echo/sandbox examples have no
   declared secrets, so the container `--secrets /secrets.env` mount is not
   exercised in automation. The live `topic-researcher` run confirmed the
   fail-closed path (missing `ANTHROPIC_API_KEY` → exit 2), but the happy path
   (a profile injecting a real value that reaches the LLM call) needs credits.
2. **Real LLM run.** Every automated runner/CLI/e2e test uses `FakeHarness` or
   a no-LLM `tool_call` spec. No test runs a packaged `role:` workflow against
   a real provider (needs credits + network; the `topic-researcher` example is
   the manual live smoke for this).
3. **V4 TOOLS_RESOLVABLE failure path.** No test asserts that a `tools:` module
   neither vendored nor in `requirements.txt` makes V4 `fail`.
4. **V5 SANDBOX_IMAGE / V7 DEPS_RESOLVE failure paths.** V7's content check is
   effectively a no-op (parse-only); V5 is warn-only. No test exercises either
   as a failing/warning case.
5. **Archive output.** `--archive tar|zip` is implemented but no test asserts
   the archive is produced and is itself runnable.
6. **Partial-dir cleanup on build abort.** When `build` fails a check, the
   partial output dir's cleanup is not asserted.

A good expansion order: add the cheap no-Docker unit tests for gaps 3, 4, 5, 6
first; then a live-LLM integration suite for gaps 1 and 2 (gated on credits +
network, like `topic-researcher`).
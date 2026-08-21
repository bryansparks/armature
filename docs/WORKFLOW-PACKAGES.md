# Workflow Packages

Self-contained, verified workflow bundles a generic `armature-runner` container can execute anywhere — spec, inputs, vendored tools, deps manifest, secret *names*, and output destinations in one directory.

---

A workflow spec plus its Python tool modules is not enough to move a workflow between machines. The spec references tool modules by import path; the tool modules have their own dependencies; the run needs API keys; and the outputs need somewhere deterministic to land. Reconstructing all of that on a fresh host — or on a fleet of hosts — is error-prone.

A **workflow package** is the unit of portability. `armature package build` takes a spec and bundles everything the runner needs into one self-contained directory, then verifies it is complete. `armature package run` executes that directory in a generic `armature-runner` container and writes artifacts plus a run receipt to a results dir. The package carries secret *names*, never values — so it is safe to log, queue, and ship.

The package is data, not an image. One runner image serves every package. A pool of identical runner containers can pull packages from a queue with no changes to the package or the runner.

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

Auto-generated at build time from every `model_tiers[*].api_key_env` referenced in the spec. The bundle carries *names* only — values are injected at run from the owner's `--profile` `.env` file.

### `destinations.yaml` — the output contract

```yaml
artifacts:
  - stage_id: writer
    name: briefing
    format: markdown      # markdown | json | text
include_trace: false      # set true to emit trace.jsonl
results_layout: by_run_id
```

The runner extracts each artifact's content from the matching stage's output and writes one file per entry to the results dir.

### `inputs.yaml` — bundled defaults

Default values for runtime inputs, supplied at `build` time via `--input key=value`. Any value here can be overridden at `run` time with `--input key=value`.

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

The builder validates the spec, vendors tools, auto-generates `secrets.yaml` from the spec's `api_key_env` references, writes the manifest, and then runs the eight completeness checks below. If any check fails the build aborts.

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

`armature package verify <pkg>` re-runs all eight checks (V1–V8) without rebuilding — V8 recomputes and rewrites `manifest.sha256`. `armature package inspect <pkg>` prints the manifest read-only.

---

## `armature package run`

```bash
armature package run my_workflow.pkg \
  [--results ./results]          # results dir (default ./results)
  [--profile ~/.armature/secrets.env]  # .env with secret values
  [--input topic="override"]      # input overrides (key=value)
  [--include-trace]              # write trace.jsonl alongside results
```

By default `run` launches a generic `armature-runner` container that mounts the package and a results volume, installs `requirements.txt`, injects secrets from `--profile`, and executes the workflow. The container is the same regardless of which package it runs — the package is data.

> `--direct` is an internal in-process mode the container entrypoint uses; users run `armature package run <pkg>` and get a container.

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

The receipt is the ready-made payload for push sinks — webhook, S3, Slack. A runner can post `receipt.json` on completion and ship the matching `artifacts/` files; downstream consumers never need to parse Armature internals.

---

## The secrets model — reference-only, fail-closed

The bundle is **secret-free**. It carries *names* (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`) in `secrets.yaml`, never values. This means a package is safe to log, queue, store in version control, or hand to an untrusted executor.

At run time:

1. The owner provides a `.env` file via `--profile` (gitignored, never committed).
2. The runner injects those values into the container environment.
3. If any declared name has no value at run, the run **fails closed** — it does not silently proceed with missing credentials.

```
build time:  spec → secrets.yaml (names only) ──► bundle
run time:    --profile secrets.env (values) ──► injected into container env
             missing value for a declared name ──► fail-closed
```

---

## Nested sandbox

When a packaged workflow declares `sandbox.mode: docker`, the runner container (Docker-in-the-container) mounts the Docker socket from the host (DooD — Docker-outside-of-Docker). `DockerSandboxProvider` runs the workflow's shell and file stages as **sibling containers** on the host daemon, not as nested children. This keeps sandbox isolation identical to a non-packaged run: resource limits, network isolation, and image-digest tracing all apply, and the sandbox containers are siblings of the runner, not descendants.

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

The builder validates the spec, auto-generates `secrets.yaml` (declaring `ANTHROPIC_API_KEY`), writes the manifest, and runs all eight checks. On success:

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

## Future path

The package format is designed for pool execution, not just single runs:

- **Local pool** — a queue of packages on disk with N runner containers draining it.
- **SQS / Fargate** — packages queued on SQS, Fargate tasks pull and run them with the same runner image.
- **Push sinks** — `receipt.json` is the ready-made payload for webhook, S3, and Slack notifications on completion. The runner posts the receipt and ships the matching `artifacts/` files; downstream consumers need no Armature knowledge.

None of this requires changes to the package or the runner. The same package you build and run locally today is the unit a fleet executes tomorrow.
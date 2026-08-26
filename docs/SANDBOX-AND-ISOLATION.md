# Sandbox and Isolation in Armature

Running tools inside ephemeral Docker containers — resource limits, network isolation, per-stage images, and image digest tracing.

---

Every engineer who has deployed an AI agent to production has felt the same unease: the model can call tools, and tools can touch real things. Write a file to the wrong directory. Make an HTTP call that should have been blocked. Execute a shell command that runs with your process's full permissions.

Armature's safety rules (`safety_rules:`) address this at the *policy* level — declarative rules that inspect arguments and block or log calls. But policy has a limit: it reasons about what you told the agent it could do. The sandbox addresses this at the *execution* level — tool calls run inside ephemeral Docker containers with no access to anything outside the declared workspace, with no network unless you explicitly allow it, with CPU and memory bounded to what you specify.

Policy says what is allowed. The sandbox enforces the boundary.

---

## The security posture, in two sentences

When `sandbox.mode: docker` is set:

- Computation happens in ephemeral, resource-constrained, network-isolated containers that disappear after each call
- Files are scoped to an explicit workspace directory; nothing outside it is visible to the container
- Network is off by default; explicitly enabled only when `allow_network: true` is declared in the spec
- Environment is the image you specify, not whatever happens to be installed on the host
- Every execution is traceable by model, inputs, policy version, and image digest

That is a security posture you can describe to a security team in two sentences and defend in a review. The container boundary is the security boundary — an established, well-understood concept that does not require explaining a new abstraction.

---

## Enabling sandbox isolation

Add a `sandbox:` block to the spec. The default is `mode: none` — no change to how tools run. Set `mode: docker` to route all shell, file_write, and file_read calls through Docker:

```yaml
name: document-processor
mission: Process uploaded documents and extract structured data.

sandbox:
  mode: docker
  image: python:3.11-slim
  timeout_s: 60.0
  allow_network: false
  workspace: /workspace
  host_workspace: ./scratch
  env:
    PYTHONPATH: /workspace/lib
  cpu_limit: "1.0"      # --cpus 1.0
  memory_limit: "512m"  # --memory 512m

stages:
  - id: extract
    role:
      type: worker
      model_tier: small
      description: |
        Extract structured data from the uploaded document.
        Document path: {{ document_path }}
        Use the shell tool to run: python /workspace/extract.py {{ document_path }}
        Return the JSON output from stdout.
```

When `extract` calls the `shell` tool, the harness runs:

```
docker run --rm \
  --network none \
  --cpus 1.0 \
  --memory 512m \
  -v /absolute/path/to/scratch:/workspace \
  -e PYTHONPATH=/workspace/lib \
  python:3.11-slim \
  sh -c "python /workspace/extract.py document.pdf"
```

The container is removed when the call completes. The next call starts from a clean container.

---

## The `sandbox:` configuration fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | `none` \| `docker` | `none` | `none` leaves tool handlers untouched. `docker` replaces them with containerized versions. |
| `image` | string | `python:3.11-slim` | The container image for the spec. Overridable per stage (see below). |
| `runtime` | string | `"docker"` | The container CLI binary: `"docker"`, `"podman"`, `"nerdctl"`. See below. |
| `platform` | string \| null | `null` | Forces a specific image platform, e.g. `"linux/amd64"` or `"linux/arm64"`. `null` uses the host's native arch. |
| `timeout_s` | float | `300.0` | Maximum wall-clock time for a single shell execution. Raises `subprocess.TimeoutExpired` on breach. |
| `allow_network` | bool | `false` | `false` adds `--network none` to every container run command. Set `true` only when the workflow must make outbound calls. |
| `workspace` | string | `/workspace` | Path inside the container where the host workspace is mounted. |
| `host_workspace` | string | `.` | Host directory bind-mounted into the container. Relative paths are resolved from the working directory at harness init. |
| `env` | dict | `{}` | Environment variables injected into the container as `-e KEY=VALUE` flags. |
| `cpu_limit` | string \| null | `null` | Passed as `--cpus <value>`. Format: `"1.0"`, `"0.5"`, `"2"`. `null` omits the flag (no CPU cap). |
| `memory_limit` | string \| null | `null` | Passed as `--memory <value>`. Format: `"512m"`, `"1g"`, `"2048m"`. `null` omits the flag (no memory cap). |

### Resource limits

`cpu_limit` and `memory_limit` bind the container to explicit resource ceilings. This matters for two reasons:

**Predictable cost.** An LLM-generated shell command that spawns many processes or allocates large buffers cannot consume unbounded host resources. You know the worst-case resource usage before you deploy.

**Multi-workflow isolation.** On a host running several concurrent Armature workflows, each sandboxed stage is bounded. One workflow with a CPU-intensive stage cannot starve another.

```yaml
sandbox:
  mode: docker
  cpu_limit: "0.5"   # this stage gets at most half a core
  memory_limit: "256m"
```

### Container runtime

`runtime` controls which CLI binary Armature calls. The default is `"docker"`, which works with:

- **Docker Desktop** — the standard Docker install on Mac and Windows
- **OrbStack** — the lightweight Mac alternative to Docker Desktop; installs a `docker` CLI shim that is 100% command-compatible. OrbStack works with the default `runtime: "docker"` setting out of the box.
- **Podman with `podman-docker`** — the `podman-docker` compatibility package installs a `docker` alias. Works with the default setting.
- **Rancher Desktop** — installs a `docker` CLI that proxies to its container engine. Works with the default.

To use a runtime without a Docker-compatible shim, set `runtime` explicitly:

```yaml
sandbox:
  mode: docker
  runtime: podman    # call 'podman run' instead of 'docker run'
```

```yaml
sandbox:
  mode: docker
  runtime: nerdctl   # containerd via nerdctl
```

The `runtime` value is the binary name passed directly to `subprocess`. Any OCI-compatible CLI that accepts `run --rm`, `--network none`, `--cpus`, `--memory`, `-v`, and `-e` flags will work. The `runtime` binary is also used for `inspect` at harness startup to capture the image digest.

### Platform / architecture

`platform` passes `--platform <value>` to the container run command, forcing a specific image architecture regardless of the host's native arch.

**You usually do not need this.** Public images from Docker Hub (`python:3.11-slim`, `ubuntu:22.04`, `node:20-slim`, etc.) are published as multi-arch manifests. When you run them on an ARM Mac, the runtime pulls the `linux/arm64` variant automatically. When the same spec runs on an x86 Linux server, it pulls `linux/amd64`. The spec doesn't change; the runtime resolves the right variant for the host.

Use `platform` when you need to force a specific architecture — most commonly to validate that your workflow produces the same results on your production target:

```yaml
sandbox:
  mode: docker
  image: python:3.11-slim
  platform: linux/amd64   # force x86 even on an ARM Mac
```

**Cross-platform custom images.** If you build your own images, use `docker buildx` to produce multi-arch images and push them to your registry:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag registry.internal.company.com/data-tools:v1.2.3 \
  --push \
  .
```

The spec then references the image tag without a `platform` override — the runtime picks the right variant on each host. Only set `platform` in the spec when you have a specific reason to override the host's native resolution.

### Network isolation

`allow_network: false` (the default) adds `--network none` to every docker run. The container cannot make any network calls — not to external services, not to localhost, not to the Docker host. It can only read and write files in the bind-mounted workspace.

Set `allow_network: true` only for stages that genuinely need outbound access:

```yaml
sandbox:
  mode: docker
  allow_network: true   # the container can make HTTP calls
```

For workflows that need some stages to access the network and others not to, use per-stage image overrides (see below) in combination with safety rules — safety rules inspect tool arguments before the call; sandbox isolation enforces the boundary at execution.

---

## Per-stage image override

Different stages often need different execution environments. A data extraction stage might use `python:3.11-slim`. A transformation stage that relies on `jq` might use `ubuntu:22.04`. A code execution stage might need `node:20-slim`.

The spec-level `sandbox.image` sets the default. Each stage can override it with `sandbox_image`:

```yaml
name: multi-env-pipeline
mission: Extract, transform, and render pipeline output.

sandbox:
  mode: docker
  image: python:3.11-slim   # default for all stages
  allow_network: false

stages:
  - id: extract
    role:
      type: worker
      description: "Extract structured data using Python libraries."
    # uses sandbox.image = python:3.11-slim

  - id: transform
    sandbox_image: ubuntu:22.04    # this stage only
    role:
      type: worker
      description: "Transform using jq and shell utilities."
    depends_on: [extract]

  - id: render
    sandbox_image: node:20-slim    # different image again
    role:
      type: worker
      description: "Render output using a Node.js template engine."
    depends_on: [transform]
```

The `sandbox_image` override is per-stage and applies only to that stage's shell calls. File reads and writes always go directly to `host_workspace` regardless of image (they are host-side operations, not container operations — no image needed). The override resets automatically after the stage completes; subsequent stages return to `sandbox.image`.

---

## Image digest tracing

When `sandbox.mode: docker`, the harness runs `docker inspect` at startup to capture the image's content digest:

```
sha256:a1b2c3d4e5f6... (the full image content hash)
```

This digest is stored on every `TraceRecord` produced during the run as `sandbox_image_digest`. You can query it from the trace store:

```python
from armature.state.traces import TraceStore

store = TraceStore("~/.armature/traces.db")
traces = await store.query(workflow_name="document-processor")

for t in traces:
    print(f"{t.stage_id}: image={t.sandbox_image_digest[:12]}")
```

### Why this matters

A Docker image tag (`python:3.11-slim`) is mutable — the same tag can point to different content on different days. The digest is immutable. Two runs with the same tag may have used different actual images if the tag was updated between them.

Recording the digest means:
- **Reproducibility audit**: you can prove exactly which image content ran at any point in time
- **Regression detection**: if behavior changes between runs, check whether the digest changed — a tag update is often the cause
- **Compliance evidence**: regulated environments often require proof that specific, known software versions processed sensitive data
- **Incident response**: when a run produces unexpected output, the digest tells you whether the execution environment changed

The digest is `null` in three cases: `sandbox.mode: none` (no Docker), `docker inspect` fails (Docker not installed, image not pulled), or the image has no local manifest (network-only pull required). All three are safe — the run proceeds, tracing simply records no digest.

---

## How the sandbox interacts with safety rules

The sandbox and safety rules are complementary controls, not alternatives.

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| Safety rules | Inspect tool arguments before dispatch | What the agent is *allowed* to request |
| Sandbox | Constrain execution environment | What the container is *capable* of doing |

A safety rule can block `shell` calls that include `rm -rf`. The sandbox can prevent the container from accessing anything outside the mounted workspace even if the shell call runs. Defense in depth: policy at the rule layer, enforcement at the execution layer.

Both layers are visible in the spec:

```yaml
safety_mode: strict
safety_rules:
  - tool: shell
    condition: {field: cmd, op: not_contains, value: "rm -rf"}
    action: allow

sandbox:
  mode: docker
  image: python:3.11-slim
  allow_network: false
  cpu_limit: "1.0"
  memory_limit: "512m"
```

A security reviewer can see both layers in the same file: what the policy permits, and what the container is physically capable of. Neither layer requires understanding the LLM.

---

## The audit trail for a sandboxed run

A production run with `mode: docker` produces a trace record for every stage containing:

| Field | Value |
|-------|-------|
| `stage_id` | Which stage ran |
| `model` | Which LLM generated the tool call |
| `inputs` | The arguments passed to the tool |
| `policy_version` | Hash of the safety rules at execution time |
| `sandbox_image_digest` | SHA256 of the Docker image that executed the call |
| `success` | Whether execution completed without error |
| `latency_ms` | Container startup + execution time |

Combined, these fields answer the question: *"For this tool call, which model made this request, what arguments did it supply, which safety policy was in force, which Docker image ran it, and did it succeed?"*

That is a complete audit record for a tool execution. No additional instrumentation required.

---

## Practical deployment notes

### Pre-pull images at startup

Docker container startup is fast for pre-pulled images (~100–300ms overhead) and slow for images that must be pulled from the registry (~seconds to minutes). Pull your images before running workflows in production:

```bash
docker pull python:3.11-slim
docker pull ubuntu:22.04
docker pull node:20-slim
```

If an image is not available locally and `allow_network: false`, the container will fail to start. Pull on deployment; do not rely on pull-on-demand.

### Using a private registry

Set `image` to a fully qualified registry path. The harness calls `docker run` directly — standard Docker authentication applies. Log in with `docker login` before running workflows that use private images:

```yaml
sandbox:
  image: registry.internal.company.com/data-tools:v1.2.3
```

### The workspace bind mount

`host_workspace` is the only directory visible to the container. Everything outside it is inaccessible. This is the primary filesystem isolation mechanism.

Set it to a dedicated scratch directory, not your project root:

```yaml
sandbox:
  host_workspace: ./sandbox_workspace  # not "." or ".."
```

The harness resolves the path to absolute at init time and mounts it as `-v /absolute/path:/workspace`. Files written by the container appear in this directory on the host; the host's other directories are invisible to the container.

### File operations bypass the container

`file_write` and `file_read` tool handlers write and read directly on the host filesystem (within `host_workspace`) rather than invoking Docker. This is intentional: it avoids spawning an extra container for simple I/O, which would add 100–300ms per file operation. The security boundary is the `host_workspace` directory — the handlers refuse paths that escape it.

### Running sandboxed workflows as packages (DooD)

When a workflow is shipped as a **workflow package** and run in the `armature-runner` container (`armature package run <pkg>`), a `sandbox.mode: docker` spec runs its shell/file stages as *sibling* containers on the **host** Docker daemon — Docker-outside-of-Docker. The runner container mounts the host Docker socket and runs as root *only* for packages that declare the docker sandbox (every other package runs without the socket and without root, least-privilege). The sandbox containers are siblings of the runner, not nested inside it, so the same image, network, and resource limits above apply on the host daemon. This is the deployment shape for a pool of worker containers. See `docs/WORKFLOW-PACKAGES.md`.

---

## What the sandbox does not cover

The sandbox governs tool execution. It does not:

- **Constrain LLM API calls** — model calls go to the provider API directly; use safety rules and model tiers to control provider selection and cost
- **Isolate inter-stage data** — the context dict flows through all stages; use `isolated: true` + `signature.input` to scope what each stage sees (see `CONTEXT-ISOLATION.md`)
- **Encrypt workspace files** — files written to `host_workspace` are plaintext on the host; use host-level encryption or a mounted encrypted volume if required
- **Sandbox subagent specs** — child workflow specs run in separate harness instances; configure `sandbox:` in each child spec independently

---

## A complete sandboxed workflow

```yaml
name: code-runner
version: "1.0"
mission: Execute user-submitted code in an isolated environment and return results.

sandbox:
  mode: docker
  image: python:3.11-slim
  timeout_s: 30.0
  allow_network: false
  workspace: /workspace
  host_workspace: ./code_sandbox
  cpu_limit: "0.5"
  memory_limit: "256m"

safety_mode: strict
safety_rules:
  - tool: shell
    condition: {field: cmd, op: starts_with, value: "python /workspace/"}
    action: allow

stages:
  - id: run_code
    role:
      name: CodeExecutor
      type: worker
      model_tier: small
      description: |
        Execute the provided Python code safely.
        Code has been written to /workspace/solution.py by the caller.
        Run: python /workspace/solution.py
        Capture stdout and stderr. Return:
        {"stdout": "...", "stderr": "...", "exit_code": 0}
    
  - id: evaluate
    role:
      name: ResultJudge
      type: judge
      model_tier: frontier
      description: |
        Evaluate whether the code execution produced the expected result.
        Execution output: {{ run_code }}
        Expected output: {{ expected_output }}
        Return {"correct": true|false, "score": 0.0-1.0, "explanation": "..."}.
    depends_on: [run_code]
```

This workflow:
- Runs user-submitted Python in a container with no network, no access to the host, bounded to half a core and 256MB
- Uses strict mode so only explicitly permitted shell patterns can execute
- Records the image digest on every trace for audit

The security team's question — "what can this agent touch on our infrastructure?" — has a clean answer: whatever is in `./code_sandbox`, and nothing else.

---

*The sandbox is the execution-layer complement to safety rules. Policy defines what is allowed; the container enforces what is possible. Together they give agentic workflows a security posture that enterprise infrastructure teams recognize and can reason about.*

# sandbox-shell example package

A no-LLM package that exercises the **nested sandbox** (DooD) path. The single
stage runs `echo hello-from-sandbox` via the built-in `shell` tool — but
because the spec declares `sandbox.mode: docker`, that command does **not** run
in the runner container. The `DockerSandboxProvider` rewrites the shell handler
so the command runs inside an ephemeral **sibling** container on the host
daemon, spawned through the Docker socket the runner container mounts.

```
host docker daemon
   ├── armature-runner container  (runs the package, has docker-cli + socket)
   │      └─ issues: docker run --rm --network none alpine:3.20 sh -c "echo ..."
   └── alpine:3.20 sibling container  (executes the echo, exits, is removed)
```

This keeps sandbox isolation identical to a non-packaged run: resource limits,
network isolation, and image tracing all apply, and the sandbox container is a
sibling of the runner, not a descendant.

## Build

```bash
armature package build \
  --spec examples/packages/sandbox-shell/workflow.yaml \
  --out sandbox-shell.pkg \
  --destinations examples/packages/sandbox-shell/destinations.yaml
```

No `--tools` (uses the built-in `shell` tool), no `--runtime-inputs` (no
inputs), no `--profile` (no secrets).

## Run (in a container)

```bash
armature package run sandbox-shell.pkg --results ./results
```

```
results/<run_id>/
  receipt.json
  result.json
  artifacts/
    result.json       # {"stdout": "hello-from-sandbox\n", "stderr": "", "exit_code": 0}
  trace.jsonl
```

## DooD bind-mount note

The sibling container bind-mounts `sandbox.host_workspace` (here
`/tmp/armature-sandbox-demo`) at `/workspace`. Because the Docker socket routes
to the **host** daemon, the bind source is resolved on the host, not inside the
runner container — a standard DooD consideration. This demo runs only `echo`,
which never touches `/workspace`, so it is unaffected. A package whose sandbox
command reads or writes files must point `host_workspace` at a path the host
daemon can see and that the sibling should mount.
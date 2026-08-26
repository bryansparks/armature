# echo-tool example package

The smallest no-LLM workflow package. A single `tool_call` stage echoes a
runtime-supplied message through a vendored tool. No API key, no network, no
LLM — so it runs anywhere a container can run, deterministically.

It is the canonical container smoke test: build it, run it in an
`armature-runner` container, and check the host results dir for a receipt and
an artifact.

## Build

```bash
armature package build \
  --spec examples/packages/echo-tool/workflow.yaml \
  --out echo-tool.pkg \
  --tools examples/packages/echo-tool/tools \
  --runtime-inputs msg \
  --input msg="hello-default"
```

`msg` is declared in `contracts.inputs` and listed as a runtime input, so it
can be overridden at run time. The bundled default (`hello-default`) is only a
fallback.

## Run (in a container)

```bash
armature package run echo-tool.pkg \
  --input msg="hello-via-container" \
  --include-trace \
  --results ./results
```

This launches an `armature-runner` container, mounts the package read-only,
runs the workflow, and writes results to the host:

```
results/<run_id>/
  receipt.json        # status, run_id, duration, artifacts[]
  result.json         # engine result dict
  artifacts/
    echo.md           # "hello-via-container"
  trace.jsonl         # full trace (--include-trace)
```

## Run in-process (no Docker)

```bash
armature package run echo-tool.pkg --direct --input msg="hello-direct" --results ./results
```

`--direct` is the internal in-process path the container entrypoint uses; handy
for local debugging without a container.
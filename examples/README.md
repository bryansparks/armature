# Example workflow packages

Each subdirectory is the **source** for a workflow package — a spec, optionally
vendored tools, a destinations contract, and a README. Build one with
`armature package build`, then run it with `armature package run`. See
`docs/WORKFLOW-PACKAGES.md` for the full feature reference.

| Package | LLM? | Secrets? | Sandbox? | What it demonstrates |
|---|---|---|---|---|
| [`echo-tool`](packages/echo-tool) | no | no | no | Smallest no-LLM container smoke; runtime input + vendored tool + artifact delivery |
| [`topic-researcher`](packages/topic-researcher) | yes | `ANTHROPIC_API_KEY` | no | Real LLM pipeline; reference-only secrets + multi-stage context flow + live smoke |
| [`sandbox-shell`](packages/sandbox-shell) | no | no | docker (DooD) | Nested sandbox; runner spawns an isolated sibling container via the host socket |

## Quick start

```bash
# Build + run the no-LLM smoke in a container:
armature package build \
  --spec examples/packages/echo-tool/workflow.yaml \
  --out echo-tool.pkg \
  --tools examples/packages/echo-tool/tools \
  --runtime-inputs msg \
  --input msg="hello-default"

armature package run echo-tool.pkg --input msg="hello-via-container" --results ./results
```

## Automated tests

`tests/packaging/test_docker_integration.py` builds the `armature-runner` image
and runs `echo-tool` and `sandbox-shell` in real containers. The tests skip
automatically when Docker is not available, so `pytest tests/` stays green on
any machine without Docker. Run them explicitly:

```bash
pytest tests/packaging/test_docker_integration.py -v
# or by marker:
pytest -m docker
```

`topic-researcher` needs an API key and is not in the automated suite — run it
manually as a live LLM smoke.
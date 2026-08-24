# topic-researcher example package

A real LLM workflow package: a researcher stage gathers notes on a topic, a
writer stage turns them into a 3-paragraph briefing. Unlike `echo-tool`, this
one calls an LLM, so it needs an API key at run time.

This is the example to reach for when you want to prove the full packaged-LLM
path end to end — secrets injection, multi-stage context flow, and a markdown
artifact delivered to the host results dir.

## Secrets

The package bundles the secret **name** (`ANTHROPIC_API_KEY`) only — never a
value. At run time the owner supplies values via `--profile`:

```bash
cp examples/packages/topic-researcher/secrets.env.example ~/.armature/secrets.env
# edit ~/.armature/secrets.env and paste your real key
```

Keep `~/.armature/secrets.env` gitignored. If the key is missing at run, the
run **fails closed** rather than proceeding without credentials.

## Build

```bash
armature package build \
  --spec examples/packages/topic-researcher/workflow.yaml \
  --out topic-researcher.pkg \
  --destinations examples/packages/topic-researcher/destinations.yaml \
  --runtime-inputs topic \
  --profile ~/.armature/secrets.env
```

Passing `--profile` at build time lets V3 (SECRETS_DECLARED) verify the declared
name resolves against your profile, so a bad build fails early.

## Run (in a container)

```bash
armature package run topic-researcher.pkg \
  --profile ~/.armature/secrets.env \
  --input topic="quantum error correction" \
  --include-trace \
  --results ./results
```

```
results/<run_id>/
  receipt.json          # status, run_id, duration, artifacts[]
  result.json           # engine result dict
  artifacts/
    briefing.md         # the writer stage's briefing
  trace.jsonl           # full LLM dialog trace
```

> This example is **not** covered by the automated test suite — it needs an
> API key and network access. Run it manually as a live smoke.
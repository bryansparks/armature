# Armature — Long-Horizon & Trigger Architecture

*Session: 2026-06-03. Pick up from here tomorrow.*

---

## What We Built Tonight (Context)

Working through the `launchpad` Armature workflow revealed how scriptable and observable
the platform already is. Key additions made during this session:

- `armature report --workflow <name>` — resolve most recent run by workflow name
- `armature report --output-file <name>.md/.html` — Rich-rendered file export
- `armature dashboard --workflow` — tool metrics, local timezone, integer tool counts
- `TraceStore.latest_run_id(workflow_name)` — new convenience query
- `armature/report/run_report.py` — new Rich-based single-run report renderer

---

## The Vision: Scriptable, Orchestratable Workflows

The realization from tonight: Armature is becoming a platform that a higher-level
orchestrator can sit on top of. A human (or another system) clicks a button, an Armature
workflow runs in the background, and results are interrogated via the report/dashboard
commands. Armature-on-Armature is a natural next step.

---

## Trigger Architecture

### What Already Works Today

`armature serve` exposes a full async HTTP + SSE interface that any external trigger
can call without Armature needing to know about it:

```
POST /run/async          # submit a workflow run, returns {"job_id": "..."}
GET  /run/{job_id}/events  # SSE stream: stage_start, token, stage_complete, run_complete
GET  /run/{job_id}/result  # poll for completion, returns full result dict
```

Any trigger — cron job, Telegram bot, file watcher, GitHub webhook, Zapier — just POSTs
to `/run/async` with the workflow inputs and gets a job ID back. The SSE endpoint lets a
UI stream progress in real time without polling.

**This already works. No new code needed for basic triggered execution.**

### Adding Native Trigger Support (Future)

For Armature to *own* the trigger lifecycle, a `triggers:` block in the spec YAML:

```yaml
name: daily-digest
version: "1.0"

triggers:
  - type: cron
    schedule: "0 9 * * *"          # daily 9am
  - type: webhook
    path: /webhook/daily-digest    # POST to this path triggers a run
  - type: file_watch
    path: ~/inbox/*.json           # new file → run, file path injected as input
  - type: telegram
    bot_token_env: TELEGRAM_BOT_TOKEN   # message text injected as input

stages:
  ...
```

`armature serve` would register these at startup. A lighter `armature watch` CLI command
(no HTTP server) would handle local/daemon use cases.

---

## Long-Horizon State (The Key Missing Piece)

Today each Armature run is **stateless relative to prior runs**. Each activation starts
fresh. For long-horizon agentic teams the workflow needs to "remember" what it did before.

### The Pattern: Rolling Continuation Context

```
Trigger fires
    ↓
Query TraceStore.latest_run_id(workflow_name)   # find the previous run
    ↓
Extract "carry-forward" outputs from that run   # curated key outputs
    ↓
Inject as prior_run context into harness.run()  # agents see prior work
    ↓
Run workflow                                    # agents reason about continuity
    ↓
Store traces                                    # becomes next activation's "prior_run"
```

Each stage that wants continuity declares it in `signature.input`:

```yaml
stages:
  - id: monitor
    signature:
      input:
        prior_run: "summary and findings from the previous monitoring cycle"
        trigger_payload: "the event that woke this workflow"
    role:
      type: researcher
      prompt: |
        Review what was found last time (prior_run) and what changed
        since then (trigger_payload). Focus your research on new developments.
```

### The Spec Change: `continuation:` Block

```yaml
name: market-monitor
version: "1.0"

continuation:
  carry_forward:
    - key: monitor.summary        # stage_id.output_key
    - key: monitor.alerts_sent
    - key: analyst.recommendations
  inject_as: prior_run            # available to all stages as this context key

triggers:
  - type: cron
    schedule: "0 8 * * 1-5"      # weekdays at 8am

stages:
  - id: monitor
    ...
```

The engine would:
1. Before each run, look up the previous run's traces
2. Extract the `carry_forward` keys from their outputs
3. Inject them as `prior_run` in the context passed to `harness.run()`

### Implementation Path (When Ready)

1. Add `continuation: ContinuationConfig | None = None` to `WorkflowSpec` model
2. Add `_load_prior_context(workflow_name) -> dict` to `Harness` — queries `TraceStore`
3. Merge prior context into `harness.run(inputs)` before execution
4. Wire trigger dispatch in `armature serve` startup

---

## Practical Trigger Examples

### Telegram Bot → Armature Workflow

```python
# thin shim — knows nothing about Armature internals
async def on_telegram_message(msg):
    response = await httpx.post("http://localhost:8000/run/async", json={
        "workflow": "assistant",
        "inputs": {"user_message": msg.text, "user_id": msg.from_user.id}
    })
    job_id = response.json()["job_id"]
    # stream SSE back to Telegram as the workflow runs
    async for event in sse_stream(f"/run/{job_id}/events"):
        if event["type"] == "response_stage_complete":
            await bot.send_message(msg.chat.id, event["content"])
```

### File Drop → Processing Workflow

```bash
# launchd / systemd file watcher
fswatch ~/inbox/ | while read path; do
    curl -X POST http://localhost:8000/run/async \
         -d "{\"workflow\": \"doc-processor\", \"inputs\": {\"file_path\": \"$path\"}}"
done
```

### Cron → Daily Monitoring Workflow

```bash
# crontab
0 8 * * 1-5  curl -s -X POST http://localhost:8000/run/async \
             -d '{"workflow": "market-monitor"}' \
             | jq -r '.job_id' \
             >> ~/.armature/scheduled-jobs.log
```

---

## The Orchestrator-on-Armature Idea

A higher-level orchestrator that *itself runs as an Armature workflow* can:
- Decide which sub-workflows to trigger based on conditions
- Interrogate sub-workflow results via `TraceStore` directly
- Produce a meta-report synthesizing results across multiple workflows

This is Armature-on-Armature: the orchestrator spec has an `orchestrator` role stage
that reads from `TraceStore` and decides what to run next.

---

## Next Steps (Priority Order)

1. **`continuation:` spec field** — most leverage; enables true long-horizon memory
2. **`triggers:` spec field + `armature serve` registration** — native trigger dispatch
3. **`armature watch` command** — lightweight daemon for local/no-server use
4. **Telegram adapter** — concrete first trigger integration (high wow factor)
5. **Orchestrator workflow pattern** — a launchpad-style example of Armature-on-Armature

---

*Pick up at step 1. The `continuation:` block in `WorkflowSpec` (spec/models.py) and
`_load_prior_context()` in Harness (runtime/engine.py) are the two entry points.*

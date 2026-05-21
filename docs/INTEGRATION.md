# Armature Integration Guide

## When to Use Armature

Armature is a **batch-oriented agentic harness**. It runs a declarative YAML
workflow from start to finish, producing a structured result. This makes it
well-suited for:

- Multi-stage research, analysis, and synthesis pipelines
- Code review, PR analysis, content generation
- Data extraction and transformation workflows
- Any task that has a clear start and end with defined outputs

**Armature is not designed for:**

- Conversational loops where each turn waits for the next user message
- Real-time streaming with sub-second token delivery to users
- Stateful sessions where a single "agent" maintains memory across many turns

If you are building a chatbot, Armature can power the heavy-lifting **inside**
each turn — not the conversational loop itself.

---

## LangGraph Sidecar Pattern

The recommended pattern for chatbot-style applications is to use LangGraph
(or any state machine) for the conversation loop, and call Armature as an
**async sidecar** when a user request requires deep, multi-stage work.

```
User
 │
 ▼
LangGraph turn loop
 ├─ classify intent
 ├─ [chitchat]  ──→  fast LLM response (100ms)
 └─ [research]  ──→  POST /run/async → Armature sidecar
                         ├─ gather stage
                         ├─ assess stage
                         └─ synthesize stage
                      ← result dict
                     compose response → user
```

### Why This Works

- LangGraph owns the conversation state and routing logic
- Armature owns multi-stage orchestration, parallelism, and structured outputs
- The two systems are loosely coupled via HTTP — no shared process, no import

### Latency Acknowledgement

Because Armature workflows take seconds to minutes, users would see a blank
screen without feedback. The solution is an **immediate acknowledgement token**:

```python
# In the SSE streaming endpoint — BEFORE awaiting graph.ainvoke()
yield f"data: {json.dumps({'type': 'status', 'text': 'Researching...'})}\n\n"

# Then run the actual workflow
state = await graph.ainvoke(...)
```

The user sees "Researching..." within milliseconds. The full response arrives
after Armature completes. This pattern works for any slow async operation.

---

## Async Service Endpoints

Armature's HTTP service exposes both synchronous and asynchronous endpoints.

### Synchronous (simple use cases)

```
POST /run
  Body: { spec_path, inputs, session_dir? }
  Returns: { run_id, status: "complete", result }
```

Blocks until the workflow finishes. Fine for server-side batch jobs; not
appropriate for user-facing HTTP requests that might timeout.

### Asynchronous (production chatbots)

**Step 1 — Submit the job:**
```
POST /run/async
  Body: { spec_path, inputs, session_dir? }
  Returns 202: { job_id, status: "pending" }
```

**Step 2a — Poll for completion:**
```
GET /run/{job_id}
  Returns: { job_id, status, run_id, result, error }
```

Poll until `status` is `"complete"` or `"failed"`.

**Step 2b — Stream events (optional):**
```
GET /run/{job_id}/events
  Content-Type: text/event-stream

  data: {"type": "stage_start",    "stage_id": "gather"}
  data: {"type": "stage_complete", "stage_id": "gather"}
  data: {"type": "stage_start",    "stage_id": "assess"}
  data: {"type": "stage_complete", "stage_id": "assess"}
  data: {"type": "run_complete",   "run_id": "abc12345"}
```

The SSE stream closes automatically after `run_complete`.

---

## Template: LangGraph + Armature Sidecar

A working template lives at `templates/langgraph-sidecar/`. It demonstrates
all of the patterns described above.

```
templates/langgraph-sidecar/
├── docker-compose.yml          # two services: bot (8000) + armature (8100)
├── .env.example
├── bot/
│   ├── app.py                  # FastAPI with /chat and /chat/stream endpoints
│   ├── graph.py                # LangGraph: classify → research|chitchat
│   ├── nodes.py                # classify_node, research_node, chitchat_node
│   ├── armature_client.py      # run_workflow() and stream_workflow_events()
│   ├── state.py                # ChatState TypedDict
│   └── requirements.txt
└── workflows/
    └── research.yml            # 3-stage: gather → assess → synthesize
```

### Quick Start

```bash
cp .env.example .env
# Set ANTHROPIC_API_KEY in .env

docker compose up
# Bot at http://localhost:8000
# Armature at http://localhost:8100
```

### Chat endpoint (sync)
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "s1", "message": "What is retrieval-augmented generation?"}'
```

### Streaming endpoint
```bash
curl -N "http://localhost:8000/chat/stream?session_id=s1&message=Explain+LLM+agents"
```

---

## Armature Client (Python)

For direct Python use without Docker:

```python
from armature_client import run_workflow, stream_workflow_events

# Blocking poll
result = await run_workflow(
    spec_path="/path/to/workflow.yml",
    inputs={"query": "your question"},
)
synthesis = result["synthesize"]["content"]

# SSE streaming
async for event in stream_workflow_events(spec_path, inputs):
    if event["type"] == "stage_start":
        print(f"Starting: {event['stage_id']}")
    elif event["type"] == "run_complete":
        print("Done")
```

---

## Configuring the Armature URL

The client reads `ARMATURE_URL` from the environment (default:
`http://localhost:8100`). Set this in your `.env` or deployment config:

```
ARMATURE_URL=http://armature:8100   # inside docker-compose network
ARMATURE_URL=http://10.0.1.5:8100  # remote instance
```

---

## Positioning Summary

| Capability | LangGraph | Armature |
|---|---|---|
| Conversational state | ✓ | — |
| Multi-turn routing | ✓ | — |
| Parallel multi-stage work | — | ✓ |
| Structured JSON outputs | — | ✓ |
| Declarative YAML workflows | — | ✓ |
| Skill injection | — | ✓ |
| Fan-out / map-reduce | — | ✓ |
| Safety rules | — | ✓ |
| Per-run trace storage | — | ✓ |

Use LangGraph for the conversation. Use Armature for the work.

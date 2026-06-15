# Armature for Chat and Streaming Applications

Using Armature as a structured reasoning backend for chat interfaces, voice agents, and real-time applications.

---

Armature is not a conversational loop engine — it does not natively implement the open-ended `think → respond → think` cycle of a general-purpose chatbot. What it does instead is something more useful for production: **structured, auditable reasoning** delivered through a streaming HTTP interface. For applications that need a chat frontend over complex multi-step AI reasoning, Armature is the right backend.

This document describes the sidecar architecture: a chat frontend that calls Armature over HTTP, receives streaming token output, and presents the result to the user in real time.

---

## The sidecar model

In the sidecar architecture, Armature runs as a lightweight service alongside your application. The application handles the user interface — web, mobile, voice, Slack bot, whatever. When a user submits a request that requires structured AI reasoning, the application calls Armature and streams the response back.

```
User ──► Application Frontend
              │
              │ POST /workflows/answer-query/run/async
              ▼
         Armature Service
              │
              ├── Stage 1: classify intent (small model, ~100ms)
              ├── Stage 2: retrieve relevant context (researcher)
              ├── Stage 3: draft response (worker)
              └── Stage 4: validate for safety/tone (judge)
              │
              │ GET /run/{job_id}/events (SSE stream)
              ▼
         Application Frontend
              │
              ▼
            User (sees tokens as they arrive)
```

The application is not an AI system. The AI system is Armature. The application is a thin HTTP client that delegates reasoning to the harness and streams the result back to the user.

---

## Enabling token streaming

Mark any stage with `response_stage: true`. That stage's LLM output is streamed token by token to the job's event queue as it is generated, rather than being buffered until completion:

```yaml
name: support-assistant
mission: Answer customer support questions accurately, concisely, and helpfully.

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: classify
    role:
      name: IntentClassifier
      type: worker
      model_tier: small
      description: |
        Classify this support query into one of: billing, technical, account, general.
        Query: {{ user_query }}
        Return {"intent": "...", "complexity": "low|medium|high"}.

  - id: retrieve_context
    role:
      name: KnowledgeRetriever
      type: researcher
      model_tier: small
      description: |
        Given a {{ classify.intent }} query of {{ classify.complexity }} complexity,
        retrieve the most relevant documentation sections.
        Return {"context": "...", "sources": [...]}.
    depends_on: [classify]

  - id: answer
    response_stage: true                    # stream tokens as generated
    role:
      name: SupportAgent
      type: worker
      model_tier: frontier
      description: |
        Answer this {{ classify.intent }} support question using the retrieved context.
        Question: {{ user_query }}
        Context: {{ retrieve_context.context }}
        Be concise and specific. If you don't know, say so.
    depends_on: [retrieve_context]
```

The `classify` and `retrieve_context` stages run silently in the background. When `answer` starts generating, tokens arrive at the client immediately.

---

## The async endpoint pattern

For streaming, always use the async endpoint. The sync endpoint blocks until the workflow completes before returning:

```
# Sync — blocks until done, returns full result (good for batch, bad for chat)
POST /workflows/support-assistant/run
Body: {"inputs": {"user_query": "..."}}

# Async — returns immediately with job_id, stream events separately
POST /workflows/support-assistant/run/async
Body: {"inputs": {"user_query": "..."}}
Returns: {"job_id": "abc123", "status": "pending"}
```

### Step 1: submit the run

```python
import httpx

async def ask_armature(user_query: str) -> str:
    async with httpx.AsyncClient() as client:
        # Submit
        resp = await client.post(
            "http://localhost:8080/workflows/support-assistant/run/async",
            json={"inputs": {"user_query": user_query}},
        )
        job_id = resp.json()["job_id"]
    return job_id
```

### Step 2: stream the event-source

```python
async def stream_response(job_id: str):
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "GET",
            f"http://localhost:8080/run/{job_id}/events",
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])

                if event["type"] == "token":
                    yield event["content"]           # individual token

                elif event["type"] == "stage_start":
                    pass                              # show "thinking..." indicator

                elif event["type"] == "stage_complete":
                    pass                              # hide indicator

                elif event["type"] == "response_stage_complete":
                    pass                              # full response available in event["content"]

                elif event["type"] == "run_complete":
                    break
```

### The event types

| Event type | When it fires | Key fields |
|------------|--------------|------------|
| `stage_start` | Stage begins executing | `stage_id` |
| `stage_complete` | Stage finishes | `stage_id` |
| `token` | Token streamed from `response_stage` | `content` (the token) |
| `response_stage_complete` | `response_stage` stage finishes | `stage_id`, `content` (full text) |
| `run_complete` | Workflow finished | — |

The client gets a real-time view of the pipeline. It can show "Retrieving knowledge..." while `retrieve_context` runs, then switch to a streaming text display when tokens arrive from `answer`.

---

## FastAPI integration — websockets and HTTP streaming

If your application is itself a FastAPI service, you can expose a websocket or SSE endpoint that wraps the Armature call:

```python
from fastapi import FastAPI, WebSocket
from fastapi.responses import StreamingResponse
import httpx, json, asyncio

app = FastAPI()

ARMATURE = "http://localhost:8080"

@app.websocket("/chat")
async def chat_ws(websocket: WebSocket):
    await websocket.accept()
    while True:
        user_query = await websocket.receive_text()

        # Submit to Armature
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ARMATURE}/workflows/support-assistant/run/async",
                json={"inputs": {"user_query": user_query}},
            )
        job_id = resp.json()["job_id"]

        # Stream tokens back to the browser
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", f"{ARMATURE}/run/{job_id}/events") as r:
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    if event["type"] == "token":
                        await websocket.send_text(event["content"])
                    elif event["type"] == "run_complete":
                        await websocket.send_text("\n[done]")
                        break
```

The browser connects over WebSocket, sends queries as text messages, and receives tokens as they arrive. The application does no AI work — it is a passthrough.

---

## Latency optimization with model tiers

The sidecar pattern's latency profile depends on two things: which models you use for pre-processing stages, and whether those stages run sequentially or in parallel.

Assign the smallest capable model to fast classification and retrieval stages. Reserve the frontier model only for the final response generation — the only stage the user is waiting to see stream:

```yaml
model_tiers:
  tiny:
    provider: anthropic
    model: claude-haiku-4-5-20251001    # ~50–100ms for simple classification
  frontier:
    provider: anthropic
    model: claude-opus-4-7              # streaming response visible immediately

stages:
  - id: classify
    role:
      model_tier: tiny                  # fast
      ...

  - id: retrieve_context
    role:
      model_tier: tiny                  # fast
      ...
    depends_on: [classify]

  - id: answer
    response_stage: true
    role:
      model_tier: frontier              # tokens arrive within ~500ms of stage start
      ...
    depends_on: [retrieve_context]
```

A typical latency budget:
- `classify`: ~80ms (haiku, simple classification)
- `retrieve_context`: ~150ms (haiku, structured retrieval)
- Time-to-first-token for `answer`: ~500ms (opus, streaming begins)
- Perceived latency: user sees the cursor moving in ~730ms

This is comparable to direct LLM call latency, but the response is backed by classification and retrieved context rather than raw user input.

### Parallel pre-processing

If classification and retrieval are independent, run them in parallel:

```yaml
  - id: classify
    role:
      model_tier: tiny
      ...
    # no depends_on — fires immediately

  - id: retrieve_safety_rules
    role:
      model_tier: tiny
      ...
    # no depends_on — fires simultaneously with classify

  - id: answer
    response_stage: true
    role:
      model_tier: frontier
      ...
    depends_on: [classify, retrieve_safety_rules]   # waits for both
```

`classify` and `retrieve_safety_rules` run in the same DAG wave — concurrently. Wall time drops to `max(classify, retrieve_safety_rules)` rather than their sum.

---

## Session continuity across turns

A chat session is a sequence of turns. Armature's `continuation:` mechanism carries forward the outputs of prior turns automatically. Each turn is a fresh workflow activation; the harness loads the prior turn's result and injects it as `prior_run`:

```yaml
name: conversational-assistant
mission: Maintain a helpful, contextually aware conversation.

continuation:
  carry_forward:
    - key: answer.response_text
    - key: answer.conversation_summary
  inject_as: prior_turn

stages:
  - id: answer
    response_stage: true
    role:
      type: worker
      description: |
        Prior conversation:
        {% if prior_turn is defined %}
        Previous response: {{ prior_turn.response_text }}
        Conversation so far: {{ prior_turn.conversation_summary }}
        {% endif %}
        
        Current question: {{ user_query }}
        
        Answer the current question in context of the conversation.
        Return {
          "response_text": "...",
          "conversation_summary": "two-sentence summary of the full conversation including this turn"
        }.
```

Turn 1: `prior_turn` is absent. The assistant answers cold.
Turn 2: `prior_turn.response_text` and `prior_turn.conversation_summary` are injected. The assistant has context of what was just said.
Turn N: The rolling summary compresses the conversation history without unbounded growth.

This is not a full conversation buffer — it is a structured summary carried forward, which is more durable and cheaper than replaying a full transcript.

---

## Named workflow registry — production deployment

In production, pre-load your workflow specs at startup using the registry. This avoids file I/O on every request and gives you clean `/workflows/{name}/run` routes:

```python
# main.py
from fastapi import FastAPI
from armature.service.app import build_app
from armature.service.registry import WorkflowRegistry
from pathlib import Path

registry = WorkflowRegistry()
registry.load_dir(Path("specs/"))        # loads all *.yaml in specs/

app = build_app(registry=registry)
```

```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --workers 4
```

Your chat frontend calls:

```
POST http://your-service:8080/workflows/support-assistant/run/async
POST http://your-service:8080/workflows/sales-assistant/run/async
POST http://your-service:8080/workflows/code-reviewer/run/async
```

Each workflow is a separate YAML spec. You can run many workflows from a single Armature service instance. Hot-reload is as simple as calling `registry.load_dir()` again — no restart required.

---

## When the sidecar pattern fits (and when it doesn't)

### Good fit: structured reasoning behind a chat interface

Armature is ideal when the AI work behind a chat turn involves:
- **Multiple steps**: classify → retrieve → draft → validate
- **Quality gates**: a judge stage that checks the response before it goes to the user
- **Cost control**: tiny models for classification, frontier only for the user-visible response
- **Observability**: every turn is a traced run — HQS, quorum scores, latency all recorded
- **Auditability**: compliance teams can inspect every decision, every token

Examples: customer support assistants, legal Q&A tools, technical documentation search, code review feedback, structured report generation.

### Partial fit: multi-turn conversations with deep context

Armature's `continuation:` mechanism handles session state, but it passes structured summaries rather than full transcripts. For conversations where verbatim recall of earlier statements matters, supplement with an explicit memory stage that maintains a rolling full-text buffer in `_memory`.

### Not a fit: open-ended tool-use agents

If the core primitive is an open-ended ReAct loop — a model that decides at runtime how many steps to take, which tools to call, and when it is done — that belongs in a framework designed for cycles (LangGraph, AutoGPT). Armature's DAG is deliberately acyclic.

The right composition: Armature handles the outer workflow structure and all surrounding concerns (observability, safety, continuity); a LangGraph agent handles one inner stage that needs open-ended tool use. See `DAG-vs-LANGGRAPH.md` for the full comparison.

---

## A complete example: customer support assistant

```yaml
name: support-assistant
version: "1.0"
mission: >
  Provide accurate, concise, empathetic answers to customer support questions.
  Escalate if the issue is outside documented capabilities.

model_tiers:
  tiny:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

continuation:
  carry_forward:
    - key: respond.conversation_summary
  inject_as: prior_turn

safety_rules:
  - tool: http_post
    condition:
      field: url
      op: not_contains
      value: "internal.company.com"
    action: block

stages:
  - id: classify
    role:
      name: IntentClassifier
      type: worker
      model_tier: tiny
      description: |
        Classify this support query:
        Query: {{ user_query }}
        Return {"intent": "billing|technical|account|general|escalate",
                "urgency": "low|normal|high",
                "topic_summary": "one sentence"}.

  - id: retrieve
    role:
      name: KnowledgeRetriever
      type: researcher
      model_tier: tiny
      description: |
        For a {{ classify.intent }} query about: {{ classify.topic_summary }}
        List the three most relevant documentation sections.
        Return {"context": "...", "doc_refs": [...]}.
    depends_on: [classify]
    skip_if: "{{ classify.intent == 'escalate' }}"

  - id: respond
    response_stage: true
    role:
      name: SupportAgent
      type: worker
      model_tier: frontier
      description: |
        {% if prior_turn is defined %}
        Conversation so far: {{ prior_turn.conversation_summary }}
        {% endif %}

        Customer query: {{ user_query }}
        Intent: {{ classify.intent }} ({{ classify.urgency }} urgency)
        
        {% if classify.intent != 'escalate' %}
        Relevant documentation:
        {{ retrieve.context }}
        {% endif %}
        
        {% if classify.intent == 'escalate' %}
        This query requires human escalation. Acknowledge warmly and
        explain that a specialist will follow up within 2 hours.
        {% else %}
        Answer the customer's question using the documentation provided.
        If the documentation doesn't cover it, say so clearly.
        {% endif %}
        
        Return {
          "response_text": "the answer to show the customer",
          "conversation_summary": "one-sentence summary of the full conversation",
          "escalated": {{ 'true' if classify.intent == 'escalate' else 'false' }}
        }.
    depends_on: [classify, retrieve]
```

This workflow:
- Runs in ~700ms time-to-first-token for most queries
- Maintains conversation context across turns via `continuation:`
- Escalates automatically when the classifier detects an out-of-scope query
- Streams tokens to the chat UI as they arrive
- Records a full trace for every turn — HQS-scored, latency-tracked, inspectable

The chat frontend handles only UI. All AI logic is in the YAML spec.

---

*The sidecar pattern is the right way to add structured AI reasoning to an existing application — your codebase stays clean, your AI system is observable, and your users see tokens streaming as fast as the model can produce them.*

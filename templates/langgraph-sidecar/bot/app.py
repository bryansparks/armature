"""FastAPI application with SSE streaming chat endpoint.

The /chat/stream endpoint sends:
  - An immediate acknowledgement token ("Researching..." or "Thinking...")
    while Armature works in the background
  - Then streams the final response token by token

This is the "latency acknowledgement" pattern: the user sees immediate
feedback rather than a blank screen during the Armature workflow run.
"""
from __future__ import annotations
import asyncio
import json
import uuid
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
from .graph import graph

app = FastAPI(title="LangGraph + Armature Chatbot")


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    session_id: str
    response: str
    run_id: str | None = None


# In-memory session store (use Redis in production)
_sessions: dict[str, list] = {}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Synchronous chat endpoint -- waits for full response."""
    messages = _sessions.get(request.session_id, [])
    messages.append(HumanMessage(content=request.message))

    state = await graph.ainvoke({
        "session_id": request.session_id,
        "messages": messages,
        "research_result": None,
        "intent": "",
    })

    ai_messages = [m for m in state["messages"] if hasattr(m, "content") and m.type == "ai"]
    response_text = ai_messages[-1].content if ai_messages else ""
    _sessions[request.session_id] = list(state["messages"])

    return ChatResponse(session_id=request.session_id, response=response_text)


@app.get("/chat/stream")
async def chat_stream(session_id: str, message: str):
    """SSE streaming endpoint with latency acknowledgement.

    Immediately emits a status token, then streams the final response.
    Event format:
        data: {"type": "status", "text": "Researching..."}
        data: {"type": "token", "text": "Based on recent research..."}
        data: {"type": "done"}
    """
    async def event_stream():
        messages = _sessions.get(session_id, [])
        messages.append(HumanMessage(content=message))

        # 1. Classify intent quickly to choose acknowledgement text
        #    (We run a lightweight pre-classify to pick the right status message)
        needs_research = any(kw in message.lower() for kw in
                             ["research", "analyze", "find", "what is", "explain", "how does"])
        status_text = "Researching..." if needs_research else "Thinking..."

        # 2. Immediately send latency acknowledgement token
        yield f"data: {json.dumps({'type': 'status', 'text': status_text})}\n\n"

        # 3. Run the full graph (Armature work happens inside research_node)
        state = await graph.ainvoke({
            "session_id": session_id,
            "messages": messages,
            "research_result": None,
            "intent": "",
        })

        ai_messages = [m for m in state["messages"] if hasattr(m, "content") and m.type == "ai"]
        response_text = ai_messages[-1].content if ai_messages else ""
        _sessions[session_id] = list(state["messages"])

        # 4. Stream the response word by word (simulate token streaming)
        words = response_text.split()
        for i, word in enumerate(words):
            chunk = word + (" " if i < len(words) - 1 else "")
            yield f"data: {json.dumps({'type': 'token', 'text': chunk})}\n\n"
            await asyncio.sleep(0.02)  # pacing -- replace with real LLM streaming

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}

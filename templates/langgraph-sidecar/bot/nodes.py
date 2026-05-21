"""LangGraph node implementations."""
from __future__ import annotations
import os
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from .armature_client import run_workflow
from .state import ChatState

_llm = ChatAnthropic(model="claude-haiku-4-5-20251001")
_RESEARCH_SPEC = "/workflows/research.yml"


async def classify_node(state: ChatState) -> ChatState:
    """Fast intent classifier -- routes to research or chitchat."""
    last_user_msg = next(
        (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        "",
    )
    prompt = (
        "Classify the user message as one of: research, chitchat.\n"
        "Reply with only the word.\n"
        f"Message: {last_user_msg}"
    )
    response = await _llm.ainvoke([HumanMessage(content=prompt)])
    intent = response.content.strip().lower()
    intent = intent if intent in ("research", "chitchat") else "chitchat"
    return {"intent": intent}


async def research_node(state: ChatState) -> ChatState:
    """Call Armature for multi-stage research. Blocks until complete."""
    last_user_msg = next(
        (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        "",
    )
    result = await run_workflow(
        spec_path=_RESEARCH_SPEC,
        inputs={
            "query": last_user_msg,
            "session_id": state["session_id"],
        },
    )
    return {"research_result": result}


async def respond_with_research_node(state: ChatState) -> ChatState:
    """Compose a response using the Armature research result."""
    research = state.get("research_result") or {}
    synthesis = research.get("synthesize", {})
    content = synthesis.get("content") or str(research)

    last_user_msg = next(
        (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        "",
    )
    prompt = (
        f"The user asked: {last_user_msg}\n\n"
        f"Research findings:\n{content}\n\n"
        "Write a clear, helpful response based on the research."
    )
    response = await _llm.ainvoke([SystemMessage(content="You are a helpful assistant."),
                                   HumanMessage(content=prompt)])
    return {"messages": [AIMessage(content=response.content)], "research_result": None}


async def chitchat_node(state: ChatState) -> ChatState:
    """Handle conversational messages directly in LangGraph."""
    response = await _llm.ainvoke(
        [SystemMessage(content="You are a helpful assistant.")] + state["messages"]
    )
    return {"messages": [AIMessage(content=response.content)]}


def route_after_classify(state: ChatState) -> str:
    return state["intent"]

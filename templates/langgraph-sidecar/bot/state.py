"""Conversation state for the LangGraph chatbot."""
from __future__ import annotations
from typing import Annotated, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class ChatState(TypedDict):
    session_id: str
    messages: Annotated[list[BaseMessage], add_messages]
    # Set by Armature sidecar node when research is needed
    research_result: dict | None
    # Routing decision from classify node
    intent: str

"""LangGraph conversation graph definition."""
from __future__ import annotations
from langgraph.graph import StateGraph, END
from .state import ChatState
from .nodes import (
    classify_node,
    research_node,
    respond_with_research_node,
    chitchat_node,
    route_after_classify,
)


def build_graph():
    g = StateGraph(ChatState)

    g.add_node("classify",         classify_node)
    g.add_node("research",         research_node)           # calls Armature
    g.add_node("respond_research", respond_with_research_node)
    g.add_node("chitchat",         chitchat_node)

    g.set_entry_point("classify")

    g.add_conditional_edges(
        "classify",
        route_after_classify,
        {
            "research": "research",
            "chitchat": "chitchat",
        },
    )
    g.add_edge("research",         "respond_research")
    g.add_edge("respond_research", END)
    g.add_edge("chitchat",         END)

    return g.compile()


graph = build_graph()

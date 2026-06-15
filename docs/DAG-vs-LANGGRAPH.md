# Armature DAG vs. LangGraph

A practical comparison of two different models for building agentic workflows.

---

## Armature's DAG at a glance

The entire execution engine is 88 lines. Kahn's algorithm (topological sort) plus `asyncio.gather` per wave. Every stage whose dependencies have all completed fires concurrently in the same wave. Results merge into a shared context dict. No classes to subclass, no state machine to wire up.

The critical property: **the DAG is a consequence of the spec, not something you write**. You declare `depends_on: [stage_a, stage_b]` in YAML and the executor derives the execution graph automatically. The workflow author never sees the DAG directly.

```yaml
stages:
  - id: gather_data
    role: ...

  - id: analyse
    depends_on: [gather_data]
    role: ...

  - id: summarise
    depends_on: [gather_data]   # runs in parallel with analyse
    role: ...

  - id: report
    depends_on: [analyse, summarise]   # waits for both
    role: ...
```

`gather_data` fires first. When it completes, `analyse` and `summarise` both fire concurrently (same wave). When both finish, `report` fires. Zero lines of orchestration code — the executor infers it from `depends_on`.

---

## LangGraph's model

LangGraph is a **graph you construct in code** — nodes are Python functions, edges are explicit calls to `graph.add_edge()` or conditional branches (`add_conditional_edges`). You compile the graph, then stream it. The state is a typed `TypedDict` that flows through nodes and accumulates via reducer functions.

It is designed around one specific primitive: **cycles** — the ability for a graph to loop back to an earlier node based on a condition. That is where its complexity comes from. A ReAct agent loop (`call_model → call_tool → call_model → ...`) is a natural fit.

```python
# LangGraph — you write the graph explicitly
graph = StateGraph(AgentState)
graph.add_node("call_model", call_model)
graph.add_node("call_tool", call_tool)
graph.add_edge(START, "call_model")
graph.add_conditional_edges("call_model", should_continue, {
    "continue": "call_tool",
    "end": END,
})
graph.add_edge("call_tool", "call_model")
agent = graph.compile()
```

---

## Side-by-side comparison

| Dimension | Armature DAG | LangGraph |
|-----------|-------------|-----------|
| **Graph definition** | Implicit from `depends_on:` in YAML | Explicit: `add_node`, `add_edge` in Python |
| **Cycles** | None — deliberately acyclic | First-class — the whole point |
| **Parallelism** | Automatic (`asyncio.gather` per ready wave) | Manual — you control when to fan out |
| **State model** | Shared context dict, accumulated per wave | `TypedDict` with per-key reducer functions |
| **Authoring surface** | YAML spec, readable by non-engineers | Python code, requires LangGraph mental model |
| **Observability** | TraceStore, HQS, dashboards, `armature report` | Bring your own |
| **Safety rules** | Declarative `safety_rules:` with strict mode | Bring your own |
| **Self-improvement** | Optimizer reads traces, proposes YAML diffs | None built in |
| **Loop control** | `on_fail.loop` with backoff declared in spec | Conditional edges in Python |
| **Long-horizon state** | `continuation:` carries outputs across runs | Bring your own |
| **Event-driven activation** | `triggers:` + `armature watch` daemon | Bring your own |
| **API** | `POST /workflows/{name}/run` out of the box | Bring your own |

---

## Where each wins

### LangGraph wins when you need cycles

If your core primitive is a **stateful loop** — a ReAct agent that runs `think → act → observe → think` indefinitely until it decides it is done — LangGraph's graph model is the right fit. The cycle is the feature. It also gives you finer control over exactly when state merges and how reducer functions combine partial results.

Good fits for LangGraph:
- Open-ended tool-use agents with no predetermined structure
- Search agents that loop until a stopping condition
- Any workflow where "how many steps" is determined at runtime by the model

### Armature wins when your workflow terminates

If your workflow is a **directed pipeline** — even a complex one with fan-out, fan-in, conditional skipping (`skip_if:`), multiple role types, retries, and judge gates — the YAML spec is the better authoring surface. Non-engineers can read, edit, and reason about a YAML file. A LangGraph Python graph requires a developer to modify.

More importantly, the harness handles observability, safety, self-improvement, and continuation **automatically**. None of that exists in LangGraph — you would need to build it yourself on top of the library.

Good fits for Armature:
- Multi-stage analysis pipelines (gather → analyse → synthesise → judge)
- Recurring workflows with memory of prior runs (`continuation:`)
- Workflows where quality scoring, trace capture, and self-improvement matter
- Teams where non-engineers need to read and tune the workflow
- Anything that needs a stable HTTP API without additional infrastructure

---

## The deeper distinction: library vs. harness

**LangGraph is a plumbing library** — it gives you the pipes, but you build the sink, the faucets, and the water pressure yourself. Every production concern (observability, safety, quality scoring, API surface, self-improvement) is left to the engineer.

**Armature is a finished harness** — the orchestration, governance, quality scoring, trace-driven self-improvement, and a stable named-workflow API are already built in. The tradeoff is flexibility vs. capability-for-free. For production agentic teams with real quality requirements, "capability-for-free" usually wins.

---

## They compose

The two are not mutually exclusive. A LangGraph ReAct agent could be one **tool** that an Armature worker stage calls via `http_post` or a Python adapter. Armature handles the outer pipeline structure, scheduling, quality assurance, and observability; the inner ReAct loop runs in LangGraph where cycles are genuinely needed. This is the right architecture when you have a mix: some stages with fixed structure (data fetch, judge evaluation, report generation) and one stage that needs open-ended tool use.

```yaml
stages:
  - id: gather_context
    role:
      type: researcher
      description: Fetch relevant background data.

  - id: react_agent          # calls a LangGraph agent via HTTP
    tool_call:
      name: http_post
      args:
        url: "http://localhost:7000/agent/run"
        body:
          task: "{{ gather_context.summary }}"
    depends_on: [gather_context]

  - id: judge
    role:
      type: judge
      description: Score the agent's output for quality and accuracy.
    depends_on: [react_agent]
```

---

*Armature — the harness is more important than the model.*
